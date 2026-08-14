"""Narrow adapter for inspecting and committing candidate Git artifacts."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from ..candidate import CandidateArtifact, CommitRequest
from ..harness.workspace import Workspace


class GitArtifactWorkspace:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def inspect(self, worktree: Path) -> tuple[PurePosixPath, ...]:
        return tuple(
            PurePosixPath(path)
            for path in self.workspace.changed_paths(worktree)
        )

    def commit(self, request: CommitRequest) -> CandidateArtifact:
        paths = [path.as_posix() for path in request.changed_paths]
        sha = self.workspace.commit(
            request.worktree,
            f"{request.round_id}-c{request.candidate_id}",
            paths,
        )
        return CandidateArtifact(
            request.parent_sha,
            sha,
            request.changed_paths,
        )
