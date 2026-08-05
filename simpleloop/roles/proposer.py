"""Proposer Scientist: a generative-basis-driven idea lifecycle runtime.

The Scientist moves through one session in three phases — GENERATE, VALIDATE,
COMMIT — carrying a round-local epistemic state. The Generative Basis (G1–G9)
is internalized in the prompt as thinking operations that help produce ideas;
this module is the machinery that enforces the lifecycle:

- submit cannot bypass real evidence (evidence-basis guard: at least one
  decision-relevant fact must be examined this round before submitting);
- a failed direction cannot be retried verbatim (reject_candidate opens a new
  Generate episode: it compresses the failed direction into a taboo record,
  truncates the conversation history, and re-enters Generate with the failure
  as new input);
- a submit that lands in a taboo family is refused unless it cites new evidence
  examined this round that distinguishes it from the failed attempts;
- how the idea is generated remains the Scientist's own job — the Generative
  Basis never constrains what may be proposed.

Doc reference: docs/generator.md (Generative Basis).
Continuity across rounds is supplied by the persistent Experiment Ledger,
Finding Archive, and Frontier — NOT by this runtime's round-local state.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory

from .model import ChatModel
from .research_tools import (
    MEMORY_TOOL_ACTIONS,
    ResearchTools,
    render_research_tool_prompt,
)
from ..container.runtime import ApptainerRuntime
from ..memory.models import (
    ExistingFindingTarget,
    NewFindingTarget,
    ResearchProposal,
)
from ..explore.classify import jaccard_overlap
from ..explore.models import ExploreReport, SEVERITY_CHALLENGE
from ..explore.families import normalize_region, _bucket_path
from ..explore.render import (
    render_challenge_repair_message,
    render_explore_for_state_header,
)
from ..prompts import load_semantic


class ProposerError(RuntimeError):
    """The Proposer violated its action or budget contract."""


@dataclass(frozen=True)
class ProposerResult:
    """One round's structured output.

    ``proposals`` is a list of ``ResearchProposal``. When the Scientist judges
    that no experiment is worth its cost it abstains (``abstained`` True,
    empty proposals). ``deliberation_telemetry`` carries behavioral facts for
    the round record; ``trace`` is the non-authoritative full trajectory
    (never injected into a future round).
    """

    proposals: list[ResearchProposal]
    usage: object = None
    abstained: bool = False
    abstain_reason: str | None = None
    abstain_blocking_unknown: str | None = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BranchResult:
    """One hypothesis branch's research outcome.

    The branch researcher (cognitive element, C1-C4) runs the generate→validate
    →commit lifecycle on a single confirmed hypothesis. It produces 0 or 1
    proposals — 0 when the hypothesis failed validation (abandoned or rejected
    without a surviving candidate), 1 when a candidate held.
    """
    hypothesis: object  # HypothesisCard
    proposal: ResearchProposal | None = None
    abandoned: bool = False
    abandon_reason: str | None = None
    usage: object = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)
    # Novel directions discovered during research (for the future feedback loop;
    # not yet wired back to the generator in this iteration).
    novel_directions: list[str] = field(default_factory=list)


# --- Idea lifecycle state machine ------------------------------------------

class IdeaPhase(str, Enum):
    GENERATE = "generate"
    VALIDATE = "validate"
    COMMIT = "commit"


class ResearchProgress(str, Enum):
    ADVANCING = "advancing"
    STALLED = "stalled"
    CONTRADICTED = "contradicted"


# Research tools never change phase.
_RESEARCH_TOOL_ACTIONS = frozenset(
    {"run_research_command"} | MEMORY_TOOL_ACTIONS
)

# Actions legal in each phase.
_LEGAL_ACTIONS = {
    IdeaPhase.GENERATE: _RESEARCH_TOOL_ACTIONS | {"generate"},
    IdeaPhase.VALIDATE: _RESEARCH_TOOL_ACTIONS | {"assess_candidate"},
    IdeaPhase.COMMIT: {
        "reject_candidate", "submit_proposals", "abandon_round",
    },
}

# Where each control action lands.
_TRANSITION_TARGET = {
    ("generate", "generate"): IdeaPhase.VALIDATE,
    ("validate", "assess_candidate"): IdeaPhase.COMMIT,
    ("commit", "reject_candidate"): IdeaPhase.GENERATE,  # new episode
    ("commit", "submit_proposals"): None,                # terminal
    ("commit", "abandon_round"): None,                   # terminal, zero proposals
}

# Tunables.
_STALL_THRESHOLD = 4          # tool calls without assess -> nudge
_JACCARD_DUP_THRESHOLD = 0.8  # soft near-duplicate nudge
_DUP_WINDOW = 10              # how many recent ledger proposals to consider
_MAX_EVIDENCE_REFS = 5        # cap shown in the state header


@dataclass
class TabooRecord:
    """A failed direction, compressed. The taboo is on the mechanism family
    (region + mechanism bucket), not the instruction text, so rewording does
    not reset it. ``evidence_summary`` is the one fact that, had it been
    known, might have saved the direction — shown to Generate as guidance."""

    region: str
    mechanism: str
    reason: str
    evidence_summary: str

    @property
    def family_id(self) -> str:
        return f"{self.region}::{self.mechanism}"


@dataclass
class WorkingState:
    """Round-local epistemic state. Lives only in this runtime; never written
    to the Ledger or Finding archive. Drives control flow, guards, the compact
    state header, and the non-authoritative trace."""

    phase: IdeaPhase = IdeaPhase.GENERATE
    # Monotonic once True: the session has seen decision-relevant evidence
    # (from hints, history, or a tool call this round).
    evidence_basis: bool = False
    # Arc-scoped counters (reset on each new Generate episode).
    tool_calls_this_arc: int = 0
    research_steps_since_generate: int = 0
    last_tool_fingerprint: str | None = None
    # Current judgment, rendered back each step.
    generative_operations: tuple[str, ...] = ()
    candidate_directions: str = ""
    decision_relevant_unknown: str = ""
    current_judgment: str = ""
    current_information_goal: str = ""
    blocking_unknown: str = ""
    research_progress: ResearchProgress | None = None
    draft_proposal: str = ""
    weakest_premise: str = ""
    draft_resembles_history: bool = False
    # Evidence actually acquired this round (refs the Scientist may cite).
    session_evidence: set[str] = field(default_factory=set)
    # Evidence acquired THIS round via a tool call (not startup-pack history).
    # The taboo escape hatch requires new evidence, not a ref to something the
    # startup pack already showed.
    new_evidence: set[str] = field(default_factory=set)
    # Taboo set: failed directions this round. A submit landing in a taboo
    # family is refused unless it cites new evidence examined this round.
    taboo_set: list[TabooRecord] = field(default_factory=list)
    # Telemetry / trace accumulation.
    action_log: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    protocol_repairs: int = 0
    signals_fired: list[str] = field(default_factory=list)
    weakest_premise_log: str = ""
    episode_count: int = 0  # how many Generate episodes this round


def _legal_action_list(phase: IdeaPhase) -> str:
    return ", ".join(sorted(_LEGAL_ACTIONS[phase]))


def _validate_phase_transition(
    phase: IdeaPhase, action_name: str,
) -> IdeaPhase | None:
    """Return the next phase (or None for terminal), or raise ProposerError."""
    if action_name in _RESEARCH_TOOL_ACTIONS:
        return phase  # tools never change phase
    target = _TRANSITION_TARGET.get((phase.value, action_name))
    if target is None and (phase.value, action_name) not in _TRANSITION_TARGET:
        raise ProposerError(
            f"action {action_name!r} is not legal in phase {phase.value}; "
            f"legal actions: {_legal_action_list(phase)}"
        )
    return target


def _bump(state: WorkingState, name: str) -> None:
    state.counts[name] = state.counts.get(name, 0) + 1


# --- Prompt scaffolding ----------------------------------------------------

_PROTOCOL_ENVELOPE = (
    "Runtime contract (immutable):\n"
    "Return exactly one JSON object per response, with no prose outside it."
)

_RESEARCH_PHASE_NOTE = (
    "Research tools (legal in any phase, never change the phase):\n"
    + render_research_tool_prompt()
)

_GENERATE_ACTION_PROMPT = """Generate control action:
- {"action":"generate","candidate_directions":"...",
  "generative_operations":["G6","G2"],"decision_relevant_unknown":"...",
  "known_evidence":["..."],"next_information_goal":"...",
  "alternative_directions":["..."]}
  Leave Generate for Validate. candidate_directions is the direction(s) you
  want to take forward. generative_operations names the G1–G9 operations you
  used (for traceability only — they do not constrain what you may propose).
  decision_relevant_unknown is the single fact that would most change whether
  this direction is worth an experiment. known_evidence is what you directly
  read or observed. next_information_goal and alternative_directions are
  optional.
"""

_VALIDATE_ACTION_PROMPT = """Validate control action:
- {"action":"assess_candidate","current_judgment":"...",
  "supporting_evidence":["..."],"blocking_unknown":"...",
  "research_progress":"advancing|stalled|contradicted","draft_proposal":"...",
  "weakest_premise":"...","evidence_refs":["experiment:r3c0","source:src/foo.cc"]}
  Leave Validate for Commit. State your judgment of this candidate, what
  supports it, what still blocks you, and whether you are advancing, stalled,
  or contradicted. draft_proposal is optional. weakest_premise is the single
  premise whose failure would sink the candidate. evidence_refs are real things
  you examined this round (at least one experiment: or source:; a finding: alone
  is not enough) — cite them so the runtime can confirm you actually looked.
"""

_COMMIT_ACTION_PROMPT = """Commit control actions (phase transitions):
- {"action":"reject_candidate","what_failed":"...",
  "failed_mechanism":"...","failed_region":"...",
  "evidence_summary":"...","new_information_goal":"..."}
  The candidate failed validation. This OPENS A NEW GENERATE EPISODE: the
  failed direction is compressed into a taboo record (region + mechanism),
  the conversation history is truncated to the startup pack plus the taboo
  summary, and you re-enter Generate with the failure as new input. Use this
  to pivot to a genuinely different direction — do not retry the same family.
  failed_mechanism and failed_region should match the candidate's mechanism
  family so the taboo is enforceable.
- {"action":"submit_proposals","proposals":[
    {"instruction":"...",
     "research_target":{"mode":"existing","finding_id":"F-NNN"},
     "evidence_refs":["experiment:r3c0"],"material_difference":"..."},
    {"instruction":"...",
     "research_target":{"mode":"new","question":"...","mechanisms":["..."],
     "code_regions":["..."]}}
  ]}
  Submit when a candidate holds. 1..N proposals — the budget is a ceiling, not
  a quota. evidence_refs and material_difference are optional unless the
  proposal resembles a prior one. Each proposal declares an existing finding
  (mode=existing, F-NNN) or a new question (mode=new). There is NO annotations
  field.
- {"action":"abandon_round","reason":"...","blocking_unknown":"..."}
  End the round with zero proposals when no direction clears the bar.
  blocking_unknown is optional: the one fact that, had you known it, would
  have changed the decision.
"""

_RUNTIME_BOUNDARIES = """Runtime boundaries:
- /source is the accepted revision, /repo is its read-only Git repository,
  /history.jsonl and /rounds are persisted run evidence when present, and
  /scratch is temporary writable space.
- You cannot call the Executor or Harness, edit candidates, choose a parent,
  or declare evaluation and Gate facts. Only Harness records are authoritative.
""".strip()

_BUDGET_REMINDER = (
    "Research budget is nearly exhausted. Choose one of: submit the proposal "
    "the evidence justifies (only if you have examined real evidence this "
    "round); reject the candidate if it is not worth more time; or abandon the "
    "round if no experiment is worth its execution cost."
)


def _runtime_protocol() -> str:
    return "\n\n".join((
        _PROTOCOL_ENVELOPE,
        _RESEARCH_PHASE_NOTE,
        _GENERATE_ACTION_PROMPT,
        _VALIDATE_ACTION_PROMPT,
        _COMMIT_ACTION_PROMPT,
        _RUNTIME_BOUNDARIES,
    ))


_MAX_PROTOCOL_REPAIRS = 2

_GUARD_REASONS = {
    "needs_evidence": (
        "You have not yet examined any decision-relevant evidence this round. "
        "Investigate (call a research tool) before assessing toward a decision."
    ),
    "near_duplicate": (
        "A submitted proposal is a near-duplicate of a prior experiment. "
        "Reframe to a genuinely different direction via reject_candidate, or "
        "cite a prior experiment ref in evidence_refs and state a concrete "
        "material_difference."
    ),
    "taboo_family": (
        "This proposal lands in a mechanism family that already failed this "
        "round (recorded in the taboo set). To submit in a taboo family you "
        "must cite NEW evidence examined this round (in evidence_refs) that "
        "distinguishes this attempt from the failed ones. If you have no such "
        "evidence, reject_candidate and pivot to a different family."
    ),
    "repeated_tool": (
        "That tool call is identical to the previous one and would add no new "
        "information. Change the query, inspect a different episode/finding, "
        "or move on via assess_candidate."
    ),
    "challenge_response_required": (
        "Explore health shows active family/global stagnation "
        "(challenge_required). Either reject_candidate, abandon_round, or "
        "submit_proposals with a challenge_response object naming the stalled "
        "family, the null hypothesis, why this is not another same-family "
        "variant, why one more experiment is worth its cost, and real "
        "evidence_refs you examined this round."
    ),
    "stalled_family_excluded": (
        "A family with an active challenge-severity signal is stalled. "
        "Submitting another proposal in the same region is not permitted — "
        "reframe_research or abandon_direction. A challenge_response cannot "
        "buy back into an exhausted family."
    ),
}


# Text fields a challenge_response must carry (each non-empty) when the
# Explore report marks challenge_required. evidence_refs is validated
# separately via _validate_evidence_refs (needs empirical evidence).
_CHALLENGE_TEXT_FIELDS = (
    "triggered_policy",
    "stalled_family",
    "what_was_exhausted",
    "null_hypothesis",
    "why_this_is_not_same_family_variant",
    "why_worth_one_more_experiment",
)
_CHALLENGE_ALLOWED_KEYS = set(_CHALLENGE_TEXT_FIELDS) | {"evidence_refs"}


def _guard_repair_message(reason: str, *, explore: ExploreReport | None = None) -> str:
    if reason == "challenge_response_required" and explore is not None:
        return render_challenge_repair_message(explore)
    base = _GUARD_REASONS.get(reason, "")
    return (
        f"Protocol correction required ({reason}). {base} Return exactly one "
        "JSON action object matching the Runtime contract, with no prose or "
        "additional JSON."
    )


def _phase_repair_message(phase: IdeaPhase) -> str:
    return (
        f"Protocol correction required (phase_illegal). Current phase "
        f"is {phase.value}. Legal actions in this phase: "
        f"{_legal_action_list(phase)}. submit_proposals and abandon_round "
        "are only legal from Commit, which you reach via generate then "
        "assess_candidate. Return exactly one JSON action object matching "
        "the Runtime contract, with no prose or additional JSON."
    )


# --- Logging summaries (never leak raw content) ----------------------------

def _action_summary(action: dict, *, phase: IdeaPhase,
                    next_phase: IdeaPhase | None) -> str:
    name = action["action"]
    if name == "run_research_command":
        return (
            f"phase={phase.value} action={name} cwd={action['cwd']} "
            f"command_chars={len(action['command'])}"
        )
    if name == "inspect_episode":
        return f"phase={phase.value} action={name} ref_chars={len(action['ref'])}"
    if name in ("list_findings", "search_findings", "inspect_finding",
                "search_experiments"):
        extra = ""
        if "query" in action:
            extra = f" query_chars={len(action.get('query', ''))}"
        elif "finding_id" in action:
            extra = f" finding_id={action.get('finding_id', '')}"
        return f"phase={phase.value} action={name}{extra}"
    tgt = next_phase.value if next_phase is not None else "exit"
    if name == "submit_proposals":
        return f"phase={phase.value}->exit action={name} count={len(action['proposals'])}"
    if name == "abandon_round":
        return f"phase={phase.value}->exit action={name}"
    return f"phase={phase.value}->{tgt} action={name}"


def _result_summary(action: dict, observation: dict) -> str:
    parts = [f"result={'ok' if observation.get('ok') else 'error'}"]
    if action["action"] == "run_research_command":
        if "returncode" in observation:
            parts.append(f"exit_code={observation['returncode']}")
        output = observation.get("output")
        if isinstance(output, str):
            parts.append(f"output_chars={len(output)}")
        if observation.get("timed_out"):
            parts.append("timed_out=true")
    return " ".join(parts)


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


def _parse_challenge_response(value) -> dict:
    """Parse the optional submit_proposals challenge_response block.

    Required when Explore health marks ``challenge_required``. All text fields
    must be non-empty strings when present; ``evidence_refs`` is a possibly
    empty string list (the guard re-validates that it resolves to real
    evidence examined this round). Returns a dict carrying the validated fields
    plus the raw ``evidence_refs`` list.
    """
    if not isinstance(value, dict):
        raise ProposerError("challenge_response must be an object")
    if set(value) - _CHALLENGE_ALLOWED_KEYS:
        raise ProposerError(
            f"challenge_response has unexpected keys: {sorted(value)}"
        )
    out: dict[str, object] = {}
    for field_name in _CHALLENGE_TEXT_FIELDS:
        v = value.get(field_name, "")
        if not isinstance(v, str) or not v.strip():
            raise ProposerError(
                f"challenge_response.{field_name} must be a non-empty string"
            )
        out[field_name] = v.strip()
    out["evidence_refs"] = _require_string_list(
        value.get("evidence_refs", []),
        name="challenge_response.evidence_refs", allow_empty=True,
    )
    return out


def _parse_progress(value: str) -> ResearchProgress:
    try:
        return ResearchProgress(value)
    except ValueError:
        raise ProposerError(
            "research_progress must be one of "
            f"{[p.value for p in ResearchProgress]}"
        ) from None


def _parse_premise(value) -> str:
    """weakest_premise may be a string or a list of strings (joined by '; ')."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        if not value:
            raise ProposerError("weakest_premise list must be non-empty")
        parts = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ProposerError(
                    "weakest_premise list items must be non-empty strings"
                )
            parts.append(item.strip())
        return "; ".join(parts)
    raise ProposerError("weakest_premise must be a string or list of strings")


def _parse_action(text: str, candidates_per_round: int) -> dict:
    try:
        action = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProposerError("proposer response must be one JSON object") from exc
    if not isinstance(action, dict) or not isinstance(action.get("action"), str):
        raise ProposerError("proposer action must be a JSON object with action")
    name = action["action"]

    if name == "run_research_command":
        _require_keys(action, {"action", "command"}, {"cwd"})
        command = action["command"]
        cwd = action.get("cwd", "source")
        if not isinstance(command, str) or not command.strip():
            raise ProposerError("research command must be non-empty")
        if cwd not in {"source", "scratch"}:
            raise ProposerError("research cwd must be source or scratch")
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
    if name == "generate":
        _require_keys(
            action, {"action", "candidate_directions"},
            {"generative_operations", "decision_relevant_unknown",
             "known_evidence", "next_information_goal",
             "alternative_directions"},
        )
        cd = action["candidate_directions"]
        if not isinstance(cd, str) or not cd.strip():
            raise ProposerError(
                "generate.candidate_directions must be non-empty"
            )
        return {
            "action": name,
            "candidate_directions": cd.strip(),
            "generative_operations": tuple(_require_string_list(
                action.get("generative_operations", []),
                name="generative_operations", allow_empty=True,
            )),
            "decision_relevant_unknown": _opt_str(
                action.get("decision_relevant_unknown")),
            "known_evidence": _require_string_list(
                action.get("known_evidence", []),
                name="known_evidence", allow_empty=True,
            ),
            "next_information_goal": _opt_str(
                action.get("next_information_goal")),
            "alternative_directions": _require_string_list(
                action.get("alternative_directions", []),
                name="alternative_directions", allow_empty=True,
            ),
        }
    if name == "assess_candidate":
        _require_keys(
            action, {"action", "current_judgment", "research_progress"},
            {"supporting_evidence", "blocking_unknown", "draft_proposal",
             "weakest_premise", "evidence_refs"},
        )
        cj = action["current_judgment"]
        if not isinstance(cj, str) or not cj.strip():
            raise ProposerError(
                "assess_candidate.current_judgment must be non-empty"
            )
        weakest = action.get("weakest_premise", "")
        if weakest:
            weakest = _parse_premise(weakest)
        refs = _require_string_list(
            action.get("evidence_refs", []),
            name="evidence_refs", allow_empty=True,
        )
        return {
            "action": name,
            "current_judgment": cj.strip(),
            "supporting_evidence": _require_string_list(
                action.get("supporting_evidence", []),
                name="supporting_evidence", allow_empty=True,
            ),
            "blocking_unknown": _opt_str(action.get("blocking_unknown")),
            "research_progress": _parse_progress(action["research_progress"]),
            "draft_proposal": _opt_str(action.get("draft_proposal")),
            "weakest_premise": weakest,
            "evidence_refs": refs,
        }
    if name == "reject_candidate":
        _require_keys(
            action,
            {"action", "what_failed", "failed_mechanism", "failed_region"},
            {"evidence_summary", "new_information_goal"},
        )
        for label in ("what_failed", "failed_mechanism", "failed_region"):
            val = action[label]
            if not isinstance(val, str) or not val.strip():
                raise ProposerError(f"reject_candidate.{label} must be non-empty")
        return {
            "action": name,
            "what_failed": action["what_failed"].strip(),
            "failed_mechanism": action["failed_mechanism"].strip(),
            "failed_region": action["failed_region"].strip(),
            "evidence_summary": _opt_str(action.get("evidence_summary")),
            "new_information_goal": _opt_str(
                action.get("new_information_goal")),
        }
    if name == "abandon_round":
        _require_keys(action, {"action", "reason"}, {"blocking_unknown"})
        reason = action["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ProposerError("abandon_round reason must be non-empty")
        unknown = action.get("blocking_unknown")
        if unknown is not None and (
                not isinstance(unknown, str) or not unknown.strip()):
            raise ProposerError(
                "abandon_round blocking_unknown must be a non-empty "
                "string when present"
            )
        return {
            "action": name,
            "reason": reason.strip(),
            "blocking_unknown": (unknown or "").strip() or None,
        }
    if name == "submit_proposals":
        _require_keys(action, {"action", "proposals"}, {"challenge_response"})
        proposals = action["proposals"]
        if (not isinstance(proposals, list)
                or not 1 <= len(proposals) <= candidates_per_round):
            raise ProposerError(
                f"expected 1..{candidates_per_round} proposals; to submit "
                "none, use abandon_round from Commit"
            )
        parsed = [_parse_proposal(item) for item in proposals]
        challenge_response = None
        if "challenge_response" in action:
            challenge_response = _parse_challenge_response(
                action["challenge_response"]
            )
        return {
            "action": name,
            "proposals": parsed,
            "challenge_response": challenge_response,
        }
    raise ProposerError(f"unknown proposer action: {name}")


def _opt_str(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ProposerError("expected a string field")
    return value.strip()


def _protocol_reason(exc: ProposerError) -> str:
    if isinstance(exc.__cause__, (TypeError, json.JSONDecodeError)):
        return "invalid_json"
    return "invalid_action"


# --- Evidence tracking & guards -------------------------------------------

def _register_evidence(state: WorkingState, action: dict, observation: dict) -> None:
    """Record the references a successful tool call made available to cite.

    Tool-acquired evidence is added to BOTH ``session_evidence`` (citable) and
    ``new_evidence`` (acquired this round, not from the startup pack). The
    taboo escape hatch checks ``new_evidence`` — citing a startup-pack ref does
    not distinguish a new attempt from the failed one.
    """
    if not observation.get("ok"):
        return
    name = action["action"]
    if name == "run_research_command":
        state.session_evidence.add("__source_examined__")
        state.new_evidence.add("__source_examined__")
        return
    result = observation.get("result")
    if name == "inspect_episode":
        eid = (result or {}).get("experiment_id")
        if eid:
            ref = f"experiment:{eid}"
            state.session_evidence.add(ref)
            state.new_evidence.add(ref)
    elif name == "inspect_finding":
        fid = (result or {}).get("id")
        if fid:
            ref = f"finding:{fid}"
            state.session_evidence.add(ref)
            state.new_evidence.add(ref)
    elif name in ("search_findings", "list_findings"):
        for item in result or []:
            fid = item.get("id") if isinstance(item, dict) else None
            if fid:
                ref = f"finding:{fid}"
                state.session_evidence.add(ref)
                state.new_evidence.add(ref)
    elif name == "search_experiments":
        for exp in _iter_experiment_hits(result):
            eid = exp.get("experiment_id") if isinstance(exp, dict) else None
            if eid:
                ref = f"experiment:{eid}"
                state.session_evidence.add(ref)
                state.new_evidence.add(ref)


def _iter_experiment_hits(result) -> list:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        out: list = []
        for key in ("relevant", "contrasting", "diverse"):
            out.extend(result.get(key) or [])
        return out
    return []


def _source_path_exists(relpath: str, source_root: Path) -> bool:
    relpath = relpath.strip().lstrip("/")
    candidates = [relpath]
    if ":" in relpath:
        candidates.append(relpath.rsplit(":", 1)[0])
    for cand in candidates:
        try:
            if cand and (source_root / cand).exists():
                return True
        except OSError:
            continue
    return False


def _validate_evidence_refs(
    refs: list[str], state: WorkingState, source_root: Path,
) -> bool:
    """True when each ref resolves to real evidence examined this round, and at
    least one empirical/source ref (not findings only) is present."""
    if not refs:
        return False
    has_empirical = False
    for ref in refs:
        if ":" not in ref:
            return False
        kind, _, rest = ref.partition(":")
        if kind == "experiment":
            if ref not in state.session_evidence:
                return False
            has_empirical = True
        elif kind == "finding":
            if ref not in state.session_evidence:
                return False
        elif kind == "source":
            if "__source_examined__" not in state.session_evidence:
                return False
            if not _source_path_exists(rest, source_root):
                return False
            has_empirical = True
        else:
            return False
    return has_empirical


def _validate_new_evidence_refs(
    refs: list[str], state: WorkingState, source_root: Path,
) -> bool:
    """True when each ref resolves to evidence acquired via a tool call THIS
    round (not startup-pack history), and at least one empirical/source ref is
    present. Used by the taboo escape hatch — citing a startup-pack ref does
    not distinguish a new attempt from the failed one."""
    if not refs:
        return False
    has_empirical = False
    for ref in refs:
        if ":" not in ref:
            return False
        kind, _, rest = ref.partition(":")
        if kind == "experiment":
            if ref not in state.new_evidence:
                return False
            has_empirical = True
        elif kind == "finding":
            if ref not in state.new_evidence:
                return False
        elif kind == "source":
            if "__source_examined__" not in state.new_evidence:
                return False
            if not _source_path_exists(rest, source_root):
                return False
            has_empirical = True
        else:
            return False
    return has_empirical


def _proposal_fingerprint(prop: ResearchProposal, findings_by_id: dict) -> tuple:
    """Structured fingerprint for deterministic near-duplicate detection.
    New-finding proposals include their (normalized) question so that two
    genuinely different questions never collide just because both have empty
    mechanism/region tags."""
    target = prop.research_target
    if isinstance(target, ExistingFindingTarget):
        finding = findings_by_id.get(target.finding_id)
        mechs = tuple(sorted(finding.mechanisms)) if finding else ()
        regions = tuple(sorted(finding.code_regions)) if finding else ()
        return ("existing", target.finding_id, mechs, regions)
    mechs = tuple(sorted(target.mechanisms))
    regions = tuple(sorted(target.code_regions))
    return ("new", _normalize_instruction(target.question), mechs, regions)


def _find_deterministic_duplicate(
    proposals: list[ResearchProposal],
    history: list[dict],
    findings_by_id: dict,
) -> int | None:
    """Return the index of a proposal that deterministically matches a recent
    ledger proposal (identical normalized instruction, or identical structured
    fingerprint). None otherwise."""
    norm_history = [_normalize_instruction(h["instruction"]) for h in history]
    history_fps = {
        _proposal_fingerprint(h["proposal_obj"], findings_by_id)
        for h in history if h.get("proposal_obj")
    }
    for i, prop in enumerate(proposals):
        if _normalize_instruction(prop.instruction) in norm_history:
            return i
        if _proposal_fingerprint(prop, findings_by_id) in history_fps:
            return i
    return None


def _normalize_instruction(text: str) -> str:
    return " ".join(text.lower().split())


def _draft_resembles_history(draft: str, history: list[dict]) -> bool:
    """Soft lexical signal: does the current draft proposal closely resemble a
    recent ledger proposal? Used only to nudge in the state header, never to
    block (rewording defeats it; the deterministic fingerprint is the hard gate)."""
    if not draft.strip():
        return False
    return any(
        jaccard_overlap(draft, h["instruction"]) >= _JACCARD_DUP_THRESHOLD
        for h in history
    )


def _proposal_family(
    prop: ResearchProposal, findings_by_id: dict,
) -> tuple[str, str]:
    """Return (region_bucket, mechanism_bucket) for a proposal, matching the
    Explore family-key scheme so taboo enforcement is consistent."""
    target = prop.research_target
    if isinstance(target, ExistingFindingTarget):
        finding = findings_by_id.get(target.finding_id)
        mechs = tuple(sorted(finding.mechanisms)) if finding else ()
        regions = tuple(sorted(finding.code_regions)) if finding else ()
    else:
        mechs = tuple(sorted(target.mechanisms))
        regions = tuple(sorted(target.code_regions))
    region = regions[0] if regions else "unknown-region"
    mechanism = mechs[0] if mechs else "unknown-mechanism"
    return region, mechanism


def _proposal_in_taboo(
    prop: ResearchProposal, findings_by_id: dict,
    taboo_set: list[TabooRecord],
) -> TabooRecord | None:
    """Return the matching TabooRecord if the proposal's family is taboo, else
    None. Matches on region + mechanism bucket (the family, not the wording)."""
    region, mechanism = _proposal_family(prop, findings_by_id)
    for taboo in taboo_set:
        if taboo.region == region and taboo.mechanism == mechanism:
            return taboo
    return None


def _proposal_region_buckets(prop: ResearchProposal) -> set[str]:
    """Region buckets a proposal targets — from research_target code_regions
    and source paths in the instruction. Matched against stalled family
    regions to hard-exclude same-region submits."""
    buckets: set[str] = set()
    target = prop.research_target
    if isinstance(target, NewFindingTarget):
        for r in target.code_regions:
            b = normalize_region(r)
            if b:
                buckets.add(b)
    for m in re.finditer(r'[A-Za-z0-9_./-]+\.(?:cc|h|cpp|hpp)', prop.instruction):
        buckets.add(_bucket_path(m.group()))
    return buckets


def _stalled_family_regions(explore: ExploreReport) -> set[str]:
    """Region buckets of all families with an active challenge-severity signal."""
    out: set[str] = set()
    for fam in explore.families:
        if any(s.active and s.severity == SEVERITY_CHALLENGE
               for s in fam.policy_signals):
            out.add(fam.code_region)
    return out


def _global_stall_hot_regions(explore: ExploreReport) -> set[str]:
    """When global stall is active but no specific family is, find the regions
    that account for the most recent attempts — these are the local-exploitation
    traps the global stall is signaling. Submitting in them is hard-excluded."""
    out: set[str] = set()
    gh = explore.global_health
    if gh is None:
        return out
    # Collect (region, attempts) from families that have been touched recently.
    fams = sorted(explore.families, key=lambda f: -f.attempts)
    total = sum(f.attempts for f in fams)
    if total == 0:
        return out
    # Regions covering >= 30% of all attempts are "hot" — the proposer has
    # been spending most of its budget there.
    threshold = max(1, int(0.3 * total))
    for f in fams:
        if f.attempts >= threshold:
            out.add(f.code_region)
    return out


def _validate_action_guard(
    state: WorkingState, action: dict, history: list[dict],
    findings_by_id: dict, source_root: Path,
    explore: ExploreReport | None = None,
) -> str | None:
    """Return a repair reason, or None when the action satisfies the guards."""
    name = action["action"]
    if name in _RESEARCH_TOOL_ACTIONS:
        # Exact-repeat only: an identical tool call back-to-back adds nothing.
        fp = _fingerprint(action)
        if (state.last_tool_fingerprint is not None
                and fp == state.last_tool_fingerprint):
            return "repeated_tool"
        return None
    if name == "assess_candidate":
        if not state.evidence_basis:
            return "needs_evidence"
        return None
    if name == "submit_proposals":
        # Evidence-basis: must have examined something decision-relevant.
        if not state.evidence_basis:
            return "needs_evidence"
        # Near-duplicate: a proposal matching a recent ledger experiment must
        # cite a real experiment ref AND state a material_difference. This is
        # a hard gate — without both, the submit is refused.
        dup_idx = _find_deterministic_duplicate(
            action["proposals"], history, findings_by_id,
        )
        if dup_idx is not None:
            prop = action["proposals"][dup_idx]
            if not prop.evidence_refs or not (
                    prop.material_difference
                    and prop.material_difference.strip()):
                return "near_duplicate"
        # Taboo family: a proposal landing in a family that failed this round
        # must cite NEW evidence examined this round (via a tool call, not the
        # startup pack) that distinguishes it from the failed attempts.
        # material_difference alone is NOT enough.
        for prop in action["proposals"]:
            taboo = _proposal_in_taboo(prop, findings_by_id, state.taboo_set)
            if taboo is not None:
                if not _validate_new_evidence_refs(
                    list(prop.evidence_refs), state, source_root,
                ):
                    return "taboo_family"
        # Explore challenge gate REMOVED: in the branch-then-deepen
        # architecture, Explore's negative feedback steers the GENERATOR
        # (via the generation boundary), not the submit gate. A branch that
        # reaches submit has already passed evidence_basis + taboo checks;
        # the orchestrator's list-wise comparison handles cross-branch
        # selection. The challenge_required field is retained on
        # ExploreReport for telemetry only.
        return None
    return None


# --- State header ---------------------------------------------------------

def _render_state_header(
    state: WorkingState, explore: ExploreReport | None,
) -> str:
    """Compact, delta-only epistemic state, injected so the Scientist keeps its
    own judgment in view without re-reading the whole history."""
    lines = ["Working state (your current epistemic position):"]
    lines.append(
        f"  phase={state.phase.value}  "
        f"progress="
        f"{state.research_progress.value if state.research_progress else '-'}"
        f"  episode={state.episode_count}"
    )
    if state.candidate_directions:
        lines.append(
            f"  candidate_directions: "
            f"{_truncate(state.candidate_directions, 160)}")
    if state.generative_operations:
        lines.append(
            f"  generative_operations: {', '.join(state.generative_operations)}")
    if state.decision_relevant_unknown:
        lines.append(
            f"  decision_relevant_unknown: "
            f"{_truncate(state.decision_relevant_unknown, 160)}")
    if state.blocking_unknown:
        lines.append(
            f"  blocking_unknown: {_truncate(state.blocking_unknown, 120)}")
    if state.current_information_goal:
        lines.append(
            f"  current_information_goal: "
            f"{_truncate(state.current_information_goal, 120)}")
    if state.draft_proposal:
        lines.append(
            f"  draft_proposal: {_truncate(state.draft_proposal, 160)}")
    if state.draft_resembles_history:
        lines.append(
            "  possible_near_duplicate: this draft resembles a prior proposal "
            "— be sure it is materially different before submitting")
    if state.taboo_set:
        lines.append(
            f"  taboo_set ({len(state.taboo_set)} failed direction(s)):")
        for t in state.taboo_set:
            lines.append(
                f"    {t.family_id}  reason={_truncate(t.reason, 80)}  "
                f"evidence={_truncate(t.evidence_summary, 80)}")
        lines.append(
            "  A submit in a taboo family must cite NEW evidence examined this "
            "round that distinguishes it from the failed attempt(s).")
    explore_block = render_explore_for_state_header(explore)
    if explore_block:
        lines.append(explore_block)
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- Telemetry & trace ----------------------------------------------------

def _build_telemetry(
    state: WorkingState, *, steps: int, abandoned: bool,
    explore: ExploreReport | None = None,
    challenge_response_provided: bool = False,
) -> dict:
    return {
        "steps": steps,
        "tool_calls": state.counts.get("tool", 0),
        "generate_count": state.counts.get("generate", 0),
        "assess_count": state.counts.get("assess_candidate", 0),
        "reject_count": state.counts.get("reject_candidate", 0),
        "episode_count": state.episode_count,
        "taboo_count": len(state.taboo_set),
        "abandoned": abandoned,
        "protocol_repairs": state.protocol_repairs,
        "signals_fired": list(state.signals_fired),
        "explore_challenge_required": bool(
            explore and explore.challenge_required
        ),
        "challenge_response_provided": challenge_response_provided,
    }


def _build_trace(
    state: WorkingState, *, round_id: int,
    explore: ExploreReport | None = None,
) -> dict:
    if explore is not None:
        explore_block = {
            "challenge_required": explore.challenge_required,
            "challenge_reasons": list(explore.challenge_reasons),
            "active_families": [
                fam.family_id for fam in explore.families
                if any(s.active for s in fam.policy_signals)
            ],
        }
    else:
        explore_block = {
            "challenge_required": False,
            "challenge_reasons": [],
            "active_families": [],
        }
    return {
        "round": round_id,
        "candidate_directions": state.candidate_directions,
        "decision_relevant_unknown": state.decision_relevant_unknown,
        "blocking_unknown": state.blocking_unknown,
        "draft_proposal": state.draft_proposal,
        "weakest_premise": state.weakest_premise_log,
        "actions": list(state.action_log),
        "taboo_set": [
            {"family": t.family_id, "reason": t.reason}
            for t in state.taboo_set
        ],
        "explore": explore_block,
        "authority": "non_authoritative",
        "inject_into_future_context": False,
    }


# --- The Scientist runtime ------------------------------------------------

class ProposerAgent:
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
    ):
        self.model = model
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self.max_steps = max_steps
        self.command_timeout_seconds = command_timeout_seconds
        self.command_output_cap_chars = command_output_cap_chars
        self.usage_observer = usage_observer

    def run(
        self,
        *,
        goal: str,
        editable: list[str],
        frozen: list[str],
        memory_service,
        base_sha: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        candidates_per_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        hints: list[str] | None = None,
    ) -> ProposerResult:
        system_prompt = (
            f"{load_semantic('proposer', prompt_dir).rstrip()}\n\n"
            f"{_runtime_protocol()}"
        )
        experiments = memory_service.load_experiments()
        findings = memory_service.load_findings()
        # Explore report: compute once per wakeup — the single source of truth
        # for the startup pack, the per-step state header, the nudges, and the
        # challenge_response guard. Fail-soft to None (every consumer handles
        # None as "no health view").
        try:
            explore = memory_service.analyze_explore(current_round=current_round)
        except Exception:
            explore = None
        startup_pack = memory_service.build_startup_pack(
            goal=goal,
            editable=editable,
            frozen=frozen,
            base_sha=base_sha,
            gate_block=gate_block,
            candidates_per_round=candidates_per_round,
            hints=hints,
            current_round=current_round,
            explore=explore,
        )
        messages = [{
            "role": "user",
            "content": startup_pack,
        }]
        # Keep the startup pack boundary so reject_candidate can truncate
        # back to it when opening a new Generate episode.
        startup_len = 1
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages = []
        state = WorkingState()
        # Startup evidence: anything the pack already showed is real and citable.
        state.evidence_basis = bool(hints) or bool(experiments) or bool(findings)
        state.session_evidence = {
            f"experiment:{e.experiment_id}" for e in experiments
        } | {f"finding:{fid}" for fid in findings}
        # Recent ledger proposals for near-duplicate checks.
        recent = sorted(
            experiments, key=lambda e: (e.round, e.candidate)
        )[-_DUP_WINDOW:]
        history_props = [
            {"instruction": e.proposal,
             "proposal_obj": _experiment_to_proposal(e)}
            for e in recent if e.proposal
        ]
        budget_reminder_step = int(0.8 * self.max_steps)
        reminded = False
        print(f"[proposer] started max_steps={self.max_steps}", flush=True)
        with TemporaryDirectory(prefix="simpleloop-research-") as scratch:
            tools = ResearchTools(
                runtime=self.runtime,
                source=source_path,
                repo=repo_path,
                history_dir=run_dir,
                scratch=Path(scratch),
                memory_service=memory_service,
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
                current_round=current_round,
            )
            final = None
            for _step in range(self.max_steps):
                step = _step + 1
                print(
                    f"[proposer step {step}/{self.max_steps}] thinking",
                    flush=True,
                )
                if (not reminded and budget_reminder_step > 0
                        and step >= budget_reminder_step):
                    messages.append({
                        "role": "user", "content": _BUDGET_REMINDER,
                    })
                    reminded = True
                action, next_phase, reply_text = self._step(
                    state, messages, system_prompt, deadline, usages,
                    candidates_per_round, history_props, findings,
                    source_path, step, explore,
                )
                name = action["action"]

                # Terminal actions.
                if name == "submit_proposals":
                    _bump(state, name)
                    state.action_log.append({"action": name, "step": step})
                    print(
                        f"[proposer] finished steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    final = ProposerResult(
                        proposals=action["proposals"],
                        usage=usages,
                        deliberation_telemetry=_build_telemetry(
                            state, steps=step, abandoned=False,
                            explore=explore,
                            challenge_response_provided=(
                                action.get("challenge_response") is not None
                            ),
                        ),
                        trace=_build_trace(
                            state, round_id=current_round, explore=explore,
                        ),
                    )
                    break
                if name == "abandon_round":
                    _bump(state, name)
                    state.action_log.append({"action": name, "step": step})
                    print(
                        f"[proposer] abstained steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s "
                        f"reason_chars={len(action['reason'])}",
                        flush=True,
                    )
                    final = ProposerResult(
                        proposals=[],
                        usage=usages,
                        abstained=True,
                        abstain_reason=action["reason"],
                        abstain_blocking_unknown=action["blocking_unknown"],
                        deliberation_telemetry=_build_telemetry(
                            state, steps=step, abandoned=True, explore=explore,
                        ),
                        trace=_build_trace(
                            state, round_id=current_round, explore=explore,
                        ),
                    )
                    break

                # Control actions: internal state transitions, no tool call.
                if name == "generate":
                    self._apply_generate(state, action)
                    _bump(state, name)
                    state.phase = next_phase
                    messages.append(
                        {"role": "assistant", "content": reply_text})
                    self._note_state(messages, state, explore)
                    continue
                if name == "assess_candidate":
                    self._apply_assess(state, action, history_props)
                    _bump(state, name)
                    state.phase = next_phase
                    messages.append(
                        {"role": "assistant", "content": reply_text})
                    self._note_state(messages, state, explore)
                    continue
                if name == "reject_candidate":
                    _bump(state, name)
                    # Open a new Generate episode: compress the failed
                    # direction into a taboo record, truncate the conversation
                    # history to the startup pack, and inject the taboo summary
                    # as the sole prior context. This is the pivot mechanism —
                    # without truncation the model re-derives the same
                    # conclusion from the old reasoning chain (the r8→r9
                    # failure mode).
                    taboo = TabooRecord(
                        region=action["failed_region"],
                        mechanism=action["failed_mechanism"],
                        reason=action["what_failed"],
                        evidence_summary=action.get("evidence_summary") or "",
                    )
                    state.taboo_set.append(taboo)
                    self._reset_arc(state)
                    state.episode_count += 1
                    state.phase = next_phase
                    # Truncate: keep only the startup pack, then inject the
                    # compressed failure summary as the new context.
                    del messages[startup_len:]
                    summary = self._render_episode_summary(state)
                    messages.append({"role": "user", "content": summary})
                    self._note_state(messages, state, explore)
                    continue

                # Research tool action.
                observation = tools.execute(action, deadline=deadline)
                _bump(state, "tool")
                state.tool_calls_this_arc += 1
                state.research_steps_since_generate += 1
                state.evidence_basis = True
                _register_evidence(state, action, observation)
                state.last_tool_fingerprint = _fingerprint(action)
                print(
                    f"[proposer step {step}/{self.max_steps}] "
                    f"{_result_summary(action, observation)}",
                    flush=True,
                )
                envelope = {
                    "state": _render_state_header(state, explore),
                    "tool_result": observation,
                }
                # Soft stall / near-duplicate nudges attach to the tool result.
                nudge = self._maybe_nudge(state, explore)
                if nudge:
                    envelope["note"] = nudge
                    state.signals_fired.append(nudge[:48])
                messages.extend([
                    {"role": "assistant", "content": reply_text},
                    {"role": "user", "content": json.dumps(
                        envelope, ensure_ascii=False,
                    )},
                ])
            if final is None:
                raise ProposerError("proposer exceeded researcher.max_steps")
            return final

    def research_branch(
        self,
        *,
        hypothesis,  # HypothesisCard
        goal: str,
        editable: list[str],
        frozen: list[str],
        memory_service,
        base_sha: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        probe_evidence_ref: str | None = None,
        hints: list[str] | None = None,
        explore=None,
        max_steps: int | None = None,
    ) -> BranchResult:
        """Research one confirmed hypothesis in isolation (branch-then-deepen).

        This is the cognitive element (C1-C4) demoted from global proposer to
        per-branch researcher. It starts in VALIDATE (the hypothesis is the
        generate output — the direction is already chosen by the generator),
        so the "0 tool calls then declare direction" failure mode of the old
        single-track proposer cannot occur. The branch runs its own
        generate→validate→commit lifecycle with its own context, can reframe
        within the branch, and produces 0 or 1 proposals.
        """
        from .hypothesis import HypothesisCard  # avoid top-level cycle
        assert isinstance(hypothesis, HypothesisCard)

        system_prompt = (
            f"{load_semantic('proposer', prompt_dir).rstrip()}\n\n"
            f"{_runtime_protocol()}"
        )
        experiments = memory_service.load_experiments()
        findings = memory_service.load_findings()
        if explore is None:
            try:
                explore = memory_service.analyze_explore(
                    current_round=current_round)
            except Exception:
                explore = None
        startup_pack = memory_service.build_startup_pack(
            goal=goal, editable=editable, frozen=frozen,
            base_sha=base_sha, gate_block=gate_block,
            candidates_per_round=1, hints=hints,
            current_round=current_round, explore=explore,
        )
        # Inject the hypothesis as the branch's initial direction. The branch
        # starts in VALIDATE — the generator already did Generate.
        branch_intro = (
            "You are researching ONE hypothesis in isolation. The direction "
            "was chosen by the generator; your job is to validate it deeply, "
            "not to re-choose.\n\n"
            f"Hypothesis (from {hypothesis.generative_op}):\n"
            f"  region: {hypothesis.region}\n"
            f"  mechanism: {hypothesis.mechanism}\n"
            f"  intervention_family: {hypothesis.intervention_family}\n"
            f"  why_plausible: {hypothesis.why_plausible}\n"
            f"  critical_unknown: {hypothesis.critical_unknown}\n"
        )
        if probe_evidence_ref:
            branch_intro += (
                f"\nProbe confirmed the mechanism exists: {probe_evidence_ref}\n"
            )
        branch_intro += (
            "\nStart in VALIDATE. Investigate the critical_unknown, read the "
            "source, then assess_candidate. You may reject_candidate to reframe "
            "within this branch, or submit_proposals (at most 1) if the "
            "evidence supports it, or abandon if it does not."
        )
        messages = [
            {"role": "user", "content": startup_pack},
            {"role": "user", "content": branch_intro},
        ]
        startup_len = len(messages)
        steps_budget = max_steps or self.max_steps
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages = []
        state = WorkingState()
        # Start in VALIDATE — the hypothesis IS the generate output.
        state.phase = IdeaPhase.VALIDATE
        state.candidate_directions = (
            f"{hypothesis.mechanism} → {hypothesis.intervention_family} "
            f"in {hypothesis.region}")
        state.decision_relevant_unknown = hypothesis.critical_unknown
        state.evidence_basis = (
            bool(hints) or bool(experiments) or bool(findings)
            or bool(probe_evidence_ref))
        state.session_evidence = {
            f"experiment:{e.experiment_id}" for e in experiments
        } | {f"finding:{fid}" for fid in findings}
        if probe_evidence_ref and probe_evidence_ref.startswith("source:"):
            state.session_evidence.add("__source_examined__")
            state.new_evidence.add("__source_examined__")
        recent = sorted(
            experiments, key=lambda e: (e.round, e.candidate)
        )[-_DUP_WINDOW:]
        history_props = [
            {"instruction": e.proposal,
             "proposal_obj": _experiment_to_proposal(e)}
            for e in recent if e.proposal
        ]
        budget_reminder_step = int(0.8 * steps_budget)
        reminded = False
        print(
            f"[branch {hypothesis.generative_op} "
            f"{hypothesis.signature()}] started max_steps={steps_budget}",
            flush=True,
        )
        with TemporaryDirectory(prefix="simpleloop-branch-") as scratch:
            tools = ResearchTools(
                runtime=self.runtime,
                source=source_path,
                repo=repo_path,
                history_dir=run_dir,
                scratch=Path(scratch),
                memory_service=memory_service,
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
                current_round=current_round,
            )
            final: ProposerResult | None = None
            for _step in range(steps_budget):
                step = _step + 1
                print(
                    f"[branch step {step}/{steps_budget}] thinking",
                    flush=True,
                )
                if (not reminded and budget_reminder_step > 0
                        and step >= budget_reminder_step):
                    messages.append({
                        "role": "user", "content": _BUDGET_REMINDER,
                    })
                    reminded = True
                action, next_phase, reply_text = self._step(
                    state, messages, system_prompt, deadline, usages,
                    1, history_props, findings,
                    source_path, step, explore,
                )
                name = action["action"]

                if name == "submit_proposals":
                    _bump(state, name)
                    state.action_log.append({"action": name, "step": step})
                    print(
                        f"[branch] finished steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    final = ProposerResult(
                        proposals=action["proposals"],
                        usage=usages,
                        deliberation_telemetry=_build_telemetry(
                            state, steps=step, abandoned=False,
                            explore=explore,
                        ),
                        trace=_build_trace(
                            state, round_id=current_round, explore=explore,
                        ),
                    )
                    break
                if name == "abandon_round":
                    _bump(state, name)
                    state.action_log.append({"action": name, "step": step})
                    print(
                        f"[branch] abandoned steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s "
                        f"reason_chars={len(action['reason'])}",
                        flush=True,
                    )
                    final = ProposerResult(
                        proposals=[],
                        usage=usages,
                        abstained=True,
                        abstain_reason=action["reason"],
                        abstain_blocking_unknown=action["blocking_unknown"],
                        deliberation_telemetry=_build_telemetry(
                            state, steps=step, abandoned=True, explore=explore,
                        ),
                        trace=_build_trace(
                            state, round_id=current_round, explore=explore,
                        ),
                    )
                    break

                if name == "generate":
                    self._apply_generate(state, action)
                    _bump(state, name)
                    state.phase = next_phase
                    messages.append(
                        {"role": "assistant", "content": reply_text})
                    self._note_state(messages, state, explore)
                    continue
                if name == "assess_candidate":
                    self._apply_assess(state, action, history_props)
                    _bump(state, name)
                    state.phase = next_phase
                    messages.append(
                        {"role": "assistant", "content": reply_text})
                    self._note_state(messages, state, explore)
                    continue
                if name == "reject_candidate":
                    _bump(state, name)
                    taboo = TabooRecord(
                        region=action["failed_region"],
                        mechanism=action["failed_mechanism"],
                        reason=action["what_failed"],
                        evidence_summary=action.get("evidence_summary") or "",
                    )
                    state.taboo_set.append(taboo)
                    self._reset_arc(state)
                    state.episode_count += 1
                    state.phase = next_phase
                    del messages[startup_len:]
                    summary = self._render_episode_summary(state)
                    messages.append({"role": "user", "content": summary})
                    self._note_state(messages, state, explore)
                    continue

                observation = tools.execute(action, deadline=deadline)
                _bump(state, "tool")
                state.tool_calls_this_arc += 1
                state.research_steps_since_generate += 1
                state.evidence_basis = True
                _register_evidence(state, action, observation)
                state.last_tool_fingerprint = _fingerprint(action)
                print(
                    f"[branch step {step}/{steps_budget}] "
                    f"{_result_summary(action, observation)}",
                    flush=True,
                )
                envelope = {
                    "state": _render_state_header(state, explore),
                    "tool_result": observation,
                }
                nudge = self._maybe_nudge(state, explore)
                if nudge:
                    envelope["note"] = nudge
                    state.signals_fired.append(nudge[:48])
                messages.extend([
                    {"role": "assistant", "content": reply_text},
                    {"role": "user", "content": json.dumps(
                        envelope, ensure_ascii=False,
                    )},
                ])
            if final is None:
                # Budget exhausted without a terminal action — treat as abandon.
                print(
                    f"[branch] budget exhausted without conclusion "
                    f"(steps={steps_budget})", flush=True,
                )
                return BranchResult(
                    hypothesis=hypothesis, abandoned=True,
                    abandon_reason="branch budget exhausted",
                    usage=usages,
                    deliberation_telemetry=_build_telemetry(
                        state, steps=steps_budget, abandoned=True,
                        explore=explore,
                    ),
                    trace=_build_trace(
                        state, round_id=current_round, explore=explore,
                    ),
                )
            if final.abstained:
                return BranchResult(
                    hypothesis=hypothesis, abandoned=True,
                    abandon_reason=final.abstain_reason,
                    usage=usages,
                    deliberation_telemetry=final.deliberation_telemetry,
                    trace=final.trace,
                )
            return BranchResult(
                hypothesis=hypothesis,
                proposal=final.proposals[0] if final.proposals else None,
                usage=usages,
                deliberation_telemetry=final.deliberation_telemetry,
                trace=final.trace,
            )

    # -- helpers -----------------------------------------------------------

    def _step(self, state, messages, system_prompt, deadline, usages,
              candidates_per_round, history_props, findings, source_root,
              step_label, explore):
        """One model turn with up to _MAX_PROTOCOL_REPAIRS retries. Returns the
        parsed action, its target phase, and the raw reply text."""
        for repair in range(_MAX_PROTOCOL_REPAIRS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProposerError("proposer deadline exceeded")
            reply = self.model.complete(
                system=system_prompt,
                messages=messages,
                timeout_seconds=remaining,
            )
            usages.append(reply.usage)
            if (self.usage_observer is not None
                    and reply.usage is not None):
                self.usage_observer(reply.usage)
            try:
                action = _parse_action(
                    reply.text,
                    candidates_per_round=candidates_per_round,
                )
            except ProposerError as exc:
                if repair == _MAX_PROTOCOL_REPAIRS:
                    raise ProposerError(
                        "proposer action protocol failed after "
                        f"{_MAX_PROTOCOL_REPAIRS} repairs"
                    ) from None
                reason = _protocol_reason(exc)
                state.protocol_repairs += 1
                print(
                    f"[proposer step {step_label}/{self.max_steps}] "
                    f"protocol repair {repair + 1}/{_MAX_PROTOCOL_REPAIRS} "
                    f"reason={reason}",
                    flush=True,
                )
                messages.extend([
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": (
                        "Protocol correction required "
                        f"({reason}). Return exactly one JSON action object "
                        "matching the Runtime contract, with no prose or "
                        "additional JSON."
                    )},
                ])
                continue
            try:
                next_phase = _validate_phase_transition(
                    state.phase, action["action"],
                )
            except ProposerError:
                if repair == _MAX_PROTOCOL_REPAIRS:
                    raise ProposerError(
                        "proposer action protocol failed after "
                        f"{_MAX_PROTOCOL_REPAIRS} repairs"
                    ) from None
                state.protocol_repairs += 1
                print(
                    f"[proposer step {step_label}/{self.max_steps}] "
                    f"protocol repair {repair + 1}/{_MAX_PROTOCOL_REPAIRS} "
                    f"reason=phase_illegal",
                    flush=True,
                )
                messages.extend([
                    {"role": "assistant", "content": reply.text},
                    {"role": "user",
                     "content": _phase_repair_message(state.phase)},
                ])
                continue
            guard = _validate_action_guard(
                state, action, history_props, findings, source_root,
                explore=explore,
            )
            if guard is not None:
                if repair == _MAX_PROTOCOL_REPAIRS:
                    raise ProposerError(
                        "proposer action protocol failed after "
                        f"{_MAX_PROTOCOL_REPAIRS} repairs"
                    ) from None
                state.protocol_repairs += 1
                print(
                    f"[proposer step {step_label}/{self.max_steps}] "
                    f"protocol repair {repair + 1}/{_MAX_PROTOCOL_REPAIRS} "
                    f"reason={guard}",
                    flush=True,
                )
                messages.extend([
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": _guard_repair_message(
                        guard, explore=explore,
                    )},
                ])
                continue
            print(
                f"[proposer step {step_label}/{self.max_steps}] "
                f"{_action_summary(action, phase=state.phase, next_phase=next_phase)}",
                flush=True,
            )
            return action, next_phase, reply.text

    def _apply_generate(self, state: WorkingState, action: dict) -> None:
        self._reset_arc(state)
        state.candidate_directions = action["candidate_directions"]
        state.generative_operations = action.get("generative_operations") or ()
        state.decision_relevant_unknown = action.get(
            "decision_relevant_unknown") or ""
        state.current_information_goal = action.get(
            "next_information_goal") or ""

    def _apply_assess(self, state: WorkingState, action: dict,
                      history_props: list[dict]) -> None:
        state.current_judgment = action["current_judgment"]
        state.blocking_unknown = action.get("blocking_unknown") or ""
        state.research_progress = action["research_progress"]
        state.draft_proposal = action.get("draft_proposal") or ""
        state.draft_resembles_history = _draft_resembles_history(
            state.draft_proposal, history_props,
        )
        if state.draft_resembles_history:
            state.signals_fired.append("possible_near_duplicate")
        state.weakest_premise = action.get("weakest_premise") or ""
        state.weakest_premise_log = action.get("weakest_premise") or ""

    def _reset_arc(self, state: WorkingState) -> None:
        state.tool_calls_this_arc = 0
        state.research_steps_since_generate = 0
        state.draft_proposal = ""
        state.weakest_premise = ""
        state.current_judgment = ""
        state.blocking_unknown = ""
        state.research_progress = None

    def _render_episode_summary(self, state: WorkingState) -> str:
        """Compress the taboo set into a summary injected as the sole prior
        context after truncation. This is what makes reject_candidate a real
        pivot instead of a no-op."""
        lines = [
            "New Generate episode. Prior direction(s) failed and are now "
            "taboo — do NOT retry them or produce a reworded variant:",
        ]
        for t in state.taboo_set:
            lines.append(
                f"  TABOO  family={t.family_id}  reason={t.reason}  "
                f"evidence={t.evidence_summary or '(none recorded)'}"
            )
        lines.append("")
        lines.append(
            "Use the Generative Basis to produce a genuinely different "
            "direction. The taboo set is enforced: a submit landing in a taboo "
            "family is refused unless it cites NEW evidence examined this "
            "round that distinguishes it from the failed attempt(s)."
        )
        return "\n".join(lines)

    def _note_state(self, messages: list, state: WorkingState,
                    explore: ExploreReport | None) -> None:
        messages.append({
            "role": "user",
            "content": _render_state_header(state, explore),
        })

    def _maybe_nudge(
        self, state: WorkingState, explore: ExploreReport | None,
    ) -> str | None:
        notes = []
        if (state.phase == IdeaPhase.VALIDATE
                and state.research_steps_since_generate >= _STALL_THRESHOLD):
            notes.append(
                "investigation has run several steps without an assessment; "
                "assess_candidate or reject_candidate if the key premise is "
                "stuck")
        if explore is not None and not explore.first_round:
            for fh in explore.findings:
                mc = next(
                    (s for s in fh.policy_signals
                     if s.name == "mechanism_challenge" and s.active),
                    None,
                )
                if mc is not None:
                    notes.append(
                        f"finding {fh.finding_id} shows repeated "
                        "eligible-neutral attempts — consider challenging the "
                        "mechanism or rejecting the candidate")
                    break
            # Family-level stall: opening a fresh Finding each round does NOT
            # reset the family evidence (the family aggregates across findings).
            for fam in explore.families:
                stall = next(
                    (s for s in fam.policy_signals
                     if s.name in ("family_stall", "family_regressing")
                     and s.active),
                    None,
                )
                if stall is not None:
                    notes.append(
                        f"FAMILY {stall.name.upper()}: family "
                        f"{fam.code_region} has {fam.consecutive_no_improve} "
                        f"consecutive no-improve attempts across findings "
                        f"{', '.join(fam.finding_ids)}. Reject the candidate "
                        "and pivot to a different mechanism family, or submit "
                        "only with a challenge_response — do not submit "
                        "another variant."
                    )
                    break
            gh = explore.global_health
            if gh is not None:
                gs = next(
                    (s for s in gh.policy_signals
                     if s.name == "global_stall" and s.active),
                    None,
                )
                if gs is not None:
                    mechs = ", ".join(gh.recent_mechanisms[:5])
                    notes.append(
                        f"GLOBAL STALL: {gh.consecutive_no_improve_rounds} "
                        f"consecutive no-improve rounds. Mechanisms tried: "
                        f"{mechs}. Reject the candidate and pivot to a "
                        "different mechanism family or abandon — do not "
                        "submit another variant of the same approach."
                    )
        return "; ".join(notes) if notes else None


def _fingerprint(action: dict) -> str:
    """Canonical fingerprint of a tool action for exact-repeat detection."""
    name = action["action"]
    if name == "run_research_command":
        return f"{name}:{action['cwd']}:{action['command']}"
    if name == "inspect_episode":
        return f"{name}:{action['ref']}"
    if name == "inspect_finding":
        return f"{name}:{action['finding_id']}"
    if name in ("search_findings", "search_experiments"):
        return f"{name}:{action.get('query')}"
    if name == "list_findings":
        return f"{name}:{action.get('state')}:{action.get('limit')}"
    return name


def _experiment_to_proposal(exp):
    """Reconstruct a ResearchProposal from a ledger experiment for
    fingerprinting. The ledger stores instruction + finding_id only, so a
    new-finding entry has empty mechanism/region tags (its structured
    fingerprint then only matches via normalized instruction)."""
    target = (ExistingFindingTarget(finding_id=exp.finding_id)
              if exp.finding_id
              else NewFindingTarget(question=exp.proposal[:200]))
    return ResearchProposal(instruction=exp.proposal, research_target=target)
