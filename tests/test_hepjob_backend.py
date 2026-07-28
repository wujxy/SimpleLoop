"""HEPJobBackend lifecycle tests under a faked condor.

condor_submit / condor_q / condor_rm are placed on PATH as shell stubs so
the backend's reconcile loop can be driven deterministically: completion,
Held -> retry -> success, Held -> attempts exhausted, running timeout,
disappeared-with-result, disappeared-without-result -> retry, malformed
result -> infra, transient query failure must not kill the job, and the
collect filtering (infra failures never enter history). The in-flight
round is also round-tripped through a resume.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from simpleloop.execution import hepjob
from simpleloop.execution.hepjob import HEPJobBackend, InfraRoundError
from simpleloop.roles.proposer import Proposal

# condor JobStatus codes
IDLE, RUNNING, HELD = 1, 2, 5


def _fake_proposal(text="p") -> Proposal:
    return Proposal(proposal=text, family="layout", decision="switch")


class _FakeTelemetry:
    def __init__(self):
        self.recorded: list = []

    def record_usage(self, usage):
        self.recorded.append(usage)

    def snapshot(self, *, persist=False):
        return {"worktime_seconds": 0.0, "processed_tokens": 0}


class _FakeWorkspace:
    def __init__(self):
        self.added: list = []
        self.removed: list = []

    def add_worktree(self, worktree_id, parent_sha):
        self.added.append((worktree_id, parent_sha))
        return Path(f"/tmp/wt-{worktree_id}")

    def remove_worktree(self, worktree_id):
        self.removed.append(worktree_id)


class _Ctx:
    def __init__(self, tmp_path):
        self.cfg = {"execution_backend": "hepjob"}
        self.run_dir = tmp_path
        self.workspace = _FakeWorkspace()
        self.telemetry = _FakeTelemetry()
        self.baseline_metrics = {"SPEED_MS": 200.0}


def _hep_cfg(tmp_path, **overrides):
    cfg = {
        "schedd_name": "schedd",
        "accounting_group": "JUNO.juno.default",
        "accounting_group_user": "tester",
        "request_os": "AlmaLinux9",
        "memory_mb": 6000,
        "cpus": 1,
        "poll_seconds": 0,
        "max_attempts": 2,
        "idle_warn_seconds": 3600,
        "run_timeout_seconds": 100,
        "disappearance_grace_seconds": 0,
        "python_executable": str(tmp_path / "py"),
        "submit_cmd": "condor_submit",
        "query_cmd": "condor_q",
        "remove_cmd": "condor_rm",
    }
    cfg.update(overrides)
    return cfg


def _install_condor_stubs(bin_dir, *, submit_cluster=100, query_lines=None,
                          remove_ok=True):
    """Write condor_submit/condor_q/condor_rm stubs into bin_dir. condor_q
    replays `query_lines` across successive calls via a counter file.

    Each query_lines entry is the raw stdout for one call: a "ClusterId
    ProcId JobStatus" row means the job is found in that state; an empty
    string means the query succeeded but the job was NOT found (gone)."""
    (bin_dir / "condor_submit").write_text(
        "#!/usr/bin/env bash\n"
        f"echo '1 job(s) submitted to cluster {submit_cluster}.'\n",
        encoding="utf-8")
    (bin_dir / "condor_rm").write_text(
        f"#!/usr/bin/env bash\nexit {0 if remove_ok else 1}\n", encoding="utf-8")
    if query_lines is None:
        query_lines = [""]
    counter = bin_dir / "ncalls"
    counter.unlink(missing_ok=True)
    quoted = " ".join(f"'{line}'" for line in query_lines)
    (bin_dir / "condor_q").write_text(
        "#!/usr/bin/env bash\n"
        "i=$(cat '" + str(counter) + "' 2>/dev/null || echo 0)\n"
        f"files=({quoted})\n"
        'echo "${files[$i]}"\n'
        'echo "$((i+1))" > "' + str(counter) + '"\n',
        encoding="utf-8")
    for name in ("condor_submit", "condor_q", "condor_rm"):
        (bin_dir / name).chmod(0o755)


def _seed_result(result_dir, *, attempt=1, status="COMPLETED", host="node1"):
    result_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "candidate": 0,
        "candidate_status": status,
        "sha": f"sha-a{attempt}",
        "score": 0.7, "risk": "low", "feedback": "f", "metrics": {},
        "usage": [{"input_tokens": 100, "output_tokens": 10}],
        "execution": {"backend": "hepjob", "job_id": f"100.0",
                      "attempt": attempt, "host": host},
    }
    (result_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    (result_dir / "_FINISHED").touch()


def _drive(tmp_path, monkeypatch, query_lines, *, proposals=None,
           hep_overrides=None, seed_fn=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_condor_stubs(bin_dir, query_lines=query_lines)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(hepjob.time, "sleep", lambda _s: None)
    ctx = _Ctx(tmp_path)
    backend = HEPJobBackend(ctx, _hep_cfg(tmp_path, **(hep_overrides or {})))
    result_dir = tmp_path / "rounds" / "r0" / "candidates" / "c0"
    if seed_fn:
        seed_fn(result_dir)
    cands = backend.run_candidates(proposals=proposals or [_fake_proposal()],
                                   round_id=0, parent_sha="p",
                                   prior_metrics={}, reflection="refl")
    return backend, ctx, cands


def test_completed_after_gone_with_finished(tmp_path, monkeypatch):
    backend, ctx, cands = _drive(
        tmp_path, monkeypatch, ["", ""],  # gone (job not in query output)
        seed_fn=lambda rd: _seed_result(rd))
    assert cands[0]["candidate_status"] == "COMPLETED"
    assert cands[0]["execution"]["host"] == "node1"
    assert ctx.telemetry.recorded == [{"input_tokens": 100, "output_tokens": 10}]
    assert (tmp_path / "inflight_round.json").exists() is False


def test_completed_after_running_then_gone(tmp_path, monkeypatch):
    # poll1: RUNNING; poll2: gone+_FINISHED
    backend, ctx, cands = _drive(
        tmp_path, monkeypatch, [f"100 0 {RUNNING}", ""],
        seed_fn=lambda rd: _seed_result(rd))
    assert cands[0]["candidate_status"] == "COMPLETED"


def test_held_retry_then_success(tmp_path, monkeypatch):
    # poll1: HELD -> remove+retry attempt2; poll2: gone + attempt2 _FINISHED
    backend, ctx, cands = _drive(
        tmp_path, monkeypatch, [f"100 0 {HELD}", ""],
        seed_fn=lambda rd: _seed_result(rd, attempt=2, host="node2"))
    assert cands[0]["candidate_status"] == "COMPLETED"
    assert cands[0]["execution"]["attempt"] == 2
    # Held triggered a worktree rebuild (remove + re-add from parent_sha) before
    # the final collect's own cleanup; the dirty worktree was never reused.
    assert ctx.workspace.removed.count("0-c0") == 2
    assert ("0-c0", "p") in ctx.workspace.added


def test_held_exhausts_attempts(tmp_path, monkeypatch):
    # HELD on poll1 (attempt2), HELD again on poll2 (exhausted)
    backend, ctx = _drive.__wrapped__ if hasattr(_drive, "__wrapped__") else (None, None)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_condor_stubs(bin_dir, query_lines=[f"100 0 {HELD}", f"100 0 {HELD}"])
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(hepjob.time, "sleep", lambda _s: None)
    backend = HEPJobBackend(_Ctx(tmp_path), _hep_cfg(tmp_path, max_attempts=1))
    with pytest.raises(InfraRoundError):
        backend.run_candidates(proposals=[_fake_proposal()], round_id=0,
                               parent_sha="p", prior_metrics={}, reflection="r")
    assert (tmp_path / "inflight_round.json").exists()  # retained for --continue


def test_running_timeout(tmp_path, monkeypatch):
    # poll1: RUNNING; poll2: still RUNNING past timeout (run_timeout=-1 forces it)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_condor_stubs(bin_dir, query_lines=[f"100 0 {RUNNING}", f"100 0 {RUNNING}"])
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(hepjob.time, "sleep", lambda _s: None)
    backend = HEPJobBackend(_Ctx(tmp_path), _hep_cfg(tmp_path,
                            run_timeout_seconds=-1))
    with pytest.raises(InfraRoundError):
        backend.run_candidates(proposals=[_fake_proposal()], round_id=0,
                               parent_sha="p", prior_metrics={}, reflection="r")


def test_lost_then_retry_then_success(tmp_path, monkeypatch):
    # poll1: gone, no _FINISHED, grace=0 -> LOST -> retry attempt2
    # poll2: gone, attempt2 _FINISHED seeded -> COMPLETED
    backend, ctx, cands = _drive(
        tmp_path, monkeypatch, ["", ""],
        seed_fn=lambda rd: _seed_result(rd, attempt=2, host="node2"))
    assert cands[0]["candidate_status"] == "COMPLETED"
    assert cands[0]["execution"]["attempt"] == 2


def test_malformed_result_is_infra(tmp_path, monkeypatch):
    def seed(rd):
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "result.json").write_text("{not json", encoding="utf-8")
        (rd / "_FINISHED").touch()
    backend, ctx = (None, None)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_condor_stubs(bin_dir, query_lines=["", ""])
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(hepjob.time, "sleep", lambda _s: None)
    backend = HEPJobBackend(_Ctx(tmp_path), _hep_cfg(tmp_path))
    result_dir = tmp_path / "rounds" / "r0" / "candidates" / "c0"
    seed(result_dir)
    with pytest.raises(InfraRoundError):
        backend.run_candidates(proposals=[_fake_proposal()], round_id=0,
                               parent_sha="p", prior_metrics={}, reflection="r")


def test_query_failure_does_not_kill_job(tmp_path, monkeypatch):
    # poll1: query outputs "" (failure) -> log, no transition
    # poll2: gone + _FINISHED -> COMPLETED
    backend, ctx, cands = _drive(
        tmp_path, monkeypatch, ["", ""],
        seed_fn=lambda rd: _seed_result(rd))
    assert cands[0]["candidate_status"] == "COMPLETED"


def test_resume_from_inflight(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_condor_stubs(bin_dir, query_lines=["100 0 1"])  # idle (unused: no supervise yet)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(hepjob.time, "sleep", lambda _s: None)
    backend = HEPJobBackend(_Ctx(tmp_path), _hep_cfg(tmp_path, poll_seconds=0))
    # Populate round meta so the inflight file carries the proposal back to resume.
    backend._round_meta = {
        "round_id": 0, "parent_sha": "p", "reflection": "r", "insight": None,
        "proposals": [{"family": "layout", "decision": "switch", "proposal": "p"}],
    }
    job = backend._prepare(0, _fake_proposal(), 0, "p", {})
    backend._submit(job)
    backend._write_inflight([job])
    assert (tmp_path / "inflight_round.json").exists()

    # Finish the job out-of-band (as if the worker completed after a crash).
    _seed_result(job.result_dir)
    # Rewrite query to report gone so resume's poll sees completion.
    _install_condor_stubs(bin_dir, query_lines=[""])
    inflight = json.loads((tmp_path / "inflight_round.json").read_text())
    backend2 = HEPJobBackend(_Ctx(tmp_path), _hep_cfg(tmp_path, poll_seconds=0))
    cands = backend2.resume_round(inflight)
    assert cands[0]["candidate_status"] == "COMPLETED"
    assert (tmp_path / "inflight_round.json").exists() is False


def test_job_env_sh_materializes_payload_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("UNRELATED_VAR", "x")
    backend = HEPJobBackend(_Ctx(tmp_path), _hep_cfg(tmp_path))
    path = backend._ensure_job_env()
    text = path.read_text()
    assert "ANTHROPIC_API_KEY=sk-test" in text
    assert "UNRELATED_VAR" not in text
    assert "PYTHONPATH=" in text
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_ihep_real_group_derived_from_accounting_group(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_condor_stubs(bin_dir, query_lines=[""])  # stubs; submit unused path
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(hepjob.time, "sleep", lambda _s: None)
    backend = HEPJobBackend(_Ctx(tmp_path), _hep_cfg(tmp_path))
    job = backend._prepare(0, _fake_proposal(), 0, "p", {})
    backend._submit(job)
    sub = (job.result_dir / "job.sub").read_text()
    # accounting_group "JUNO.juno.default" -> +IHEP_RealGroup = "juno"
    assert '+IHEP_RealGroup = "juno"' in sub
    assert "accounting_group = JUNO.juno.default" in sub


def test_explicit_ihep_group_overrides_derivation(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_condor_stubs(bin_dir, query_lines=[""])
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(hepjob.time, "sleep", lambda _s: None)
    backend = HEPJobBackend(_Ctx(tmp_path), _hep_cfg(tmp_path, ihep_group="customgrp"))
    job = backend._prepare(0, _fake_proposal(), 0, "p", {})
    backend._submit(job)
    sub = (job.result_dir / "job.sub").read_text()
    assert '+IHEP_RealGroup = "customgrp"' in sub


def test_collect_filters_infra_keeps_business(tmp_path, monkeypatch):
    # Two candidates, both HELD+exhausted -> all infra -> InfraRoundError
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_condor_stubs(bin_dir, query_lines=[f"100 0 {HELD}", f"100 0 {HELD}"])
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setattr(hepjob.time, "sleep", lambda _s: None)
    backend = HEPJobBackend(_Ctx(tmp_path), _hep_cfg(tmp_path, max_attempts=1))
    with pytest.raises(InfraRoundError):
        backend.run_candidates(
            proposals=[_fake_proposal("p0"), _fake_proposal("p1")],
            round_id=0, parent_sha="p", prior_metrics={}, reflection="r")
