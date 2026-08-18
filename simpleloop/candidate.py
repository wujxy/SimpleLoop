"""Typed business facts produced by one candidate execution."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import PurePosixPath
from typing import Mapping, Protocol

from .stages.proposer import Proposal
from .world.contracts import CommitRequest, SourceWorkspace


class CandidateStatus(str, Enum):
    COMPLETED = "COMPLETED"
    GATE_REJECTED = "GATE_REJECTED"
    EXECUTOR_FAILED = "EXECUTOR_FAILED"
    NO_CHANGE = "NO_CHANGE"
    EVAL_FAILED = "EVAL_FAILED"
    WORKER_FAILED = "WORKER_FAILED"
    BASELINE = "BASELINE"
    # The experimenter session ended without completing the intervention
    # (died, timed out, or ended with no SELF_REPORT). The intervention was
    # never performed: no evaluation runs, the partial diff is committed for
    # traceability, and a harness-signed post-mortem states what happened.
    IMPLEMENTATION_INCOMPLETE = "IMPLEMENTATION_INCOMPLETE"


# Statuses whose candidates never reached evaluation: the intervention was
# not performed. Single source for every consumer (loop all-dead streak,
# Scientist world event / progress pack, Reflection execution-cost views).
NOT_PERFORMED_STATUSES: frozenset[str] = frozenset({
    status.value for status in (
        CandidateStatus.IMPLEMENTATION_INCOMPLETE,
        CandidateStatus.EXECUTOR_FAILED,
        CandidateStatus.WORKER_FAILED,
    )
})


def parse_stop_cause(reason: str | None, default: str = "crashed") -> str:
    """Extract the ``stop_cause=`` the executor stage prefixes into its
    failure reasons. Single reader for the wire protocol both the candidate
    pipeline and the Reflection views parse."""
    text = str(reason or "")
    if "stop_cause=" in text:
        cause = text.split("stop_cause=", 1)[1].split(";", 1)[0].strip()
        if cause:
            return cause
    return default


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    reason: str | None = None
    output: str = ""
    self_report: Mapping[str, object] | None = None


@dataclass(frozen=True)
class CandidateArtifact:
    parent_sha: str
    sha: str
    changed_paths: tuple[PurePosixPath, ...] = ()


@dataclass(frozen=True)
class EvaluationResult:
    text: str
    metrics: Mapping[str, object] = field(default_factory=dict)
    returncodes: tuple[int, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class GateResult:
    passed: bool | None
    detail: str = ""


@dataclass(frozen=True)
class GateDecision:
    results: Mapping[str, GateResult]
    passed: bool
    eligible: bool


@dataclass(frozen=True)
class CandidateResult:
    candidate_id: int
    experiment_id: str
    proposal: Proposal
    parent_sha: str
    status: CandidateStatus
    execution: ExecutionResult
    artifact: CandidateArtifact | None
    evaluation: EvaluationResult | None
    gate: GateDecision
    usage: tuple[Mapping[str, object], ...] = ()
    telemetry: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.artifact and self.artifact.parent_sha != self.parent_sha:
            raise ValueError(
                "artifact parent_sha differs from candidate parent_sha"
            )

    @property
    def sha(self) -> str | None:
        return self.artifact.sha if self.artifact else None

    @property
    def metrics(self) -> Mapping[str, object]:
        return self.evaluation.metrics if self.evaluation else {}

    @property
    def eligible(self) -> bool:
        return self.gate.eligible


@dataclass(frozen=True)
class CandidatePlan:
    candidate_id: int
    parent_sha: str
    proposal: Proposal


@dataclass(frozen=True)
class CandidateBatchRequest:
    round_id: int
    candidates: tuple[CandidatePlan, ...]


@dataclass(frozen=True)
class CandidateBatchResult:
    candidates: tuple[CandidateResult, ...]
    telemetry: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateRequest:
    round_id: int
    candidate_id: int
    parent_sha: str
    proposal: Proposal
    workspace: SourceWorkspace


class ArtifactWorkspace(Protocol):
    def inspect(self, workspace: SourceWorkspace) -> tuple[PurePosixPath, ...]:
        ...

    def commit(
        self,
        workspace: SourceWorkspace,
        request: CommitRequest,
    ) -> CandidateArtifact:
        ...


class CandidateTrace(Protocol):
    def record_execution(
        self,
        request: CandidateRequest,
        execution: ExecutionResult,
        artifact: CandidateArtifact | None,
    ) -> None:
        ...

    def record_evaluation(
        self,
        request: CandidateRequest,
        evaluation: EvaluationResult | None,
        gate: GateDecision,
        status: CandidateStatus,
    ) -> None:
        ...


def _candidate_result(
    request: CandidateRequest,
    *,
    status: CandidateStatus,
    execution: ExecutionResult,
    gate: GateDecision,
    artifact: CandidateArtifact | None = None,
    evaluation: EvaluationResult | None = None,
) -> CandidateResult:
    return CandidateResult(
        candidate_id=request.candidate_id,
        experiment_id=f"r{request.round_id}c{request.candidate_id}",
        proposal=request.proposal,
        parent_sha=request.parent_sha,
        status=status,
        execution=execution,
        artifact=artifact,
        evaluation=evaluation,
        gate=gate,
    )


def candidate_failure_from_request(
    request: CandidateRequest,
    reason: str,
    *,
    gate_spec=None,
    status: CandidateStatus = CandidateStatus.WORKER_FAILED,
) -> CandidateResult:
    from .stages.gate import GateSpec, unavailable_gates

    if not isinstance(gate_spec, GateSpec):
        # Pre-composition failures may only have transport request facts.
        gate_spec = GateSpec("OBJECTIVE", ())
    return _candidate_result(
        request,
        status=status,
        execution=ExecutionResult(status.value, reason=reason),
        gate=unavailable_gates(gate_spec),
    )


def run_candidate(
    request: CandidateRequest,
    *,
    executor,
    artifacts: ArtifactWorkspace,
    evaluator,
    gate_spec,
    trace: CandidateTrace,
) -> CandidateResult:
    """Execute one candidate through the explicit business stages."""
    from .stages.evaluator import EvaluationRequest
    from .stages.executor import ExecutionRequest
    from .stages.gate import apply_gates, unavailable_gates

    execution = executor.execute(ExecutionRequest(
        request.round_id,
        request.candidate_id,
        request.proposal,
        request.workspace,
    ))
    # Death detection (failure-path design §3.1): a session that timed out,
    # crashed, or ended without a parseable SELF_REPORT did not complete the
    # intervention. It is NOT an experiment — no evaluation runs. The partial
    # diff (if any) is committed for traceability and a harness-signed
    # post-mortem states what happened; the Researcher sees the outcome as
    # INTERVENTION_NOT_PERFORMED, never as a tested-and-failed experiment.
    report_outcome = (
        (execution.self_report or {}).get("outcome")
        if isinstance(execution.self_report, dict) else None
    )
    died_silent = (
        execution.status == "EXECUTED"
        and report_outcome not in {"completed", "partial", "blocked"}
    )
    if execution.status == "EXECUTOR_FAILED" or died_silent:
        stop_cause = (
            "session_ended_without_report" if died_silent
            else parse_stop_cause(execution.reason)
        )
        changed = artifacts.inspect(request.workspace)
        artifact = None
        if changed:
            artifact = artifacts.commit(request.workspace, CommitRequest(
                request.round_id,
                request.candidate_id,
                request.parent_sha,
                changed,
            ))
        execution = replace(
            execution,
            status=CandidateStatus.IMPLEMENTATION_INCOMPLETE.value,
            reason=(
                "[harness post-mortem] the experimenter session ended "
                f"without completing the intervention; stop_cause="
                f"{stop_cause}; changed: "
                f"{', '.join(str(p) for p in changed) if changed else 'none'}"
                f"; last words: {execution.output[-300:]!r}"
            ),
        )
        trace.record_execution(request, execution, artifact)
        return _candidate_result(
            request,
            status=CandidateStatus.IMPLEMENTATION_INCOMPLETE,
            execution=execution,
            artifact=artifact,
            gate=unavailable_gates(
                gate_spec,
                reason=(
                    "not run because the intervention was never completed "
                    f"({stop_cause})"
                ),
            ),
        )

    changed_paths = artifacts.inspect(request.workspace)
    if not changed_paths:
        execution = replace(
            execution,
            status=CandidateStatus.NO_CHANGE.value,
            reason="executor made no changes",
        )
        trace.record_execution(request, execution, None)
        gate = apply_gates(
            None,
            gate_spec,
            skip_reason="not run because Executor produced no change",
        )
        return _candidate_result(
            request,
            status=CandidateStatus.NO_CHANGE,
            execution=execution,
            gate=gate,
        )

    artifact = artifacts.commit(
        request.workspace,
        CommitRequest(
            request.round_id,
            request.candidate_id,
            request.parent_sha,
            changed_paths,
        ),
    )
    execution = replace(execution, status="COMMITTED")
    trace.record_execution(request, execution, artifact)
    evaluation = evaluator.evaluate(EvaluationRequest(request.workspace))
    gate = apply_gates(evaluation, gate_spec)
    if evaluation.error is not None:
        status = CandidateStatus.EVAL_FAILED
    elif gate.passed:
        status = CandidateStatus.COMPLETED
    else:
        status = CandidateStatus.GATE_REJECTED
    trace.record_evaluation(request, evaluation, gate, status)
    return _candidate_result(
        request,
        status=status,
        execution=execution,
        artifact=artifact,
        evaluation=evaluation,
        gate=gate,
    )


def run_candidate_guarded(
    request: CandidateRequest,
    *,
    executor,
    artifacts: ArtifactWorkspace,
    evaluator,
    gate_spec,
    trace: CandidateTrace,
) -> CandidateResult:
    """Normalize an unexpected pipeline exception as one terminal result."""
    from .stages.gate import unavailable_gates

    try:
        return run_candidate(
            request,
            executor=executor,
            artifacts=artifacts,
            evaluator=evaluator,
            gate_spec=gate_spec,
            trace=trace,
        )
    except Exception as exc:
        return _candidate_result(
            request,
            status=CandidateStatus.WORKER_FAILED,
            execution=ExecutionResult(
                CandidateStatus.WORKER_FAILED.value,
                reason=f"candidate worker failed: {exc}",
            ),
            gate=unavailable_gates(gate_spec),
        )
