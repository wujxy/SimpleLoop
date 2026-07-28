"""CandidateWorker unit tests: spec serialization, run_candidate business
status, the standalone CLI's atomic result contract, and the catch-all
invariant (any business failure still yields result.json + _FINISHED)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop import candidate_worker as worker_mod
from simpleloop.candidate_worker import (
    CandidateDeps,
    CandidateSpec,
    candidate_accepted,
    candidate_failure,
    main,
    run_candidate,
    write_result,
)
from simpleloop.harness.evals import EvalResult
from simpleloop.roles.executor import ExecResult
from simpleloop.roles.judger import Judgment


_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
           "gates": [{"key": "CORRECTNESS"}]}


def _spec(tmp_path: Path, **overrides) -> CandidateSpec:
    values = dict(
        round_id=3, candidate_id=7, parent_sha="abc123",
        family="layout", decision="switch", proposal="do the thing",
        prior_metrics={"SPEED_MS": 150.0},
        worktree_path=str(tmp_path / "wt"),
        result_dir=str(tmp_path / "result"),
        attempt=2,
    )
    values.update(overrides)
    return CandidateSpec(**values)


def _deps(tmp_path: Path, cfg: dict | None = None) -> CandidateDeps:
    class FakeRuntime:
        def preflight(self):
            pass

    class FakeWorkspace:
        def diff(self, parent_sha, sha):
            return f"diff {parent_sha}..{sha}"

    return CandidateDeps(
        cfg=cfg or {
            "goal": "g", "editable_paths": ["src/**"], "frozen_paths": [],
            "eval_commands": ["eval"], "metrics": _SCHEMA,
        },
        run_dir=tmp_path, runtime=FakeRuntime(), workspace=FakeWorkspace(),
        executor_agent=object(), judger_agent=object(),
        gate_lines="", baseline_metrics={"SPEED_MS": 200.0},
    )


def _judgment() -> Judgment:
    return Judgment(score=0.7, risk="low", feedback="f" * 300,
                    feedback_for_proposer="mechanism plausible")


def test_spec_serialization_round_trip(tmp_path: Path):
    spec = _spec(tmp_path)
    again = CandidateSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert again == spec


def test_run_candidate_completed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(worker_mod.executor_mod, "execute",
                        lambda *a, **k: ExecResult(sha="def456", reason=None,
                                                   changed_paths=["a.cc"]))
    monkeypatch.setattr(worker_mod.evals, "run_eval",
                        lambda *a, **k: EvalResult(
                            "eval", {"SPEED_MS": 100.0, "CORRECTNESS": True},
                            (0,)))
    monkeypatch.setattr(worker_mod.judger_mod, "judge",
                        lambda *a, **k: _judgment())
    result = run_candidate(_deps(tmp_path), _spec(tmp_path))
    assert result["candidate_status"] == "COMPLETED"
    assert result["sha"] == "def456"
    assert result["accepted"] is True
    assert result["metrics"]["SPEED_MS"] == 100.0


def test_run_candidate_no_change_and_gate_rejected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(worker_mod.evals, "run_eval",
                        lambda *a, **k: EvalResult("", {}, ()))
    monkeypatch.setattr(worker_mod.judger_mod, "judge",
                        lambda *a, **k: _judgment())
    monkeypatch.setattr(worker_mod.executor_mod, "execute",
                        lambda *a, **k: ExecResult(
                            sha=None, reason="executor made no changes",
                            changed_paths=[]))
    result = run_candidate(_deps(tmp_path), _spec(tmp_path))
    assert result["candidate_status"] == "NO_CHANGE"
    assert result["accepted"] is False

    monkeypatch.setattr(worker_mod.executor_mod, "execute",
                        lambda *a, **k: ExecResult(
                            sha=None, reason="gate rejected: frozen paths",
                            changed_paths=["tests/x.py"]))
    result = run_candidate(_deps(tmp_path), _spec(tmp_path))
    assert result["candidate_status"] == "GATE_REJECTED"


def test_run_candidate_business_failure_is_stage_tagged(tmp_path: Path,
                                                        monkeypatch):
    def boom(*_a, **_k):
        raise ValueError("judger returned junk")

    monkeypatch.setattr(worker_mod.executor_mod, "execute",
                        lambda *a, **k: ExecResult(sha="def456", reason=None,
                                                   changed_paths=["a.cc"]))
    monkeypatch.setattr(worker_mod.evals, "run_eval",
                        lambda *a, **k: EvalResult("eval", {"SPEED_MS": 1.0},
                                                   (0,)))
    monkeypatch.setattr(worker_mod.judger_mod, "judge", boom)
    result = run_candidate(_deps(tmp_path), _spec(tmp_path))
    assert result["candidate_status"] == "JUDGER_FAILED"
    assert result["sha"] == "def456"
    assert result["score"] == 0.0


def test_write_result_is_atomic_and_marks_finished(tmp_path: Path):
    out = tmp_path / "r"
    write_result(out, {"candidate": 0})
    assert json.loads((out / "result.json").read_text()) == {"candidate": 0}
    assert (out / "_FINISHED").exists()
    assert not (out / "result.json.tmp").exists()


def _write_manifest(tmp_path: Path, spec: CandidateSpec,
                    extra: dict | None = None) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "config.resolved.json").write_text("{}", encoding="utf-8")
    manifest = {"run_id": "run_001", **spec.to_dict(),
                "run_dir": str(run_dir), **(extra or {})}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_cli_writes_terminal_result(tmp_path: Path, monkeypatch):
    spec = _spec(tmp_path)
    manifest = _write_manifest(tmp_path, spec)
    monkeypatch.setattr(worker_mod, "build_deps",
                        lambda cfg, run_dir, usage_observer=None: (
                            _deps(tmp_path)))
    monkeypatch.setattr(worker_mod, "run_candidate",
                        lambda deps, spec: {"candidate": spec.candidate_id,
                                            "candidate_status": "COMPLETED"})
    rc = main(["--manifest", str(manifest), "--job-id", "123.4"])
    assert rc == 0
    result_dir = Path(spec.result_dir)
    assert (result_dir / "_FINISHED").exists()
    result = json.loads((result_dir / "result.json").read_text())
    assert result["candidate"] == 7
    assert result["execution"]["job_id"] == "123.4"
    assert result["execution"]["attempt"] == 2
    assert result["execution"]["host"]
    assert result["usage"] == []


def test_cli_catch_all_still_finishes(tmp_path: Path, monkeypatch):
    """A non-AgentError bug mid-run is a BUSINESS failure: result.json +
    _FINISHED must still appear so the backend never mistakes it for a
    lost job and wastes a retry."""
    spec = _spec(tmp_path)
    manifest = _write_manifest(tmp_path, spec)
    monkeypatch.setattr(worker_mod, "build_deps",
                        lambda cfg, run_dir, usage_observer=None: (
                            _deps(tmp_path)))

    def explode(_deps, _spec):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(worker_mod, "run_candidate", explode)
    rc = main(["--manifest", str(manifest)])
    assert rc == 0
    result_dir = Path(spec.result_dir)
    assert (result_dir / "_FINISHED").exists()
    result = json.loads((result_dir / "result.json").read_text())
    assert result["candidate_status"] == "WORKER_FAILED"
    assert "unexpected bug" in result["feedback"]


def test_cli_unreadable_manifest_is_infra_failure(tmp_path: Path):
    """No manifest -> no result_dir -> exit non-zero with NO _FINISHED:
    the only situation where a job-level retry is meaningful."""
    rc = main(["--manifest", str(tmp_path / "missing.json")])
    assert rc == 2


def test_candidate_accepted_gate_semantics():
    assert candidate_accepted("sha", {"G": True}, {"gates": [{"key": "G"}]})
    assert not candidate_accepted("sha", {"G": False},
                                  {"gates": [{"key": "G"}]})
    assert not candidate_accepted("sha", {}, {"gates": [{"key": "G"}]})
    assert not candidate_accepted(None, {"G": True},
                                  {"gates": [{"key": "G"}]})


def test_candidate_failure_shape(tmp_path: Path):
    failure = candidate_failure(3, _spec(tmp_path), "boom", "parent")
    assert failure["candidate_status"] == "WORKER_FAILED"
    assert failure["risk"] == "high"
    assert failure["accepted"] is False
    assert failure["base_sha"] == "parent"
