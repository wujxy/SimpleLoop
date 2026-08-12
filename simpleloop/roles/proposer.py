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
)
from .scientist_session import ScientistSession
from ..container.runtime import ApptainerRuntime
from ..memory.context import build_generation_context
from ..memory.models import (
    ExistingFindingTarget,
    NewFindingTarget,
    ResearchProposal,
)
from ..prompts import load_semantic


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
SCIENTIST_PROMPT_VERSION = "scientist-v1"


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
# auditable and is what tail_turns() reads on the next resume.

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
- /history.jsonl and /rounds are persisted run evidence when present;
  /scratch is temporary writable space.
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
    "executed as experiments. Leave a continuation note for your resumed self: "
    "what you currently understand, what you believe and why, what evidence "
    "changed your view, and what you were about to pursue. Write it in the "
    "first person, as your own running account. Return one JSON object:\n"
    '  {"notebook": "<your continuation note>"}'
)


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


def _build_world_event(memory_service, current_round: int, base_sha: str) -> str | None:
    """The two-part resume event.

    SELF  — the directions YOU submitted were executed; here is what happened
            (authoritative harness results, facts not interpretations).
    PROJECT — how the project's incumbent/world moved.

    Single-lane: every experiment from last round is this Scientist's. True
    per-Scientist attribution (filtering by scientist_id once multiple lanes
    exist) is deferred — see scientist_session.py docstring.
    """
    try:
        experiments = memory_service.load_experiments()
    except Exception:
        return None
    last = [e for e in experiments if e.round == current_round - 1]
    if not last:
        return None
    last.sort(key=lambda e: (e.round, e.candidate))
    lines = [
        "Your research is resuming. While you were paused, the directions you "
        "submitted were executed as experiments. Their outcomes (authoritative "
        "harness results — these are facts, not your interpretations):",
    ]
    for e in last:
        gate = "pass" if e.gate_passed else "fail"
        sel = " selected" if e.selected else ""
        finding = e.finding_id or "-"
        lines.append(
            f"  {e.experiment_id}  finding={finding}  status={e.status}  "
            f"gate={gate}{sel}  {_fmt_metrics(e.metrics)}"
        )
    selected = [e for e in last if e.selected]
    if selected:
        lines.append(
            "Project state: the harness selected one of the above as the new "
            f"accepted revision. The incumbent is now {base_sha[:10]}."
        )
    else:
        lines.append(
            "Project state: no candidate cleared the gates this round, so the "
            f"accepted revision is unchanged ({base_sha[:10]})."
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
                raise ProposerError(
                    f"search_experiments.filters has unknown keys: {sorted(unknown)}"
                )
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
            messages = list(session.tail_turns(_TAIL_TURNS))
            world_event = _build_world_event(
                memory_service, current_round, base_sha,
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
            # archive must record it. tail_turns() still excludes it on a later
            # resume (it is an orphan user, superseded by the notebook + the
            # next world event); archiving and re-injection are separate.
            session.append_message(
                "user", world_event, round_id=current_round,
            )
            print(
                f"[scientist] resume — scientist_id={session.scientist_id[:8]} "
                f"tail={len(messages) // 2} turn-blocks",
                flush=True,
            )

        steps_budget = max_steps or self.max_steps
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages: list = []
        state = WorkingState()
        budget_reminder_step = int(0.8 * steps_budget)
        reminded = False

        proposals: list[ResearchProposal] = []
        abstain_reason: str | None = None

        with TemporaryDirectory(prefix="simpleloop-scratch-") as scratch, \
                TemporaryDirectory(prefix="simpleloop-session-") as session_root:
            home = Path(session_root) / "home"
            home.mkdir(mode=0o700)
            tools = ResearchTools(
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
            # If the round STARTS already over threshold (a bloated resume tail
            # of large prior-round observations — the cross-round stacking case),
            # shed before the first model call so we never open a round already
            # over the window. No usage yet → char-based estimate drives it.
            self._maybe_compact(messages, [], state)
            for _step_num in range(steps_budget):
                step = _step_num + 1
                print(f"[scientist step {step}/{steps_budget}] thinking",
                      flush=True)
                if (not reminded and budget_reminder_step > 0
                        and step >= budget_reminder_step):
                    messages.append({"role": "user", "content": _BUDGET_NUDGE})
                    reminded = True

                action, reply_text = self._step(
                    state, messages, system_prompt, deadline, usages, step,
                    source_root=source_path, steps_budget=steps_budget,
                )
                name = action["action"]
                state.action_log.append({"action": name, "step": step})

                if name == "submit_proposals":
                    proposals = action["proposals"]
                    _bump(state, name)
                    # The terminal submit reply enters BOTH the live context
                    # and the archive, so the suspension checkpoint sees what
                    # the Scientist just decided (it must recall its own
                    # submitted directions when writing the continuation note).
                    messages.append({"role": "assistant", "content": reply_text})
                    session.append_message("assistant", reply_text,
                                           round_id=current_round)
                    print(
                        f"[scientist] submit {len(proposals)} proposal(s) "
                        f"step={step} elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    self._suspension_checkpoint(
                        system_prompt, messages, state, session, deadline,
                        usages, current_round,
                    )
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
                    f"[scientist step {step}/{steps_budget}] "
                    f"{name} ok={observation.get('ok')}",
                    flush=True,
                )
                # The live context grows by one (assistant, observation) pair
                # per step. On source-heavy tasks this fills the window long
                # before the step budget — shed oldest pairs when it does. The
                # archive was just written in full above, so compacting the live
                # list loses nothing from the Scientist's lived record.
                self._maybe_compact(messages, usages, state)

            # Budget exhausted without a submit.
            abstain_reason = (
                "research budget exhausted before the Scientist submitted "
                "directions"
            )
            print(f"[scientist] {abstain_reason}", flush=True)
            self._suspension_checkpoint(
                system_prompt, messages, state, session, deadline, usages,
                current_round,
            )
            return ScientistRound(
                proposals=[],
                abstained=True,
                abstain_reason=abstain_reason,
                usage=usages,
                deliberation_telemetry=_build_telemetry(
                    state, steps=steps_budget, outcome="abstain"),
                trace=_build_trace(
                    state, round_id=current_round, outcome="abstain"),
            )

    def _suspension_checkpoint(
        self, system_prompt: str, messages: list[dict], state: WorkingState,
        session: ScientistSession, deadline: float, usages: list,
        round_id: int,
    ) -> None:
        """Ask the Scientist to leave a continuation note for its resumed self,
        and persist it as the notebook (rewritten, not appended).

        Best-effort: a parse failure or zero remaining budget leaves the prior
        notebook untouched rather than failing the round.
        """
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
            note = None
        if isinstance(note, str) and note.strip():
            session.write_notebook(note.strip())
            session.append_message("user", _SUSPEND_PROMPT, round_id=round_id)
            session.append_message("assistant", reply.text, round_id=round_id)
            print("[scientist] notebook updated at suspension", flush=True)
        else:
            print("[scientist] suspension produced no notebook; left as-is",
                  flush=True)
