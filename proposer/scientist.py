"""The Scientist: one persistent researcher per proposer lane.

The Scientist owns the research problem. It wakes into a world (workspace +
shell + a library of prior experiments), forms its own understanding, and
submits directions it judges worth an experiment. There is no Generator, no
hypothesis card, no Sieve/Select/Enrich — those were a workflow imposed on the
model from outside. The Scientist is a person; the workflow lives in its head,
not in the runtime.

Continuity model (three layers):
  - recent trajectory      short-term cognition (this round's lived conversation)
  - notebook.md            revisable autobiographical long-term memory (an anchor,
                           NOT the sole carrier of identity — lossy, may be wrong)
  - ledger / workspace     world facts (authoritative; win on disagreement)

The call boundary is NOT a cognitive boundary: one Scientist's trajectory
persists across rounds via session.jsonl + notebook.md, and a round ends in a
*SUSPENSION* (paused while experiments run), not a psychological episode. The
next round is a resume, not a restart.

Output protocol: every response is one JSON object ``{"message": ..., "action":
{...}}``. ``message`` is optional natural text the Scientist chooses to leave in
its trajectory — communication, NOT a substitute for acting, and NOT the
continuity mechanism (the trajectory is). The runtime parses only the required
``action``; the raw reply (message included) is appended to the conversation.

The ``ProposerResult`` shape is unchanged so loop.py / the execution backends
need no modification — the lane contract (result.json) is the interface
firewall of this refactor.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from .model import ChatModel
from .research_tools import (
    MEMORY_TOOL_ACTIONS,
    ResearchTools,
    render_research_tool_prompt,
)
from .research_agent import (
    AgentError,
    ResearchAgent,
    WorkingState,
    _bump,
    _build_telemetry,
    _build_trace,
    _fingerprint,
    _register_evidence,
    _source_path_exists,
    _stamp,
)
from .scientist_session import ScientistSession, read_expectations
from .runtime import ApptainerRuntime, MountMap
from .memory.context import build_generation_context
from .memory.service import read_reflection_records
from .memory.models import (
    ExistingFindingTarget,
    NewFindingTarget,
    ResearchProposal,
)
from .prompts import load_semantic


class ProposerError(AgentError):
    """The Scientist violated its action or budget contract."""


@dataclass(frozen=True)
class ProposerResult:
    """One round's structured output — the interface the Loop consumes.

    Shape is unchanged from the Generator→Cognitive pipeline so loop.py, the
    execution backends, and the trace/handoff writers need no changes.
    ``proposals`` is a list of ``ResearchProposal``; ``abstained`` is True when
    the Scientist submitted zero directions this round.
    """

    proposals: list[ResearchProposal]
    usage: object = None
    abstained: bool = False
    abstain_reason: str | None = None
    abstain_blocking_unknown: str | None = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScientistRound:
    """Internal: one Scientist round's outcome, mapped to LaneResult by the
    orchestrator."""

    proposals: list[ResearchProposal]
    abstained: bool = False
    abstain_reason: str | None = None
    usage: object = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SelfReviewResult:
    """Internal: one self-review round's outcome (RSI S3c). The Host appends the
    relevant fields to reviews.jsonl (S3c.2). ``decision`` is KEEP or CHANGE.

    On a CHANGE, ``self_change`` is the WHAT/WHY the Scientist believes is
    limiting Goal progress (the self-executor in S3d does HOW). On a KEEP,
    ``keep_reason`` states why current progress is sufficient and
    ``next_review_after_rounds`` is the Scientist's commitment for when to
    re-examine itself (the Host's clock, not a fixed schedule).

    ``abstained`` reflects a self-review that exhausted its budget before
    reaching a decision — surfaced as a KEEP with a default commitment so the
    scheduler is not stalled.
    """

    decision: str
    diagnosis: str
    keep_reason: str | None = None
    next_review_after_rounds: int | None = None
    self_change: dict | None = None
    abstained: bool = False
    usage: object = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ReflectionResult:
    """Internal: one Reflection round's outcome.

    ``handoff`` is the note to the next self — a cognitive warning, not an
    instruction, not a proposal, and not a self_decision. The Host appends it
    to run_dir/reflection/history.jsonl; the next task round replays it with
    an explicit epistemic status.

    ``self_limitation_suspected`` is ADVISORY RSI evidence only (a repeated
    pattern the reflection named); reflection never decides KEEP/CHANGE and
    never specifies a self_change.

    ``abstained`` reflects a reflection that exhausted its budget before
    producing a handoff.
    """

    handoff: str
    self_limitation_suspected: bool = False
    note: str | None = None
    abstained: bool = False
    usage: object = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


# --- Tunables --------------------------------------------------------------

# Research / memory tools never terminate the loop.
_RESEARCH_TOOL_ACTIONS = frozenset({"run_research_command"} | MEMORY_TOOL_ACTIONS)

# How many complete (assistant→observation) turn-blocks to carry from the prior
# trajectory into the resume context. The notebook carries the long-term; this
# carries recent lived detail. Each observation is capped at
# command_output_cap_chars, so this bounds the short-term-memory token cost.
_TAIL_TURNS = 8

# Prompt-version stamp recorded in meta.json so a prompt change is observable
# per Scientist across rounds.
SCIENTIST_PROMPT_VERSION = "scientist-v5"


# --- Live-context compaction (Option A: deterministic shedding) -----------
#
# Within one round the live ``messages`` list grows by two messages per step
# (assistant reply + tool observation). On a source-reading-heavy task each
# observation can be ~10 KB, so by ~step 60-80 the context window is full —
# long before the 200-step budget. Compaction is the safety net: when the
# context crosses a token threshold, shed the OLDEST (assistant, observation)
# turn-pairs, keeping the framing seed + the most recent pairs verbatim.
#
# Design (ported from ../SimpleLoop scientist_context._cap_tail):
#   - whole-pair accounting: an observation is never orphaned from the action
#     that produced it (the resumed Scientist would stare at a result it can't
#     remember wanting);
#   - most-recent pair always survives, even when the cap is smaller than one
#     pair (sentinel guard);
#   - char + pair-count dual budget.
#
# This compacts ONLY the live ``messages`` sent to the model. The immutable
# session.jsonl archive is appended to in full (every observation, every world
# event) and is never mutated — the Scientist's complete lived history stays
# auditable. (tail_turns() reads this archive, but is no longer used by the
# default cross-round resume; it is retained for audit and explicit recovery.)

@dataclass(frozen=True)
class ContextPolicy:
    """Scientist live-context compaction policy.

    ``emergency_threshold_tokens`` is the trigger: when the most recent model
    call's prompt-token count exceeds it, compact. Set to None to disable.
    The window knobs bound what survives compaction (most-recent pairs, within
    a char budget). Defaults are conservative — a normal 6-12 step round never
    triggers; this only fires in the long-investigation tail.
    """
    emergency_threshold_tokens: int | None = 100_000
    window_pairs: int = 3
    window_max_chars: int = 24_000

    @classmethod
    def from_config(cls, raw: object) -> "ContextPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("loop.context: must be an object")
        unknown = set(raw) - {
            "emergency_threshold_tokens", "window_pairs", "window_max_chars",
        }
        if unknown:
            raise ValueError(
                f"loop.context: unknown key(s): {sorted(unknown)}")
        policy = cls()
        if "emergency_threshold_tokens" in raw:
            value = raw["emergency_threshold_tokens"]
            if value is not None and (
                    not isinstance(value, int) or isinstance(value, bool)
                    or value < 1):
                raise ValueError(
                    "loop.context.emergency_threshold_tokens: "
                    "must be a positive integer or null")
            policy = ContextPolicy(
                emergency_threshold_tokens=value,
                window_pairs=policy.window_pairs,
                window_max_chars=policy.window_max_chars,
            )
        if "window_pairs" in raw:
            value = raw["window_pairs"]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(
                    "loop.context.window_pairs: must be a positive integer")
            policy = ContextPolicy(
                emergency_threshold_tokens=policy.emergency_threshold_tokens,
                window_pairs=value,
                window_max_chars=policy.window_max_chars,
            )
        if "window_max_chars" in raw:
            value = raw["window_max_chars"]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(
                    "loop.context.window_max_chars: must be a positive integer")
            policy = ContextPolicy(
                emergency_threshold_tokens=policy.emergency_threshold_tokens,
                window_pairs=policy.window_pairs,
                window_max_chars=value,
            )
        return policy


def _prompt_tokens(usage: object) -> int | None:
    """Extract the prompt-token count from a provider usage object (dict,
    pydantic model, or None). OpenAI-compatible endpoints report
    ``prompt_tokens``; some adapters use ``input_tokens``."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "input_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                return value
        return None
    if hasattr(usage, "model_dump"):
        dumped = usage.model_dump()
        return _prompt_tokens(dumped)
    for attr in ("prompt_tokens", "input_tokens"):
        value = getattr(usage, attr, None)
        if isinstance(value, int):
            return value
    return None


def _estimate_tokens(messages: list[dict]) -> int:
    """Char-based fallback (~4 chars/token for mixed code/prose) when the
    provider reports no prompt-token count. Used only for the trigger then."""
    return sum(len(m.get("content") or "") for m in messages) // 4


def _cap_tail(
    tail: list[dict], window_pairs: int, window_max_chars: int,
) -> list[dict]:
    """Keep the most recent whole (assistant, user) turn-units from ``tail``
    until the cap bites, preserving chronological and within-pair order.

    A turn-unit is an assistant message immediately followed by a user message
    (a tool observation OR a protocol correction). The most-recent unit is
    always retained: the cap checks are guarded by ``start`` still sitting on
    the sentinel, so the first (most-recent) unit is always admitted —
    guaranteeing the most-recent complete pair survives even when the cap is
    smaller than a single pair. A trailing unpaired message is treated as a
    singleton unit.
    """
    if not tail:
        return tail
    chars = 0
    pairs_kept = 0
    start = len(tail)  # sentinel: nothing retained yet
    i = len(tail) - 1
    while i >= 0:
        cur = tail[i]
        nxt = tail[i - 1] if i >= 1 else None
        if (cur.get("role") == "user" and nxt is not None
                and nxt.get("role") == "assistant"):
            # a (assistant, user) pair straddling i-1, i
            pair_chars = (len(cur.get("content") or "")
                          + len(nxt.get("content") or ""))
            if start < len(tail) and (
                    pairs_kept >= window_pairs
                    or chars + pair_chars > window_max_chars):
                break
            start = i - 1
            chars += pair_chars
            pairs_kept += 1
            i -= 2
        else:
            single_chars = len(cur.get("content") or "")
            if start < len(tail) and chars + single_chars > window_max_chars:
                break
            start = i
            chars += single_chars
            i -= 1
    return tail[start:]


def _compact_live_messages(
    messages: list[dict], *, window_pairs: int, window_max_chars: int,
) -> tuple[list[dict], dict]:
    """Compact the live ``messages`` list: preserve the framing preamble
    (everything before the first assistant message — cold-start seed, world
    event) and cap the (assistant, user) turn-pair region to the most recent
    pairs within budget.

    Returns (new_messages, info). ``info["compacted"]`` is False when there
    was nothing to shed (too few messages, or the result would not shrink).
    Never mutates the input; the caller assigns the result back.
    """
    if len(messages) <= 2:
        return messages, {"compacted": False}
    first_assistant = None
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant":
            first_assistant = i
            break
    if first_assistant is None or first_assistant >= len(messages) - 1:
        return messages, {"compacted": False}
    preamble = messages[:first_assistant]
    tail = messages[first_assistant:]
    kept = _cap_tail(tail, window_pairs, window_max_chars)
    new_messages = list(preamble) + list(kept)
    if len(new_messages) >= len(messages):
        return messages, {"compacted": False}
    dropped = len(messages) - len(new_messages)
    return new_messages, {
        "compacted": True,
        "dropped": dropped,
        "kept_pairs": len(kept) // 2,
        "before_msgs": len(messages),
        "after_msgs": len(new_messages),
    }


# --- Prompt scaffolding ----------------------------------------------------

_TOOL_BLOCK = (
    "Research tools (your lab and your library — use them freely to "
    "investigate, verify, and understand):\n" + render_research_tool_prompt()
)

_PROTOCOL_BLOCK = """Output protocol (immutable): every response is exactly one \
JSON object. It carries one required field and one optional field.

  {"message": "...optional...", "action": {"action": "...", ...fields...}}

- "action" (required): one research tool call, OR submit_proposals.
- "message" (optional): natural text you choose to leave in your own research
  trajectory — what you might say aloud while working. It is NOT required and
  NOT a substitute for acting; if you have nothing to add, omit it. It is
  communication that future-you will see, not a report you must file.

Control action (the only non-tool action):
- {"action":"submit_proposals","proposals":[
    {"instruction":"...",
     "research_target":{"mode":"existing","finding_id":"F-NNN"}
                      | {"mode":"new","question":"...",
                         "mechanisms":[...],"code_regions":[...]},
     "evidence_refs":["source:src/foo.cc:FunctionName"],
     "material_difference":"..."}]}
  Submit the directions you believe are worth an experiment. You may submit
  between 0 and {n} proposals. {n} is exploitable research capacity — NOT a
  quota to fill and NOT a reward to hoard: submit every direction you judge
  worth its execution cost, neither padding to fill the slots nor withholding a
  worthwhile bet. Submitting 0 (with a reason in "message") is an honest
  abstention when you currently see nothing worth the compute; a single
  well-considered direction is legitimate; several distinct directions worth
  testing in parallel are equally legitimate. The instruction states WHAT to
  try and WHY you think it may move the goal; the executor reads the real code
  and decides the concrete implementation, so you need not reach line-level
  detail. evidence_refs and material_difference are optional.
"""

_RUNTIME_BOUNDARIES = """Runtime boundaries:
- /work is your writable lab: the accepted source tree's editable paths,
  materialized read-write. Read it, write scratch code, compile, run toy
  experiments to understand the code and the task. It is disposable — nothing
  you write here becomes an artifact. Other source files are visible read-only.
- /repo is the read-only Git repository. Use `git show <sha>`, `git diff`,
  `git log` to inspect any prior experiment's source (the history is shared).
  You CANNOT commit, branch, or reset — creating artifacts is the executor's
  job, and the read-only /repo structurally prevents it.
- /scratch is temporary writable space.
- Anything you measure in your lab (a toy build, a probe) is for YOUR
  understanding only. It is never a merit fact: whether a change is faster or
  correct is the Harness's verdict, not yours. You may predict, judge, and bet
  boldly — but distinguish your scientific judgment from what has actually been
  established by experiment.
- You cannot call the executor or Harness, edit candidates, choose a parent,
  or declare evaluation and Gate facts. Only Harness records are authoritative.
""".strip()

_COLD_START = (
    "You are beginning this research. The goal, the gates, and the current "
    "accepted revision are in your standing context above. Begin working on "
    "the research problem. Use the laboratory as your judgment requires — "
    "investigate, probe, and form your own understanding. When you have "
    "directions you believe should be tried, submit them. There is no phase "
    "you must rush through; take the time your judgment needs."
)

_BUDGET_NUDGE = (
    "Your research turn is nearing its computation budget. If you have "
    "directions you currently believe are worth an experiment, submit them now "
    "via submit_proposals. This is a resource notice, not a required phase — "
    "keep researching if your best judgment says nothing yet clears the bar."
)

_SUSPEND_PROMPT = (
    "Your research is being paused while the directions you submitted are "
    "executed as experiments. Leave a continuation note for your resumed self "
    "— first person, as your own running account. Capture what would let you "
    "re-enter this investigation after the world may have changed:\n"
    "\n"
    "  - what you currently understand about the problem, and why you believe "
    "it;\n"
    "  - the specific code regions, mechanisms, or measurements your current "
    "view rests on — so you can re-check them against the world that exists "
    "when you resume, not rely on this note as fact;\n"
    "  - what remains unresolved or uncertain.\n"
    "\n"
    "Separately — and this part is recorded verbatim and replayed next to the "
    "results before you see them — register what you expect from each "
    "direction you submitted. For each proposal slot (0-based, in submission "
    "order): what outcome you honestly expect and why, and what outcome would "
    "WEAKEN the belief that motivated that direction. Write your honest prior, "
    "not a safe prediction — a pre-registration you hedged cannot discipline "
    "your future judgment.\n"
    "\n"
    "This is autobiographical memory, not an established account of the "
    "present or future world, and not a plan your future self must follow. You "
    "may revise or reject any of it when you resume. Return one JSON object:\n"
    '  {"notebook": "<your continuation note>",\n'
    '   "expectations": [{"slot": 0,\n'
    '                     "expectation": "<what outcome I expect and why>",\n'
    '                     "would_weaken": "<what outcome would weaken the '
    'belief motivating this direction>"}]}\n'
    "(one expectations entry per submitted proposal slot)"
)


def _valid_expectations(raw: object) -> list[dict]:
    """Filter a suspend-reply ``expectations`` list down to well-formed rows.

    Malformed entries are dropped rather than failing the round — a partial
    pre-registration is still better than none (missing slots render as
    NOT RECORDED in the world event).
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        slot = item.get("slot")
        expectation = item.get("expectation")
        if not isinstance(slot, int) or isinstance(slot, bool):
            continue
        if not isinstance(expectation, str) or not expectation.strip():
            continue
        row = {"slot": slot, "expectation": expectation.strip()}
        weaken = item.get("would_weaken")
        if isinstance(weaken, str) and weaken.strip():
            row["would_weaken"] = weaken.strip()
        out.append(row)
    return out


# --- Self-review prompt scaffolding (RSI S3c) ------------------------------

_SELF_REVIEW_PROTOCOL_BLOCK = """Output protocol (immutable): every response is exactly one \
JSON object.

  {"message": "...optional...", "action": {"action": "...", ...fields...}}

- "action" (required): one research tool call, OR submit_self_decision.
- "message" (optional): natural text for your own trajectory; not required and
  not a substitute for acting.

Control action (the only non-tool action):
- {"action":"submit_self_decision",
   "decision":"KEEP"|"CHANGE",
   "diagnosis":"why your current self is, or is not, limiting Goal progress",
   "keep_reason":"..." | null,                    # required when KEEP
   "next_review_after_rounds": <int> | null,      # required when KEEP: after how
                                                   # many task rounds to re-examine
   "self_change":{"target":"...","intent":"...",
                  "instruction":"...","evidence_refs":[...]} | null}  # required when CHANGE
  target ∈ {prompt, context, tools, runtime, retrieval, model-policy}.
  KEEP requires keep_reason (tied to Goal-progress evidence) and
  next_review_after_rounds (your commitment). CHANGE requires self_change naming
  the mechanism you believe limits progress and WHAT/WHY changing it would help;
  the concrete HOW is not yours to specify. Both decisions need a reason grounded
  in the evidence above — there is no default KEEP and no default CHANGE.

Deliberation order (mandatory): FIRST build the strongest case AGAINST the current
self, each charge citing specific evidence (experiment ids, rounds, trajectory
patterns); THEN the strongest case FOR it (alternative explanations of the same
evidence); ONLY THEN decide, and state in the diagnosis which charges survived the
defense and which did not. A verdict that skips the prosecution is a defense; a
verdict that skips testing the charges is an execution. Neither is a review.
"""

_SELF_REVIEW_COLD_START = (
    "You are beginning a self-review. The Original Goal, your recent task "
    "progress, your self-review history, and a pointer to your own source are "
    "in your standing context. Judge honestly whether your current way of "
    "working is advancing the Goal sufficiently, then submit your decision. "
    "Read your own source or the records when a suspicion needs grounding; do "
    "not decide from reputation."
)

_SELF_REVIEW_BUDGET_NUDGE = (
    "Your self-review turn is nearing its budget. If you have grounded your "
    "judgment, submit your self_decision now."
)

# If a self-review exhausts its budget without deciding, surface a KEEP with
# this default commitment so the Host's scheduler re-opens self-attention soon
# rather than stalling.
_SELF_REVIEW_DEFAULT_DEFER = 3


# --- Reflection prompt scaffolding (continuity design §9-§16) -------------

_REFLECTION_PROTOCOL_BLOCK = """Output protocol (immutable): every response is exactly one \
JSON object.

  {"message": "...optional...", "action": {"action": "...", ...fields...}}

- "action" (required): one research tool call, OR submit_reflection_handoff.
- "message" (optional): natural text for your own trajectory; not required and
  not a substitute for acting.

Control action (the only non-tool action):
- {"action":"submit_reflection_handoff",
   "handoff":"<note to your next self — a cognitive warning about how the
              recent you may have failed, grounded in cited evidence; NOT an
              instruction, NOT a next direction, NOT a proposal>",
   "self_limitation_suspected": true|false,   # advisory RSI evidence only
   "note":"...optional context..."}
  handoff is required and must be a non-empty string. self_limitation_suspected
  defaults to false and true is a strong claim you must earn: set it true only
  when you can cite the SAME named limitation evidenced in the ledger on at
  least TWO distinct past rounds (a single episode is not a pattern, and a hard
  problem is not a self-limitation). The handoff carries the warning either
  way; the flag only marks recurrence (it becomes advisory evidence for a
  later self-review — reflection itself never modifies you).
  Do NOT submit proposals, do NOT submit a self_decision, do NOT choose the
  next research direction. Reveal the inertia; the next self re-decides.
"""

_REFLECTION_COLD_START = (
    "You are beginning a reflection. The Goal, the current work, the "
    "deterministic reflection evidence pack, and your own notebook are in "
    "your standing context. Your job is not to advance the research — it is "
    "to doubt it: find where the recent version of you may have failed to "
    "advance the Goal effectively, cite the evidence, and leave your next "
    "self a warning. Then submit your handoff."
)

_REFLECTION_BUDGET_NUDGE = (
    "Your reflection turn is nearing its budget. If your strongest challenge "
    "is grounded, submit your reflection_handoff now."
)


def _build_reflection_prompt(
    *, charter: str, goal: str, base_sha: str, pack: str, notebook: str,
) -> str:
    """Assemble the standing context for a Reflection round.

    Mirrors _build_self_review_prompt, but the evidence pack is the aggregate
    trajectory view (not round-granularity progress), and the notebook is
    framed as a CLAIM BY THE ENTITY UNDER AUDIT — reflection's object of
    study includes the notebook's own assertions."""
    parts = [
        charter.rstrip(),
        "Original Goal (the standard your recent research is judged against):\n"
        + goal.strip(),
        f"Current accepted revision of the work: {base_sha[:12]}…  The "
        "research world is readable via your tools; verify claims against it "
        "when the pack is not enough.",
    ]
    parts.append(pack.strip() or (
        "Reflection evidence pack: no task history yet (an honest near-empty "
        "pack — weigh what little evidence exists, and say so in the "
        "handoff rather than manufacturing critique)."))
    parts.append(_TOOL_BLOCK)
    parts.append(_REFLECTION_PROTOCOL_BLOCK)
    parts.append(_RUNTIME_BOUNDARIES)
    if notebook.strip():
        parts.append(
            "The recent you's research notebook (a CLAIM BY THE ENTITY YOU "
            "ARE AUDITING, not memory to trust — it was written by the very "
            "trajectory under review; check its assertions against the "
            "evidence pack and the current work):\n\n" + notebook.strip()
        )
    return "\n\n".join(parts)


def _build_self_progress_pack(run_dir: Path, objective_key: str | None,
                              current_round: int, n: int = 12) -> str:
    """A compact, factual summary of recent Goal progress from history.jsonl —
    the authoritative 'am I progressing fast enough?' evidence (semantics §5.2).
    Reports outcomes only (objective trajectory, selection, gates), never
    proposal/eval text (the S2c coverage-free principle). '' when no history."""
    from .memory.history import read_history
    try:
        rows = read_history(Path(run_dir) / "history.jsonl")
    except (OSError, ValueError):
        return ""
    if not rows:
        return ""
    recent = rows[-n:]
    obj_label = objective_key or "(objective)"
    lines = [f"Recent task progress (last {len(recent)} of {len(rows)} task round(s)):"]
    for row in recent:
        rnd = row.get("round")
        cands = row.get("candidates") or []
        n_gate = sum(1 for c in cands
                     if isinstance(c, dict) and c.get("gate_passed"))
        selected = next((c for c in cands
                         if isinstance(c, dict) and c.get("selected")), None)
        if selected is not None:
            metrics = selected.get("metrics") or {}
            obj = metrics.get(objective_key) if objective_key else None
            obj_s = f"{obj}" if isinstance(obj, (int, float)) else "?"
            lines.append(
                f"  - round {rnd}: {len(cands)} candidate(s), {n_gate} passed "
                f"gates; selected {obj_label} = {obj_s}.")
        else:
            lines.append(
                f"  - round {rnd}: {len(cands)} candidate(s), {n_gate} passed "
                f"gates; none selected.")
    # Advisory RSI bridge: a recent reflection that suspected a stable
    # self-limitation is evidence a self-review should weigh — one judgment,
    # not a diagnosis. Best-effort (a missing reflection log reads as none).
    try:
        recent = read_reflection_records(run_dir)[-3:]
        if any(record.get("self_limitation_suspected") for record in recent):
            lines.append(
                "A recent reflection flagged a suspected self-limitation "
                "(advisory evidence, one judgment — verify it against this "
                "progress record yourself).")
    except Exception:
        pass
    return "\n".join(lines)


def _read_self_review_history_text(reviews_path: Path, n: int = 6) -> str:
    """Past self-review judgments as provenance text — their reasons and the
    conditions that held then, not conclusions to inherit (semantics §14/§15).
    '' when there is no self-review history yet."""
    path = Path(reviews_path)
    if not path.exists():
        return ""
    try:
        rows = [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        return ""
    if not rows:
        return ""
    lines = [f"Your self-review history (last {len(rows[-n:])} of {len(rows)}):"]
    for r in rows[-n:]:
        rnd = r.get("round")
        decision = r.get("decision")
        diag = str(r.get("diagnosis") or "").strip()
        change = r.get("change") or {}
        tgt = change.get("target") if isinstance(change, dict) else None
        intent = str(change.get("intent") or "").strip() if isinstance(change, dict) else ""
        head = f"  - round {rnd}: {decision}"
        tail = []
        if diag:
            tail.append(f"diagnosis: {diag}")
        if decision == "KEEP":
            tail.append(f"keep_reason: {str(r.get('keep_reason') or '').strip()}")
        elif decision == "CHANGE":
            tail.append(f"target: {tgt}; intent: {intent}")
            if r.get("adopted") is True:
                tail.append("(adopted)")
            elif r.get("adopted") is False:
                tail.append("(not adopted)")
        lines.append(head + (" — " + "; ".join(tail) if tail else ""))
    lines.append(
        "Inherit the reasons and conditions behind these, not their conclusions "
        "— the facts have changed since they were made.")
    return "\n".join(lines)


def _build_self_review_prompt(
    *, charter: str, goal: str, incumbent_self_sha: str,
    progress_pack: str, self_history: str, notebook: str,
) -> str:
    """Assemble the Scientist's standing context for a self-review round."""
    parts = [
        charter.rstrip(),
        "Original Goal (the only anchor — self-improvement has no independent "
        "value):\n" + goal.strip(),
        f"Current self: incumbent_self_sha = {incumbent_self_sha[:12]}…  "
        "Your own source is readable at /work — read it when a suspicion points "
        "at a specific mechanism in you, rather than reasoning about yourself "
        "from reputation.",
    ]
    parts.append(progress_pack.strip() or (
        "Recent task progress: no task history yet (this may be an early "
        "self-review with little evidence — weigh that honestly)."))
    if self_history.strip():
        parts.append(self_history.strip())
    parts.append(_TOOL_BLOCK)
    parts.append(_SELF_REVIEW_PROTOCOL_BLOCK)
    parts.append(_RUNTIME_BOUNDARIES)
    if notebook.strip():
        parts.append(
            "Your own research notebook (REVISABLE AUTOBIOGRAPHICAL MEMORY — "
            "your running self-account from task research; it may lag or be "
            "wrong, so when it disagrees with the records above, trust the "
            "records):\n" + notebook.strip())
    return "\n\n".join(parts)


def _build_system_prompt(
    *,
    charter: str,
    goal: str,
    editable: list[str],
    base_sha: str,
    gate_block: str,
    proposal_slots: int,
    hints: list[str] | None,
    notebook: str,
) -> str:
    """Assemble the Scientist's standing context.

    The notebook is embedded here (not as a user message) so it is framed as
    the Scientist's OWN standing self-account — and explicitly labelled
    revisable autobiographical memory, not instruction or established fact.
    """
    world = build_generation_context(
        goal=goal, editable=editable, frozen=[], base_sha=base_sha,
        gate_block=gate_block,
    )
    parts = [
        charter.rstrip(),
        world,
        _TOOL_BLOCK,
        _PROTOCOL_BLOCK.replace("{n}", str(proposal_slots)),
        _RUNTIME_BOUNDARIES,
    ]
    if hints:
        bullets = "\n".join(f"  - {h}" for h in hints)
        parts.append(
            f"Guidance (high-value directions to consider, not requirements):\n"
            f"{bullets}"
        )
    if notebook.strip():
        parts.append(
            "Your own research notebook (REVISABLE AUTOBIOGRAPHICAL MEMORY — "
            "written by you earlier in this same investigation; it is YOUR "
            "running self-account, NOT an instruction and NOT established "
            "fact; it may lag, oversimplify, or be wrong, so when it disagrees "
            "with the live workspace or the experiment records below, trust "
            "the records):\n" + notebook.strip()
        )
    return "\n\n".join(parts)


def _fmt_metrics(metrics: dict) -> str:
    if not metrics:
        return "metrics=(none)"
    parts = []
    for key, value in metrics.items():
        if isinstance(value, float):
            parts.append(f"{key}={value:g}")
        else:
            parts.append(f"{key}={value}")
    return "metrics=" + ", ".join(parts)


def _build_world_event(
    memory_service, current_round: int, base_sha: str,
    expectations: dict[int, dict] | None = None,
) -> str | None:
    """The resume world-transition event.

    Reports the OUTCOMES of the most recent experiment round (what reality
    returned) and states the world that now exists. The directions themselves
    are NOT echoed here — they live in the Scientist's notebook, and one
    experiment's detail is available via inspect_episode. Authoritative
    harness facts only — no interpretation ("this direction is exhausted",
    etc.); the Scientist produces the meaning.

    Three outcomes are distinguished so the Scientist's world model is not
    fed a falsehood: a candidate that passed the gates but did not beat the
    incumbent is NOT reported as "no candidate cleared the gates".

    The round replayed is the latest experiment round strictly before
    ``current_round`` — NOT ``current_round - 1``, because self-review and
    reflection rounds consume round ids without producing experiments (the
    old filter would then fall through to the "you submitted no directions"
    fallback and tell the Scientist a falsehood).

    ``expectations`` (from ``read_expectations``) pairs each outcome with the
    pre-registered expectation the Scientist recorded at that round's
    suspension — verbatim, before the results existed. A missing or
    failed-capture expectation is stated explicitly: an outcome that cannot
    be checked against a prior commitment is information, not silence.

    Single-lane: every experiment from that round is this Scientist's. True
    per-Scientist attribution (filtering by scientist_id once multiple lanes
    exist) is deferred — see scientist_session.py docstring.
    """
    try:
        experiments = memory_service.load_experiments()
    except Exception:
        return None
    prior_rounds = [e.round for e in experiments if e.round < current_round]
    if not prior_rounds:
        return None
    replay_round = max(prior_rounds)
    last = [e for e in experiments if e.round == replay_round]
    last.sort(key=lambda e: (e.round, e.candidate))

    # The parent these candidates were built on is the world the Scientist was
    # studying that round (single-lane: all share one parent).
    prev_sha = last[0].parent_sha or "—"

    lines = [
        "Your research is resuming. While you were paused, the directions you "
        "submitted were executed as experiments, and the research world may "
        "have changed. The following are authoritative harness facts about the "
        "OUTCOMES — what you asked is in your own notebook; if you need the "
        "detail of one experiment, inspect it deliberately.",
        "",
        f"Previous accepted revision: {prev_sha[:10]}",
        "",
        f"Outcomes from your experiments in round {replay_round}:",
        "",
    ]
    round_expectations = (expectations or {}).get(replay_round) or {}
    exp_rows = round_expectations.get("expectations") or []
    exp_by_slot = {
        row.get("slot"): row for row in exp_rows if isinstance(row, dict)
    }
    for e in last:
        if e.selected:
            outcome = "SELECTED_AS_NEW_INCUMBENT"
        elif e.gate_passed:
            outcome = "PASSED_GATES_NOT_IMPROVED"
        else:
            outcome = "FAILED_GATES"
        candidate = e.candidate_sha[:10] if e.candidate_sha else "—"
        paths = ", ".join(e.changed_paths) if e.changed_paths else "—"
        lines.append(f"  {e.experiment_id}")
        lines.append(
            f"    parent revision: {e.parent_sha[:10]}   "
            f"candidate: {candidate}"
        )
        lines.append(f"    changed paths: {paths}")
        lines.append(
            f"    gate: {'PASSED' if e.gate_passed else 'FAILED'}   "
            f"{_fmt_metrics(e.metrics)}"
        )
        lines.append(f"    outcome: {outcome}")
        row = exp_by_slot.get(e.candidate)
        if row is not None:
            lines.append(
                "    pre-registered expectation (your own words at "
                "suspension, before this result existed): "
                f"{row.get('expectation')}"
            )
            if row.get("would_weaken"):
                lines.append(
                    f"    you said this would weaken the belief if: "
                    f"{row.get('would_weaken')}"
                )
        elif round_expectations:
            lines.append(
                "    pre-registered expectation: NOT RECORDED for this slot "
                "(capture failed or skipped) — this outcome cannot be checked "
                "against a prior commitment."
            )
        else:
            lines.append(
                "    pre-registered expectation: NONE was recorded for this "
                "round — this outcome cannot be checked against a prior "
                "commitment."
            )
        lines.append("")
    lines.append(
        "Work in this order: first close the previous loop — for each outcome "
        "above, settle what it did to the belief that motivated the "
        "experiment (supported it / weakened it / left it undecided) — then "
        "investigate and choose new directions from the world that exists "
        "now. This ordering is how you work, not a separate report to file."
    )
    lines.append("")

    lines.append(f"Current accepted revision: {base_sha[:10]}")
    lines.append("")

    selected = [e for e in last if e.selected]
    any_passed = any(e.gate_passed for e in last)
    if selected:
        lines.append(
            "The harness selected one of the above as the new accepted "
            f"revision ({prev_sha[:10]} → {base_sha[:10]}). Your earlier "
            "observations were made on the previous revision and may no longer "
            "describe the current code."
        )
    elif any_passed:
        lines.append(
            "Candidate(s) passed the gates but none improved the incumbent, so "
            f"the accepted revision is unchanged ({base_sha[:10]}). The world "
            "has not changed."
        )
    else:
        lines.append(
            "No candidate passed the gates; the accepted revision is unchanged "
            f"({base_sha[:10]})."
        )
    return "\n".join(lines)


# --- Guard repair messages -------------------------------------------------

_GUARD_REASONS = {
    "repeated_tool": (
        "That tool call is identical to the previous one and would add no new "
        "information. Change the query, inspect a different region, or move on "
        "via submit_proposals."
    ),
}


def _guard_repair_message(reason: str) -> str:
    base = _GUARD_REASONS.get(reason, "")
    return (
        f"Protocol correction required ({reason}). {base} Return exactly one "
        "JSON object with an 'action', with no prose or additional JSON "
        "outside it."
    )


# --- Action parsing -------------------------------------------------------

def _require_keys(
    value: dict,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    allowed = required | (optional or set())
    if not required <= set(value) or set(value) - allowed:
        raise ProposerError(
            f"invalid keys for {value.get('action')}: {sorted(value)}"
        )


def _require_string_list(value, *, name: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ProposerError(f"{name} must be a list")
    if not allow_empty and not value:
        raise ProposerError(f"{name} must be non-empty")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ProposerError(f"{name} must contain non-empty strings")
        out.append(item.strip())
    return out


def _parse_research_target(value) -> ExistingFindingTarget | NewFindingTarget:
    if not isinstance(value, dict):
        raise ProposerError("research_target must be an object")
    mode = value.get("mode")
    if mode == "existing":
        if set(value) - {"mode", "finding_id"}:
            raise ProposerError(
                f"research_target(existing) has unexpected keys: {sorted(value)}"
            )
        finding_id = value.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise ProposerError(
                "research_target(existing).finding_id must be a non-empty string"
            )
        return ExistingFindingTarget(finding_id=finding_id.strip())
    if mode == "new":
        allowed = {"mode", "question", "mechanisms", "code_regions"}
        if set(value) - allowed:
            raise ProposerError(
                f"research_target(new) has unexpected keys: {sorted(value)}"
            )
        question = value.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ProposerError(
                "research_target(new).question must be a non-empty string"
            )
        mechanisms = tuple(_require_string_list(
            value.get("mechanisms", []),
            name="research_target.mechanisms", allow_empty=True,
        ))
        code_regions = tuple(_require_string_list(
            value.get("code_regions", []),
            name="research_target.code_regions", allow_empty=True,
        ))
        return NewFindingTarget(
            question=question.strip(),
            mechanisms=mechanisms,
            code_regions=code_regions,
        )
    raise ProposerError(
        f"research_target.mode must be 'existing' or 'new', got {mode!r}"
    )


def _parse_proposal(value) -> ResearchProposal:
    if not isinstance(value, dict):
        raise ProposerError("proposal must be an object")
    allowed = {
        "instruction", "research_target", "evidence_refs", "material_difference",
    }
    if set(value) - allowed:
        raise ProposerError(f"proposal has unexpected keys: {sorted(value)}")
    instruction = value.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ProposerError("proposal.instruction must be non-empty")
    target = _parse_research_target(value.get("research_target"))
    evidence_refs = tuple(_require_string_list(
        value.get("evidence_refs", []),
        name="proposal.evidence_refs", allow_empty=True,
    ))
    md = value.get("material_difference")
    if md is not None and (not isinstance(md, str) or not md.strip()):
        raise ProposerError(
            "proposal.material_difference must be a non-empty string when present"
        )
    return ResearchProposal(
        instruction=instruction.strip(),
        research_target=target,
        evidence_refs=evidence_refs,
        material_difference=(md.strip() if isinstance(md, str) else None),
    )


# The self-mechanism surfaces a CHANGE may target (RSI semantics §10). Frozen as
# part of the self-review contract — a CHANGE must name which part of itself the
# Scientist believes is limiting Goal progress.
_SELF_CHANGE_TARGETS = frozenset({
    "prompt", "context", "tools", "runtime", "retrieval", "model-policy",
})


def _parse_self_change(value: dict) -> dict:
    """Validate a CHANGE's self_change object: {target, intent, instruction,
    evidence_refs?}. target ∈ _SELF_CHANGE_TARGETS; intent/instruction are the
    WHAT/WHY for the self-executor (HOW is not the Scientist's job, semantics
    §11)."""
    if not isinstance(value, dict):
        raise ProposerError("self_change must be an object")
    _require_keys(
        value, {"target", "intent", "instruction"}, {"action", "evidence_refs"})
    target = value["target"]
    if target not in _SELF_CHANGE_TARGETS:
        raise ProposerError(
            f"self_change.target must be one of {sorted(_SELF_CHANGE_TARGETS)}; "
            f"got {target!r}")
    intent = value["intent"]
    instruction = value["instruction"]
    for nm, val in (("intent", intent), ("instruction", instruction)):
        if not isinstance(val, str) or not val.strip():
            raise ProposerError(f"self_change.{nm} must be a non-empty string")
    evidence_refs: list[str] = []
    if "evidence_refs" in value:
        evidence_refs = _require_string_list(
            value["evidence_refs"], name="self_change.evidence_refs",
            allow_empty=True)
    return {
        "target": target,
        "intent": intent.strip(),
        "instruction": instruction.strip(),
        "evidence_refs": tuple(evidence_refs),
    }


def _dispatch(action: dict, proposal_slots: int) -> dict:
    """Validate one inner action object. Tool actions and submit_proposals."""
    name = action.get("action")
    if not isinstance(name, str):
        raise ProposerError("action.action must be a string")

    # --- research / memory tools (never terminate) ---
    if name == "run_research_command":
        _require_keys(action, {"action", "command"}, {"cwd"})
        command = action["command"]
        cwd = action.get("cwd", "work")
        if not isinstance(command, str) or not command.strip():
            raise ProposerError("research command must be non-empty")
        if cwd not in {"work", "scratch"}:
            raise ProposerError("research cwd must be work or scratch")
        return {"action": name, "command": command, "cwd": cwd}
    if name == "inspect_episode":
        _require_keys(action, {"action", "ref"})
        ref = action["ref"]
        if not isinstance(ref, str) or not ref.strip():
            raise ProposerError("episode ref must be non-empty")
        return {"action": name, "ref": ref.strip()}
    if name == "list_findings":
        _require_keys(action, {"action"}, {"state", "limit"})
        state = action.get("state", "active")
        if state not in {"active", "open", "dormant", "archived", "all"}:
            raise ProposerError(
                "list_findings.state must be one of active/open/dormant/"
                "archived/all"
            )
        limit = action.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProposerError("list_findings.limit must be a positive integer")
        return {"action": name, "state": state, "limit": limit}
    if name == "search_findings":
        _require_keys(action, {"action", "query"}, {"limit"})
        query = action["query"]
        if not isinstance(query, str) or not query.strip():
            raise ProposerError("search_findings.query must be non-empty")
        limit = action.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProposerError(
                "search_findings.limit must be a positive integer"
            )
        return {"action": name, "query": query.strip(), "limit": limit}
    if name == "inspect_finding":
        _require_keys(action, {"action", "finding_id"})
        fid = action["finding_id"]
        if not isinstance(fid, str) or not fid.strip():
            raise ProposerError("inspect_finding.finding_id must be non-empty")
        return {"action": name, "finding_id": fid.strip()}
    if name == "search_experiments":
        _require_keys(
            action, {"action", "query"},
            {"filters", "limit", "buckets"},
        )
        query = action["query"]
        if not isinstance(query, str) or not query.strip():
            raise ProposerError("search_experiments.query must be non-empty")
        filters = action.get("filters")
        if filters is not None and not isinstance(filters, dict):
            raise ProposerError("search_experiments.filters must be an object")
        allowed_filters = {
            "gate_passed", "eligible", "selected", "finding_id",
            "changed_path", "round_min", "round_max", "status",
        }
        if filters:
            unknown = set(filters) - allowed_filters
            if unknown:
                # Tolerate unknown filter keys (the model often conflates
                # list_findings' `state` into search_experiments.filters).
                # Drop them rather than burn a protocol-repair round-trip on a
                # harmless extra key — a meaningful filter set still applies.
                filters = {k: v for k, v in filters.items()
                           if k in allowed_filters}
        limit = action.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProposerError(
                "search_experiments.limit must be a positive integer"
            )
        buckets = action.get("buckets", True)
        if not isinstance(buckets, bool):
            raise ProposerError("search_experiments.buckets must be a bool")
        return {
            "action": name, "query": query.strip(),
            "filters": filters or {}, "limit": limit, "buckets": buckets,
        }

    # --- terminal action ---
    if name == "submit_proposals":
        _require_keys(action, {"action", "proposals"})
        proposals = action["proposals"]
        if not isinstance(proposals, list):
            raise ProposerError("proposals must be a list")
        if len(proposals) > proposal_slots:
            raise ProposerError(
                f"at most {proposal_slots} proposal(s) allowed; "
                f"got {len(proposals)}"
            )
        # 0 proposals is a legal abstention.
        parsed = [_parse_proposal(item) for item in proposals]
        return {"action": name, "proposals": parsed}

    # --- terminal action: self-review decision (RSI S3c) ---
    if name == "submit_self_decision":
        _require_keys(
            action, {"action", "decision", "diagnosis"},
            {"keep_reason", "next_review_after_rounds", "self_change"})
        decision = action["decision"]
        if decision not in ("KEEP", "CHANGE"):
            raise ProposerError("self_decision.decision must be KEEP or CHANGE")
        diagnosis = action["diagnosis"]
        if not isinstance(diagnosis, str) or not diagnosis.strip():
            raise ProposerError(
                "self_decision.diagnosis must be a non-empty string")
        keep_reason = action.get("keep_reason")
        next_review = action.get("next_review_after_rounds")
        self_change = action.get("self_change")
        if decision == "KEEP":
            # KEEP must justify sufficiency AND commit to a re-examination time
            # (the Host's clock兑现s it — semantics §17).
            if not isinstance(keep_reason, str) or not keep_reason.strip():
                raise ProposerError(
                    "KEEP requires a non-empty keep_reason tied to Goal progress")
            if (not isinstance(next_review, int) or isinstance(next_review, bool)
                    or next_review < 1):
                raise ProposerError(
                    "KEEP requires next_review_after_rounds (a positive int): "
                    "after how many task rounds you should re-examine yourself")
            self_change = None
        else:  # CHANGE
            self_change = _parse_self_change(self_change or {})
            # CHANGE's commitment is set by the Host after adoption (S3d); the
            # proposer may optionally suggest one here.
            if next_review is not None and (
                    not isinstance(next_review, int)
                    or isinstance(next_review, bool) or next_review < 1):
                raise ProposerError(
                    "next_review_after_rounds must be a positive int if given")
            keep_reason = None
        return {
            "action": name, "decision": decision, "diagnosis": diagnosis.strip(),
            "keep_reason": (keep_reason.strip()
                            if isinstance(keep_reason, str) else None),
            "next_review_after_rounds": next_review,
            "self_change": self_change,
        }

    # --- terminal action: reflection handoff (continuity design §14) ---
    if name == "submit_reflection_handoff":
        _require_keys(
            action, {"action", "handoff"},
            {"self_limitation_suspected", "note"})
        handoff = action["handoff"]
        if not isinstance(handoff, str) or not handoff.strip():
            raise ProposerError(
                "reflection_handoff.handoff must be a non-empty string")
        suspected = action.get("self_limitation_suspected", False)
        if not isinstance(suspected, bool):
            raise ProposerError(
                "reflection_handoff.self_limitation_suspected must be a "
                "boolean")
        note = action.get("note")
        if note is not None and (
                not isinstance(note, str) or not note.strip()):
            raise ProposerError(
                "reflection_handoff.note must be a non-empty string if given")
        return {
            "action": name,
            "handoff": handoff.strip(),
            "self_limitation_suspected": suspected,
            "note": note.strip() if isinstance(note, str) else None,
        }

    raise ProposerError(f"unknown action: {name}")


def parse_response(text: str, proposal_slots: int) -> dict:
    """Parse one Scientist response.

    Top level is ``{"message"?: str, "action": {...}}``. ``message`` is
    optional natural text (left in the trajectory via the raw reply; not read
    here). Only the inner ``action`` is validated and returned.
    """
    try:
        obj = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProposerError("response must be one JSON object") from exc
    if not isinstance(obj, dict):
        raise ProposerError("response must be one JSON object")
    action = obj.get("action")
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        raise ProposerError(
            "response must be a JSON object with an 'action' object"
        )
    return _dispatch(action, proposal_slots)


# --- Guard ----------------------------------------------------------------

def _validate_action_guard(
    state: WorkingState, action: dict, source_root: Path,
) -> str | None:
    """Only remaining guard: an exact-repeat tool call back-to-back adds
    nothing and risks a loop. Every cognitive guard (block evidence, quota
    consistency, select-before-submit) was removed with the pipeline."""
    name = action["action"]
    if name in _RESEARCH_TOOL_ACTIONS:
        fp = _fingerprint(action)
        if (state.last_tool_fingerprint is not None
                and fp == state.last_tool_fingerprint):
            return "repeated_tool"
    return None


# --- The Scientist --------------------------------------------------------

class ScientistAgent(ResearchAgent):
    _error_class = ProposerError

    def __init__(
        self,
        *,
        model: ChatModel,
        runtime: ApptainerRuntime,
        timeout_seconds: int,
        max_steps: int,
        command_timeout_seconds: int,
        command_output_cap_chars: int,
        usage_observer=None,
        context_policy: ContextPolicy | None = None,
    ):
        super().__init__(
            model=model, runtime=runtime,
            timeout_seconds=timeout_seconds, max_steps=max_steps,
            command_timeout_seconds=command_timeout_seconds,
            command_output_cap_chars=command_output_cap_chars,
            usage_observer=usage_observer,
        )
        self._proposal_slots = 1
        self._context_policy = context_policy or ContextPolicy()

    def _maybe_compact(
        self, messages: list[dict], usages: list, state: WorkingState,
    ) -> None:
        """Shed the oldest turn-pairs from the live ``messages`` when the
        context crosses the token threshold. Mutates ``messages`` in place
        (the caller's reference stays valid); the session.jsonl archive is
        untouched. Uses the most recent model call's prompt-token count, with a
        char-based fallback when the provider reports none."""
        policy = self._context_policy
        threshold = policy.emergency_threshold_tokens
        if threshold is None:
            return
        usage = usages[-1] if usages else None
        tokens = _prompt_tokens(usage)
        if tokens is None:
            tokens = _estimate_tokens(messages)
        if tokens <= threshold:
            return
        new_messages, info = _compact_live_messages(
            messages,
            window_pairs=policy.window_pairs,
            window_max_chars=policy.window_max_chars,
        )
        if not info["compacted"]:
            return
        messages[:] = new_messages
        _bump(state, "compact")
        print(
            f"[scientist] emergency compact at ~{tokens} tokens: "
            f"{info['before_msgs']}→{info['after_msgs']} msgs, "
            f"kept {info['kept_pairs']} turn-pair(s) "
            f"(window={policy.window_pairs}/{policy.window_max_chars}c)",
            flush=True,
        )

    def _parse_action(self, text: str) -> dict:
        return parse_response(text, self._proposal_slots)

    def _validate_guard(
        self, state: WorkingState, action: dict, source_root: Path,
    ) -> str | None:
        return _validate_action_guard(state, action, source_root)

    def research(
        self,
        *,
        goal: str,
        editable: list[str],
        world_mount,
        memory_service,
        base_sha: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        proposal_slots: int,
        hints: list[str] | None = None,
        session: ScientistSession,
        max_steps: int | None = None,
    ) -> ScientistRound:
        """Run one round of this Scientist's research, persisting its lived
        trajectory and notebook into ``session`` as it goes."""
        self._proposal_slots = proposal_slots
        charter = load_semantic("proposer", prompt_dir)
        system_prompt = _build_system_prompt(
            charter=charter, goal=goal, editable=editable, base_sha=base_sha,
            gate_block=gate_block, proposal_slots=proposal_slots,
            hints=hints, notebook=session.notebook,
        )

        # --- assemble the live context (cold start vs resume) ---
        if session.is_first_round():
            messages: list[dict] = [{"role": "user", "content": _COLD_START}]
            print("[scientist] cold start — first round of this Scientist",
                  flush=True)
        else:
            # Cross-round continuity = coverage map + autobiographical notebook
            # + world-transition outcomes. The coverage map orients the
            # Scientist to what is already covered (so it does not re-mine
            # ground); the world event reports last round's OUTCOMES (not the
            # direction texts); the notebook carries its own running
            # understanding. The previous round's raw trajectory is NOT
            # re-injected: it is lived history about a world that may no longer
            # exist, and carrying it hot would let the old world dominate the
            # resumed Scientist's attention.
            messages: list[dict] = []
            if memory_service is not None:
                try:
                    coverage = memory_service.build_coverage_pack(
                        current_round=current_round)
                    if coverage:
                        messages.append(
                            {"role": "user", "content": coverage})
                except Exception as exc:
                    print(
                        f"[scientist] coverage pack build failed: {exc}",
                        flush=True,
                    )
            world_event = _build_world_event(
                memory_service, current_round, base_sha,
                expectations=read_expectations(run_dir),
            )
            if world_event is None:
                world_event = (
                    "Your research is resuming. No experiments were run from "
                    "your last round (you submitted no directions, or the "
                    "round abstained). The accepted revision remains "
                    f"{base_sha[:10]}."
                )
            messages.append({"role": "user", "content": world_event})
            # Archive the world event into the Scientist's lived history — it is
            # something this Scientist was told (observed), so the immutable
            # archive must record it. (The coverage map is ephemeral —
            # recomputed each round — so it is not archived.)
            session.append_message(
                "user", world_event, round_id=current_round,
            )
            # Replay the latest not-yet-replayed reflection handoff — the
            # continuity design makes it the primary anchor of the round
            # AFTER a reflection, with an explicit epistemic status: it is
            # one judgment, not a fact and not an instruction. The guarantee
            # is demotion (unfinished plans no longer carry by default), not
            # promotion (the criticism does not become truth).
            try:
                replayed_through = int(
                    session.meta.get("last_reflection_replayed", -1))
            except (TypeError, ValueError):
                replayed_through = -1
            pending = [
                record for record in read_reflection_records(run_dir)
                if isinstance(record.get("round_id"), int)
                and record["round_id"] > replayed_through
                and record.get("handoff")
                and not record.get("abstained")
            ]
            if pending:
                record = pending[-1]
                handoff_msg = (
                    f"Reflection handoff from round {record['round_id']} — "
                    "one judgment your past self made after deliberately "
                    "doubting its own trajectory. It is not a fact and not an "
                    "instruction; weigh it against the current evidence, then "
                    "decide for yourself:\n\n" + str(record["handoff"])
                )
                messages.append({"role": "user", "content": handoff_msg})
                session.append_message(
                    "user", handoff_msg, round_id=current_round)
                session.note_replayed_reflection(record["round_id"])
            print(
                f"[scientist] resume — scientist_id={session.scientist_id[:8]} "
                f"cold context (raw tail not re-injected); coverage map + "
                f"world-transition injected"
                + ("; reflection handoff replayed" if pending else ""),
                flush=True,
            )

        # The setup above (charter, system prompt, cold-start/resume context) is
        # task-specific; the agentic step-loop below is mode-agnostic and shared
        # with self_review() via _deliberate. tools_factory / make_result carry
        # the task-specific construction (workspace-bound tools; ScientistRound).
        def tools_factory(scratch, home):
            return ResearchTools(
                runtime=self.runtime,
                workspace=source_path,
                repo=repo_path,
                history_dir=run_dir,
                scratch=Path(scratch),
                world_mount=world_mount,
                home=home,
                memory_service=memory_service,
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
                current_round=current_round,
            )

        def make_result(action, state, usages, step, outcome):
            if outcome == "submit":
                proposals = action["proposals"]
                abstained = len(proposals) == 0
                return ScientistRound(
                    proposals=proposals,
                    abstained=abstained,
                    abstain_reason=(
                        "submitted no directions this round"
                        if abstained else None
                    ),
                    usage=usages,
                    deliberation_telemetry=_build_telemetry(
                        state, steps=step, outcome="submit"),
                    trace=_build_trace(
                        state, round_id=current_round, outcome="submit"),
                )
            abstain_reason = (
                "research budget exhausted before the Scientist submitted "
                "directions"
            )
            return ScientistRound(
                proposals=[],
                abstained=True,
                abstain_reason=abstain_reason,
                usage=usages,
                deliberation_telemetry=_build_telemetry(
                    state, steps=step, outcome="abstain"),
                trace=_build_trace(
                    state, round_id=current_round, outcome="abstain"),
            )

        return self._deliberate(
            system_prompt=system_prompt,
            messages=messages,
            session=session,
            current_round=current_round,
            max_steps=max_steps,
            source_root=source_path,
            tools_factory=tools_factory,
            terminal_name="submit_proposals",
            budget_nudge=_BUDGET_NUDGE,
            capture_expectations=True,
            make_result=make_result,
        )

    def self_review(
        self, *,
        goal: str,
        self_repo: Path,
        run_dir: Path,
        reviews_path: Path,
        incumbent_self_sha: str,
        objective_key: str | None,
        current_round: int,
        prompt_dir: Path | None,
        session: ScientistSession,
        memory_service=None,
        max_steps: int | None = None,
    ) -> SelfReviewResult:
        """Run one self-review round (RSI S3c): the same Scientist studies itself
        as the research system and emits a KEEP/CHANGE self_decision. Reuses the
        shared ``_deliberate`` loop with the self-review charter/world/terminal.

        CHANGE records intent only — the self-executor (S3d) does HOW. The Host
        (S3c.2) appends the result to reviews.jsonl and兑现s the commitment.
        """
        charter = load_semantic("self_review", prompt_dir)
        progress_pack = _build_self_progress_pack(
            run_dir, objective_key, current_round)
        self_history = _read_self_review_history_text(reviews_path)
        system_prompt = _build_self_review_prompt(
            charter=charter, goal=goal,
            incumbent_self_sha=incumbent_self_sha,
            progress_pack=progress_pack, self_history=self_history,
            notebook=session.notebook,
        )

        # Same cold-start vs resume pattern as task research. The Scientist's
        # autobiography (session/notebook) persists across task AND self rounds
        # — identity continuity, not system-prompt continuity (semantics §9/§20).
        if session.is_first_round():
            messages: list[dict] = [
                {"role": "user", "content": _SELF_REVIEW_COLD_START}]
            print("[scientist] self-review cold start", flush=True)
        else:
            msg = ("Your self-review is resuming. Re-ground in the current "
                   "progress and your self-review history above, then decide.")
            messages: list[dict] = [{"role": "user", "content": msg}]
            session.append_message("user", msg, round_id=current_round)

        def tools_factory(scratch, home):
            # The self-world: the Scientist reads its own source. workspace =
            # self_repo (read-only via an empty MountMap — editing is the
            # self-executor's job in S3d, not the reviewer's).
            return ResearchTools(
                runtime=self.runtime,
                workspace=self_repo,
                repo=self_repo,
                history_dir=run_dir,
                scratch=Path(scratch),
                world_mount=MountMap(),
                home=home,
                memory_service=memory_service,
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
                current_round=current_round,
            )

        def make_result(action, state, usages, step, outcome):
            if outcome == "submit":
                return SelfReviewResult(
                    decision=action["decision"],
                    diagnosis=action["diagnosis"],
                    keep_reason=action.get("keep_reason"),
                    next_review_after_rounds=action.get("next_review_after_rounds"),
                    self_change=action.get("self_change"),
                    usage=usages,
                    deliberation_telemetry=_build_telemetry(
                        state, steps=step, outcome="submit"),
                    trace=_build_trace(
                        state, round_id=current_round, outcome="self_review"),
                )
            # Budget exhausted before a decision: KEEP with a default commitment
            # so the Host's scheduler re-opens self-attention soon, not never.
            return SelfReviewResult(
                decision="KEEP",
                diagnosis="self-review budget exhausted before a decision",
                keep_reason="no decision reached; defaulting to KEEP pending a "
                            "real review at the next opening",
                next_review_after_rounds=_SELF_REVIEW_DEFAULT_DEFER,
                abstained=True,
                usage=usages,
                deliberation_telemetry=_build_telemetry(
                    state, steps=step, outcome="abstain"),
                trace=_build_trace(
                    state, round_id=current_round, outcome="self_review"),
            )

        return self._deliberate(
            system_prompt=system_prompt,
            messages=messages,
            session=session,
            current_round=current_round,
            max_steps=max_steps,
            source_root=self_repo,
            tools_factory=tools_factory,
            terminal_name="submit_self_decision",
            budget_nudge=_SELF_REVIEW_BUDGET_NUDGE,
            make_result=make_result,
        )

    def reflection(
        self, *,
        goal: str,
        editable: list[str],
        world_mount,
        memory_service,
        base_sha: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        prompt_dir: Path | None,
        session: ScientistSession,
        max_steps: int | None = None,
    ) -> ReflectionResult:
        """Run one Reflection round: the same Scientist temporarily stops
        advancing the research and audits how the recent version of itself
        has been conducting it. Distinct from self_review (RSI): the object
        under audit is the RESEARCH trajectory in the research world, not the
        self source; the output is a handoff warning, not a KEEP/CHANGE.

        The evidence pack is the deterministic aggregate trajectory view
        (memory.build_reflection_pack) — patterns invisible at round
        granularity are what reflection exists to see. tools_factory is the
        research world (same as a task round): reflection reads the real code
        and may run probes, but its only terminal action is the handoff.
        """
        charter = load_semantic("reflection", prompt_dir)
        pack = ""
        if memory_service is not None:
            try:
                pack = memory_service.build_reflection_pack(
                    current_round=current_round)
            except Exception as exc:
                print(f"[scientist] reflection pack build failed: {exc}",
                      flush=True)
        system_prompt = _build_reflection_prompt(
            charter=charter, goal=goal, base_sha=base_sha,
            pack=pack, notebook=session.notebook,
        )

        # Same cold-start vs resume pattern as task research — the notebook
        # and session persist across task AND reflection rounds (identity
        # continuity). The reflection round's own suspension checkpoint
        # rewrites the notebook: the charter directs that rewrite toward
        # drift correction and demoting unsupported unfinished plans.
        if session.is_first_round():
            messages: list[dict] = [
                {"role": "user", "content": _REFLECTION_COLD_START}]
            print("[scientist] reflection cold start", flush=True)
        else:
            msg = ("Your reflection is resuming. Re-ground in the evidence "
                   "pack and the current work, then leave your handoff.")
            messages = [{"role": "user", "content": msg}]
            session.append_message("user", msg, round_id=current_round)

        def tools_factory(scratch, home):
            # The research world — identical to a task round. Reflection
            # audits the research, so it sees the same reality the normal
            # Scientist works in (not self_review's self-repo world).
            return ResearchTools(
                runtime=self.runtime,
                workspace=source_path,
                repo=repo_path,
                history_dir=run_dir,
                scratch=Path(scratch),
                world_mount=world_mount,
                home=home,
                memory_service=memory_service,
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
                current_round=current_round,
            )

        def make_result(action, state, usages, step, outcome):
            if outcome == "submit":
                return ReflectionResult(
                    handoff=action["handoff"],
                    self_limitation_suspected=action.get(
                        "self_limitation_suspected", False),
                    note=action.get("note"),
                    usage=usages,
                    deliberation_telemetry=_build_telemetry(
                        state, steps=step, outcome="submit"),
                    trace=_build_trace(
                        state, round_id=current_round, outcome="reflection"),
                )
            # Budget exhausted before a handoff: an explicit abstention, not
            # a fabricated critique. The Host records it as abstained and the
            # interval schedules the next real reflection.
            return ReflectionResult(
                handoff="(reflection budget exhausted before a handoff)",
                abstained=True,
                usage=usages,
                deliberation_telemetry=_build_telemetry(
                    state, steps=step, outcome="abstain"),
                trace=_build_trace(
                    state, round_id=current_round, outcome="reflection"),
            )

        return self._deliberate(
            system_prompt=system_prompt,
            messages=messages,
            session=session,
            current_round=current_round,
            max_steps=max_steps,
            source_root=source_path,
            tools_factory=tools_factory,
            terminal_name="submit_reflection_handoff",
            budget_nudge=_REFLECTION_BUDGET_NUDGE,
            make_result=make_result,
        )

    def _deliberate(
        self, *,
        system_prompt: str,
        messages: list[dict],
        session: ScientistSession,
        current_round: int,
        max_steps: int | None,
        source_root: Path,
        tools_factory,
        terminal_name: str,
        budget_nudge: str,
        make_result,
        capture_expectations: bool = False,
    ):
        """The shared agentic step-loop for one deliberation (task research OR
        self-review). The caller builds the mode-specific system prompt, initial
        messages, and a ``tools_factory(scratch, home) -> ResearchTools``; this
        owns the budget, session archival, emergency compact, suspension
        checkpoint, and the terminal/abstain handling — the invariants that must
        be identical across modes.

        ``make_result(action, state, usages, step, outcome)`` builds the
        mode-specific return: ``outcome == "submit"`` means the ``terminal_name``
        action was reached (``action`` is its parsed dict); ``"abstain"`` means
        the budget ran out first (``action`` is None).

        ``capture_expectations`` is True only for task research rounds — only
        they submit experiments, so only they pre-register expectations.
        """
        steps_budget = max_steps or self.max_steps
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages: list = []
        state = WorkingState()
        budget_reminder_step = int(0.8 * steps_budget)
        reminded = False

        with TemporaryDirectory(prefix="simpleloop-scratch-") as scratch, \
                TemporaryDirectory(prefix="simpleloop-session-") as session_root:
            home = Path(session_root) / "home"
            home.mkdir(mode=0o700)
            tools = tools_factory(scratch, home)
            # If the round STARTS already over threshold (a bloated resume tail
            # of large prior-round observations — the cross-round stacking case),
            # shed before the first model call so we never open a round already
            # over the window. No usage yet → char-based estimate drives it.
            self._maybe_compact(messages, [], state)
            for _step_num in range(steps_budget):
                step = _step_num + 1
                print(f"[{_stamp()}] [scientist step {step}/{steps_budget}] "
                      f"thinking",
                      flush=True)
                if (not reminded and budget_reminder_step > 0
                        and step >= budget_reminder_step):
                    messages.append({"role": "user", "content": budget_nudge})
                    reminded = True

                action, reply_text = self._step(
                    state, messages, system_prompt, deadline, usages, step,
                    source_root=source_root, steps_budget=steps_budget,
                )
                name = action["action"]
                state.action_log.append({"action": name, "step": step})

                if name == terminal_name:
                    _bump(state, name)
                    # The terminal reply enters BOTH the live context and the
                    # archive, so the suspension checkpoint sees what the
                    # Scientist just decided (it must recall its own decision
                    # when writing the continuation note).
                    messages.append({"role": "assistant", "content": reply_text})
                    session.append_message("assistant", reply_text,
                                           round_id=current_round)
                    print(
                        f"[scientist] {name} step={step} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    self._suspension_checkpoint(
                        system_prompt, messages, state, session, deadline,
                        usages, current_round,
                        capture_expectations=capture_expectations,
                    )
                    return make_result(action, state, usages, step, "submit")

                # tool call
                observation = tools.execute(action, deadline=deadline)
                _bump(state, "tool")
                _register_evidence(state, action, observation)
                state.last_tool_fingerprint = _fingerprint(action)
                if observation.get("ok") and name == "run_research_command":
                    _bump(state, "source_read")
                    state.located = True
                obs_envelope = json.dumps(
                    {"tool_result": observation}, ensure_ascii=False,
                )
                messages.extend([
                    {"role": "assistant", "content": reply_text},
                    {"role": "user", "content": obs_envelope},
                ])
                session.append_message("assistant", reply_text,
                                       round_id=current_round)
                session.append_message("user", obs_envelope,
                                       round_id=current_round)
                print(
                    f"[{_stamp()}] [scientist step {step}/{steps_budget}] "
                    f"{name} ok={observation.get('ok')}",
                    flush=True,
                )
                # The live context grows by one (assistant, observation) pair
                # per step. On source-heavy tasks this fills the window long
                # before the step budget — shed oldest pairs when it does. The
                # archive was just written in full above, so compacting the live
                # list loses nothing from the Scientist's lived record.
                self._maybe_compact(messages, usages, state)

            # Budget exhausted without a terminal action.
            print(f"[scientist] budget exhausted before {terminal_name}",
                  flush=True)
            self._suspension_checkpoint(
                system_prompt, messages, state, session, deadline, usages,
                current_round,
                capture_expectations=capture_expectations,
            )
            return make_result(None, state, usages, steps_budget, "abstain")

    def _suspension_checkpoint(
        self, system_prompt: str, messages: list[dict], state: WorkingState,
        session: ScientistSession, deadline: float, usages: list,
        round_id: int, *, capture_expectations: bool = False,
    ) -> None:
        """Ask the Scientist to leave a continuation note for its resumed self,
        and persist it as the notebook (rewritten, not appended).

        With ``capture_expectations`` (task rounds only) the reply's
        pre-registered expectations are persisted as a durable row — including
        an explicit ``captured=False`` row when the capture fails, so a missing
        pre-registration is visible rather than silent.

        Best-effort: a parse failure or zero remaining budget leaves the prior
        notebook untouched rather than failing the round.
        """
        if capture_expectations:
            session.append_expectations(round_id, [], captured=False)
        remaining = deadline - time.monotonic()
        if remaining <= 5:
            return
        prompt_messages = list(messages) + [{"role": "user", "content": _SUSPEND_PROMPT}]
        try:
            reply = self.model.complete(
                system=system_prompt, messages=prompt_messages,
                timeout_seconds=remaining,
            )
        except Exception as exc:
            print(f"[scientist] suspension checkpoint model call failed: {exc}",
                  flush=True)
            return
        if reply.usage is not None:
            usages.append(reply.usage)
            if self.usage_observer is not None:
                self.usage_observer(reply.usage)
        try:
            obj = json.loads(reply.text)
            note = obj.get("notebook")
        except (json.JSONDecodeError, TypeError, AttributeError):
            obj, note = None, None
        if capture_expectations:
            valid = _valid_expectations(obj.get("expectations") if obj else None)
            if valid:
                session.append_expectations(round_id, valid, captured=True)
        if isinstance(note, str) and note.strip():
            session.write_notebook(note.strip())
            session.append_message("user", _SUSPEND_PROMPT, round_id=round_id)
            session.append_message("assistant", reply.text, round_id=round_id)
            print("[scientist] notebook updated at suspension", flush=True)
        else:
            print("[scientist] suspension produced no notebook; left as-is",
                  flush=True)
