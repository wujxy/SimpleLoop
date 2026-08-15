"""Candidate worker JSON boundary.

The worker protocol remains compatible with v0 artifacts. Inside the Host,
candidate business state is represented only by :class:`CandidateResult`.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Mapping

from ..candidate import (
    CandidateArtifact,
    CandidateResult,
    CandidateStatus,
    EvaluationResult,
    ExecutionResult,
    GateDecision,
    GateResult,
)
from ..stages.proposer import Proposal


class ProtocolError(RuntimeError):
    """A durable artifact does not satisfy its declared wire contract."""


def _required(raw: Mapping[str, object], key: str) -> object:
    try:
        return raw[key]
    except KeyError as exc:
        raise ProtocolError(f"missing required key: {key}") from exc


def _string(raw: Mapping[str, object], key: str, *, required: bool) -> str | None:
    value = _required(raw, key) if required else raw.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ProtocolError(f"{key} must be a string")
    return value


def _boolean(raw: Mapping[str, object], key: str) -> bool:
    value = _required(raw, key)
    if not isinstance(value, bool):
        raise ProtocolError(f"{key} must be a boolean")
    return value


def decode_candidate_result(raw: Mapping[str, object]) -> CandidateResult:
    """Validate and decode one v0 candidate ``result.json`` object."""
    if not isinstance(raw, Mapping):
        raise ProtocolError("candidate result must be an object")

    candidate_id = _required(raw, "candidate")
    if not isinstance(candidate_id, int) or isinstance(candidate_id, bool):
        raise ProtocolError("candidate must be an integer")
    proposal = _string(raw, "proposal", required=True)
    parent_sha = _string(raw, "parent_sha", required=True)

    status_value = _string(raw, "status", required=True)
    try:
        status = CandidateStatus(status_value)
    except ValueError as exc:
        raise ProtocolError(f"status is unknown: {status_value}") from exc

    experiment_id = raw.get("experiment_id", f"candidate-{candidate_id}")
    if not isinstance(experiment_id, str):
        raise ProtocolError("experiment_id must be a string")
    sha = _string(raw, "sha", required=False)

    paths = raw.get("changed_paths", [])
    if not isinstance(paths, list) or not all(
        isinstance(path, str) for path in paths
    ):
        raise ProtocolError("changed_paths must be a list of strings")

    metrics = raw.get("metrics", {})
    if not isinstance(metrics, Mapping):
        raise ProtocolError("metrics must be an object")

    gates = raw.get("gates", {})
    if not isinstance(gates, Mapping):
        raise ProtocolError("gates must be an object")
    gate_results: dict[str, GateResult] = {}
    for name, row in gates.items():
        label = f"gates.{name}"
        if not isinstance(name, str) or not isinstance(row, Mapping):
            raise ProtocolError(f"{label} must be an object")
        passed = row.get("passed")
        detail = row.get("detail", "")
        if passed is not None and not isinstance(passed, bool):
            raise ProtocolError(f"{label}.passed must be boolean or null")
        if not isinstance(detail, str):
            raise ProtocolError(f"{label}.detail must be a string")
        gate_results[name] = GateResult(passed, detail)

    gate_passed = _boolean(raw, "gate_passed")
    eligible = _boolean(raw, "eligible")
    eval_block = raw.get("eval_block", "")
    if not isinstance(eval_block, str):
        raise ProtocolError("eval_block must be a string")
    self_report = raw.get("self_report")
    if self_report is not None and not isinstance(self_report, Mapping):
        raise ProtocolError("self_report must be an object or null")
    telemetry = raw.get("telemetry", {})
    if not isinstance(telemetry, Mapping):
        raise ProtocolError("telemetry must be an object")

    artifact = (
        CandidateArtifact(
            parent_sha=parent_sha,
            sha=sha,
            changed_paths=tuple(PurePosixPath(path) for path in paths),
        )
        if sha is not None else None
    )
    evaluated = status in {
        CandidateStatus.COMPLETED,
        CandidateStatus.GATE_REJECTED,
        CandidateStatus.EVAL_FAILED,
        CandidateStatus.BASELINE,
    }
    evaluation = (
        EvaluationResult(
            text=eval_block,
            metrics=dict(metrics),
            error=eval_block if status is CandidateStatus.EVAL_FAILED else None,
        )
        if evaluated else None
    )
    failed = status in {
        CandidateStatus.EXECUTOR_FAILED,
        CandidateStatus.WORKER_FAILED,
        CandidateStatus.IMPLEMENTATION_INCOMPLETE,
    }
    reason = (
        eval_block if failed
        else "Executor produced no change"
        if status is CandidateStatus.NO_CHANGE else None
    )
    execution_status = "COMMITTED" if sha else status.value

    return CandidateResult(
        candidate_id=candidate_id,
        experiment_id=experiment_id,
        proposal=Proposal(proposal),
        parent_sha=parent_sha,
        status=status,
        execution=ExecutionResult(
            status=execution_status,
            reason=reason,
            self_report=dict(self_report) if self_report is not None else None,
        ),
        artifact=artifact,
        evaluation=evaluation,
        gate=GateDecision(gate_results, gate_passed, eligible),
        telemetry=dict(telemetry),
    )


def encode_candidate_result(
    result: CandidateResult,
    *,
    selected: bool = False,
) -> dict[str, object]:
    """Encode one candidate using the existing v0 worker wire shape."""
    eval_block = result.evaluation.text if result.evaluation else ""
    if (
        not eval_block
        and result.status in {
            CandidateStatus.EXECUTOR_FAILED,
            CandidateStatus.WORKER_FAILED,
            CandidateStatus.IMPLEMENTATION_INCOMPLETE,
        }
    ):
        reason = result.execution.reason or ""
        eval_block = (
            reason if reason.startswith("[")
            else f"[loop failure] {reason[:200]}"
        )
    return {
        "candidate": result.candidate_id,
        "experiment_id": result.experiment_id,
        "proposal": result.proposal.instruction,
        "parent_sha": result.parent_sha,
        "sha": result.sha,
        "status": result.status.value,
        "eval_block": eval_block,
        "metrics": dict(result.metrics),
        "changed_paths": [
            path.as_posix()
            for path in (
                result.artifact.changed_paths if result.artifact else ()
            )
        ],
        "gates": {
            name: {"passed": gate.passed, "detail": gate.detail}
            for name, gate in result.gate.results.items()
        },
        "gate_passed": result.gate.passed,
        "eligible": result.gate.eligible,
        "selected": selected,
        "self_report": (
            dict(result.execution.self_report)
            if result.execution.self_report is not None else None
        ),
    }
