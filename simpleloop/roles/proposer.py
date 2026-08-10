"""Scientist-Proposer inquiry runtime.

One lane develops an independent whole-problem account, an explicit working
model, competing explanations, a broad hypothesis portfolio, and only then
uses experiment history to narrow and deepen selected directions. Proposals
must preserve model, explanation, hypothesis, and direct-evidence lineage.
"""
from __future__ import annotations

import json
import time
from threading import local
from .inquiry import (
    Explanation,
    HypothesisSelection,
    InquiryPhase,
    LeveragePoint,
    ModelClaim,
    ResearchHypothesis,
    ScientistSessionState,
    WorkingModel,
    Understanding,
)
from dataclasses import asdict, dataclass, field
from .generative import render_generative_basis
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
    _fingerprint,
    _observation_evidence_refs,
    _register_evidence,
    _action_summary,
    _result_summary,
)
from ..container.runtime import ApptainerRuntime
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
    """One round's structured output.

    ``proposals`` is a list of ``ResearchProposal``. When no branch produced a
    proposal the orchestrator abstains (``abstained`` True, empty proposals).
    ``deliberation_telemetry`` carries behavioral facts for the round record;
    ``trace`` is the non-authoritative trajectory (never injected forward).
    """

    proposals: list[ResearchProposal]
    usage: object = None
    abstained: bool = False
    abstain_reason: str | None = None
    abstain_blocking_unknown: str | None = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)

@dataclass(frozen=True)
class ScientistResult:
    """Outcome of one Scientist lane under one total research budget."""

    proposals: tuple[ResearchProposal, ...] = ()
    outcome: str = "research_incomplete"
    reason: str | None = None
    usage: tuple[object, ...] = ()
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)

# --- Scientist inquiry tunables -----------------------------------------

_BLOCK_REASON_KINDS = frozenset({"false_claim", "frozen", "contradiction"})

# Research / memory tools never terminate the loop.
_RESEARCH_TOOL_ACTIONS = frozenset(
    {"run_research_command"} | MEMORY_TOOL_ACTIONS
)

_RUNTIME_BOUNDARIES = """Runtime boundaries:
- /source is the accepted revision; /repo Git metadata is mounted only after history is visible,
  /history.jsonl and /rounds are persisted evidence only when history is visible,
  and /scratch is temporary writable space.
- You cannot call the Executor or Harness, edit candidates, choose a parent,
  or declare evaluation and Gate facts. Only Harness records are authoritative.
""".strip()

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
    required = {
        "instruction", "research_target", "model_claim_refs",
        "explanation_refs", "hypothesis_id", "evidence_refs", "mechanism",
        "prediction", "affected_scope",
    }
    if set(value) != required:
        raise ProposerError(f"proposal keys must be {sorted(required)}")
    return ResearchProposal(
        instruction=_required_str(value, "instruction"),
        research_target=_parse_research_target(value["research_target"]),
        model_claim_refs=tuple(_require_string_list(
            value["model_claim_refs"], name="proposal.model_claim_refs")),
        explanation_refs=tuple(_require_string_list(
            value["explanation_refs"], name="proposal.explanation_refs")),
        hypothesis_id=_required_str(value, "hypothesis_id"),
        evidence_refs=tuple(_require_string_list(
            value["evidence_refs"], name="proposal.evidence_refs")),
        mechanism=_required_str(value, "mechanism"),
        prediction=_required_str(value, "prediction"),
        affected_scope=_required_str(value, "affected_scope"),
    )

# --- Scientist phase protocol --------------------------------------------
def _parse_research_action(raw: dict) -> dict:
    name = raw["action"]
    if name == "run_research_command":
        _require_keys(raw, {"action", "command", "evidence_paths"}, {"cwd"})
        command = _required_str(raw, "command")
        cwd = raw.get("cwd", "source")
        if cwd not in {"source", "scratch"}:
            raise ProposerError("research cwd must be source or scratch")
        evidence_paths = tuple(_require_string_list(
            raw["evidence_paths"], name="evidence_paths", allow_empty=True,
        ))
        return {
            "action": name, "command": command, "cwd": cwd,
            "evidence_paths": evidence_paths,
        }
    if name == "inspect_episode":
        _require_keys(raw, {"action", "ref"})
        return {"action": name, "ref": _required_str(raw, "ref")}
    if name == "list_findings":
        _require_keys(raw, {"action"}, {"state", "limit"})
        state = raw.get("state", "active")
        if state not in {"active", "open", "dormant", "archived", "all"}:
            raise ProposerError("invalid finding state")
        limit = raw.get("limit", 20)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProposerError("limit must be a positive integer")
        return {"action": name, "state": state, "limit": limit}
    if name == "search_findings":
        _require_keys(raw, {"action", "query"}, {"limit"})
        limit = raw.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProposerError("limit must be a positive integer")
        return {"action": name, "query": _required_str(raw, "query"), "limit": limit}
    if name == "inspect_finding":
        _require_keys(raw, {"action", "finding_id"})
        return {"action": name, "finding_id": _required_str(raw, "finding_id")}
    if name == "search_experiments":
        _require_keys(raw, {"action", "query"}, {"filters", "limit", "buckets"})
        filters = raw.get("filters") or {}
        allowed = {
            "gate_passed", "eligible", "selected", "finding_id",
            "changed_path", "round_min", "round_max", "status",
        }
        if not isinstance(filters, dict) or set(filters) - allowed:
            raise ProposerError("search_experiments.filters is invalid")
        limit = raw.get("limit", 10)
        buckets = raw.get("buckets", True)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ProposerError("limit must be a positive integer")
        if not isinstance(buckets, bool):
            raise ProposerError("buckets must be a bool")
        return {
            "action": name, "query": _required_str(raw, "query"),
            "filters": filters, "limit": limit, "buckets": buckets,
        }
    if name == "block":
        _require_keys(raw, {"action", "reason_kind", "explanation", "evidence_refs"})
        if raw["reason_kind"] not in _BLOCK_REASON_KINDS:
            raise ProposerError("invalid block reason_kind")
        return {
            "action": name,
            "reason_kind": raw["reason_kind"],
            "explanation": _required_str(raw, "explanation"),
            "evidence_refs": tuple(_require_string_list(raw["evidence_refs"], name="evidence_refs")),
        }
    raise ProposerError(f"unknown research action: {name}")

_FRESH_PHASE_ACTIONS = {
    InquiryPhase.UNDERSTAND: {"run_research_command", "commit_understanding"},
    InquiryPhase.MODEL: {"run_research_command", "propose_working_model", "continue_investigation", "commit_working_model"},
    InquiryPhase.EXPLAIN: {"run_research_command", "submit_explanation", "commit_explanation_set", "reopen_model"},
    InquiryPhase.EXPLORE: {"run_research_command", "emit_lever_map", "submit_hypothesis", "commit_hypothesis_portfolio", "reopen_explain", "reopen_model"},
}
_NARROW_ACTIONS = {"run_research_command", "select_for_deepen", "continue_explore", "reopen_explain", "reopen_model", "fresh_reframe", "abandon_portfolio"}
_DEEPEN_ACTIONS = {"run_research_command", "submit_proposals", "return_to_narrow", "continue_explore", "reopen_explain", "reopen_model", "fresh_reframe", "abandon_direction"}

def phase_allowed_actions(phase: InquiryPhase, history_visible: bool) -> frozenset[str]:
    """The single source of truth for phase and history capabilities."""
    if phase in {InquiryPhase.NARROW, InquiryPhase.DEEPEN}:
        if not history_visible:
            raise ValueError(f"{phase.value} requires history")
        actions = _NARROW_ACTIONS if phase is InquiryPhase.NARROW else _DEEPEN_ACTIONS
    else:
        actions = _FRESH_PHASE_ACTIONS[phase]
    if history_visible:
        actions = actions | MEMORY_TOOL_ACTIONS
    return frozenset(actions | {"block"})

PHASE_ATTENTION = {
    InquiryPhase.UNDERSTAND: "Current mode: UNDERSTAND. Do not search for modifications yet. Investigate broadly and form a coarse account of how the whole problem produces the target outcome; restating the goal is not understanding. Identify unknowns that could change your later model.",
    InquiryPhase.MODEL: "Current mode: MODEL. Construct a working representation that can explain the target outcome and support counterfactual reasoning. A list of components or facts is not sufficient. Do not seek completeness; seek a model sufficient for the next consequential research decision.",
    InquiryPhase.EXPLAIN: "Current mode: EXPLAIN. Form an account of the mechanism, structural limitation, obstruction, or dependency that produces the gap or creates the opportunity. Keep materially different accounts alive where evidence permits. Do not design the intervention yet.",
    InquiryPhase.EXPLORE: "Current mode: EXPLORE. Using the working model and explanations, search broadly across materially different mechanism families before investing deeply in any one direction.",
    InquiryPhase.NARROW: "Current mode: NARROW. Past experiments are now available as evidence. Use them to support, refute, or revise the independently formed model and hypotheses. Historical vocabulary must not replace your own representation.",
    InquiryPhase.DEEPEN: "Current mode: DEEPEN. Detailed investigation is now justified. Test each selected hypothesis critical premise, trace its real scope, derive observable consequences, and submit only if the mechanism survives.",
}

_SCIENTIST_ACTION_SCHEMAS = {
    "commit_understanding": '{"action":"commit_understanding","problem":"...","target_outcome":"...","boundary":"...","current_account_of_the_whole":"...","key_unknowns":["..."]}',
    "continue_investigation": '{"action":"continue_investigation","question":"...","decision_impact":"..."}',
    "propose_working_model": '{"action":"propose_working_model","working_model":{"representation":"...","explanatory_structure":"...","claims":[{"id":"M1","claim":"...","evidence_refs":["source:path"]}],"important_unknowns":["..."]}}',
    "commit_working_model": '{"action":"commit_working_model","model_version":2,"model_check":{"explains_target":"...","counterfactual":{"change":"...","predicted_effect":"...","model_claim_refs":["M1"]},"important_unknowns":[{"question":"...","why_it_matters":"..."}],"blocking_unknown":null,"why_model_is_sufficient_for_next_stage":"..."}}',
    "submit_explanation": '{"action":"submit_explanation","id":"E1","phenomenon":"...","account":"...","model_basis":["M1"],"expected_if_true":["..."],"evidence_needed":["..."]}',
    "commit_explanation_set": '{"action":"commit_explanation_set","explanation_ids":["E1","E2"],"explanation_sufficiency_justification":null}',
    "emit_lever_map": '{"action":"emit_lever_map","levers":[{"id":"L1","target_mechanism":"...","why_leverage_exists":"...","model_basis":["M1"],"explanation_basis":["E1"]}]}',
    "submit_hypothesis": '{"action":"submit_hypothesis","id":"H1","generative_op":"G2","model_basis":["M1"],"explanation_basis":["E1"],"mechanism":"...","intervention_family":"...","scope":"...","why_plausible":"...","critical_unknown":"..."}',
    "commit_hypothesis_portfolio": '{"action":"commit_hypothesis_portfolio","hypothesis_ids":["H1"],"coverage_rationale":"...","portfolio_sufficiency_justification":null,"unused_generative_ops":[]}',
    "select_for_deepen": '{"action":"select_for_deepen","selected":[{"hypothesis_id":"H1","evidence_refs":["experiment:r0c0"],"rationale":"..."}]}',
    "submit_proposals": '{"action":"submit_proposals","proposals":[{"instruction":"...","research_target":{"mode":"new","question":"..."},"model_claim_refs":["M1"],"explanation_refs":["E1"],"hypothesis_id":"H1","evidence_refs":["source:path"],"mechanism":"...","prediction":"...","affected_scope":"..."}]}',
    "continue_explore": '{"action":"continue_explore","reason":"...","evidence_refs":["experiment:r0c0"]}',
    "reopen_explain": '{"action":"reopen_explain","reason":"...","evidence_refs":["experiment:r0c0"]}',
    "reopen_model": '{"action":"reopen_model","reason":"...","evidence_refs":["experiment:r0c0"]}',
    "return_to_narrow": '{"action":"return_to_narrow","reason":"...","evidence_refs":["source:path"]}',
    "fresh_reframe": '{"action":"fresh_reframe","reason":"...","evidence_refs":["experiment:r0c0"]}',
    "abandon_portfolio": '{"action":"abandon_portfolio","reason":"...","evidence_refs":["experiment:r0c0"]}',
    "abandon_direction": '{"action":"abandon_direction","hypothesis_id":"H1","reason":"...","evidence_refs":["source:path"]}',
    "block": '{"action":"block","reason_kind":"false_claim|frozen|contradiction","explanation":"...","evidence_refs":["source:path"]}',
}

def render_scientist_action_protocol(actions) -> str:
    schemas = [schema for name, schema in _SCIENTIST_ACTION_SCHEMAS.items() if name in actions]
    return "Allowed control actions (return exactly one JSON object):\n- " + "\n- ".join(schemas)

def _build_phase_system_prompt(
    prompt_dir: Path | None, phase: InquiryPhase, history_visible: bool, *,
    assigned_ops: tuple[str, ...] = (),
) -> str:
    actions = phase_allowed_actions(phase, history_visible)
    tools = render_research_tool_prompt(actions & ({"run_research_command"} | MEMORY_TOOL_ACTIONS))
    basis = render_generative_basis(assigned_ops) if phase in {
        InquiryPhase.EXPLORE, InquiryPhase.NARROW, InquiryPhase.DEEPEN,
    } else ""
    return "\n\n".join(filter(None, (
        load_semantic("proposer", prompt_dir).rstrip(),
        PHASE_ATTENTION[phase],
        basis,
        render_scientist_action_protocol(actions),
        tools,
        _RUNTIME_BOUNDARIES,
    )))

def _required_str(value: dict, name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ProposerError(f"{name} must be a non-empty string")
    return item.strip()

def _optional_nonempty(value, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProposerError(f"{name} must be null or a non-empty string")
    return value.strip()

def _parse_scientist_action(text: str) -> dict:
    """Parse action structure independently of phase permissions."""
    try:
        raw = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProposerError("proposer response must be one JSON object") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("action"), str):
        raise ProposerError("proposer action must be a JSON object with action")
    name = raw["action"]
    if name in ({"run_research_command"} | MEMORY_TOOL_ACTIONS | {"block"}):
        return _parse_research_action(raw)
    if name == "commit_understanding":
        _require_keys(raw, {"action", "problem", "target_outcome", "boundary", "current_account_of_the_whole", "key_unknowns"})
        return {"action": name, "understanding": Understanding(
            problem=_required_str(raw, "problem"),
            target_outcome=_required_str(raw, "target_outcome"),
            boundary=_required_str(raw, "boundary"),
            current_account_of_the_whole=_required_str(raw, "current_account_of_the_whole"),
            key_unknowns=tuple(_require_string_list(raw["key_unknowns"], name="key_unknowns")),
        )}
    if name == "propose_working_model":
        _require_keys(raw, {"action", "working_model"})
        model = raw["working_model"]
        if not isinstance(model, dict):
            raise ProposerError("working_model must be an object")
        _require_keys(model, {"representation", "explanatory_structure", "claims", "important_unknowns"})
        if not isinstance(model["claims"], list) or not model["claims"]:
            raise ProposerError("working_model.claims must be non-empty")
        claims = []
        for claim in model["claims"]:
            if not isinstance(claim, dict):
                raise ProposerError("working_model claims must be objects")
            _require_keys(claim, {"id", "claim", "evidence_refs"})
            claims.append({
                "id": _required_str(claim, "id"),
                "claim": _required_str(claim, "claim"),
                "evidence_refs": tuple(_require_string_list(claim["evidence_refs"], name="claim.evidence_refs")),
            })
        return {"action": name, "working_model": {
            "representation": _required_str(model, "representation"),
            "explanatory_structure": _required_str(model, "explanatory_structure"),
            "claims": claims,
            "important_unknowns": tuple(_require_string_list(model["important_unknowns"], name="important_unknowns", allow_empty=True)),
        }}
    if name == "commit_working_model":
        _require_keys(raw, {"action", "model_version", "model_check"})
        version = raw["model_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ProposerError("model_version must be a positive integer")
        check = raw["model_check"]
        if not isinstance(check, dict):
            raise ProposerError("model_check must be an object")
        _require_keys(check, {"explains_target", "counterfactual", "important_unknowns", "blocking_unknown", "why_model_is_sufficient_for_next_stage"})
        cf = check["counterfactual"]
        if not isinstance(cf, dict):
            raise ProposerError("counterfactual must be an object")
        _require_keys(cf, {"change", "predicted_effect", "model_claim_refs"})
        unknowns = check["important_unknowns"]
        if not isinstance(unknowns, list):
            raise ProposerError("important_unknowns must be a list")
        for unknown in unknowns:
            if not isinstance(unknown, dict):
                raise ProposerError("important_unknowns items must be objects")
            _require_keys(unknown, {"question", "why_it_matters"})
            _required_str(unknown, "question")
            _required_str(unknown, "why_it_matters")
        blocking = check["blocking_unknown"]
        if blocking is not None and (not isinstance(blocking, str) or not blocking.strip()):
            raise ProposerError("blocking_unknown must be null or a non-empty string")
        return {"action": name, "model_version": version, "model_check": {
            "explains_target": _required_str(check, "explains_target"),
            "counterfactual": {
                "change": _required_str(cf, "change"),
                "predicted_effect": _required_str(cf, "predicted_effect"),
                "model_claim_refs": tuple(_require_string_list(cf["model_claim_refs"], name="model_claim_refs")),
            },
            "important_unknowns": tuple(unknowns),
            "blocking_unknown": blocking.strip() if isinstance(blocking, str) else None,
            "why_model_is_sufficient_for_next_stage": _required_str(check, "why_model_is_sufficient_for_next_stage"),
        }}
    if name == "submit_explanation":
        _require_keys(raw, {"action", "id", "phenomenon", "account", "model_basis", "expected_if_true", "evidence_needed"})
        return {"action": name, "explanation": Explanation(
            id=_required_str(raw, "id"), phenomenon=_required_str(raw, "phenomenon"),
            account=_required_str(raw, "account"),
            model_basis=tuple(_require_string_list(raw["model_basis"], name="model_basis")),
            expected_if_true=tuple(_require_string_list(raw["expected_if_true"], name="expected_if_true")),
            evidence_needed=tuple(_require_string_list(raw["evidence_needed"], name="evidence_needed")),
        )}
    if name == "emit_lever_map":
        _require_keys(raw, {"action", "levers"})
        if not isinstance(raw["levers"], list) or not raw["levers"]:
            raise ProposerError("levers must be a non-empty list")
        levers = []
        for item in raw["levers"]:
            if not isinstance(item, dict):
                raise ProposerError("levers must contain objects")
            _require_keys(item, {"id", "target_mechanism", "why_leverage_exists", "model_basis", "explanation_basis"})
            levers.append(LeveragePoint(
                id=_required_str(item, "id"),
                target_mechanism=_required_str(item, "target_mechanism"),
                why_leverage_exists=_required_str(item, "why_leverage_exists"),
                model_basis=tuple(_require_string_list(item["model_basis"], name="model_basis")),
                explanation_basis=tuple(_require_string_list(item["explanation_basis"], name="explanation_basis")),
            ))
        return {"action": name, "levers": tuple(levers)}
    if name == "submit_hypothesis":
        _require_keys(raw, {"action", "id", "generative_op", "model_basis", "explanation_basis", "mechanism", "intervention_family", "scope", "why_plausible", "critical_unknown"}, {"evidence_refs"})
        op = raw["generative_op"]
        if op is not None and (not isinstance(op, str) or not op.strip()):
            raise ProposerError("generative_op must be null or non-empty")
        return {"action": name, "hypothesis": ResearchHypothesis(
            id=_required_str(raw, "id"), generative_op=op.strip() if isinstance(op, str) else None,
            model_basis=tuple(_require_string_list(raw["model_basis"], name="model_basis")),
            explanation_basis=tuple(_require_string_list(raw["explanation_basis"], name="explanation_basis")),
            mechanism=_required_str(raw, "mechanism"), intervention_family=_required_str(raw, "intervention_family"),
            scope=_required_str(raw, "scope"), why_plausible=_required_str(raw, "why_plausible"),
            critical_unknown=_required_str(raw, "critical_unknown"),
            evidence_refs=tuple(_require_string_list(raw.get("evidence_refs", []), name="evidence_refs", allow_empty=True)),
        )}
    if name == "commit_explanation_set":
        _require_keys(raw, {"action", "explanation_ids", "explanation_sufficiency_justification"})
        return {"action": name, "explanation_ids": tuple(_require_string_list(raw["explanation_ids"], name="explanation_ids")), "explanation_sufficiency_justification": _optional_nonempty(raw["explanation_sufficiency_justification"], "explanation_sufficiency_justification")}
    if name == "commit_hypothesis_portfolio":
        _require_keys(raw, {"action", "hypothesis_ids", "coverage_rationale", "portfolio_sufficiency_justification", "unused_generative_ops"})
        return {"action": name, "hypothesis_ids": tuple(_require_string_list(raw["hypothesis_ids"], name="hypothesis_ids")), "coverage_rationale": _required_str(raw, "coverage_rationale"), "portfolio_sufficiency_justification": _optional_nonempty(raw["portfolio_sufficiency_justification"], "portfolio_sufficiency_justification"), "unused_generative_ops": tuple(_require_string_list(raw["unused_generative_ops"], name="unused_generative_ops", allow_empty=True))}
    if name == "select_for_deepen":
        _require_keys(raw, {"action", "selected"})
        if not isinstance(raw["selected"], list) or not raw["selected"]:
            raise ProposerError("selected must be a non-empty list")
        selected = []
        for item in raw["selected"]:
            if not isinstance(item, dict):
                raise ProposerError("selected must contain objects")
            _require_keys(item, {"hypothesis_id", "evidence_refs", "rationale"})
            selected.append(HypothesisSelection(_required_str(item, "hypothesis_id"), tuple(_require_string_list(item["evidence_refs"], name="evidence_refs")), _required_str(item, "rationale")))
        return {"action": name, "selected": tuple(selected)}
    if name == "submit_proposals":
        _require_keys(raw, {"action", "proposals"})
        if not isinstance(raw["proposals"], list) or not raw["proposals"]:
            raise ProposerError("proposals must be a non-empty list")
        return {"action": name, "proposals": tuple(
            _parse_proposal(item) for item in raw["proposals"]
        )}
    if name == "continue_investigation":
        _require_keys(raw, {"action", "question", "decision_impact"})
        return {"action": name, "question": _required_str(raw, "question"), "decision_impact": _required_str(raw, "decision_impact")}
    if name in {"continue_explore", "reopen_explain", "reopen_model", "return_to_narrow", "fresh_reframe", "abandon_portfolio"}:
        _require_keys(raw, {"action", "reason", "evidence_refs"})
        return {"action": name, "reason": _required_str(raw, "reason"), "evidence_refs": tuple(_require_string_list(raw["evidence_refs"], name="evidence_refs"))}
    if name == "abandon_direction":
        _require_keys(raw, {"action", "hypothesis_id", "reason", "evidence_refs"})
        return {"action": name, "hypothesis_id": _required_str(raw, "hypothesis_id"), "reason": _required_str(raw, "reason"), "evidence_refs": tuple(_require_string_list(raw["evidence_refs"], name="evidence_refs"))}
    raise ProposerError(f"unknown proposer action: {name}")

# --- Scientist-specific guards -------------------------------------------

from .research_agent import _source_path_exists  # noqa: E402

def _validate_block_evidence(
    refs, state: WorkingState, source_root: Path,
) -> bool:
    """True when the block cites at least one ``source:`` ref to a path the
    agent read this branch (present in ``new_evidence``) that exists under
    /source. The uniform objective choke point for every block."""
    for ref in refs:
        if ":" not in ref:
            continue
        kind, _, rest = ref.partition(":")
        if kind == "source":
            if (ref in state.new_evidence
                    and _source_path_exists(rest, source_root)):
                return True
    return False

def _upsert_by_id(items: list, item) -> None:
    for index, current in enumerate(items):
        if current.id == item.id:
            items[index] = item
            return
    items.append(item)

def _apply_scientist_action(
    session: ScientistSessionState, action: dict, *, step: int,
) -> None:
    """Apply one already-parsed and already-guarded Scientist commitment."""
    inquiry = session.inquiry
    source_phase = inquiry.phase
    name = action["action"]
    session.cumulative_action_log.append({
        "context_id": inquiry.context_id,
        "phase": source_phase.value,
        "action": name,
        "step": step,
    })
    if name == "commit_understanding":
        inquiry.understanding = action["understanding"]
        inquiry.transition(InquiryPhase.MODEL, step=step, reason=name)
    elif name == "propose_working_model":
        raw = action["working_model"]
        version = 1 if inquiry.working_model is None else inquiry.working_model.version + 1
        inquiry.working_model = WorkingModel(
            version=version,
            representation=raw["representation"],
            explanatory_structure=raw["explanatory_structure"],
            claims=tuple(ModelClaim(
                id=claim["id"], claim=claim["claim"],
                evidence_refs=claim["evidence_refs"],
            ) for claim in raw["claims"]),
            important_unknowns=raw["important_unknowns"],
        )
        inquiry.model_revisions.append(inquiry.working_model)
    elif name == "commit_working_model":
        inquiry.transition(InquiryPhase.EXPLAIN, step=step, reason=name)
    elif name == "submit_explanation":
        _upsert_by_id(inquiry.explanations, action["explanation"])
    elif name == "commit_explanation_set":
        by_id = {item.id: item for item in inquiry.explanations}
        inquiry.explanations = [by_id[item_id] for item_id in action["explanation_ids"]]
        inquiry.transition(InquiryPhase.EXPLORE, step=step, reason=name)
    elif name == "emit_lever_map":
        inquiry.lever_map = list(action["levers"])
    elif name == "submit_hypothesis":
        _upsert_by_id(inquiry.hypotheses, action["hypothesis"])
    elif name == "commit_hypothesis_portfolio":
        by_id = {item.id: item for item in inquiry.hypotheses}
        inquiry.hypotheses = [by_id[item_id] for item_id in action["hypothesis_ids"]]
        inquiry.set_history_visible(True, step=step)
        inquiry.transition(InquiryPhase.NARROW, step=step, reason=name)
    elif name == "select_for_deepen":
        inquiry.selections = list(action["selected"])
        inquiry.narrow_decisions.append({
            "step": step,
            "selected": [item.hypothesis_id for item in action["selected"]],
        })
        inquiry.deep_evidence_refs.clear()
        inquiry.transition(InquiryPhase.DEEPEN, step=step, reason=name)
    elif name in {"continue_explore", "return_to_narrow"}:
        inquiry.selections.clear()
        inquiry.proposals.clear()
        target = InquiryPhase.EXPLORE if name == "continue_explore" else InquiryPhase.NARROW
        inquiry.transition(target, step=step, reason=name)
    elif name == "reopen_explain":
        inquiry.explanations.clear()
        inquiry.lever_map.clear()
        inquiry.hypotheses.clear()
        inquiry.selections.clear()
        inquiry.proposals.clear()
        inquiry.reopen_counts[name] = inquiry.reopen_counts.get(name, 0) + 1
        inquiry.transition(InquiryPhase.EXPLAIN, step=step, reason=name)
    elif name == "reopen_model":
        inquiry.working_model = None
        inquiry.explanations.clear()
        inquiry.lever_map.clear()
        inquiry.hypotheses.clear()
        inquiry.selections.clear()
        inquiry.proposals.clear()
        inquiry.reopen_counts[name] = inquiry.reopen_counts.get(name, 0) + 1
        inquiry.transition(InquiryPhase.MODEL, step=step, reason=name)
    elif name == "fresh_reframe":
        session.start_fresh_context()
    elif name in {"abandon_portfolio", "abandon_direction"}:
        inquiry.selections.clear()
        inquiry.proposals.clear()
    elif name == "submit_proposals":
        inquiry.proposals = list(action["proposals"])

def _validate_scientist_guard(
    session: ScientistSessionState,
    action: dict,
    source_root: Path,
    select_quota: int,
    *,
    assigned_ops: tuple[str, ...] = (),
) -> str | None:
    """Validate structural commitments, never predicted scientific merit."""
    name = action.get("action")
    if name not in phase_allowed_actions(
        session.inquiry.phase, session.inquiry.history_visible,
    ):
        return f"action_not_allowed_in_{session.inquiry.phase.value}"
    if name == "block":
        return None if _validate_block_evidence(
            action["evidence_refs"], session.runtime, source_root,
        ) else "block_needs_source"

    if name in _RESEARCH_TOOL_ACTIONS:
        if name == "run_research_command":
            if action["evidence_paths"] and action["cwd"] != "source":
                return "source_evidence_requires_source_cwd"
            for path in action["evidence_paths"]:
                if not _source_path_exists(path, source_root):
                    return "invalid_source_evidence_path"
        fingerprint = _fingerprint(action)
        if fingerprint == session.runtime.last_tool_fingerprint:
            return "repeated_tool"
        return None
    model = session.inquiry.working_model
    model_ids = {claim.id for claim in model.claims} if model else set()
    explanation_ids = {item.id for item in session.inquiry.explanations}
    hypothesis_ids = {item.id for item in session.inquiry.hypotheses}

    if name == "propose_working_model":
        for claim in action["working_model"]["claims"]:
            if not set(claim["evidence_refs"]) <= session.runtime.session_evidence:
                return "ungrounded_evidence"
    elif name == "commit_working_model":
        if model is None or action["model_version"] != model.version:
            return "stale_model_version"
        refs = action["model_check"]["counterfactual"]["model_claim_refs"]
        if not set(refs) <= model_ids:
            return "unknown_model_claim"
        if action["model_check"]["blocking_unknown"] is not None:
            return "model_blocking_unknown"
    elif name == "submit_explanation":
        if not set(action["explanation"].model_basis) <= model_ids:
            return "unknown_model_claim"
    elif name == "commit_explanation_set":
        ids = set(action["explanation_ids"])
        if not ids <= explanation_ids:
            return "unknown_explanation"
        if len(ids) < 2 and not action["explanation_sufficiency_justification"]:
            return "explanation_below_breadth_target"
    elif name == "emit_lever_map":
        for lever in action["levers"]:
            if not set(lever.model_basis) <= model_ids:
                return "unknown_model_claim"
            if not set(lever.explanation_basis) <= explanation_ids:
                return "unknown_explanation"
    elif name == "submit_hypothesis":
        hypothesis = action["hypothesis"]
        if not set(hypothesis.model_basis) <= model_ids:
            return "unknown_model_claim"
        if not set(hypothesis.explanation_basis) <= explanation_ids:
            return "unknown_explanation"
        if not session.inquiry.lever_map:
            return "lever_map_required"
        if not set(hypothesis.evidence_refs) <= session.runtime.session_evidence:
            return "ungrounded_evidence"
        if assigned_ops and hypothesis.generative_op not in assigned_ops:
            return "unassigned_generative_op"
    elif name == "commit_hypothesis_portfolio":
        ids = set(action["hypothesis_ids"])
        if not ids <= hypothesis_ids:
            return "unknown_hypothesis"
        committed = [h for h in session.inquiry.hypotheses if h.id in ids]
        breadth = len({h.signature() for h in committed})
        if breadth < max(4, select_quota * 2):
            if not action["portfolio_sufficiency_justification"]:
                return "portfolio_below_breadth_target"
    elif name == "select_for_deepen":
        if len(action["selected"]) > select_quota:
            return "selection_exceeds_quota"
        if any(item.hypothesis_id not in hypothesis_ids for item in action["selected"]):
            return "unknown_hypothesis"
        if any(
            not set(item.evidence_refs) <= session.runtime.session_evidence
            for item in action["selected"]
        ):
            return "ungrounded_evidence"
    elif name == "submit_proposals":
        selected_ids = {item.hypothesis_id for item in session.inquiry.selections}
        proposal_ids = [item.hypothesis_id for item in action["proposals"]]
        if len(proposal_ids) > select_quota or not set(proposal_ids) <= selected_ids:
            return "proposal_requires_selected_hypothesis"
        for proposal in action["proposals"]:
            if not set(proposal.model_claim_refs) <= model_ids:
                return "unknown_model_claim"
            if not set(proposal.explanation_refs) <= explanation_ids:
                return "unknown_explanation"
            if not set(proposal.evidence_refs) <= session.runtime.session_evidence:
                return "ungrounded_evidence"
            if not any(
                ref in session.inquiry.deep_evidence_refs
                for ref in proposal.evidence_refs
            ):
                return "proposal_requires_deep_evidence"
    elif name in {
        "continue_explore", "reopen_explain", "reopen_model",
        "return_to_narrow", "fresh_reframe", "abandon_portfolio",
        "abandon_direction",
    }:
        if not set(action["evidence_refs"]) <= session.runtime.session_evidence:
            return "ungrounded_evidence"
        if name == "fresh_reframe" and session.fresh_reframes >= 1:
            return "fresh_reframe_limit"
    return None

# --- Scientist-Proposer ------------------------------------------------

class ProposerAgent(ResearchAgent):
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
    ):
        super().__init__(
            model=model, runtime=runtime,
            timeout_seconds=timeout_seconds, max_steps=max_steps,
            command_timeout_seconds=command_timeout_seconds,
            command_output_cap_chars=command_output_cap_chars,
            usage_observer=usage_observer,
        )
        self._scientist_context = local()

    def _parse_action(self, text: str) -> dict:
        return _parse_scientist_action(text)

    def _validate_guard(
        self, state: WorkingState, action: dict, source_root: Path,
    ) -> str | None:
        context = self._scientist_context
        return _validate_scientist_guard(
            context.session, action, source_root, context.select_quota,
            assigned_ops=context.assigned_ops,
        )

    @staticmethod
    def _scientist_state_message(session: ScientistSessionState) -> str:
        inquiry = session.inquiry
        model = inquiry.working_model
        payload = {
            "context_id": inquiry.context_id,
            "phase": inquiry.phase.value,
            "history_visible": inquiry.history_visible,
            "understanding": asdict(inquiry.understanding) if inquiry.understanding else None,
            "working_model": asdict(model) if model else None,
            "explanations": [asdict(item) for item in inquiry.explanations],
            "lever_map": [asdict(item) for item in inquiry.lever_map],
            "hypotheses": [asdict(item) for item in inquiry.hypotheses],
            "selections": [asdict(item) for item in inquiry.selections],
        }
        return "Accepted. Current committed inquiry state:\n" + json.dumps(
            payload, ensure_ascii=False,
        )

    @staticmethod
    def _scientist_trace(session: ScientistSessionState, *, outcome: str) -> dict:
        inquiry = session.inquiry
        contexts = [*session.archived_contexts, session.context]
        actions = list(session.cumulative_action_log)

        def first_step(action_name: str):
            return next((
                item["step"] for item in actions
                if item["action"] == action_name
            ), None)

        def target_dict(target):
            if isinstance(target, ExistingFindingTarget):
                return {"mode": "existing", "finding_id": target.finding_id}
            return {
                "mode": "new", "question": target.question,
                "mechanisms": list(target.mechanisms),
                "code_regions": list(target.code_regions),
            }

        def proposal_dict(proposal):
            return {
                "instruction": proposal.instruction,
                "research_target": target_dict(proposal.research_target),
                "model_claim_refs": list(proposal.model_claim_refs),
                "explanation_refs": list(proposal.explanation_refs),
                "hypothesis_id": proposal.hypothesis_id,
                "evidence_refs": list(proposal.evidence_refs),
                "mechanism": proposal.mechanism,
                "prediction": proposal.prediction,
                "affected_scope": proposal.affected_scope,
            }

        def transition_dict(item):
            return {
                "context_id": item.context_id, "source": item.source.value,
                "target": item.target.value, "step": item.step,
                "reason": item.reason,
                "history_visible": item.history_visible,
            }

        def context_dict(context):
            state = context.inquiry
            return {
                "context_id": state.context_id,
                "phase": state.phase.value,
                "history_visible": state.history_visible,
                "history_injected_at_step": state.history_injected_at_step,
                "understanding": asdict(state.understanding) if state.understanding else None,
                "working_model": asdict(state.working_model) if state.working_model else None,
                "model_revisions": [asdict(item) for item in state.model_revisions],
                "explanations": [asdict(item) for item in state.explanations],
                "lever_map": [asdict(item) for item in state.lever_map],
                "fresh_hypotheses": [asdict(item) for item in state.hypotheses],
                "narrow_decisions": list(state.narrow_decisions),
                "selected_hypotheses": [asdict(item) for item in state.selections],
                "deep_evidence": sorted(state.deep_evidence_refs),
                "proposals": [proposal_dict(item) for item in state.proposals],
                "phase_transitions": [transition_dict(item) for item in state.phase_transitions],
                "reopen_counts": dict(state.reopen_counts),
            }

        context_summaries = [context_dict(context) for context in contexts]
        tool_calls_by_phase: dict[str, int] = {}
        for item in actions:
            if item["action"] in _RESEARCH_TOOL_ACTIONS:
                phase = item["phase"]
                tool_calls_by_phase[phase] = tool_calls_by_phase.get(phase, 0) + 1
        return {
            "outcome": outcome,
            "phase": inquiry.phase.value,
            "current_phase": inquiry.phase.value,
            "contexts": context_summaries,
            "history_visible": inquiry.history_visible,
            "history_injected_at_step": inquiry.history_injected_at_step,
            "phase_transitions": [
                transition_dict(item) for context in contexts
                for item in context.inquiry.phase_transitions
            ],
            "actions": actions,
            "understanding": asdict(inquiry.understanding) if inquiry.understanding else None,
            "working_model": asdict(inquiry.working_model) if inquiry.working_model else None,
            "model_revisions": [asdict(item) for item in inquiry.model_revisions],
            "explanations": [asdict(item) for item in inquiry.explanations],
            "lever_map": [asdict(item) for item in inquiry.lever_map],
            "fresh_hypotheses": [asdict(item) for item in inquiry.hypotheses],
            "narrow_decisions": list(inquiry.narrow_decisions),
            "selected_hypotheses": [asdict(item) for item in inquiry.selections],
            "deep_evidence": sorted(inquiry.deep_evidence_refs),
            "proposals": [proposal_dict(item) for item in inquiry.proposals],
            "reopen_counts": dict(inquiry.reopen_counts),
            "fresh_reframes": session.fresh_reframes,
            "usage_by_phase": session.usage_by_phase,
            "wall_time_by_phase": dict(session.wall_time_by_phase),
            "tool_calls_by_phase": tool_calls_by_phase,
            "steps_to_working_model": first_step("commit_working_model"),
            "steps_to_portfolio": first_step("commit_hypothesis_portfolio"),
            "steps_to_outcome": max((item["step"] for item in actions), default=0),
        }

    def run_lane(
        self,
        *,
        assigned_ops: tuple[str, ...],
        select_quota: int,
        scientist_steps: int,
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
        hints: list[str] | None = None,
    ) -> ScientistResult:
        """Run one Scientist session under one total lane-local step budget."""
        if select_quota < 1 or scientist_steps < 1:
            raise ValueError("select_quota and scientist_steps must be positive")
        session = ScientistSessionState.fresh()
        messages = [{"role": "user", "content": (
            memory_service.build_fresh_inquiry_context(
                goal=goal, editable=editable, frozen=frozen,
                base_sha=base_sha, gate_block=gate_block, hints=hints,
            )
        )}]
        usages: list[object] = []
        deadline = time.monotonic() + self.timeout_seconds
        context = self._scientist_context
        context.session = session
        context.select_quota = select_quota
        context.assigned_ops = assigned_ops
        try:
            with TemporaryDirectory(prefix="simpleloop-scientist-") as scratch:
                tools = self._make_tools(
                    source=source_path, repo=repo_path, history_dir=None,
                    scratch=Path(scratch), memory_service=None,
                    current_round=current_round, history_enabled=False,
                )
                for step in range(1, scientist_steps + 1):
                    phase = session.inquiry.phase
                    step_started = time.monotonic()

                    def record_phase_wall():
                        elapsed = time.monotonic() - step_started
                        session.wall_time_by_phase[phase.value] = (
                            session.wall_time_by_phase.get(phase.value, 0.0) + elapsed
                        )
                    system = _build_phase_system_prompt(
                        prompt_dir, phase, session.inquiry.history_visible,
                        assigned_ops=assigned_ops,
                    )
                    usage_start = len(usages)
                    action, reply_text = self._step(
                        session.runtime, messages, system, deadline, usages, step,
                        source_root=source_path, steps_budget=scientist_steps,
                    )
                    phase_usage = usages[usage_start:]
                    session.usage_by_phase.setdefault(phase.value, []).extend(phase_usage)
                    session.cumulative_usage.extend(
                        item for item in phase_usage if isinstance(item, dict)
                    )
                    name = action["action"]
                    session.runtime.action_log.append({"action": name, "step": step})
                    trace_action = None
                    if name in _RESEARCH_TOOL_ACTIONS or name == "block":
                        trace_action = {
                            "context_id": session.inquiry.context_id,
                            "phase": phase.value,
                            "action": name,
                            "step": step,
                        }
                        if name in _RESEARCH_TOOL_ACTIONS:
                            trace_action["request_summary"] = _action_summary(action)
                        session.cumulative_action_log.append(trace_action)
                    if name in _RESEARCH_TOOL_ACTIONS:
                        observation = tools.execute(action, deadline=deadline)
                        _bump(session.runtime, "tool")
                        if name == "run_research_command":
                            _bump(session.runtime, "source_read")
                        _register_evidence(session.runtime, action, observation)
                        trace_action["observation_summary"] = _result_summary(
                            action, observation,
                        )
                        observed_refs = _observation_evidence_refs(action, observation)
                        trace_action["evidence_refs"] = sorted(observed_refs)
                        if phase is InquiryPhase.DEEPEN:
                            session.inquiry.deep_evidence_refs.update(observed_refs)
                        session.runtime.last_tool_fingerprint = _fingerprint(action)
                        record_phase_wall()
                        messages.extend([
                            {"role": "assistant", "content": reply_text},
                            {"role": "user", "content": json.dumps(
                                observation, ensure_ascii=False,
                            )},
                        ])
                        continue
                    if name == "block":
                        record_phase_wall()
                        return ScientistResult(
                            outcome="block", reason=action["explanation"],
                            usage=tuple(usages),
                            deliberation_telemetry={
                                "steps": step,
                                "usage_by_phase": session.usage_by_phase,
                                "wall_time_by_phase": dict(session.wall_time_by_phase),
                            },
                            trace=self._scientist_trace(session, outcome="block"),
                        )
                    was_history_visible = session.inquiry.history_visible
                    old_context_id = session.inquiry.context_id
                    _apply_scientist_action(session, action, step=step)
                    if name == "submit_proposals":
                        record_phase_wall()
                        return ScientistResult(
                            proposals=tuple(session.inquiry.proposals),
                            outcome="proposals", usage=tuple(usages),
                            deliberation_telemetry={
                                "steps": step,
                                "usage_by_phase": session.usage_by_phase,
                                "wall_time_by_phase": dict(session.wall_time_by_phase),
                            },
                            trace=self._scientist_trace(session, outcome="proposals"),
                        )
                    if name in {"abandon_portfolio", "abandon_direction"}:
                        record_phase_wall()
                        return ScientistResult(
                            outcome="research_incomplete", reason=action["reason"],
                            usage=tuple(usages),
                            deliberation_telemetry={
                                "steps": step,
                                "usage_by_phase": session.usage_by_phase,
                                "wall_time_by_phase": dict(session.wall_time_by_phase),
                            },
                            trace=self._scientist_trace(
                                session, outcome="research_incomplete",
                            ),
                        )
                    if session.inquiry.context_id != old_context_id:
                        record_phase_wall()
                        messages = [{"role": "user", "content": (
                            memory_service.build_fresh_inquiry_context(
                                goal=goal, editable=editable, frozen=frozen,
                                base_sha=base_sha, gate_block=gate_block, hints=hints,
                            ) + "\nFresh reframe: construct an independent account without prior inquiry artifacts or history."
                        )}]
                        tools = self._make_tools(
                            source=source_path, repo=repo_path, history_dir=None,
                            scratch=Path(scratch), memory_service=None,
                            current_round=current_round, history_enabled=False,
                        )
                        continue
                    messages.append({"role": "assistant", "content": reply_text})
                    if not was_history_visible and session.inquiry.history_visible:
                        messages.append({"role": "user", "content": (
                            memory_service.build_history_entry_pack(
                                current_round=current_round,
                            )
                        )})
                        tools = self._make_tools(
                            source=source_path, repo=repo_path,
                            history_dir=run_dir, scratch=Path(scratch),
                            memory_service=memory_service,
                            current_round=current_round, history_enabled=True,
                        )
                    messages.append({
                        "role": "user",
                        "content": self._scientist_state_message(session),
                    })
                    record_phase_wall()
        finally:
            del context.session
            del context.select_quota
            del context.assigned_ops
        return ScientistResult(
            outcome="research_incomplete",
            reason="scientist step budget exhausted before proposal commitment",
            usage=tuple(usages),
            deliberation_telemetry={
                "steps": scientist_steps,
                "usage_by_phase": session.usage_by_phase,
                "wall_time_by_phase": dict(session.wall_time_by_phase),
            },
            trace=self._scientist_trace(session, outcome="research_incomplete"),
        )

