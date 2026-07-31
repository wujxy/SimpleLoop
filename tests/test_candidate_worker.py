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
    candidate_failure,
    main,
    run_candidate,
    write_result,
)
from simpleloop.harness.evals import EvalResult
from simpleloop.roles.executor import ExecResult


_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
           "gates": [{"key": "CORRECTNESS"}]}


def _spec(tmp_path: Path, **overrides) -> CandidateSpec:
    values = dict(
        round_id=3, candidate_id=7, parent_sha="abc123",
        proposal="do the thing",
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
        executor_agent=object(), gate_lines="",
    )


def test_spec_serialization_round_trip(tmp_path: Path):
    spec = _spec(tmp_path, prompt_dir=str(tmp_path / "prompts"))
    again = CandidateSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert again == spec


def test_spec_ignores_legacy_semantic_fields(tmp_path: Path):
    data = _spec(tmp_path).to_dict()
    data.update({
        "family": "layout",
        "decision": "switch",
        "prior_metrics": {"SPEED_MS": 150.0},
        "baseline_metrics": {"SPEED_MS": 200.0},
    })

    assert CandidateSpec.from_dict(data) == _spec(tmp_path)


def test_run_candidate_completed(tmp_path: Path, monkeypatch):
    def fake_execute(*_args, **kwargs):
        return ExecResult(
            sha="def456", reason=None, changed_paths=["a.cc"],
            path_gate_passed=True, path_gate_violations=[],
        )

    monkeypatch.setattr(worker_mod.executor_mod, "execute", fake_execute)
    monkeypatch.setattr(worker_mod.evals, "run_eval",
                        lambda *a, **k: EvalResult(
                            "eval", {"SPEED_MS": 100.0, "CORRECTNESS": True},
                            (0,)))
    deps = _deps(tmp_path)
    deps.prompt_dir = tmp_path / "prompts"
    result = run_candidate(deps, _spec(tmp_path))
    assert result["status"] == "COMPLETED"
    assert result["sha"] == "def456"
    assert result["parent_sha"] == "abc123"
    assert result["gate_passed"] is True
    assert result["eligible"] is True
    assert result["gates"]["EVAL_COMMANDS"]["passed"] is True
    assert result["metrics"]["SPEED_MS"] == 100.0
    assert not ({"score", "risk", "feedback", "feedback_for_proposer",
                 "accepted"} & result.keys())


@pytest.mark.parametrize("objective", [float("nan"), float("inf")])
def test_candidate_with_nonfinite_objective_is_ineligible(objective: float):
    assert worker_mod._eligible(
        "candidate", True, {"SPEED_MS": objective}, _SCHEMA,
    ) is False


def test_run_candidate_no_change_skips_eval(tmp_path: Path, monkeypatch):
    called = False

    def fake_eval(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(worker_mod.evals, "run_eval", fake_eval)
    monkeypatch.setattr(worker_mod.executor_mod, "execute",
                        lambda *a, **k: ExecResult(
                            sha=None, reason="executor made no changes",
                            changed_paths=[], path_gate_passed=True,
                            path_gate_violations=[]))
    result = run_candidate(_deps(tmp_path), _spec(tmp_path))
    assert called is False
    assert result["status"] == "NO_CHANGE"
    assert result["eligible"] is False
    assert result["gates"]["EVAL_COMMANDS"] == {
        "passed": None,
        "detail": "not run because Executor produced no change",
    }


def test_path_gate_rejection_is_terminal_and_skips_eval(tmp_path: Path,
                                                         monkeypatch):
    called = False

    def fake_eval(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(worker_mod.evals, "run_eval", fake_eval)

    monkeypatch.setattr(worker_mod.executor_mod, "execute",
                        lambda *a, **k: ExecResult(
                            sha=None, reason="gate rejected: frozen paths",
                            changed_paths=["tests/x.py"],
                            path_gate_passed=False,
                            path_gate_violations=[
                                "tests/x.py: touches a frozen path",
                            ]))
    result = run_candidate(_deps(tmp_path), _spec(tmp_path))
    assert called is False
    assert result["status"] == "PATH_GATE_REJECTED"
    assert result["sha"] is None
    assert result["gates"]["PATHS"]["passed"] is False
    assert result["gates"]["CORRECTNESS"]["passed"] is None


def test_nonzero_eval_command_is_a_gate_rejection(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(worker_mod.executor_mod, "execute",
                        lambda *a, **k: ExecResult(
                            sha="def456", reason=None, changed_paths=["a.cc"],
                            path_gate_passed=True, path_gate_violations=[]))
    monkeypatch.setattr(worker_mod.evals, "run_eval",
                        lambda *a, **k: EvalResult(
                            "failed", {"SPEED_MS": 90.0,
                                       "CORRECTNESS": True}, (7,)))
    result = run_candidate(_deps(tmp_path), _spec(tmp_path))
    assert result["status"] == "GATE_REJECTED"
    assert result["gates"]["EVAL_COMMANDS"]["passed"] is False
    assert result["gate_passed"] is False
    assert result["eligible"] is False


def test_eval_exception_retains_sha_and_factual_failure(tmp_path: Path,
                                                        monkeypatch):
    monkeypatch.setattr(worker_mod.executor_mod, "execute",
                        lambda *a, **k: ExecResult(
                            sha="def456", reason=None, changed_paths=["a.cc"],
                            path_gate_passed=True, path_gate_violations=[]))
    monkeypatch.setattr(worker_mod.evals, "run_eval",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("container unavailable")))

    result = run_candidate(_deps(tmp_path), _spec(tmp_path))

    assert result["status"] == "EVAL_FAILED"
    assert result["sha"] == "def456"
    assert result["gates"]["PATHS"]["passed"] is True
    assert result["gates"]["EVAL_COMMANDS"]["passed"] is False
    assert result["gates"]["CORRECTNESS"]["passed"] is None


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
                        lambda cfg, run_dir, usage_observer=None,
                        prompt_dir=None: (
                            _deps(tmp_path)))
    monkeypatch.setattr(worker_mod, "run_candidate",
                        lambda deps, spec: {"candidate": spec.candidate_id,
                                            "status": "COMPLETED"})
    rc = main(["--manifest", str(manifest), "--job-id", "123.4"])
    assert rc == 0
    result_dir = Path(spec.result_dir)
    assert (result_dir / "_FINISHED").exists()
    result = json.loads((result_dir / "result.json").read_text())
    # result.json is pure business; telemetry/audit live in the sidecar.
    assert result == {"candidate": 7, "status": "COMPLETED"}
    sidecar = json.loads((result_dir / "usage.json").read_text())
    assert sidecar["execution"]["job_id"] == "123.4"
    assert sidecar["execution"]["attempt"] == 2
    assert sidecar["execution"]["host"]
    assert sidecar["usage"] == []


def test_cli_catch_all_still_finishes(tmp_path: Path, monkeypatch):
    """A non-AgentError bug mid-run is a BUSINESS failure: result.json +
    _FINISHED must still appear so the backend never mistakes it for a
    lost job and wastes a retry."""
    spec = _spec(tmp_path)
    manifest = _write_manifest(tmp_path, spec)
    monkeypatch.setattr(worker_mod, "build_deps",
                        lambda cfg, run_dir, usage_observer=None,
                        prompt_dir=None: (
                            _deps(tmp_path)))

    def explode(_deps, _spec):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(worker_mod, "run_candidate", explode)
    rc = main(["--manifest", str(manifest)])
    assert rc == 0
    result_dir = Path(spec.result_dir)
    assert (result_dir / "_FINISHED").exists()
    result = json.loads((result_dir / "result.json").read_text())
    assert result["status"] == "WORKER_FAILED"
    assert "unexpected bug" in result["eval_block"]


def test_cli_unreadable_manifest_is_infra_failure(tmp_path: Path):
    """No manifest -> no result_dir -> exit non-zero with NO _FINISHED:
    the only situation where a job-level retry is meaningful."""
    rc = main(["--manifest", str(tmp_path / "missing.json")])
    assert rc == 2


def test_candidate_failure_shape(tmp_path: Path):
    failure = candidate_failure(3, _spec(tmp_path), "boom", "parent")
    assert failure["status"] == "WORKER_FAILED"
    assert failure["parent_sha"] == "parent"
    assert failure["gate_passed"] is False
    assert failure["eligible"] is False
