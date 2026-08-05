"""Proposer Scientist: a metacognition-driven deliberation runtime.

The Scientist moves through one session in three frames of mind — FRAME,
RESEARCH, DECIDE — carrying a round-local epistemic state. The 4+2 cognitive
basis (C1 Research Target, C2 Evidence Discipline, C3 Evidence-Guided Inquiry,
C4 Research Decision; X1 Challenge/Reframe, X2 Proposal Verification) is
internalized in the prompt; this module is the machinery that enforces it:

- submit cannot bypass real evidence (evidence-basis guard, verification must
  be backed by references actually examined this round);
- fixated failure cannot bypass re-judgment (deterministic deliberation
  signals distinguish feasibility from mechanism stalls; near-duplicate
  proposals must declare a material difference);
- how the scientific explanation is formed remains the Scientist's own job.

Doc reference: docs/simpleloop_scientist_deliberation_policy_design.md.
Continuity across rounds is supplied by the persistent Experiment Ledger,
Finding Archive, and Frontier — NOT by this runtime's round-local state.
"""
from __future__ import annotations

import json
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
from ..explore.models import ExploreReport
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


# --- Cognitive state machine ----------------------------------------------

class ResearchPhase(str, Enum):
    FRAME = "frame"
    RESEARCH = "research"
    DECIDE = "decide"


class VerificationStatus(str, Enum):
    NOT_STARTED = "not_started"
    NEEDS_EVIDENCE = "needs_evidence"
    SUPPORTED = "supported"
    FAILED = "failed"


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
    ResearchPhase.FRAME: _RESEARCH_TOOL_ACTIONS | {"frame_research"},
    ResearchPhase.RESEARCH: _RESEARCH_TOOL_ACTIONS | {"assess_research"},
    ResearchPhase.DECIDE: {
        "continue_research", "reframe_research", "begin_verification",
        "submit_proposals", "abandon_direction",
    },
}

# Where each control action lands.
_TRANSITION_TARGET = {
    ("frame", "frame_research"): ResearchPhase.RESEARCH,
    ("research", "assess_research"): ResearchPhase.DECIDE,
    ("decide", "continue_research"): ResearchPhase.RESEARCH,
    ("decide", "reframe_research"): ResearchPhase.FRAME,
    ("decide", "begin_verification"): ResearchPhase.RESEARCH,
    ("decide", "submit_proposals"): None,   # terminal
    ("decide", "abandon_direction"): None,  # terminal, zero proposals
}

# Tunables.
_STALL_THRESHOLD = 4          # RESEARCH tool calls without assess -> nudge
_JACCARD_DUP_THRESHOLD = 0.8  # soft near-duplicate nudge
_DUP_WINDOW = 10              # how many recent ledger proposals to consider
_MAX_EVIDENCE_REFS = 5        # cap shown in the state header


@dataclass
class WorkingState:
    """Round-local epistemic state. Lives only in this runtime; never written
    to the Ledger or Finding archive. Drives control flow, guards, the compact
    state header, and the non-authoritative trace."""

    phase: ResearchPhase = ResearchPhase.FRAME
    verification_mode: bool = False
    verification_status: VerificationStatus = VerificationStatus.NOT_STARTED
    # Monotonic once True: the session has seen decision-relevant evidence
    # (from hints, history, or a tool call this round).
    evidence_basis: bool = False
    # Arc-scoped counters (reset on each new frame/reframe).
    tool_calls_this_arc: int = 0
    research_steps_since_frame: int = 0
    last_tool_fingerprint: str | None = None
    # Current judgment, rendered back each step.
    research_question: str = ""
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
    # Telemetry / trace accumulation.
    action_log: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    protocol_repairs: int = 0
    signals_fired: list[str] = field(default_factory=list)
    weakest_premise_log: str = ""


def _legal_action_list(phase: ResearchPhase) -> str:
    return ", ".join(sorted(_LEGAL_ACTIONS[phase]))


def _validate_phase_transition(
    phase: ResearchPhase, action_name: str,
) -> ResearchPhase | None:
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

_FRAME_ACTION_PROMPT = """Frame control action:
- {"action":"frame_research","research_question":"...",
  "decision_relevant_unknown":"...","known_evidence":["..."],
  "working_inference":"...","next_information_goal":"...",
  "alternative_explanations":["..."]}
  Leave Frame for Research. research_question is the judgment whose answer
  changes whether an experiment is worth spending; decision_relevant_unknown
  is the single fact that would change that judgment. known_evidence is what
  you directly read or observed (separate from inference). working_inference,
  next_information_goal, alternative_explanations are optional.
"""

_RESEARCH_ACTION_PROMPT = """Research control action:
- {"action":"assess_research","current_judgment":"...",
  "supporting_evidence":["..."],"blocking_unknown":"...",
  "research_progress":"advancing|stalled|contradicted","draft_proposal":"...",
  "verification":{"weakest_premise":"...","status":"supported|failed",
                  "evidence_refs":["experiment:r3c0","source:src/foo.cc"]}}
  Leave Research for Decide. State your judgment, what supports it, what still
  blocks you, and whether you are advancing, stalled, or contradicted.
  draft_proposal is optional. The optional "verification" block is how a
  proposal becomes supported: status "supported" requires evidence_refs that
  are real things you examined this round (at least one experiment: or source:;
  a finding: alone is not enough). status "failed" means the premise did not
  hold. Declaring status without real evidence_refs will be refused.
"""

_DECIDE_ACTION_PROMPT = """Decide control actions (phase transitions):
- {"action":"continue_research","next_information_goal":"...",
  "why_it_matters":"..."}
  Return to Research when one decision-relevant unknown is still resolvable.
- {"action":"reframe_research","what_failed":"...",
  "new_research_question":"...","new_decision_relevant_unknown":"..."}
  Return to Frame when the question itself was wrong or the direction keeps
  failing. Reframing resets verification.
- {"action":"begin_verification","draft_proposal":"...",
  "weakest_premise":"...","verification_question":"...",
  "next_information_goal":"..."}
  When you have a concrete change worth testing, enter Research in
  verification mode to test its weakest premise against source / Ledger /
  metrics. This does NOT mark the proposal supported — only a later
  assess_research with real evidence_refs does.
"""

_TERMINAL_ACTION_PROMPT = (
    "Terminal actions (only legal from Decide):\n"
    '- {"action":"submit_proposals","proposals":[\n'
    '    {"instruction":"...", '
    '"research_target":{"mode":"existing","finding_id":"F-NNN"},'
    '"evidence_refs":["experiment:r3c0"],"material_difference":"..."},\n'
    '    {"instruction":"...", '
    '"research_target":{"mode":"new","question":"...","mechanisms":["..."],'
    '"code_regions":["..."]}}\n'
    '  ],"challenge_response":{"triggered_policy":"...",'
    '"stalled_family":"...","what_was_exhausted":"...",'
    '"null_hypothesis":"...","why_this_is_not_same_family_variant":"...",'
    '"why_worth_one_more_experiment":"...","evidence_refs":['
    '"experiment:r3c0"]}}\n'
    "  Submit only after a proposal is verified (assess_research with a "
    "supported verification block backed by real evidence_refs). 1..N "
    "proposals — the budget is a ceiling, not a quota. evidence_refs and "
    "material_difference are optional unless the proposal resembles a prior "
    "one. Each proposal declares an existing finding (mode=existing, F-NNN) "
    "or a new question (mode=new). There is NO annotations field.\n"
    "  challenge_response is OPTIONAL — but REQUIRED when Explore health "
    "marks challenge_required (active family/global stagnation). It must "
    "name the stalled family, the null hypothesis, why this proposal is not "
    "another same-family variant, why one more experiment is worth its cost, "
    "and real evidence_refs you examined this round (at least one "
    "experiment:/source:). If you cannot honestly fill it, reframe_research "
    "or abandon_direction instead. reframe_research and abandon_direction "
    "never need a challenge_response.\n"
    '- {"action":"abandon_direction","reason":"...",'
    '"blocking_unknown":"..."}\n'
    "  End the round with zero proposals when no experiment is worth its "
    "cost. blocking_unknown is optional: the one fact that, had you known "
    "it, would have changed the decision."
)

_RUNTIME_BOUNDARIES = """Runtime boundaries:
- /source is the accepted revision, /repo is its read-only Git repository,
  /history.jsonl and /rounds are persisted run evidence when present, and
  /scratch is temporary writable space.
- You cannot call the Executor or Harness, edit candidates, choose a parent,
  or declare evaluation and Gate facts. Only Harness records are authoritative.
""".strip()

_BUDGET_REMINDER = (
    "Research budget is nearly exhausted. Choose one of: verify and submit "
    "the proposal the evidence justifies (only if its weakest premise is "
    "backed by real evidence you examined); continue only if one concrete "
    "unanswered question can still be resolved within budget; or abandon the "
    "direction if no experiment is worth its execution cost."
)


def _runtime_protocol() -> str:
    return "\n\n".join((
        _PROTOCOL_ENVELOPE,
        _RESEARCH_PHASE_NOTE,
        _FRAME_ACTION_PROMPT,
        _RESEARCH_ACTION_PROMPT,
        _DECIDE_ACTION_PROMPT,
        _TERMINAL_ACTION_PROMPT,
        _RUNTIME_BOUNDARIES,
    ))


_MAX_PROTOCOL_REPAIRS = 2

_GUARD_REASONS = {
    "needs_evidence": (
        "You have not yet examined any decision-relevant evidence this round. "
        "Investigate (call a research tool) before assessing toward a decision."
    ),
    "needs_verification": (
        "submit_proposals requires a verified proposal. Run assess_research "
        "with a verification block (status supported, real evidence_refs you "
        "examined this round) first; or begin_verification to test the "
        "weakest premise."
    ),
    "near_duplicate": (
        "A submitted proposal is a near-duplicate of a prior experiment. "
        "Either cite a prior experiment ref in evidence_refs and state a "
        "concrete material_difference, or reframe to a genuinely different "
        "question."
    ),
    "unverified_evidence": (
        "The verification block claims 'supported' but its evidence_refs are "
        "not real things examined this round (each must be an experiment: or "
        "source: you actually looked at, plus optionally finding:; at least "
        "one experiment:/source: is required). Gather the evidence first."
    ),
    "repeated_tool": (
        "That tool call is identical to the previous one and would add no new "
        "information. Change the query, inspect a different episode/finding, "
        "or move on via assess_research."
    ),
    "challenge_response_required": (
        "Explore health shows active family/global stagnation "
        "(challenge_required). Either reframe_research, abandon_direction, or "
        "submit_proposals with a challenge_response object naming the stalled "
        "family, the null hypothesis, why this is not another same-family "
        "variant, why one more experiment is worth its cost, and real "
        "evidence_refs you examined this round."
    ),
}


# Text fields a challenge_response must carry (each non-empty) when the
# Explore report marks challenge_required. evidence_refs is validated
# separately via _validate_verification_refs (needs empirical evidence).
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


def _phase_repair_message(phase: ResearchPhase) -> str:
    return (
        f"Protocol correction required (phase_illegal). Current research "
        f"phase is {phase.value}. Legal actions in this phase: "
        f"{_legal_action_list(phase)}. submit_proposals and abandon_direction "
        "are only legal from Decide, which you reach via frame_research then "
        "assess_research. Return exactly one JSON action object matching the "
        "Runtime contract, with no prose or additional JSON."
    )


# --- Logging summaries (never leak raw content) ----------------------------

def _action_summary(action: dict, *, phase: ResearchPhase,
                    next_phase: ResearchPhase | None) -> str:
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
    if name == "abandon_direction":
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
    for field in _CHALLENGE_TEXT_FIELDS:
        v = value.get(field, "")
        if not isinstance(v, str) or not v.strip():
            raise ProposerError(
                f"challenge_response.{field} must be a non-empty string"
            )
        out[field] = v.strip()
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


def _parse_verification(value) -> dict:
    if not isinstance(value, dict):
        raise ProposerError("verification must be an object")
    allowed = {"weakest_premise", "status", "evidence_refs"}
    if set(value) - allowed:
        raise ProposerError(f"verification has unexpected keys: {sorted(value)}")
    status = value.get("status")
    if status not in ("supported", "failed"):
        raise ProposerError("verification.status must be 'supported' or 'failed'")
    premise = value.get("weakest_premise", "")
    if not isinstance(premise, str):
        raise ProposerError("verification.weakest_premise must be a string")
    refs = _require_string_list(
        value.get("evidence_refs", []),
        name="verification.evidence_refs", allow_empty=(status == "failed"),
    )
    return {
        "weakest_premise": premise.strip(),
        "status": status,
        "evidence_refs": refs,
    }


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
    if name == "frame_research":
        _require_keys(
            action, {"action", "research_question", "decision_relevant_unknown"},
            {"known_evidence", "working_inference", "next_information_goal",
             "alternative_explanations"},
        )
        rq = action["research_question"]
        dru = action["decision_relevant_unknown"]
        for label, val in (("research_question", rq),
                           ("decision_relevant_unknown", dru)):
            if not isinstance(val, str) or not val.strip():
                raise ProposerError(f"frame_research.{label} must be non-empty")
        return {
            "action": name,
            "research_question": rq.strip(),
            "decision_relevant_unknown": dru.strip(),
            "known_evidence": _require_string_list(
                action.get("known_evidence", []),
                name="known_evidence", allow_empty=True,
            ),
            "working_inference": _opt_str(action.get("working_inference")),
            "next_information_goal": _opt_str(action.get("next_information_goal")),
            "alternative_explanations": _require_string_list(
                action.get("alternative_explanations", []),
                name="alternative_explanations", allow_empty=True,
            ),
        }
    if name == "assess_research":
        _require_keys(
            action, {"action", "current_judgment", "research_progress"},
            {"supporting_evidence", "blocking_unknown", "draft_proposal",
             "verification"},
        )
        cj = action["current_judgment"]
        if not isinstance(cj, str) or not cj.strip():
            raise ProposerError("assess_research.current_judgment must be non-empty")
        verification = None
        if "verification" in action:
            verification = _parse_verification(action["verification"])
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
            "verification": verification,
        }
    if name == "continue_research":
        _require_keys(action, {"action", "next_information_goal", "why_it_matters"})
        goal = action["next_information_goal"]
        why = action["why_it_matters"]
        for label, val in (("next_information_goal", goal),
                           ("why_it_matters", why)):
            if not isinstance(val, str) or not val.strip():
                raise ProposerError(f"continue_research.{label} must be non-empty")
        return {
            "action": name,
            "next_information_goal": goal.strip(),
            "why_it_matters": why.strip(),
        }
    if name == "reframe_research":
        _require_keys(
            action,
            {"action", "what_failed", "new_research_question",
             "new_decision_relevant_unknown"},
        )
        wf = action["what_failed"]
        nrq = action["new_research_question"]
        ndru = action["new_decision_relevant_unknown"]
        for label, val in (("what_failed", wf), ("new_research_question", nrq),
                           ("new_decision_relevant_unknown", ndru)):
            if not isinstance(val, str) or not val.strip():
                raise ProposerError(f"reframe_research.{label} must be non-empty")
        return {
            "action": name,
            "what_failed": wf.strip(),
            "new_research_question": nrq.strip(),
            "new_decision_relevant_unknown": ndru.strip(),
        }
    if name == "begin_verification":
        _require_keys(
            action,
            {"action", "draft_proposal", "weakest_premise", "verification_question"},
            {"next_information_goal"},
        )
        dp = action["draft_proposal"]
        wp = action["weakest_premise"]
        vq = action["verification_question"]
        for label, val in (("draft_proposal", dp), ("weakest_premise", wp),
                           ("verification_question", vq)):
            if not isinstance(val, str) or not val.strip():
                raise ProposerError(f"begin_verification.{label} must be non-empty")
        return {
            "action": name,
            "draft_proposal": dp.strip(),
            "weakest_premise": wp.strip(),
            "verification_question": vq.strip(),
            "next_information_goal": _opt_str(action.get("next_information_goal")),
        }
    if name == "abandon_direction":
        _require_keys(action, {"action", "reason"}, {"blocking_unknown"})
        reason = action["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise ProposerError("abandon_direction reason must be non-empty")
        unknown = action.get("blocking_unknown")
        if unknown is not None and (
                not isinstance(unknown, str) or not unknown.strip()):
            raise ProposerError(
                "abandon_direction blocking_unknown must be a non-empty "
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
                "none, use abandon_direction from Decide"
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
    """Record the references a successful tool call made available to cite."""
    if not observation.get("ok"):
        return
    name = action["action"]
    if name == "run_research_command":
        state.session_evidence.add("__source_examined__")
        return
    result = observation.get("result")
    if name == "inspect_episode":
        eid = (result or {}).get("experiment_id")
        if eid:
            state.session_evidence.add(f"experiment:{eid}")
    elif name == "inspect_finding":
        fid = (result or {}).get("id")
        if fid:
            state.session_evidence.add(f"finding:{fid}")
    elif name in ("search_findings", "list_findings"):
        for item in result or []:
            fid = item.get("id") if isinstance(item, dict) else None
            if fid:
                state.session_evidence.add(f"finding:{fid}")
    elif name == "search_experiments":
        for exp in _iter_experiment_hits(result):
            eid = exp.get("experiment_id") if isinstance(exp, dict) else None
            if eid:
                state.session_evidence.add(f"experiment:{eid}")


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


def _validate_verification_refs(
    refs: list[str], state: WorkingState, source_root: Path,
) -> bool:
    """A supported verification must rest on real evidence examined this round:
    each ref resolvable, and at least one empirical/source ref (not findings only)."""
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
        if state.last_tool_fingerprint is not None and fp == state.last_tool_fingerprint:
            return "repeated_tool"
        return None
    if name == "assess_research":
        ver = action.get("verification")
        if ver and ver["status"] == "supported":
            if not _validate_verification_refs(
                ver["evidence_refs"], state, source_root,
            ):
                return "unverified_evidence"
        if not state.evidence_basis:
            return "needs_evidence"
        return None
    if name == "submit_proposals":
        if state.verification_status != VerificationStatus.SUPPORTED:
            return "needs_verification"
        dup_idx = _find_deterministic_duplicate(
            action["proposals"], history, findings_by_id,
        )
        if dup_idx is not None:
            prop = action["proposals"][dup_idx]
            if not prop.evidence_refs or not (
                    prop.material_difference and prop.material_difference.strip()):
                return "near_duplicate"
        # Explore challenge: a stalled family/global state must be answered with
        # a challenge_response whose evidence_refs resolve to real evidence this
        # round. This never forbids submit — it forces the Scientist to state,
        # on the record, why the next experiment is not another exhausted
        # variant. reframe_research and abandon_direction remain free of it.
        if explore is not None and explore.challenge_required:
            cr = action.get("challenge_response")
            if cr is None:
                return "challenge_response_required"
            if not _validate_verification_refs(
                cr["evidence_refs"], state, source_root,
            ):
                return "challenge_response_required"
        return None
    return None


# --- State header ---------------------------------------------------------

def _render_state_header(
    state: WorkingState, explore: ExploreReport | None,
) -> str:
    """Compact, delta-only epistemic state, injected so the Scientist keeps its
    own judgment in view without re-reading the whole history."""
    lines = ["Working state (your current epistemic position):"]
    lines.append(f"  phase={state.phase.value}  "
                 f"verification={state.verification_status.value}"
                 f"{' (mode)' if state.verification_mode else ''}"
                 f"  progress="
                 f"{state.research_progress.value if state.research_progress else '-'}")
    if state.research_question:
        lines.append(f"  research_question: {_truncate(state.research_question, 160)}")
    if state.decision_relevant_unknown:
        lines.append(
            f"  decision_relevant_unknown: "
            f"{_truncate(state.decision_relevant_unknown, 160)}")
    if state.blocking_unknown:
        lines.append(f"  blocking_unknown: {_truncate(state.blocking_unknown, 120)}")
    if state.current_information_goal:
        lines.append(
            f"  current_information_goal: "
            f"{_truncate(state.current_information_goal, 120)}")
    if state.draft_proposal:
        lines.append(f"  draft_proposal: {_truncate(state.draft_proposal, 160)}")
    if state.draft_resembles_history:
        lines.append(
            "  possible_near_duplicate: this draft resembles a prior proposal "
            "— be sure it is materially different before verifying")
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
        "frame_count": state.counts.get("frame_research", 0),
        "continue_count": state.counts.get("continue_research", 0),
        "reframe_count": state.counts.get("reframe_research", 0),
        "verification_attempts": state.counts.get("begin_verification", 0),
        "verification_status": state.verification_status.value,
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
        "research_question": state.research_question,
        "decision_relevant_unknown": state.decision_relevant_unknown,
        "blocking_unknown": state.blocking_unknown,
        "draft_proposal": state.draft_proposal,
        "weakest_premise": state.weakest_premise_log,
        "verification_status": state.verification_status.value,
        "actions": list(state.action_log),
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
        messages = [{
            "role": "user",
            "content": memory_service.build_startup_pack(
                goal=goal,
                editable=editable,
                frozen=frozen,
                base_sha=base_sha,
                gate_block=gate_block,
                candidates_per_round=candidates_per_round,
                hints=hints,
                current_round=current_round,
                explore=explore,
            ),
        }]
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
        recent = sorted(experiments, key=lambda e: (e.round, e.candidate))[-_DUP_WINDOW:]
        history_props = [
            {"instruction": e.proposal, "proposal_obj": _experiment_to_proposal(e)}
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
                    candidates_per_round, history_props, findings, source_path,
                    step, explore,
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
                if name == "abandon_direction":
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
                if name == "frame_research":
                    self._apply_frame(state, action)
                    _bump(state, name)
                    state.phase = next_phase
                    messages.append({"role": "assistant", "content": reply_text})
                    self._note_state(messages, state, explore)
                    continue
                if name == "assess_research":
                    self._apply_assess(state, action, history_props)
                    _bump(state, name)
                    state.phase = next_phase
                    messages.append({"role": "assistant", "content": reply_text})
                    self._note_state(messages, state, explore)
                    continue
                if name == "continue_research":
                    state.current_information_goal = action["next_information_goal"]
                    _bump(state, name)
                    state.phase = next_phase
                    state.research_steps_since_frame = 0
                    messages.append({"role": "assistant", "content": reply_text})
                    self._note_state(messages, state, explore)
                    continue
                if name == "reframe_research":
                    _bump(state, name)
                    self._reset_arc(state, research_question=action["new_research_question"],
                                    decision_relevant_unknown=action["new_decision_relevant_unknown"])
                    state.phase = next_phase
                    messages.append({"role": "assistant", "content": reply_text})
                    self._note_state(messages, state, explore)
                    continue
                if name == "begin_verification":
                    _bump(state, name)
                    state.verification_mode = True
                    state.verification_status = VerificationStatus.NEEDS_EVIDENCE
                    state.draft_proposal = action["draft_proposal"]
                    state.weakest_premise = action["weakest_premise"]
                    state.weakest_premise_log = action["weakest_premise"]
                    state.current_information_goal = action.get(
                        "next_information_goal") or action["verification_question"]
                    state.phase = next_phase
                    state.research_steps_since_frame = 0
                    messages.append({"role": "assistant", "content": reply_text})
                    self._note_state(messages, state, explore)
                    continue

                # Research tool action.
                observation = tools.execute(action, deadline=deadline)
                _bump(state, "tool")
                state.tool_calls_this_arc += 1
                state.research_steps_since_frame += 1
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
                    {"role": "user", "content": _phase_repair_message(state.phase)},
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

    def _apply_frame(self, state: WorkingState, action: dict) -> None:
        self._reset_arc(
            state,
            research_question=action["research_question"],
            decision_relevant_unknown=action["decision_relevant_unknown"],
        )
        state.current_information_goal = action.get("next_information_goal") or ""

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
        ver = action.get("verification")
        if ver:
            state.weakest_premise = ver["weakest_premise"]
            state.weakest_premise_log = ver["weakest_premise"]
            if ver["status"] == "supported":
                state.verification_status = VerificationStatus.SUPPORTED
                state.signals_fired.append("verification_supported")
            else:
                state.verification_status = VerificationStatus.FAILED

    def _reset_arc(self, state: WorkingState, *, research_question: str,
                   decision_relevant_unknown: str) -> None:
        state.tool_calls_this_arc = 0
        state.research_steps_since_frame = 0
        state.verification_mode = False
        state.verification_status = VerificationStatus.NOT_STARTED
        state.draft_proposal = ""
        state.weakest_premise = ""
        state.research_question = research_question
        state.decision_relevant_unknown = decision_relevant_unknown
        state.current_judgment = ""
        state.blocking_unknown = ""
        state.research_progress = None

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
        if (state.phase == ResearchPhase.RESEARCH
                and state.research_steps_since_frame >= _STALL_THRESHOLD):
            notes.append(
                "investigation has run several steps without an assessment; "
                "assess_research or reframe if the key unknown is stuck")
        if explore is not None and not explore.first_round:
            for fh in explore.findings:
                mc = next(
                    (s for s in fh.policy_signals
                     if s.name == "mechanism_challenge" and s.active),
                    None,
                )
                if mc is not None:
                    notes.append(
                        f"finding {fh.finding_id} shows repeated eligible-neutral "
                        "attempts — consider challenging the mechanism or reframing")
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
                        f"{', '.join(fam.finding_ids)}. Reframe to a different "
                        "mechanism family, abandon, or submit only with a "
                        "challenge_response — do not submit another variant."
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
                        f"{mechs}. Reframe to a different mechanism family or "
                        "abandon — do not submit another variant of the same "
                        "approach."
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
