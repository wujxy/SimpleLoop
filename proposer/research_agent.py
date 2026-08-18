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

import hashlib
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
    # The raw model reply of the most recent protocol-violating turn. When a
    # lane dies of protocol failure this is the only durable witness of what
    # the model actually emitted — carried in the trace so the failure is
    # diagnosable from the lane artifacts (omilrec: r26's death message came
    # back without it and the cause stayed invisible).
    last_raw_reply: str = ""
    candidate_directions: str = ""
    located: bool = False
    last_tool_fingerprint: str | None = None


# --- Shared tunables -------------------------------------------------------

_MAX_PROTOCOL_REPAIRS = 5

# The one reminder both repair paths send, kept identical so the model sees
# a stable protocol description no matter what tripped the repair.
_PROTOCOL_REMINDER = (
    "Return exactly one JSON object, with no prose or additional JSON. "
    "Either a single action object — its \"action\" field names the action "
    "and the action's fields sit alongside it, e.g. "
    '{"action":"read_file","path":"/work/..."} — or a batch envelope '
    '{"actions":[<action>, ...]} of up to 8 tool actions executed in '
    "order. A submit action (submit_proposals / submit_self_decision / "
    "submit_reflection_handoff) is always sent ALONE as a single object, "
    'never inside "actions".'
)


def _stamp() -> str:
    """Wall-clock tag for step logs, so per-step latency (model call vs probe
    vs repair churn) is visible end-to-end."""
    return time.strftime("%H:%M:%S")


def _usage_bits(usage) -> str:
    """Render the per-call token facts that explain WHERE time went:
    reasoning tokens (the model thinking) vs output tokens. '' when the
    provider reports no usage."""
    if not isinstance(usage, dict):
        return ""
    bits = []
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        bits.append(f" reasoning_tokens={details['reasoning_tokens']}")
    if usage.get("completion_tokens") is not None:
        bits.append(f" out_tokens={usage['completion_tokens']}")
    return "".join(bits)


# --- Shared helpers --------------------------------------------------------

def _bump(state: WorkingState, name: str) -> None:
    state.counts[name] = state.counts.get(name, 0) + 1


def _fingerprint(action: dict) -> str:
    """Canonical fingerprint of a tool action for exact-repeat detection."""
    name = action["action"]
    if name == "run_research_command":
        return (
            f"{name}:{action.get('cwd')}:{action.get('workdir')}:"
            f"{action['command']}"
        )
    if name == "read_file":
        return (
            f"{name}:{action['path']}:{action.get('offset', 1)}:"
            f"{action.get('limit', 400)}"
        )
    if name == "grep_files":
        return (
            f"{name}:{action['pattern']}:{action.get('path', '/work')}:"
            f"{action.get('glob')}"
        )
    if name == "glob_files":
        return f"{name}:{action['pattern']}:{action.get('path', '/work')}"
    if name == "write_scratch_file":
        digest = hashlib.sha1(action["content"].encode()).hexdigest()[:12]
        return f"{name}:{action['path']}:{digest}"
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
    if name in (
        "run_research_command", "read_file", "grep_files", "glob_files",
    ):
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
        "last_raw_reply": state.last_raw_reply[:400],
    }


def _action_summary(action: dict) -> str:
    name = action["action"]
    if name == "run_research_command":
        return (
            f"action={name} cwd={action.get('cwd')} "
            f"workdir={action.get('workdir')} "
            f"command_chars={len(action['command'])}"
        )
    if name == "read_file":
        return (
            f"action={name} path_chars={len(action['path'])} "
            f"offset={action.get('offset', 1)} "
            f"limit={action.get('limit', 400)}"
        )
    if name == "grep_files":
        return f"action={name} pattern_chars={len(action['pattern'])}"
    if name == "glob_files":
        return f"action={name} pattern_chars={len(action['pattern'])}"
    if name == "write_scratch_file":
        return (
            f"action={name} path_chars={len(action['path'])} "
            f"content_chars={len(action['content'])}"
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

    def _parse_action(self, text: str) -> list[dict]:
        raise NotImplementedError

    def _validate_guard(
        self, state: WorkingState, actions: list[dict], source_root: Path,
    ) -> str | None:
        """Return a repair reason, or None when the actions are valid."""
        return None

    # ---- shared tool loop ----

    def _step(
        self, state: WorkingState, messages: list, system_prompt: str,
        deadline: float, usages: list, step_label: int, *,
        source_root: Path | None = None, steps_budget: int | None = None,
    ) -> tuple[list[dict], str]:
        """One model turn with up to _MAX_PROTOCOL_REPAIRS retries. Returns
        (actions, reply_text) — always a list; a batch reply yields its
        items, a flat reply a one-element list."""
        budget = steps_budget or self.max_steps
        err = self._error_class
        for repair in range(_MAX_PROTOCOL_REPAIRS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise err("agent deadline exceeded")
            call_started = time.monotonic()
            reply = self.model.complete(
                system=system_prompt,
                messages=messages,
                timeout_seconds=remaining,
            )
            call_seconds = time.monotonic() - call_started
            print(
                f"[{_stamp()}] [agent step {step_label}/{budget}] "
                f"model={call_seconds:.0f}s"
                f"{_usage_bits(reply.usage)}",
                flush=True,
            )
            usages.append(reply.usage)
            if (self.usage_observer is not None
                    and reply.usage is not None):
                self.usage_observer(reply.usage)
            try:
                actions = self._parse_action(reply.text)
            except AgentError as exc:
                state.last_raw_reply = reply.text
                if repair == _MAX_PROTOCOL_REPAIRS:
                    raise err(
                        "action protocol failed after "
                        f"{_MAX_PROTOCOL_REPAIRS} repairs; last reply: "
                        f"{reply.text[:400]!r}"
                    ) from None
                reason = self._protocol_reason(exc)
                state.protocol_repairs += 1
                print(
                    f"[{_stamp()}] [agent step {step_label}/{budget}] "
                    f"protocol repair {repair + 1}/{_MAX_PROTOCOL_REPAIRS} "
                    f"reason={reason}",
                    flush=True,
                )
                messages.extend([
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": (
                        "Protocol correction required "
                        f"({reason}). {_PROTOCOL_REMINDER}"
                    )},
                ])
                continue
            guard = self._validate_guard(
                state, actions, source_root or Path("."),
            )
            if guard is not None:
                state.last_raw_reply = reply.text
                if repair == _MAX_PROTOCOL_REPAIRS:
                    raise err(
                        "action protocol failed after "
                        f"{_MAX_PROTOCOL_REPAIRS} repairs; last reply: "
                        f"{reply.text[:400]!r}"
                    ) from None
                state.protocol_repairs += 1
                print(
                    f"[{_stamp()}] [agent step {step_label}/{budget}] "
                    f"protocol repair {repair + 1}/{_MAX_PROTOCOL_REPAIRS} "
                    f"reason={guard}",
                    flush=True,
                )
                messages.extend([
                    {"role": "assistant", "content": reply.text},
                    {"role": "user", "content": (
                        f"Protocol correction required ({guard}). "
                        f"{_PROTOCOL_REMINDER}"
                    )},
                ])
                continue
            for action in actions:
                print(
                    f"[{_stamp()}] [agent step {step_label}/{budget}] "
                    f"{_action_summary(action)}",
                    flush=True,
                )
            return actions, reply.text

    @staticmethod
    def _protocol_reason(exc: AgentError) -> str:
        if isinstance(exc.__cause__, (TypeError, json.JSONDecodeError)):
            return "invalid_json"
        return "invalid_action"
