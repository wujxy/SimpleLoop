"""Host-level invariants for proposer lane workspaces.

Each lane gets an independent git worktree at ``base_sha`` under
``run_dir/lanes/lane-{id}/workspace``. These tests pin the structural,
isolation, history-visibility, fresh-episode, multi-lane, and
candidate-independence invariants the design relies on.

Note: the "proposer cannot commit" guarantee is enforced by the container
mount (``/repo:ro``), not by git at the host level — it is covered by
``test_runtime.py::test_research_argv_is_contained_offline_with_writable_workspace``.
These tests cover what the ``Workspace`` host API itself guarantees.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from simpleloop.world import WorkspaceSpec
from simpleloop.world.git import GitWorkspaceProvider


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True,
    ).stdout.strip()


def _make_source_repo(repo: Path) -> str:
    """A source repo with one baseline commit; returns the baseline SHA."""
    _git("init", str(repo))
    _git("-C", str(repo), "config", "user.name", "Test")
    _git("-C", str(repo), "config", "user.email", "test@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "foo.cc").write_text("int f() { return 0; }\n")
    _git("-C", str(repo), "add", "-A")
    _git("-C", str(repo), "commit", "-m", "baseline")
    return _git("-C", str(repo), "rev-parse", "HEAD")


def _workspace(tmp_path: Path) -> tuple[GitWorkspaceProvider, str]:
    source = tmp_path / "source"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    base = _make_source_repo(source)
    ws = GitWorkspaceProvider(run_dir, source, "HEAD")
    ws.initialize()
    return ws, base


def test_lane_workspace_structure(tmp_path: Path):
    ws, base = _workspace(tmp_path)

    lane = ws.create_lane(0, base).path

    assert lane == ws.lanes_root / "lane-0" / "workspace"
    assert lane.is_dir()
    # base_sha tree materialized as real files
    assert (lane / "src" / "foo.cc").read_text().startswith("int f")
    # detached HEAD at base_sha
    assert _git("-C", str(lane), "rev-parse", "HEAD") == base
    # --detach means no branch is checked out
    assert _git("-C", str(lane), "branch", "--show-current") == ""
    # .git is a gitfile (linked worktree), not a real directory
    assert (lane / ".git").is_file()
    assert (lane / ".git").read_text().strip().startswith("gitdir: ")


def test_lane_workspace_does_not_pollute_run_repo(tmp_path: Path):
    ws, base = _workspace(tmp_path)
    repo = ws.repo
    repo_head_before = _git("-C", str(repo), "rev-parse", "HEAD")
    repo_status_before = _git("-C", str(repo), "status", "--porcelain")

    lane_workspace = ws.create_lane(0, base)
    lane = lane_workspace.path
    # write a scratch file + modify a source file inside the lane workspace
    (lane / "scratch.txt").write_text("toy experiment\n")
    (lane / "src" / "foo.cc").write_text("int f() { return 1; }\n")

    # run/repo's HEAD and working tree are untouched
    assert _git("-C", str(repo), "rev-parse", "HEAD") == repo_head_before
    assert _git("-C", str(repo), "status", "--porcelain") == repo_status_before
    ws.remove_lane(lane_workspace)


def test_lane_workspace_history_visible(tmp_path: Path):
    """A lane workspace at an earlier base can still inspect a later candidate
    commit (the shared object store carries the history)."""
    ws, base = _workspace(tmp_path)
    # simulate a candidate commit descending from base, via a candidate worktree
    # (run/repo is cloned --no-checkout, so commits are made through worktrees)
    candidate = ws.create(WorkspaceSpec("cand", base))
    cand = candidate.path
    _git("-C", str(cand), "config", "user.name", "Test")
    _git("-C", str(cand), "config", "user.email", "test@example.invalid")
    (cand / "src" / "foo.cc").write_text("int f() { return 9; }\n")
    _git("-C", str(cand), "add", "-A")
    _git("-C", str(cand), "commit", "-m", "candidate")
    candidate_sha = _git("-C", str(cand), "rev-parse", "HEAD")
    ws.remove(candidate)

    # lane workspace checked out at the ORIGINAL base
    lane_workspace = ws.create_lane(0, base)
    lane = lane_workspace.path
    assert _git("-C", str(lane), "rev-parse", "HEAD") == base
    # ...can still inspect the descendant candidate commit + diff against it
    show = _git("-C", str(lane), "show", f"{candidate_sha}:src/foo.cc")
    assert "return 9" in show
    diff = _git("-C", str(lane), "diff", f"{base}..{candidate_sha}")
    assert "return 9" in diff
    ws.remove_lane(lane_workspace)


def test_lane_workspace_fresh_each_episode(tmp_path: Path):
    """Recreating a lane workspace drops the previous episode's dirty state."""
    ws, base = _workspace(tmp_path)

    lane_workspace = ws.create_lane(0, base)
    lane = lane_workspace.path
    (lane / "dirty.txt").write_text("round r scratch\n")
    (lane / "src" / "foo.cc").write_text("dirty\n")
    ws.remove_lane(lane_workspace)

    # a new episode at a (possibly different) base starts clean
    lane_workspace2 = ws.create_lane(0, base)
    lane2 = lane_workspace2.path
    assert not (lane2 / "dirty.txt").exists()
    assert (lane2 / "src" / "foo.cc").read_text().startswith("int f")
    assert _git("-C", str(lane2), "status", "--porcelain") == ""
    ws.remove_lane(lane_workspace2)


def test_multi_lane_isolation(tmp_path: Path):
    """Concurrent lanes cannot see each other's working-tree changes."""
    ws, base = _workspace(tmp_path)

    workspace0 = ws.create_lane(0, base)
    workspace1 = ws.create_lane(1, base)
    lane0, lane1 = workspace0.path, workspace1.path

    (lane0 / "only-in-lane0.txt").write_text("x\n")
    (lane1 / "only-in-lane1.txt").write_text("y\n")

    assert (lane0 / "only-in-lane0.txt").exists()
    assert not (lane0 / "only-in-lane1.txt").exists()
    assert (lane1 / "only-in-lane1.txt").exists()
    assert not (lane1 / "only-in-lane0.txt").exists()
    ws.remove_lane(workspace0)
    ws.remove_lane(workspace1)


def test_candidate_worktree_independent_of_lane_workspace(tmp_path: Path):
    """A candidate worktree is created clean from base_sha and is unaffected by
    the proposer dirtying its lane workspace."""
    ws, base = _workspace(tmp_path)

    lane_workspace = ws.create_lane(0, base)
    lane = lane_workspace.path
    (lane / "src" / "foo.cc").write_text("proposer toy edit\n")
    (lane / "toy.cpp").write_text("experiment\n")

    candidate_workspace = ws.create(WorkspaceSpec("3-c0", base))
    candidate = candidate_workspace.path
    # candidate working tree is the pristine base, not the proposer's dirty state
    assert (candidate / "src" / "foo.cc").read_text().startswith("int f")
    assert not (candidate / "toy.cpp").exists()
    assert _git("-C", str(candidate), "status", "--porcelain") == ""
    ws.remove(candidate_workspace)
    ws.remove_lane(lane_workspace)
