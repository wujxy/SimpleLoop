"""Materialize a declared production package into an isolated mutable Git repo.

Each candidate may reorganize every file in the package; evaluation assets never
enter this repository. The harness alone creates commits and worktrees.
"""
from __future__ import annotations

import io
import tarfile
import os
import subprocess
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(self, run_dir: Path, repo_path: str, baseline_ref: str, copy_entries: list[str]):
        self.run_dir = Path(run_dir)
        self.source_repo = Path(repo_path)
        self.baseline_ref = baseline_ref
        self.copy_entries = copy_entries
        self.repo = self.run_dir / "repo"        # the per-run working repo
        self.wt_root = self.run_dir / "worktrees"
        self._baseline_sha: str | None = None

    # ---- run setup ----

    def setup(self) -> None:
        """Materialize the declared production package into a private Git repo."""
        if self.repo.exists():
            return
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.repo.mkdir()
        self._archive_seed_entries()
        self._git(self.repo, "init")
        self._git(self.repo, "add", "-A")
        self._git(
            self.repo, "-c", "user.name=SimpleLoop",
            "-c", "user.email=loop@example.invalid",
            "commit", "--allow-empty", "-m", "SimpleLoop workspace baseline",
        )
        self._baseline_sha = self._git(
            self.repo, "rev-parse", "--verify", "HEAD^{commit}")

    def baseline_sha(self) -> str:
        if self._baseline_sha is None:
            self._baseline_sha = self._git(
                self.repo, "rev-parse", "--verify", "HEAD^{commit}")
        return self._baseline_sha

    def _archive_seed_entries(self) -> None:
        completed = subprocess.run(
            [
                "git", "-C", str(self.source_repo), "archive", "--format=tar",
                self.baseline_ref, "--", *self.copy_entries,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if completed.returncode:
            raise WorkspaceError(
                f"git archive failed: {completed.stderr.decode().strip()}")
        root = self.repo.resolve()
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                target = (root / member.name).resolve()
                if target != root and root not in target.parents:
                    raise WorkspaceError(f"archive member escapes workspace: {member.name}")
            archive.extractall(root)

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
