from __future__ import annotations

from pathlib import Path

import pytest

from simpleloop.rsi.body import GitSelfBodyStore, NoSelfChangeError
from simpleloop.rsi.models import SelfCommitRequest


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    root = tmp_path / "seed" / "proposer"
    (root / "prompts").mkdir(parents=True)
    (root / "__init__.py").write_text("VERSION = 0\n", encoding="utf-8")
    (root / "prompts" / "proposer.md").write_text("seed\n", encoding="utf-8")
    return root


def initialized_bodies(tmp_path: Path, seed: Path) -> GitSelfBodyStore:
    bodies = GitSelfBodyStore(tmp_path / "run" / "self")
    bodies.initialize(seed)
    return bodies


def commit_request(round_id: int, parent_sha: str) -> SelfCommitRequest:
    return SelfCommitRequest(round_id, parent_sha, "prompt")


def test_initialize_snapshots_only_proposer_and_materializes_s0(tmp_path, seed):
    bodies = GitSelfBodyStore(tmp_path / "run" / "self")

    revision = bodies.initialize(seed)

    assert (bodies.runtime_path / "proposer" / "__init__.py").is_file()
    assert not (bodies.runtime_path / "seed").exists()
    assert bodies.materialize(revision.sha) == bodies.runtime_path
    assert bodies.initial_revision == revision


def test_initialize_is_idempotent(tmp_path, seed):
    bodies = GitSelfBodyStore(tmp_path / "run" / "self")

    first = bodies.initialize(seed)
    second = bodies.initialize(seed)

    assert second == first


def test_candidate_commit_is_idempotent_after_crash(tmp_path, seed):
    bodies = initialized_bodies(tmp_path, seed)
    parent = bodies.initial_revision.sha
    workspace = bodies.prepare_candidate(5, parent)
    (workspace.path / "proposer" / "prompts" / "proposer.md").write_text(
        "changed\n", encoding="utf-8"
    )

    first = bodies.commit_candidate(workspace, commit_request(5, parent))
    second = bodies.commit_candidate(workspace, commit_request(5, parent))

    assert second == first
    assert first.parent_sha == parent
    assert first.sha != parent


def test_prepare_candidate_resumes_existing_dirty_workspace(tmp_path, seed):
    bodies = initialized_bodies(tmp_path, seed)
    parent = bodies.initial_revision.sha
    first = bodies.prepare_candidate(3, parent)
    marker = first.path / "proposer" / "resume.py"
    marker.write_text("x = 1\n", encoding="utf-8")

    resumed = bodies.prepare_candidate(3, parent)

    assert resumed.path == first.path
    assert marker.exists()


def test_candidate_can_start_from_non_active_parent(tmp_path, seed):
    bodies = initialized_bodies(tmp_path, seed)
    s0 = bodies.initial_revision.sha
    first_workspace = bodies.prepare_candidate(1, s0)
    (first_workspace.path / "proposer" / "branch.py").write_text(
        "branch = 1\n", encoding="utf-8"
    )
    first = bodies.commit_candidate(first_workspace, commit_request(1, s0))

    second_workspace = bodies.prepare_candidate(2, first.sha)

    assert second_workspace.base_sha == first.sha
    assert (second_workspace.path / "proposer" / "branch.py").is_file()


def test_commit_rejects_no_change(tmp_path, seed):
    bodies = initialized_bodies(tmp_path, seed)
    parent = bodies.initial_revision.sha
    workspace = bodies.prepare_candidate(2, parent)

    with pytest.raises(NoSelfChangeError):
        bodies.commit_candidate(workspace, commit_request(2, parent))


def test_materialize_switches_runtime_to_candidate(tmp_path, seed):
    bodies = initialized_bodies(tmp_path, seed)
    parent = bodies.initial_revision.sha
    workspace = bodies.prepare_candidate(6, parent)
    (workspace.path / "proposer" / "active.py").write_text(
        "active = True\n", encoding="utf-8"
    )
    candidate = bodies.commit_candidate(workspace, commit_request(6, parent))

    runtime = bodies.materialize(candidate.sha)

    assert (runtime / "proposer" / "active.py").is_file()


def test_discard_candidate_removes_only_managed_workspace(tmp_path, seed):
    bodies = initialized_bodies(tmp_path, seed)
    workspace = bodies.prepare_candidate(7, bodies.initial_revision.sha)

    bodies.discard_candidate(workspace)

    assert not workspace.path.exists()
