"""Git-backed source workspace provider."""
from __future__ import annotations

import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..candidate import CandidateArtifact
from .contracts import (
    ChangeSet,
    CommitRequest,
    SourceWorkspace,
    WorkspaceError,
    WorkspaceSpec,
)


class GitWorkspaceProvider:
    def __init__(
        self,
        run_dir: str | Path,
        repo_path: str | Path,
        baseline_ref: str,
    ):
        self.run_dir = Path(run_dir)
        self.source_repo = Path(repo_path)
        self.baseline_ref = baseline_ref
        self.repo = self.run_dir / "repo"
        self.wt_root = self.run_dir / "worktrees"
        self.lanes_root = self.run_dir / "lanes"
        self._baseline_sha: str | None = None

    def initialize(self) -> str:
        if not self.repo.exists():
            self.run_dir.mkdir(parents=True, exist_ok=True)
            env = {
                **os.environ,
                "GIT_LFS_SKIP_SMUDGE": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
            self._clone(env)
        return self.baseline_sha()

    def baseline_sha(self) -> str:
        if self._baseline_sha is None:
            self._baseline_sha = self._git(
                self.repo,
                "rev-parse",
                "--verify",
                f"{self.baseline_ref}^{{commit}}",
            )
        return self._baseline_sha

    def _clone(self, env: dict[str, str]) -> None:
        completed = None
        for args in (
            ["git", "clone", "--local", "--no-checkout", str(self.source_repo), str(self.repo)],
            ["git", "clone", "--no-checkout", str(self.source_repo), str(self.repo)],
        ):
            completed = subprocess.run(
                args,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )
            if completed.returncode == 0:
                return
        detail = completed.stderr.strip() if completed else "unknown error"
        raise WorkspaceError(
            f"git clone failed: {detail}\n"
            "(both --local hardlink and full copy failed)"
        )

    def create(self, spec: WorkspaceSpec) -> SourceWorkspace:
        self._validate_id(spec.workspace_id)
        self.wt_root.mkdir(parents=True, exist_ok=True)
        path = self.wt_root / f"r{spec.workspace_id}"
        branch = f"simpleloop/r{spec.workspace_id}"
        self._drop_stale(path)
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
        completed = subprocess.run(
            [
                "git", "-C", str(self.repo), "worktree", "add",
                "-B", branch, str(path), spec.revision,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        if completed.returncode:
            raise WorkspaceError(
                f"git worktree add failed: {completed.stderr.strip()}"
            )
        base_sha = self._git(path, "rev-parse", "HEAD")
        return SourceWorkspace(spec.workspace_id, path, base_sha)

    @contextmanager
    def open(self, spec: WorkspaceSpec):
        workspace = self.create(spec)
        try:
            yield workspace
        finally:
            self.remove(workspace)

    def remove(self, workspace: SourceWorkspace) -> None:
        self._remove_path(workspace.path)

    def reset(self, workspace: SourceWorkspace) -> None:
        """Return a workspace to its pristine base revision.

        Retry hygiene: an agent that timed out or lost its node may have left
        half-finished edits behind, and the next attempt must not build on
        top of them. Works for both worktree schemes (candidates and lanes)
        because it only touches the working tree in place.
        """
        if not workspace.path.exists():
            return
        self._git(workspace.path, "reset", "--hard", workspace.base_sha or "HEAD")
        self._git(workspace.path, "clean", "-fd")

    def create_lane(
        self,
        lane_id: int | str,
        revision: str,
    ) -> SourceWorkspace:
        lane = str(lane_id)
        self._validate_id(lane)
        path = self.lanes_root / f"lane-{lane}" / "workspace"
        self._drop_stale(path, clear_lingering=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
        completed = subprocess.run(
            [
                "git", "-C", str(self.repo), "worktree", "add",
                "--detach", str(path), revision,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
        )
        if completed.returncode:
            raise WorkspaceError(
                f"git worktree add (lane {lane}) failed: "
                f"{completed.stderr.strip()}"
            )
        return SourceWorkspace(
            f"lane-{lane}",
            path,
            self._git(path, "rev-parse", "HEAD"),
        )

    def remove_lane(self, workspace: SourceWorkspace) -> None:
        self._remove_path(workspace.path)

    def inspect(self, workspace: SourceWorkspace) -> ChangeSet:
        completed = subprocess.run(
            [
                "git", "-C", str(workspace.path), "status",
                "--porcelain=v1", "-z", "--untracked-files=all",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            raise WorkspaceError(
                "git status --porcelain=v1 failed: "
                f"{completed.stderr.strip()}"
            )
        return ChangeSet(tuple(
            PurePosixPath(path) for path in _status_paths(completed.stdout)
        ))

    def commit(
        self,
        workspace: SourceWorkspace,
        request: CommitRequest,
    ) -> CandidateArtifact:
        paths = [path.as_posix() for path in request.changed_paths]
        self._git(workspace.path, "restore", "--staged", "--", ".")
        self._git(workspace.path, "add", "--", *paths)
        self._git(
            workspace.path,
            "-c", "user.name=SimpleLoop",
            "-c", "user.email=loop@example.invalid",
            "commit", "-m",
            f"SimpleLoop round {request.round_id}-c{request.candidate_id}",
        )
        sha = self._git(workspace.path, "rev-parse", "HEAD")
        remaining = self.inspect(workspace).paths
        if remaining:
            raise WorkspaceError(
                "worktree differs from committed candidate: "
                + ", ".join(path.as_posix() for path in remaining)
            )
        return CandidateArtifact(request.parent_sha, sha, request.changed_paths)

    def _drop_stale(self, path: Path, *, clear_lingering: bool = False) -> None:
        if not path.exists():
            return
        subprocess.run(
            [
                "git", "-C", str(self.repo), "worktree", "remove",
                "--force", str(path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if clear_lingering and path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def _remove_path(self, path: Path) -> None:
        resolved = path.resolve()
        roots = (self.wt_root.resolve(), self.lanes_root.resolve())
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise WorkspaceError(
                f"workspace path is outside provider roots: {path}"
            )
        if not path.exists():
            return
        subprocess.run(
            [
                "git", "-C", str(self.repo), "worktree", "remove",
                "--force", str(path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "prune"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    @staticmethod
    def _validate_id(workspace_id: str) -> None:
        if (
            not workspace_id
            or workspace_id in {".", ".."}
            or "/" in workspace_id
            or "\\" in workspace_id
        ):
            raise WorkspaceError(f"invalid workspace id: {workspace_id!r}")

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            raise WorkspaceError(
                f"git {' '.join(args)} failed: {completed.stderr.strip()}"
            )
        return completed.stdout.strip()


def _status_paths(status: str) -> list[str]:
    """Parse NUL-delimited porcelain v1, retaining both rename paths."""
    fields = status.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        code = field[:2]
        path = field[3:] if len(field) > 3 else ""
        if path and path not in paths:
            paths.append(path)
        if "R" in code or "C" in code:
            if index < len(fields):
                source = fields[index]
                index += 1
                if source and source not in paths:
                    paths.append(source)
    return paths
