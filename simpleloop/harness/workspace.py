"""Workspace: per-run git repo isolation, worktree lifecycle, harness-owned
commit, diff. Each run clones the source repo into run_dir/repo (physical
run-to-run isolation); each round edits in a worktree and the harness commits."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(self, run_dir: Path, repo_path: str, baseline_ref: str, editable: list[str]):
        self.run_dir = Path(run_dir)
        self.source_repo = Path(repo_path)
        self.baseline_ref = baseline_ref
        self.editable = editable
        self.repo = self.run_dir / "repo"        # the per-run working repo
        self.wt_root = self.run_dir / "worktrees"
        self.lanes_root = self.run_dir / "lanes"  # proposer lane workspaces
        self._baseline_sha: str | None = None

    # ---- run setup ----

    def setup(self) -> None:
        """Clone the source repo into run_dir/repo. Idempotent."""
        if self.repo.exists():
            return
        self.run_dir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1", "GIT_TERMINAL_PROMPT": "0"}
        self._git_clone_local(self.source_repo, self.repo, env)
        # record baseline now, before any round touches it
        self._baseline_sha = self._git(self.repo, "rev-parse", "--verify",
                                       f"{self.baseline_ref}^{{commit}}")

    def baseline_sha(self) -> str:
        if self._baseline_sha is None:
            self._baseline_sha = self._git(self.repo, "rev-parse", "--verify",
                                           f"{self.baseline_ref}^{{commit}}")
        return self._baseline_sha

    def _git_clone_local(self, src: Path, dst: Path, env: dict) -> None:
        # Prefer --local (hardlinked objects); fall back to a full copy when
        # run_dir is on a different filesystem.
        for args in (
            ["git", "clone", "--local", "--no-checkout", str(src), str(dst)],
            ["git", "clone", "--no-checkout", str(src), str(dst)],
        ):
            completed = subprocess.run(
                args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, check=False,
            )
            if completed.returncode == 0:
                return
        raise WorkspaceError(
            f"git clone failed: {completed.stderr.strip()}\n"
            "(both --local hardlink and full copy failed)"
        )

    # ---- per-round worktree ----

    def add_worktree(self, round_id: int | str, parent_sha: str) -> Path:
        """Create a worktree at parent_sha. Returns its path (the agent's cwd)."""
        self.wt_root.mkdir(parents=True, exist_ok=True)
        wt = self.wt_root / f"r{round_id}"
        branch = f"simpleloop/r{round_id}"
        if wt.exists():
            # stale from a crashed run: drop and recreate
            self._git(self.repo, "worktree", "remove", "--force", str(wt))
        # checkout the baseline content (LFS pointers only, skipped smudge)
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
        completed = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "-B", branch, str(wt), parent_sha],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
        )
        if completed.returncode != 0:
            raise WorkspaceError(f"git worktree add failed: {completed.stderr.strip()}")
        return wt

    def remove_worktree(self, round_id: int | str) -> None:
        wt = self.wt_root / f"r{round_id}"
        if not wt.exists():
            return
        subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(wt)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        # prune the worktree admin metadata
        subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    # ---- proposer lane workspace ----

    def add_lane_workspace(self, lane_id: int | str, base_sha: str) -> Path:
        """Create an independent writable git worktree for one proposer lane at
        ``base_sha``, under ``run_dir/lanes/lane-{lane_id}/workspace``.

        Detached HEAD (no branch): the lane's git activity never creates refs in
        the canonical ``run/repo``. The base_sha tree is materialized as real
        writable files; history is reachable read-only through the worktree's
        shared object store (``.git`` -> ``run/repo/.git``). Returns the
        workspace path (the proposer's cwd)."""
        ws = self.lanes_root / f"lane-{lane_id}" / "workspace"
        if ws.exists():
            # stale from a crashed run: drop and recreate
            subprocess.run(
                ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(ws)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
            # if the admin entry is gone but the dir lingers, clear it so the
            # add below doesn't collide on "already exists"
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)
        ws.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
        completed = subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "add", "--detach", str(ws), base_sha],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
        )
        if completed.returncode != 0:
            raise WorkspaceError(
                f"git worktree add (lane {lane_id}) failed: {completed.stderr.strip()}"
            )
        return ws

    def remove_lane_workspace(self, lane_id: int | str) -> None:
        ws = self.lanes_root / f"lane-{lane_id}" / "workspace"
        if not ws.exists():
            return
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(ws)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    # ---- harness-owned commit ----

    def changed_paths(self, wt: Path) -> list[str]:
        """Dirty/added/deleted tracked + untracked paths in the worktree."""
        completed = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain=v1", "-z",
             "--untracked-files=all"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise WorkspaceError(
                f"git status --porcelain=v1 failed: {completed.stderr.strip()}"
            )
        return _status_paths(completed.stdout)

    def commit(self, wt: Path, round_id: int | str, paths: list[str]) -> str:
        """Stage exactly `paths` and commit. Returns the new SHA."""
        # reset anything the agent staged so the harness controls the index
        self._git(wt, "restore", "--staged", "--", ".")
        self._git(wt, "add", "--", *paths)
        self._git(wt, "-c", "user.name=SimpleLoop", "-c", "user.email=loop@example.invalid",
                  "commit", "-m", f"SimpleLoop round {round_id}")
        sha = self._git(wt, "rev-parse", "HEAD")
        remaining = self.changed_paths(wt)
        if remaining:
            raise WorkspaceError(
                "worktree differs from committed candidate: "
                + ", ".join(remaining)
            )
        return sha

    def diff(self, parent_sha: str, sha: str) -> str:
        """Return the unified diff between two commits."""
        return self._git(self.repo, "diff", f"{parent_sha}..{sha}")

    # ---- git helper ----

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if completed.returncode != 0:
            raise WorkspaceError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
        return completed.stdout.strip()


def _status_paths(status: str) -> list[str]:
    """Parse NUL-delimited porcelain v1, retaining both sides of renames."""
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
