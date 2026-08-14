"""Mechanism-neutral contracts for source state and sandboxed processes."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import ContextManager, Mapping, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from ..candidate import CandidateArtifact


class WorkspaceError(RuntimeError):
    pass


class SandboxError(RuntimeError):
    pass


class SandboxLaunchError(SandboxError):
    pass


@dataclass(frozen=True)
class WorkspaceSpec:
    workspace_id: str
    revision: str


@dataclass(frozen=True)
class SourceWorkspace:
    workspace_id: str
    path: Path
    base_sha: str


@dataclass(frozen=True)
class ChangeSet:
    paths: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class CommitRequest:
    round_id: int
    candidate_id: int
    parent_sha: str
    changed_paths: tuple[PurePosixPath, ...]


class WorkspaceProvider(Protocol):
    def initialize(self) -> str:
        ...

    def create(self, spec: WorkspaceSpec) -> SourceWorkspace:
        ...

    def remove(self, workspace: SourceWorkspace) -> None:
        ...

    def reset(self, workspace: SourceWorkspace) -> None:
        ...

    def open(self, spec: WorkspaceSpec) -> ContextManager[SourceWorkspace]:
        ...

    def inspect(self, workspace: SourceWorkspace) -> ChangeSet:
        ...

    def commit(
        self,
        workspace: SourceWorkspace,
        request: CommitRequest,
    ) -> "CandidateArtifact":
        ...

    def diff(self, parent_sha: str, child_sha: str) -> str:
        ...


class MountMode(str, Enum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


@dataclass(frozen=True)
class MountSpec:
    source: Path
    target: PurePosixPath
    mode: MountMode = MountMode.READ_ONLY


@dataclass(frozen=True)
class SandboxSpec:
    image: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    network: bool = True


@dataclass(frozen=True)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: PurePosixPath
    timeout_seconds: int
    stdin: str | None = None
    label: str = ""


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


class ExecutionSandbox(Protocol):
    def run(self, request: ProcessRequest) -> ProcessResult:
        ...


class SandboxProvider(Protocol):
    def bind(
        self,
        spec: SandboxSpec,
        mounts: tuple[MountSpec, ...],
    ) -> ExecutionSandbox:
        ...
