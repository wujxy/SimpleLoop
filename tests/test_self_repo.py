"""S3a unit tests: run-local self-repo lifecycle + worker redirect.

Covers the SelfRepo snapshot/state machine and the proposer_lane_worker
``_redirect_self_repo`` bootstrap. The end-to-end falsification test (edit the
snapshot, see it take effect in a real round) is a manual/round-driven check,
not reproducible here without the model API.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from simpleloop.self_repo import SelfRepo
from simpleloop.proposer_lane_worker import _redirect_self_repo


# --- SelfRepo fresh lifecycle ---------------------------------------------

def test_fresh_snapshot_creates_s0(tmp_path: Path):
    repo = SelfRepo(tmp_path)
    sha = repo.setup(resume=False)

    # Body layout: the proposer package under self/repo, plus .gitignore.
    assert (repo.repo / "proposer" / "__init__.py").is_file()
    assert (repo.repo / "proposer" / "prompts" / "proposer.md").is_file()
    assert (repo.repo / "proposer" / "scientist.py").is_file()
    assert (repo.repo / ".gitignore").is_file()
    # no caches carried into the self-repo
    assert not (repo.repo / "proposer" / "__pycache__").exists()

    # exactly one commit, S0
    log = _git(repo.repo, "log", "--format=%s")
    assert log == "S0: snapshot proposer"
    assert sha == _git(repo.repo, "rev-parse", "HEAD")

    # state.json
    state = json.loads(repo.state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == 1
    assert state["active_self_sha"] == sha
    assert state["mode"] == "task"
    assert state["next_self_review_round"] is None
    assert repo.active_self_sha == sha


def test_fresh_wipes_interrupted_init_leftover(tmp_path: Path):
    """A crash before the first round can leave self/ behind without history.jsonl;
    a fresh start must clear it so fresh = truly fresh."""
    bogus = tmp_path / "self" / "repo" / "stale"
    bogus.mkdir(parents=True)
    (tmp_path / "self" / "state.json").write_text("garbage", encoding="utf-8")

    repo = SelfRepo(tmp_path)
    repo.setup(resume=False)

    assert not (repo.repo / "stale").exists()  # leftover gone
    assert (repo.repo / "proposer" / "__init__.py").is_file()
    assert _git(repo.repo, "log", "--format=%s") == "S0: snapshot proposer"


# --- SelfRepo resume ------------------------------------------------------

def test_resume_is_idempotent(tmp_path: Path):
    repo = SelfRepo(tmp_path)
    sha0 = repo.setup(resume=False)
    # a second setup as resume must NOT re-snapshot
    sha1 = repo.setup(resume=True)
    assert sha1 == sha0
    assert _git(repo.repo, "rev-list", "--count", "HEAD") == "1"


def test_resume_defensive_resnapshot_when_state_missing(tmp_path: Path):
    repo = SelfRepo(tmp_path)
    repo.setup(resume=False)
    repo.state_path.unlink()  # simulate an old/inconsistent run-dir

    sha = repo.setup(resume=True)  # must not crash; re-snapshots S0
    assert repo.state_path.exists()
    assert repo.active_self_sha == sha


def test_resume_defensive_resnapshot_when_repo_missing(tmp_path: Path):
    repo = SelfRepo(tmp_path)
    repo.setup(resume=False)
    # wipe the repo but leave state.json (inconsistent) -> defensive re-snapshot
    import shutil
    shutil.rmtree(repo.root)

    sha = repo.setup(resume=True)
    assert (repo.repo / "proposer" / "__init__.py").is_file()
    assert repo.active_self_sha == sha


# --- import resolution proof ---------------------------------------------

def test_snapshot_is_importable_as_proposer(tmp_path: Path):
    """The whole point of S3a: with self/repo on sys.path, `import proposer`
    resolves to the snapshot, not the installed package."""
    repo = SelfRepo(tmp_path)
    repo.setup(resume=False)
    # mark the snapshot so we can tell it apart from the installed package
    marker = repo.repo / "proposer" / "SNAPSHOT_MARKER_S3A"
    marker.write_text("x", encoding="utf-8")

    out = subprocess.check_output(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import proposer; "
         "print(proposer.__file__)" % str(repo.repo)],
        text=True,
    ).strip()
    assert str(repo.repo) in out  # resolved from the snapshot
    assert (Path(out).parent / "SNAPSHOT_MARKER_S3A").exists()


# --- worker redirect bootstrap -------------------------------------------

@pytest.fixture
def isolated_argv_path():
    """Snapshot sys.argv and sys.path so redirect tests can't pollute siblings."""
    argv, path = sys.argv[:], sys.path[:]
    yield
    sys.argv, sys.path = argv, path[:]


def _write_manifest(path: Path, run_dir: str | Path) -> Path:
    path.write_text(json.dumps({"run_dir": str(run_dir)}), encoding="utf-8")
    return path


def test_redirect_activates_when_self_repo_exists(tmp_path: Path,
                                                  isolated_argv_path):
    (tmp_path / "self" / "repo" / "proposer").mkdir(parents=True)
    manifest = _write_manifest(tmp_path / "manifest.json", tmp_path)
    sys.argv = ["worker", "--manifest", str(manifest)]

    _redirect_self_repo()
    assert sys.path[0] == str((tmp_path / "self" / "repo").resolve())


def test_redirect_noop_when_self_repo_absent(tmp_path: Path,
                                             isolated_argv_path):
    manifest = _write_manifest(tmp_path / "manifest.json", tmp_path)
    sys.argv = ["worker", "--manifest", str(manifest)]
    baseline = sys.path[:]

    _redirect_self_repo()
    assert sys.path == baseline  # nothing inserted


def test_redirect_noop_without_manifest(tmp_path: Path, isolated_argv_path):
    sys.argv = ["worker", "--unrelated", "x"]
    baseline = sys.path[:]

    _redirect_self_repo()
    assert sys.path == baseline


def test_redirect_noop_when_manifest_missing_run_dir(tmp_path: Path,
                                                    isolated_argv_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps({"no_run_dir": True}), encoding="utf-8")
    sys.argv = ["worker", "--manifest", str(tmp_path / "manifest.json")]
    baseline = sys.path[:]

    _redirect_self_repo()
    assert sys.path == baseline


def test_redirect_noop_on_unreadable_manifest(tmp_path: Path,
                                              isolated_argv_path):
    (tmp_path / "self" / "repo" / "proposer").mkdir(parents=True)
    (tmp_path / "manifest.json").write_text("{ not json", encoding="utf-8")
    sys.argv = ["worker", "--manifest", str(tmp_path / "manifest.json")]
    baseline = sys.path[:]

    _redirect_self_repo()
    assert sys.path == baseline  # defensive: falls through to installed proposer


# --- helper ----------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()
