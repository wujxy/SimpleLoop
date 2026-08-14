"""S3a/S3b tests: run-local self-repo lifecycle + worker redirect + viability smoke test.

Covers the SelfRepo snapshot/state machine, the proposer_lane_worker
``_redirect_self_repo`` bootstrap, and the S3b viability smoke test: the verdict logic
(``_classify_smoke``) is unit-tested deterministically; a boot-death candidate (the
worker dies at import before the model) is an offline integration test; the
healthy-self-passes path runs a real model episode and is left as a manual e2e.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from simpleloop.self_repo import (
    SelfRepo, ViabilityResult, check_viability, _classify_smoke)
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


# === S3b: viability smoke test =============================================
#
# Viability is a single behavior-level gate (RSI §8 revised): run the candidate as a
# normal task-mode lane (goal + empty workspace) and check it reaches COMPLETED — "can
# this self still participate in the loop". The verdict logic (_classify_smoke) is pure
# and unit-tested deterministically; the boot-death case is an offline integration test
# (the worker dies at import, before the model); the healthy-self-passes path runs a
# real model episode and is left as a manual e2e.


def _candidate(run_dir: Path) -> Path:
    """Snapshot a healthy self-repo and return its repo root (the --self-repo target)."""
    sr = SelfRepo(run_dir)
    sr.setup(resume=False)
    return sr.repo


def _dummy_resolved_config(run_dir: Path) -> None:
    """check_viability copies run_dir/config.resolved.json into a temp smoke run_dir.
    A stub suffices: the boot-death worker dies at import before reading it."""
    (Path(run_dir) / "config.resolved.json").write_text(
        json.dumps({"goal": "smoke", "repo_path": str(run_dir)}), encoding="utf-8")


def _append(repo: Path, relpath: str, text: str) -> None:
    """Append text to a file under the snapshotted proposer package (a deliberate break)."""
    target = repo / "proposer" / relpath
    target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")


# --- _classify_smoke verdict logic (pure; no subprocess, no model) --------

def test_classify_completed_submit_is_viable():
    res = _classify_smoke(
        {"status": "COMPLETED", "outcome": "submit",
         "proposals": [{"instruction": "x"}]},
        exit_code=0)
    assert res.viable is True
    assert "COMPLETED" in res.detail and "n_proposals=1" in res.detail


def test_classify_completed_abstain_is_viable():
    # abstention is normal loop participation — the machinery works, it just had
    # nothing to propose on the smoke input.
    res = _classify_smoke(
        {"status": "COMPLETED", "outcome": "abstain", "proposals": []},
        exit_code=0)
    assert res.viable is True


def test_classify_lane_failed_is_not_viable():
    res = _classify_smoke(
        {"status": "LANE_FAILED", "outcome": "error", "proposals": [],
         "explanation": "research() raised: boom"},
        exit_code=0)
    assert res.viable is False
    assert "boom" in res.detail


def test_classify_no_result_is_not_viable_boot_death():
    res = _classify_smoke(None, exit_code=1, stderr_tail="SyntaxError: invalid syntax")
    assert res.viable is False
    assert "boot" in res.detail or "import" in res.detail
    assert "SyntaxError" in res.detail  # stderr tail surfaced for diagnostics


def test_classify_non_dict_result_is_not_viable():
    res = _classify_smoke("not a dict", exit_code=0)  # type: ignore[arg-type]
    assert res.viable is False


# --- spawn path (integration) ---------------------------------------------

def test_viability_fails_on_syntax_error_boot(tmp_path: Path):
    repo = _candidate(tmp_path)
    _dummy_resolved_config(tmp_path)
    # an unparseable scientist.py -> `from proposer.scientist import ...` dies at worker
    # module load (boot), before main()/model -> no result.json -> not viable.
    _append(repo, "scientist.py", "\n!!! unparseable syntax error !!!\n")
    res = check_viability(repo, tmp_path)
    assert res.viable is False
    assert "boot" in res.detail or "import" in res.detail


def test_viability_missing_config_returns_not_viable(tmp_path: Path):
    # no config.resolved.json -> check_viability cannot build the smoke run_dir
    repo = _candidate(tmp_path)
    res = check_viability(repo, tmp_path)
    assert res.viable is False
    assert "config.resolved.json" in res.detail


@pytest.mark.skip(reason="healthy-self viability runs a real model episode; run "
                         "manually with a live config + model token")
def test_viability_healthy_self_passes(tmp_path: Path):
    repo = _candidate(tmp_path)
    _dummy_resolved_config(tmp_path)
    res = check_viability(repo, tmp_path)
    assert res.viable is True


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


# === S3d: self-execution + adoption (transition) ===========================
#
# transition() = worktree @ active SHA -> self-executor edits -> commit candidate
# -> check_viability (smoke) -> adopt (ff-merge + advance active_self_sha) or keep.
# The self-executor is a claude-p Agent; here a fake stands in (writes a file to the
# worktree), and check_viability is monkeypatched so the verdict is deterministic
# (the real smoke runs a model episode — see the skipped healthy-self test).


def _self_change() -> dict:
    return {"target": "prompt", "intent": "sharpen coverage",
            "instruction": "add a coverage preamble to the charter",
            "evidence_refs": ["proposer/scientist.py"]}


class _FakeExecAgent:
    """Stands in for the claude-p Agent: run_text 'edits' one file in the worktree."""
    def __init__(self, relpath: str = "proposer/prompts/proposer.md",
                 text: str = "# touched by self-exec\n"):
        self.relpath, self.text, self.calls = relpath, text, []

    def run_text(self, prompt: str, *, cwd, label: str = "agent") -> str:
        self.calls.append((str(cwd), label))
        target = Path(cwd) / self.relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.text, encoding="utf-8")
        return "done"


def _patch_viability(monkeypatch, viable: bool, detail: str = "smoke"):
    monkeypatch.setattr(
        "simpleloop.self_repo.check_viability",
        lambda candidate_repo, run_dir: ViabilityResult(viable, detail))


def test_transition_adopts_when_viable(tmp_path: Path, monkeypatch):
    sr = SelfRepo(tmp_path); sr.setup(resume=False)
    old_sha = sr.active_self_sha
    _patch_viability(monkeypatch, True, "smoke COMPLETED")
    tr = sr.transition(agent=_FakeExecAgent(), self_change=_self_change(),
                       run_dir=tmp_path)
    assert tr.adopted is True and tr.viable is True
    assert tr.candidate_self_sha and tr.candidate_self_sha != old_sha
    assert sr.active_self_sha == tr.candidate_self_sha          # advanced
    # the active working tree now carries the executor's edit
    assert (sr.repo / "proposer" / "prompts" / "proposer.md").read_text(
        encoding="utf-8") == "# touched by self-exec\n"


def test_transition_keeps_incumbent_when_not_viable(tmp_path: Path, monkeypatch):
    sr = SelfRepo(tmp_path); sr.setup(resume=False)
    old_sha = sr.active_self_sha
    _patch_viability(monkeypatch, False, "smoke LANE_FAILED: boom")
    tr = sr.transition(agent=_FakeExecAgent(), self_change=_self_change(),
                       run_dir=tmp_path)
    assert tr.adopted is False and tr.viable is False
    assert tr.candidate_self_sha is not None      # a candidate WAS produced…
    assert sr.active_self_sha == old_sha          # …but the incumbent was kept
    # active working tree NOT updated (edit lived only in the removed worktree)
    assert (sr.repo / "proposer" / "prompts" / "proposer.md").read_text(
        encoding="utf-8") != "# touched by self-exec\n"


def test_transition_no_change_when_executor_edits_nothing(tmp_path: Path,
                                                           monkeypatch):
    sr = SelfRepo(tmp_path); sr.setup(resume=False)
    _patch_viability(monkeypatch, True)  # should NOT be reached

    class _NoEdit:
        def run_text(self, prompt, *, cwd, label="agent"):
            return "done"

    tr = sr.transition(agent=_NoEdit(), self_change=_self_change(), run_dir=tmp_path)
    assert tr.candidate_self_sha is None and tr.adopted is False
    assert "no changes" in tr.detail


def test_transition_keeps_incumbent_on_executor_error(tmp_path: Path, monkeypatch):
    sr = SelfRepo(tmp_path); sr.setup(resume=False)
    old_sha = sr.active_self_sha

    class _Crash:
        def run_text(self, prompt, *, cwd, label="agent"):
            raise RuntimeError("claude timed out")

    tr = sr.transition(agent=_Crash(), self_change=_self_change(), run_dir=tmp_path)
    assert tr.candidate_self_sha is None and tr.adopted is False
    assert sr.active_self_sha == old_sha
    assert "executor failed" in tr.detail


def test_transition_no_instruction_is_a_noop(tmp_path: Path):
    sr = SelfRepo(tmp_path); sr.setup(resume=False)
    old_sha = sr.active_self_sha
    tr = sr.transition(agent=_FakeExecAgent(),
                       self_change={"target": "prompt"}, run_dir=tmp_path)
    assert tr.candidate_self_sha is None and tr.adopted is False
    assert "no instruction" in tr.detail
    assert sr.active_self_sha == old_sha


def test_self_repo_worktree_edit_commit_adopt_mechanics(tmp_path: Path):
    """The git primitives directly, without the agent/smoke: worktree at the active
    SHA carries the proposer source; an edit + commit + adopt advances the active
    self and updates its working tree."""
    sr = SelfRepo(tmp_path); sr.setup(resume=False)
    old = sr.active_self_sha
    wt = sr._add_self_worktree(old)
    try:
        assert (wt / "proposer" / "__init__.py").is_file()    # worktree at active SHA
        (wt / "proposer" / "prompts" / "proposer.md").write_text(
            "X", encoding="utf-8")
        assert sr._self_worktree_has_changes(wt) is True
        cand = sr._commit_self_worktree(wt, "S(n+1): prompt")
        assert cand != old
        sr._adopt(cand)
        assert sr.active_self_sha == cand
        assert (sr.repo / "proposer" / "prompts" / "proposer.md").read_text(
            encoding="utf-8") == "X"
    finally:
        sr._remove_self_worktree(wt)


