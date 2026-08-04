"""Proposer Scientist agent: a single-session cognitive state machine.

Doc reference: docs/simpleloop_research_history_memory_redesign.md.
The Proposer wakes each round with no memory of the last. Continuity is
supplied by the persistent Experiment Ledger, Finding Archive, and
Frontier — NOT by summarizing prior candidates.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
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
from ..prompts import load_semantic


class ProposerError(RuntimeError):
    """The Proposer violated its action or budget contract."""


@dataclass(frozen=True)
class ProposerResult:
    """One round's structured output. ``proposals`` is a list of
    ``ResearchProposal`` (instruction + research target); the annotation
    mechanism has been removed."""

    proposals: list[ResearchProposal]
    usage: object = None


# --- Cognitive state machine (doc §6, §11, §12) ---------------------------

class ResearchPhase(str, Enum):
    OBSERVE = "observe"
    INVESTIGATE = "investigate"
    CHECKPOINT = "checkpoint"


@dataclass
class WorkingState:
    """Lightweight per-runtime state. Only `phase` drives control flow in the
    MVP; the richer doc §12 fields are added when something consumes them."""
    phase: ResearchPhase = ResearchPhase.OBSERVE


# Research tools never change phase.
_RESEARCH_TOOL_ACTIONS = frozenset(
    {"run_research_command"} | MEMORY_TOOL_ACTIONS
)

# Actions legal in each phase (doc §11 transition table).
_LEGAL_ACTIONS = {
    ResearchPhase.OBSERVE: _RESEARCH_TOOL_ACTIONS | {"frame_research"},
    ResearchPhase.INVESTIGATE: _RESEARCH_TOOL_ACTIONS | {"conclude_research"},
    ResearchPhase.CHECKPOINT: {
        "submit_proposals", "continue_investigation", "reframe_research",
    },
}

# Where each control action lands.
_TRANSITION_TARGET = {
    ("observe", "frame_research"): ResearchPhase.INVESTIGATE,
    ("investigate", "conclude_research"): ResearchPhase.CHECKPOINT,
    ("checkpoint", "continue_investigation"): ResearchPhase.INVESTIGATE,
    ("checkpoint", "reframe_research"): ResearchPhase.OBSERVE,
    ("checkpoint", "submit_proposals"): None,  # terminal
}


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


# --- Prompt scaffolding ----------------------------------------------------

_PROTOCOL_ENVELOPE = (
    "Runtime contract (immutable):\n"
    "Return exactly one JSON object per response, with no prose outside it."
)

_TERMINAL_ACTION_PROMPT = (
    "Terminal action (only legal from the research checkpoint):\n"
    '- {"action":"submit_proposals","proposals":[\n'
    '    {"instruction":"...", '
    '"research_target":{"mode":"existing","finding_id":"F-NNN"}},\n'
    '    {"instruction":"...", '
    '"research_target":{"mode":"new","question":"...",'
    '"mechanisms":["..."],"code_regions":["..."]}}\n'
    "  ]}\n"
    "  Each proposal must declare either an existing finding "
    "(mode=existing, referencing a known F-NNN) or a new one "
    "(mode=new, with a research question; mechanisms and code_regions "
    "are optional structured tags). There is NO annotations field — "
    "the Ledger and Findings Archive record continuity for you."
)

_CONTROL_ACTION_PROMPT = """Control actions (phase transitions, carry no tool result):
- {"action":"frame_research","observations":["..."],"research_questions":["..."]}
  Leave Observe for Investigate. State what you observed and the questions worth
  spending this round's budget on.
- {"action":"conclude_research","findings":["..."],"remaining_uncertainty":["..."],"decision_basis":"..."}
  Leave Investigate for the research checkpoint. State findings, residual
  uncertainty, and the basis for spending an experiment.
- {"action":"continue_investigation","gap":"...","next_question":"..."}
  From the checkpoint, return to Investigate when evidence is still insufficient.
- {"action":"reframe_research","reason":"...","observation_scope":"..."}
  From the checkpoint, return to Observe when the original framing is wrong."""

_RUNTIME_BOUNDARIES = """Runtime boundaries:
- /source is the accepted revision, /repo is its read-only Git repository,
  /history.jsonl and /rounds are persisted run evidence when present, and
  /scratch is temporary writable space.
- You cannot call the Executor or Harness, edit candidates, choose a parent,
  or declare evaluation and Gate facts. Only Harness records are authoritative.
""".strip()

_BUDGET_REMINDER = (
    "Research budget is nearly exhausted. Use the evidence already gathered "
    "to choose one of: submit the strongest justified proposals; continue only "
    "if one concrete unanswered question can still be resolved within budget."
)


def _runtime_protocol() -> str:
    return "\n\n".join((
        _PROTOCOL_ENVELOPE,
        "Research tools:\n" + render_research_tool_prompt(),
        _CONTROL_ACTION_PROMPT,
        _TERMINAL_ACTION_PROMPT,
        _RUNTIME_BOUNDARIES,
    ))


_MAX_PROTOCOL_REPAIRS = 2


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
    if name == "list_findings":
        return (
            f"phase={phase.value} action={name} "
            f"state={action.get('state', 'active')}"
        )
    if name == "search_findings":
        return (
            f"phase={phase.value} action={name} "
            f"query_chars={len(action.get('query', ''))}"
        )
    if name == "inspect_finding":
        return (
            f"phase={phase.value} action={name} "
            f"finding_id={action.get('finding_id', '')}"
        )
    if name == "search_experiments":
        return (
            f"phase={phase.value} action={name} "
            f"query_chars={len(action.get('query', ''))} "
            f"buckets={bool(action.get('buckets', True))}"
        )
    if name in ("frame_research", "conclude_research",
                "continue_investigation", "reframe_research"):
        tgt = next_phase.value if next_phase is not None else "exit"
        return f"phase={phase.value}->{tgt} action={name}"
    # submit_proposals
    return f"phase={phase.value}->exit action={name} count={len(action['proposals'])}"


def _result_summary(action: dict, observation: dict) -> str:
    parts = [f"result={'ok' if observation.get('ok') else 'error'}"]
    if action["action"] == "run_research_command":
        if "returncode" in observation:
            parts.append(f"exit_code={observation['returncode']}")
        output = observation.get("output")
        if isinstance(output, str):
            parts.append(f"output_chars={len(output)}")
        if "truncated" in observation:
            parts.append(
                f"truncated={str(bool(observation['truncated'])).lower()}"
            )
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
    if set(value) - {"instruction", "research_target"}:
        raise ProposerError(
            f"proposal has unexpected keys: {sorted(value)}"
        )
    instruction = value.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ProposerError("proposal.instruction must be non-empty")
    target = _parse_research_target(value.get("research_target"))
    return ResearchProposal(
        instruction=instruction.strip(),
        research_target=target,
    )


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
            action, {"action", "observations", "research_questions"},
        )
        return {
            "action": name,
            "observations": _require_string_list(
                action["observations"], name="observations",
            ),
            "research_questions": _require_string_list(
                action["research_questions"], name="research_questions",
            ),
        }
    if name == "conclude_research":
        _require_keys(
            action, {"action", "findings", "remaining_uncertainty",
                     "decision_basis"},
        )
        basis = action["decision_basis"]
        if not isinstance(basis, str) or not basis.strip():
            raise ProposerError("decision_basis must be non-empty")
        return {
            "action": name,
            "findings": _require_string_list(
                action["findings"], name="findings",
            ),
            "remaining_uncertainty": _require_string_list(
                action["remaining_uncertainty"],
                name="remaining_uncertainty", allow_empty=True,
            ),
            "decision_basis": basis.strip(),
        }
    if name == "continue_investigation":
        _require_keys(action, {"action", "gap", "next_question"})
        gap = action["gap"]
        question = action["next_question"]
        if not isinstance(gap, str) or not gap.strip():
            raise ProposerError("continue_investigation gap must be non-empty")
        if not isinstance(question, str) or not question.strip():
            raise ProposerError(
                "continue_investigation next_question must be non-empty"
            )
        return {"action": name, "gap": gap.strip(), "next_question": question.strip()}
    if name == "reframe_research":
        _require_keys(action, {"action", "reason", "observation_scope"})
        reason = action["reason"]
        scope = action["observation_scope"]
        if not isinstance(reason, str) or not reason.strip():
            raise ProposerError("reframe_research reason must be non-empty")
        if not isinstance(scope, str) or not scope.strip():
            raise ProposerError(
                "reframe_research observation_scope must be non-empty"
            )
        return {
            "action": name,
            "reason": reason.strip(),
            "observation_scope": scope.strip(),
        }
    if name == "submit_proposals":
        _require_keys(action, {"action", "proposals"})
        proposals = action["proposals"]
        if (not isinstance(proposals, list)
                or len(proposals) != candidates_per_round):
            raise ProposerError(
                f"expected exactly {candidates_per_round} proposals"
            )
        parsed = [_parse_proposal(item) for item in proposals]
        return {"action": name, "proposals": parsed}
    raise ProposerError(f"unknown proposer action: {name}")


def _protocol_reason(exc: ProposerError) -> str:
    if isinstance(exc.__cause__, (TypeError, json.JSONDecodeError)):
        return "invalid_json"
    return "invalid_action"


def _phase_repair_message(phase: ResearchPhase, reason: str) -> str:
    return (
        f"Protocol correction required ({reason}). Current research phase is "
        f"{phase.value}. Legal actions in this phase: "
        f"{_legal_action_list(phase)}. submit_proposals is only legal from the "
        "checkpoint, which you reach via frame_research then conclude_research. "
        "Return exactly one JSON action object matching the Runtime contract, "
        "with no prose or additional JSON."
    )


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
            ),
        }]
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        usages = []
        state = WorkingState()
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
                        print(
                            f"[proposer step {step}/{self.max_steps}] "
                            f"protocol repair {repair + 1}/"
                            f"{_MAX_PROTOCOL_REPAIRS} reason={reason}",
                            flush=True,
                        )
                        messages.extend([
                            {"role": "assistant", "content": reply.text},
                            {"role": "user", "content": (
                                "Protocol correction required "
                                f"({reason}). Return exactly one JSON action "
                                "object matching the Runtime contract, with "
                                "no prose or additional JSON."
                            )},
                        ])
                        continue
                    # Schema OK; now check the phase transition.
                    try:
                        next_phase = _validate_phase_transition(
                            state.phase, action["action"],
                        )
                    except ProposerError as exc:
                        if repair == _MAX_PROTOCOL_REPAIRS:
                            raise ProposerError(
                                "proposer action protocol failed after "
                                f"{_MAX_PROTOCOL_REPAIRS} repairs"
                            ) from exc
                        print(
                            f"[proposer step {step}/{self.max_steps}] "
                            f"protocol repair {repair + 1}/"
                            f"{_MAX_PROTOCOL_REPAIRS} "
                            f"reason=phase_illegal",
                            flush=True,
                        )
                        messages.extend([
                            {"role": "assistant", "content": reply.text},
                            {"role": "user", "content": _phase_repair_message(
                                state.phase, "phase_illegal",
                            )},
                        ])
                        continue
                    break
                print(
                    f"[proposer step {step}/{self.max_steps}] "
                    f"{_action_summary(action, phase=state.phase, next_phase=next_phase)}",
                    flush=True,
                )

                name = action["action"]

                # Terminal action.
                if name == "submit_proposals":
                    print(
                        f"[proposer] finished steps={step} "
                        f"elapsed={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    return ProposerResult(
                        proposals=action["proposals"],
                        usage=usages,
                    )

                # Control actions: internal state transitions, no tool call.
                if name == "frame_research":
                    state.phase = ResearchPhase.INVESTIGATE
                    messages.append({
                        "role": "assistant", "content": reply.text,
                    })
                    continue
                if name == "conclude_research":
                    state.phase = ResearchPhase.CHECKPOINT
                    messages.append({
                        "role": "assistant", "content": reply.text,
                    })
                    continue
                if name == "continue_investigation":
                    state.phase = ResearchPhase.INVESTIGATE
                    messages.append({
                        "role": "assistant", "content": reply.text,
                    })
                    continue
                if name == "reframe_research":
                    state.phase = ResearchPhase.OBSERVE
                    messages.append({
                        "role": "assistant", "content": reply.text,
                    })
                    continue

                # Research tool action.
                observation = tools.execute(action, deadline=deadline)
                print(
                    f"[proposer step {step}/{self.max_steps}] "
                    f"{_result_summary(action, observation)}",
                    flush=True,
                )
                messages.extend([
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": json.dumps(
                        {"tool_result": observation}, ensure_ascii=False,
                    )},
                ])
        raise ProposerError("proposer exceeded researcher.max_steps")
