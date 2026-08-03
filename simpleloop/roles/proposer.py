"""Proposer Scientist agent: a single-session cognitive state machine."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory

from .model import ChatModel
from .research_tools import (
    ResearchTools,
    render_history_directory,
    render_research_tool_prompt,
)
from ..container.runtime import ApptainerRuntime
from ..prompts import load_semantic


class ProposerError(RuntimeError):
    """The Proposer violated its action or budget contract."""


@dataclass(frozen=True)
class ProposerResult:
    proposals: list[str]
    annotations: list[dict]
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
_RESEARCH_TOOL_ACTIONS = frozenset((
    "run_research_command",
    "inspect_episode",
))

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
    '- {"action":"submit_proposals","proposals":["..."],'
    '"annotations":[{"ref":"rNcM","text":"1-200 chars"}, ...]}\n'
    "  annotations summarize the PREVIOUS round's candidates: one entry per"
    " prior candidate, each with a valid ref and a non-empty note. Use []"
    " on the first round (no previous candidates)."
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


def _parse_annotations(value, *, expected_refs: set[str]) -> list[dict]:
    """Validate submit_proposals.annotations: one {ref, text} per prior
    candidate. ``expected_refs`` is empty on the first round (no prior
    candidates), in which case annotations must be an empty list."""
    if not isinstance(value, list):
        raise ProposerError("annotations must be a list")
    if not expected_refs:
        if value:
            raise ProposerError(
                "annotations must be empty when there are no prior candidates"
            )
        return []
    if len(value) != len(expected_refs):
        raise ProposerError(
            f"annotations must have one entry per prior candidate "
            f"(expected {len(expected_refs)}, got {len(value)})"
        )
    seen: set[str] = set()
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"ref", "text"}:
            raise ProposerError(
                "each annotation must be an object with ref and text"
            )
        ref = item["ref"]
        text = item["text"]
        if not isinstance(ref, str) or ref not in expected_refs:
            raise ProposerError(
                f"annotation ref {ref!r} is not a known prior candidate ref"
            )
        if ref in seen:
            raise ProposerError(f"annotation ref {ref!r} appears more than once")
        seen.add(ref)
        if not isinstance(text, str) or not text.strip():
            raise ProposerError("annotation text must be non-empty")
        text = text.strip()
        if len(text) > 200:
            raise ProposerError("annotation text must be at most 200 characters")
        out.append({"ref": ref, "text": text})
    return out


def _parse_action(
    text: str, candidates_per_round: int, prior_refs: set[str],
) -> dict:
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
        _require_keys(action, {"action", "proposals", "annotations"})
        proposals = action["proposals"]
        if (not isinstance(proposals, list)
                or len(proposals) != candidates_per_round):
            raise ProposerError(
                f"expected exactly {candidates_per_round} proposals"
            )
        normalized = []
        for proposal in proposals:
            if not isinstance(proposal, str) or not proposal.strip():
                raise ProposerError("proposals must be nonblank strings")
            normalized.append(proposal.strip())
        annotations = _parse_annotations(
            action["annotations"], expected_refs=prior_refs,
        )
        return {
            "action": name,
            "proposals": normalized,
            "annotations": annotations,
        }
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
        history: list[dict],
        base_sha: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        candidates_per_round: int,
        gate_block: str,
        prompt_dir: Path | None,
    ) -> ProposerResult:
        prior_refs = _prior_candidate_refs(history)
        system_prompt = (
            f"{load_semantic('proposer', prompt_dir).rstrip()}\n\n"
            f"{_runtime_protocol()}"
        )
        messages = [{
            "role": "user",
            "content": _initial_context(
                goal=goal,
                editable=editable,
                frozen=frozen,
                history=history,
                base_sha=base_sha,
                candidates_per_round=candidates_per_round,
                gate_block=gate_block,
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
                history=history,
                command_timeout_seconds=self.command_timeout_seconds,
                command_output_cap_chars=self.command_output_cap_chars,
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
                            prior_refs=prior_refs,
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
                        annotations=action["annotations"],
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


def _prior_candidate_refs(history: list[dict]) -> set[str]:
    """Refs of every candidate in the most recent recorded round, or empty
    on the first round. Annotations must cover exactly these refs."""
    if not history:
        return set()
    last = max(
        (r for r in history if isinstance(r, dict)),
        key=lambda r: r.get("round", 0),
    )
    refs: set[str] = set()
    for candidate in last.get("candidates") or []:
        refs.add(f"r{last.get('round', 0)}c{candidate.get('candidate', 0)}")
    return refs


def _initial_context(
    *,
    goal: str,
    editable: list[str],
    frozen: list[str],
    history: list[dict],
    base_sha: str,
    candidates_per_round: int,
    gate_block: str,
) -> str:
    return f"""Research objective:
{goal}

Harness Gates:
{gate_block or "(declared in factual records)"}

Current accepted revision: {base_sha}
Editable paths: {json.dumps(editable, ensure_ascii=False)}
Frozen paths: {json.dumps(frozen, ensure_ascii=False)}

Lab notebook directory (every prior experiment as `ref: note`; the latest
round has no notes yet — that is this round's job. inspect_episode by ref for
full detail):
{render_history_directory(history)}

Submit exactly {candidates_per_round} nonblank executable proposal(s).
Every candidate begins from the accepted revision above.
"""