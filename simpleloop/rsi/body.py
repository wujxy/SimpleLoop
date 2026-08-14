"""Git-backed storage for immutable run-local proposer revisions."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path, PurePosixPath

from ..world import CommitRequest, SourceWorkspace, WorkspaceSpec
from ..world.git import GitWorkspaceProvider
from .models import SelfCandidate, SelfCommitRequest, SelfRevision


class SelfBodyError(RuntimeError):
    pass


class NoSelfChangeError(SelfBodyError):
    pass


class GitSelfBodyStore:
    """Apply self-specific revision semantics over the shared Git provider."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.repo = self.root / "repo"
        self.runtime_path = self.repo
        self._provider: GitWorkspaceProvider | None = None

    def initialize(self, seed: Path) -> SelfRevision:
        seed = Path(seed)
        if self._is_repo():
            self._ensure_provider()
            return self.initial_revision
        if self.repo.exists():
            raise SelfBodyError(f"self body path is not a Git repository: {self.repo}")
        if not seed.is_dir():
            raise SelfBodyError(f"self body seed does not exist: {seed}")
        self.repo.mkdir(parents=True)
        shutil.copytree(
            seed,
            self.repo / "proposer",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
        )
        (self.repo / ".gitignore").write_text(
            "__pycache__/\n*.pyc\n", encoding="utf-8"
        )
        self._git("init", "--quiet")
        self._git("config", "--local", "user.name", "SimpleLoop-RSI")
        self._git("config", "--local", "user.email", "rsi@simpleloop.local")
        self._git("add", "-A")
        self._git(
            "-c", "core.hooksPath=/dev/null", "commit", "--quiet",
            "-m", "S0: snapshot proposer",
        )
        self._ensure_provider()
        return self.initial_revision

    @property
    def initial_revision(self) -> SelfRevision:
        if not self._is_repo():
            raise SelfBodyError("self body is not initialized")
        roots = self._git("rev-list", "--max-parents=0", "HEAD").splitlines()
        if len(roots) != 1:
            raise SelfBodyError("self body must have exactly one initial revision")
        return SelfRevision(roots[0])

    def prepare_candidate(
        self, round_id: int, parent_sha: str,
    ) -> SourceWorkspace:
        provider = self._ensure_provider()
        workspace_id = f"self-{round_id}"
        path = provider.wt_root / f"r{workspace_id}"
        if path.exists() and self._worktree_is_resumable(path, parent_sha):
            return SourceWorkspace(workspace_id, path, parent_sha)
        if path.exists():
            provider.remove(SourceWorkspace(workspace_id, path, parent_sha))
        return provider.create(WorkspaceSpec(workspace_id, parent_sha))

    def commit_candidate(
        self,
        workspace: SourceWorkspace,
        request: SelfCommitRequest,
    ) -> SelfCandidate:
        provider = self._ensure_provider()
        if workspace.base_sha != request.parent_sha:
            raise SelfBodyError("candidate workspace parent does not match request")
        changes = provider.inspect(workspace).paths
        if changes:
            artifact = provider.commit(
                workspace,
                CommitRequest(
                    request.round_id,
                    0,
                    request.parent_sha,
                    changes,
                ),
            )
            return SelfCandidate(
                artifact.parent_sha,
                artifact.sha,
                artifact.changed_paths,
            )
        head = self._git_at(workspace.path, "rev-parse", "HEAD")
        if head == request.parent_sha:
            raise NoSelfChangeError("self editor made no changes")
        if not self._is_ancestor(request.parent_sha, head):
            raise SelfBodyError("candidate HEAD does not descend from requested parent")
        paths = tuple(
            PurePosixPath(line)
            for line in self._git_at(
                workspace.path,
                "diff", "--name-only", f"{request.parent_sha}..{head}",
            ).splitlines()
            if line
        )
        return SelfCandidate(request.parent_sha, head, paths)

    def discard_candidate(self, workspace: SourceWorkspace) -> None:
        self._ensure_provider().remove(workspace)

    def materialize(self, sha: str) -> Path:
        self._git("rev-parse", "--verify", f"{sha}^{{commit}}")
        self._git("checkout", "--quiet", "--detach", "--force", sha)
        return self.runtime_path

    def _ensure_provider(self) -> GitWorkspaceProvider:
        if not self._is_repo():
            raise SelfBodyError("self body is not initialized")
        if self._provider is None:
            self._provider = GitWorkspaceProvider(
                self.root, self.repo, self.initial_revision.sha,
            )
            self._provider.initialize()
        return self._provider

    def _worktree_is_resumable(self, path: Path, parent_sha: str) -> bool:
        try:
            head = self._git_at(path, "rev-parse", "HEAD")
        except SelfBodyError:
            return False
        return head == parent_sha or self._is_ancestor(parent_sha, head)

    def _is_ancestor(self, parent_sha: str, child_sha: str) -> bool:
        completed = subprocess.run(
            [
                "git", "-C", str(self.repo), "merge-base", "--is-ancestor",
                parent_sha, child_sha,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0

    def _is_repo(self) -> bool:
        return self.repo.is_dir() and (self.repo / ".git").exists()

    def _git(self, *args: str) -> str:
        return self._git_at(self.repo, *args)

    @staticmethod
    def _git_at(path: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            raise SelfBodyError(
                f"git {' '.join(args)} failed: {completed.stderr.strip()}"
            )
        return completed.stdout.strip()
