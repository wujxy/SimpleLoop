"""Typed business facts produced by one candidate execution."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Mapping

from .stages.proposer import Proposal


class CandidateStatus(str, Enum):
    COMPLETED = "COMPLETED"
    GATE_REJECTED = "GATE_REJECTED"
    EXECUTOR_FAILED = "EXECUTOR_FAILED"
    NO_CHANGE = "NO_CHANGE"
    EVAL_FAILED = "EVAL_FAILED"
    WORKER_FAILED = "WORKER_FAILED"
    BASELINE = "BASELINE"


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
