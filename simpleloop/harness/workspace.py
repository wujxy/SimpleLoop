"""Temporary legacy API delegating to :mod:`simpleloop.world.git`."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

from ..world import CommitRequest, SourceWorkspace, WorkspaceError, WorkspaceSpec
from ..world.git import GitWorkspaceProvider, _status_paths


class Workspace(GitWorkspaceProvider):
    """Compatibility surface while production callers migrate in Phase 3."""

    def __init__(
        self,
        run_dir: Path,
        repo_path: str,
        baseline_ref: str,
        editable: list[str],
    ):
        super().__init__(run_dir, repo_path, baseline_ref)
        self.editable = editable

    def setup(self) -> None:
        self.initialize()

    def add_worktree(self, round_id: int | str, parent_sha: str) -> Path:
        return self.create(WorkspaceSpec(str(round_id), parent_sha)).path

    def remove_worktree(self, round_id: int | str) -> None:
        workspace_id = str(round_id)
        self.remove(SourceWorkspace(
            workspace_id,
            self.wt_root / f"r{workspace_id}",
            "",
        ))

    def add_lane_workspace(self, lane_id: int | str, base_sha: str) -> Path:
        return self.create_lane(lane_id, base_sha).path

    def remove_lane_workspace(self, lane_id: int | str) -> None:
        lane = str(lane_id)
        self.remove_lane(SourceWorkspace(
            f"lane-{lane}",
            self.lanes_root / f"lane-{lane}" / "workspace",
            "",
        ))

    def changed_paths(self, wt: Path) -> list[str]:
        workspace = SourceWorkspace("legacy", Path(wt), "")
        return [path.as_posix() for path in self.inspect(workspace).paths]

    def commit(
        self,
        wt: Path,
        round_id: int | str,
        paths: list[str],
    ) -> str:
        workspace = SourceWorkspace("legacy", Path(wt), "")
        artifact = super().commit(
            workspace,
            CommitRequest(
                int(str(round_id).split("-", 1)[0]),
                _candidate_id(round_id),
                "",
                tuple(PurePosixPath(path) for path in paths),
            ),
        )
        return artifact.sha


def _candidate_id(round_id: int | str) -> int:
    text = str(round_id)
    if "-c" not in text:
        return 0
    try:
        return int(text.rsplit("-c", 1)[1])
    except ValueError:
        return 0


__all__ = ("Workspace", "WorkspaceError", "_status_paths")
