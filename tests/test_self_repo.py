"""S3a/S3b tests: run-local self-repo lifecycle + worker redirect + viability check.

Covers the SelfRepo snapshot/state machine, the proposer_lane_worker
``_redirect_self_repo`` bootstrap, and the S3b viability ``--check`` (healthy self
passes; deliberately broken candidates fail at boot / contract / continuity /
protocol-produce). The end-to-end falsification test (edit the snapshot, see it take
effect in a real round) is a manual/round-driven check, not reproducible here without
the model API.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from simpleloop.self_repo import SelfRepo, ViabilityResult, check_viability
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


# === S3b: viability --check ===============================================
#
# Each test snapshots a fresh self-repo into its own tmp_path/self/repo, optionally
# mutates one proposer source file (a deliberate break), then runs the offline
# viability subprocess. check_viability spawns a real worker process (no model), so
# these are integration tests.


def _candidate(run_dir: Path) -> Path:
    """Snapshot a healthy self-repo and return its repo root (the --self-repo target)."""
    sr = SelfRepo(run_dir)
    sr.setup(resume=False)
    return sr.repo


def _append(repo: Path, relpath: str, text: str) -> None:
    """Append text to a file under the snapshotted proposer package (a deliberate break)."""
    target = repo / "proposer" / relpath
    target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_viability_healthy_self_passes(tmp_path: Path):
    repo = _candidate(tmp_path)
    res = check_viability(repo, tmp_path)
    assert res.viable is True
    assert res.exit_code == 0
    assert "[check] OK" in res.stdout


def test_viability_fails_on_syntax_error_boot(tmp_path: Path):
    repo = _candidate(tmp_path)
    # an unparseable module -> proposer.scientist fails to import -> the worker dies
    # at module load (boot), before _run_check runs.
    _append(repo, "scientist.py", "\n!!! unparseable syntax error !!!\n")
    res = check_viability(repo, tmp_path)
    assert res.viable is False
    assert res.exit_code != 0


def test_viability_fails_on_wrong_contract_version(tmp_path: Path):
    repo = _candidate(tmp_path)
    _append(repo, "__init__.py", '\nCONTRACT_VERSION = "proposer-cli-v1"\n')
    res = check_viability(repo, tmp_path)
    assert res.viable is False
    assert res.exit_code != 0
    assert "contract" in res.stderr.lower()


def test_viability_fails_on_broken_continuity_reader(tmp_path: Path):
    repo = _candidate(tmp_path)
    # import succeeds, but load_or_create raises when called -> the continuity
    # assertion (not boot) catches it.
    _append(repo, "scientist_session.py",
            "\n\ndef _broken_load(cls, *a, **k):\n"
            "    raise RuntimeError('continuity reader broken')\n"
            "ScientistSession.load_or_create = classmethod(_broken_load)\n")
    res = check_viability(repo, tmp_path)
    assert res.viable is False
    assert res.exit_code != 0
    assert "continuity" in res.stderr.lower()


def test_viability_fails_on_broken_protocol_produce(tmp_path: Path):
    repo = _candidate(tmp_path)
    # import succeeds, but ResearchProposal construction raises -> the protocol-produce
    # assertion (not boot) catches it. (Frozen dataclasses allow __init__ reassignment;
    # frozen only blocks attribute *set* on instances.)
    _append(repo, "memory/models.py",
            "\n\ndef _broken_init(self, *a, **k):\n"
            "    raise RuntimeError('protocol produce broken: cannot construct')\n"
            "ResearchProposal.__init__ = _broken_init\n")
    res = check_viability(repo, tmp_path)
    assert res.viable is False
    assert res.exit_code != 0
    assert "protocol produce" in res.stderr.lower()


def test_viability_never_mutates_the_run_it_checks(tmp_path: Path):
    """The continuity load runs on a TEMP COPY — a viability check must not write to
    the run_dir it is evaluating."""
    repo = _candidate(tmp_path)
    prop_dir = tmp_path / "proposer"
    prop_dir.mkdir()
    session = prop_dir / "session.jsonl"
    notebook = prop_dir / "notebook.md"
    meta = prop_dir / "meta.json"
    session.write_text('{"role":"user","content":"seed"}\n', encoding="utf-8")
    notebook.write_text("# my notebook\n", encoding="utf-8")
    meta.write_text(json.dumps({"scientist_id": "abc"}), encoding="utf-8")
    before = {p: p.read_text(encoding="utf-8")
              for p in (session, notebook, meta)}

    res = check_viability(repo, tmp_path)
    assert res.viable is True

    after = {p: p.read_text(encoding="utf-8") for p in (session, notebook, meta)}
    assert before == after  # run_dir/proposer/ untouched
    # and no new files appeared in run_dir/proposer (e.g. no meta.json rewrite)
    assert sorted(p.name for p in prop_dir.iterdir()) == ["meta.json", "notebook.md",
                                                          "session.jsonl"]


# === S3c.2: commitment + self-review ledger ================================

def test_update_commitment_writes_next_review_round(tmp_path: Path):
    repo = SelfRepo(tmp_path)
    repo.setup(resume=False)
    assert repo.next_self_review_round is None  # dormant after S0
    repo.update_commitment(next_self_review_round=7)
    assert repo.next_self_review_round == 7
    sha = repo.active_self_sha
    repo.update_commitment(next_self_review_round=12)
    assert repo.next_self_review_round == 12
    assert repo.active_self_sha == sha  # commitment writes preserve the SHA


def test_append_review_and_last_review_round(tmp_path: Path):
    repo = SelfRepo(tmp_path)
    repo.setup(resume=False)
    assert repo.last_review_round() is None

    keep = {"decision": "KEEP", "diagnosis": "d", "keep_reason": "r",
            "next_review_after_rounds": 5, "self_change": None,
            "incumbent_self_sha": repo.active_self_sha, "abstained": False}
    repo.append_review(5, payload=keep, next_review_round=10)

    change = {"decision": "CHANGE", "diagnosis": "d2", "keep_reason": None,
              "next_review_after_rounds": None,
              "self_change": {"target": "prompt", "intent": "i",
                              "instruction": "j", "evidence_refs": ["a"]},
              "incumbent_self_sha": repo.active_self_sha, "abstained": False}
    repo.append_review(10, payload=change, next_review_round=18)

    assert repo.last_review_round() == 10
    r0, r1 = [json.loads(ln) for ln in repo.reviews_path.read_text().splitlines()]
    assert r0["round"] == 5 and r0["decision"] == "KEEP"
    assert r0["next_review_round"] == 10 and r0["change"] is None
    assert r0["adopted"] is None and r0["viable"] is None  # S3d fields null
    assert r1["round"] == 10 and r1["decision"] == "CHANGE"
    assert r1["change"]["target"] == "prompt"


