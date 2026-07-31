"""Workspace: per-run git repo isolation, worktree lifecycle, harness-owned
commit, diff. Each run clones the source repo into run_dir/repo (physical
run-to-run isolation); each round edits in a worktree and the harness commits."""
from __future__ import annotations

import os
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

    # ---- harness-owned commit ----

    def changed_paths(self, wt: Path) -> list[str]:
        """Dirty/added/deleted tracked + untracked paths in the worktree."""
        status = self._git(wt, "status", "--porcelain=v1")
        return [p for p in (_status_path(line) for line in status.splitlines() if line.strip()) if p]

    def commit(self, wt: Path, round_id: int | str, paths: list[str]) -> str:
        """Stage exactly `paths` and commit. Returns the new SHA."""
        # reset anything the agent staged so the harness controls the index
        self._git(wt, "restore", "--staged", "--", ".")
        self._git(wt, "add", "--", *paths)
        self._git(wt, "-c", "user.name=SimpleLoop", "-c", "user.email=loop@example.invalid",
                  "commit", "-m", f"SimpleLoop round {round_id}")
        return self._git(wt, "rev-parse", "HEAD")

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


def _status_path(line: str) -> str:
    # porcelain v1: XY <path>; handle renames "R  old -> new"
    if len(line) > 2 and line[2] == " ":
        path = line[3:]
    elif len(line) > 1 and line[1] == " ":
        path = line[2:]
    else:
        path = line[3:] if len(line) > 3 else ""
    if " -> " in path:
        path = path.rsplit(" -> ", 1)[-1]
    return path.strip()
