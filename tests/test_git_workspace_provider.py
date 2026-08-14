from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from simpleloop.world import (
    CommitRequest,
    SourceWorkspace,
    WorkspaceError,
    WorkspaceSpec,
)
from simpleloop.world.git import GitWorkspaceProvider


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout.strip()


def _make_provider(tmp_path: Path) -> tuple[GitWorkspaceProvider, str]:
    source = tmp_path / "source"
    _git("init", str(source))
    _git("-C", str(source), "config", "user.name", "Test")
    _git("-C", str(source), "config", "user.email", "test@example.invalid")
    (source / "src").mkdir()
    (source / "src" / "a.py").write_text("old\n", encoding="utf-8")
    _git("-C", str(source), "add", "-A")
    _git("-C", str(source), "commit", "-m", "baseline")
    base = _git("-C", str(source), "rev-parse", "HEAD")
    provider = GitWorkspaceProvider(tmp_path / "run", source, "HEAD")
    assert provider.initialize() == base
    return provider, base


def test_open_creates_and_always_removes_workspace(tmp_path: Path):
    provider, base = _make_provider(tmp_path)

    with pytest.raises(RuntimeError, match="stop"):
        with provider.open(WorkspaceSpec("0-c0", base)) as workspace:
            assert workspace.path.exists()
            raise RuntimeError("stop")

    assert not workspace.path.exists()


def test_commit_returns_typed_artifact_and_keeps_sha_reachable(tmp_path: Path):
    provider, base = _make_provider(tmp_path)

    with provider.open(WorkspaceSpec("0-c0", base)) as workspace:
        (workspace.path / "src" / "a.py").write_text("new\n", encoding="utf-8")
        changes = provider.inspect(workspace)
        artifact = provider.commit(
            workspace,
            CommitRequest(0, 0, base, changes.paths),
        )

    assert artifact.parent_sha == base
    assert [path.as_posix() for path in artifact.changed_paths] == ["src/a.py"]
    assert "+new" in provider.diff(base, artifact.sha)
    assert _git("-C", str(provider.repo), "cat-file", "-t", artifact.sha) == "commit"


def test_lane_workspace_is_detached_and_uses_existing_layout(tmp_path: Path):
    provider, base = _make_provider(tmp_path)

    lane = provider.create_lane(2, base)
    try:
        assert lane.path == provider.lanes_root / "lane-2" / "workspace"
        assert _git("-C", str(lane.path), "branch", "--show-current") == ""
    finally:
        provider.remove_lane(lane)

    assert not lane.path.exists()


def test_remove_rejects_path_outside_provider_roots(tmp_path: Path):
    provider, base = _make_provider(tmp_path)

    with pytest.raises(WorkspaceError, match="outside provider roots"):
        provider.remove(SourceWorkspace("bad", tmp_path / "outside", base))


def test_reset_restores_pristine_base_revision(tmp_path: Path):
    provider, base = _make_provider(tmp_path)

    with provider.open(WorkspaceSpec("0-c0", base)) as workspace:
        (workspace.path / "src" / "a.py").write_text("half-edited\n", encoding="utf-8")
        (workspace.path / "stray.txt").write_text("junk\n", encoding="utf-8")

        provider.reset(workspace)

        assert (workspace.path / "src" / "a.py").read_text(encoding="utf-8") == "old\n"
        assert not (workspace.path / "stray.txt").exists()
        assert provider.inspect(workspace).paths == ()
        assert _git("-C", str(workspace.path), "rev-parse", "HEAD") == base
