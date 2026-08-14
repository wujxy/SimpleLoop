"""Mechanism-neutral job scheduling values."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping, Protocol

from ..world import SourceWorkspace
from .envelope import WorkerRequest, WorkerResult


class InfrastructureError(RuntimeError):
    """A job failed outside its business handler."""


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LOST = "lost"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 1
    timeout_seconds: int = 21600
    disappearance_grace_seconds: int = 120

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.timeout_seconds < 1:
            raise ValueError("timeout_seconds must be positive")
        if self.disappearance_grace_seconds < 0:
            raise ValueError("disappearance_grace_seconds cannot be negative")


@dataclass(frozen=True)
class ResourceSpec:
    cpus: int = 1
    memory_mb: int = 0
    requirements: str | None = None
    attributes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class JobSpec:
    request: WorkerRequest
    manifest_path: Path
    result_path: Path
    stdout_path: Path
    stderr_path: Path
    argv: tuple[str, ...]
    retry: RetryPolicy
    resources: ResourceSpec
    workspace: SourceWorkspace | None = None


@dataclass(frozen=True)
class JobHandle:
    scheduler: str
    value: str


@dataclass(frozen=True)
class JobObservation:
    handle: JobHandle
    state: JobState
    detail: str = ""


class Scheduler(Protocol):
    name: str

    def submit(self, job: JobSpec) -> JobHandle: ...

    def inspect(
        self,
        handles: tuple[JobHandle, ...],
    ) -> tuple[JobObservation, ...]: ...

    def cancel(self, handle: JobHandle) -> None: ...


@dataclass(frozen=True)
class JobBatchRequest:
    stage: str
    round_id: int
    context: Mapping[str, object]
    jobs: tuple[JobSpec, ...]
    max_parallel: int = 1

    def __post_init__(self) -> None:
        if self.max_parallel < 1:
            raise ValueError("max_parallel must be at least one")
        ids = [job.request.request_id for job in self.jobs]
        if len(ids) != len(set(ids)):
            raise ValueError("job request ids must be unique")


@dataclass(frozen=True)
class JobOutcome:
    job: JobSpec
    result: WorkerResult | None = None
    infrastructure_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None


@dataclass(frozen=True)
class JobBatchResult:
    outcomes: tuple[JobOutcome, ...]

    @property
    def completed(self) -> tuple[JobOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.succeeded)

    @property
    def failed(self) -> tuple[JobOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if not outcome.succeeded)

