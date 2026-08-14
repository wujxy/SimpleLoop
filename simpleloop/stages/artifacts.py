"""Narrow adapter for inspecting and committing candidate Git artifacts."""
from __future__ import annotations

from pathlib import PurePosixPath

from ..candidate import CandidateArtifact, CommitRequest
from ..world import SourceWorkspace, WorkspaceProvider


class GitArtifactWorkspace:
    def __init__(self, provider: WorkspaceProvider):
        self.provider = provider

    def inspect(self, workspace: SourceWorkspace) -> tuple[PurePosixPath, ...]:
        return self.provider.inspect(workspace).paths

    def commit(
        self,
        workspace: SourceWorkspace,
        request: CommitRequest,
    ) -> CandidateArtifact:
        return self.provider.commit(workspace, request)
