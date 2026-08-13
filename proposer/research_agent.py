"""Shared infrastructure for code-reading research agents.

Both the Generator (hypothesis producer) and the Cognitive element (sieve +
enrich) are agents that can read code via ``run_research_command``. They share
the same tool loop, protocol-repair logic, and state tracking — they differ
only in prompt (role-specific semantics), context (history-free vs
history-rich), and terminal actions (``submit_hypothesis`` vs
``submit_proposals``/``block``).

This module provides the shared base class ``ResearchAgent`` and the helpers
both agents use. Each subclass plugs its own ``_parse_action`` and
``_validate_action_guard``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from .model import ChatModel
from .runtime import ApptainerRuntime


class AgentError(RuntimeError):
    """Shared base for agent protocol/contract errors."""


# --- Round-local state -----------------------------------------------------

@dataclass
class WorkingState:
    """Agent-local state. Lives only in this round; never written to the
    Ledger or Finding archive. Drives the state header and telemetry."""
    counts: dict = field(default_factory=dict)
    session_evidence: set[str] = field(default_factory=set)
    new_evidence: set[str] = field(default_factory=set)
    action_log: list[dict] = field(default_factory=list)
    protocol_repairs: int = 0
    candidate_directions: str = ""
    current_information_goal: str = ""
    located: bool = False
    last_tool_fingerprint: str | None = None


# --- Shared tunables -------------------------------------------------------

_MAX_PROTOCOL_REPAIRS = 2


# --- Shared helpers --------------------------------------------------------

def _bump(state: WorkingState, name: str) -> None:
    state.counts[name] = state.counts.get(name, 0) + 1


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


def _iter_experiment_hits(result) -> list:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        out: list = []
        for key in ("relevant", "contrasting", "diverse"):
            out.extend(result.get(key) or [])
        return out
    return []


def _register_evidence(state: WorkingState, action: dict, observation: dict) -> None:
    """Record the references a successful tool call made available to cite."""
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


def _build_telemetry(
    state: WorkingState, *, steps: int, outcome: str,
    reason_kind: str | None = None, enrichment_partial: bool = False,
) -> dict:
    return {
        "steps": steps,
        "tool_calls": state.counts.get("tool", 0),
        "source_reads": state.counts.get("source_read", 0),
        "protocol_repairs": state.protocol_repairs,
        "compactions": state.counts.get("compact", 0),
        "outcome": outcome,
        "reason_kind": reason_kind,
        "enrichment_partial": enrichment_partial,
    }


def _build_trace(
    state: WorkingState, *, round_id: int, outcome: str,
    reason_kind: str | None = None, evidence_refs: tuple[str, ...] = (),
) -> dict:
    return {
        "round": round_id,
        "candidate_directions": state.candidate_directions,
        "actions": list(state.action_log),
        "outcome": outcome,
        "reason_kind": reason_kind,
        "evidence_refs": list(evidence_refs),
    }


def _action_summary(action: dict) -> str:
    name = action["action"]
    if name == "run_research_command":
        return (
            f"action={name} cwd={action['cwd']} "
            f"command_chars={len(action['command'])}"
        )
    if name == "inspect_episode":
        return f"action={name} ref_chars={len(action['ref'])}"
    if name in ("list_findings", "search_findings", "inspect_finding",
                "search_experiments"):
        extra = ""
        if "query" in action:
            extra = f" query_chars={len(action.get('query', ''))}"
        elif "finding_id" in action:
            extra = f" finding_id={action.get('finding_id', '')}"
        return f"action={name}{extra}"
    if name == "submit_proposals":
        return f"action={name} count={len(action['proposals'])}"
    if name == "submit_hypothesis":
        return f"action={name}"
    if name == "block":
        return f"action={name} reason_kind={action['reason_kind']}"
    return f"action={name}"


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


# --- The shared agent base -------------------------------------------------

class ResearchAgent:
    """Base for code-reading agents. Owns the model, runtime, tool loop, and
    protocol-repair logic. Subclasses provide ``_parse_action`` and
    ``_validate_guard``."""

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

    # ---- to be provided by subclasses ----

    _error_class = AgentError

    def _parse_action(self, text: str) -> dict:
        raise NotImplementedError

    def _validate_guard(
        self, state: WorkingState, action: dict, source_root: Path,
    ) -> str | None:
        """Return a repair reason, or None when the action is valid."""
        return None

    # ---- shared tool loop ----

    def _step(
        self, state: WorkingState, messages: list, system_prompt: str,
        deadline: float, usages: list, step_label: int, *,
        source_root: Path | None = None, steps_budget: int | None = None,
    ) -> tuple[dict, str]:
        """One model turn with up to _MAX_PROTOCOL_REPAIRS retries. Returns
        (action, reply_text)."""
        budget = steps_budget or self.max_steps
        err = self._error_class
        for repair in range(_MAX_PROTOCOL_REPAIRS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise err("agent deadline exceeded")
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
                action = self._parse_action(reply.text)
            except AgentError as exc:
                if repair == _MAX_PROTOCOL_REPAIRS:
                    raise err(
                        "action protocol failed after "
                        f"{_MAX_PROTOCOL_REPAIRS} repairs"
                    ) from None
                reason = self._protocol_reason(exc)
                state.protocol_repairs += 1
                print(
                    f"[agent step {step_label}/{budget}] "
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
            guard = self._validate_guard(
                state, action, source_root or Path("."),
            )
            if guard is not None:
                if repair == _MAX_PROTOCOL_REPAIRS:
                    raise err(
                        "action protocol failed after "
                        f"{_MAX_PROTOCOL_REPAIRS} repairs"
                    ) from None
                state.protocol_repairs += 1
                print(
                    f"[agent step {step_label}/{budget}] "
                    f"protocol repair {repair + 1}/{_MAX_PROTOCOL_REPAIRS} "
                    f"reason={guard}",
                    flush=True,
                )
                messages.extend([
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": (
                        f"Protocol correction required ({guard}). "
                        "Return exactly one JSON action object matching the "
                        "Runtime contract, with no prose or additional JSON."
                    )},
                ])
                continue
            print(
                f"[agent step {step_label}/{budget}] "
                f"{_action_summary(action)}",
                flush=True,
            )
            return action, reply.text

    @staticmethod
    def _protocol_reason(exc: AgentError) -> str:
        if isinstance(exc.__cause__, (TypeError, json.JSONDecodeError)):
            return "invalid_json"
        return "invalid_action"
