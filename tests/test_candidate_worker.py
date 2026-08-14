"""CandidateWorker unit tests: spec serialization, run_candidate business
status, the standalone CLI's atomic result contract, and the catch-all
invariant (any business failure still yields result.json + _FINISHED)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop import candidate_worker as worker_mod
from simpleloop.candidate import CandidateStatus
from simpleloop.candidate_worker import (
    CandidateDeps,
    CandidateSpec,
    candidate_failure,
    main,
    run_candidate,
    write_result,
)
from simpleloop.harness.evals import EvalResult
from simpleloop.roles import executor as exec_mod
from simpleloop.roles.executor import ExecResult, parse_self_report


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


def test_build_deps_passes_external_read_only_binds_to_executor(tmp_path: Path):
    cfg = {
        "runtime_image": tmp_path / "runtime.sif",
        "runtime_binds": [tmp_path / "evaluation-data"],
        "read_only_binds": [tmp_path / "executor-data"],
        "editable_paths": ["src"],
        "repo_path": tmp_path / "repo",
        "baseline_ref": "HEAD",
    }

    deps = worker_mod.build_deps(cfg, tmp_path / "run")

    assert deps.executor_agent.mounts.external_ro == (
        tmp_path / "executor-data",
    )


def test_spec_tolerates_unknown_manifest_fields(tmp_path: Path):
    data = _spec(tmp_path).to_dict()
    data["decision"] = "legacy semantic field"
    # from_dict ignores unknown fields so --continue across the S2c version
    # boundary (legacy manifests written before S2c carry `finding_id`) does
    # not crash; manifests are machine-generated.
    spec = CandidateSpec.from_dict(data)
    assert spec.round_id == data["round_id"]


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
    assert result.status is CandidateStatus.COMPLETED
    assert result.sha == "def456"
    assert result.parent_sha == "abc123"
    assert result.gate.passed is True
    assert result.eligible is True
    assert result.gate.results["EVAL_COMMANDS"].passed is True
    assert result.metrics["SPEED_MS"] == 100.0
    assert not hasattr(result, "selected")


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
    assert result.status is CandidateStatus.NO_CHANGE
    assert result.eligible is False
    assert result.gate.results["EVAL_COMMANDS"].passed is None
    assert result.gate.results["EVAL_COMMANDS"].detail == (
        "not run because Executor produced no change"
    )


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
    assert result.status is CandidateStatus.GATE_REJECTED
    assert result.gate.results["EVAL_COMMANDS"].passed is False
    assert result.gate.passed is False
    assert result.eligible is False


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

    assert result.status is CandidateStatus.EVAL_FAILED
    assert result.sha == "def456"
    assert result.gate.results["PATHS"].passed is True
    assert result.gate.results["EVAL_COMMANDS"].passed is False
    assert result.gate.results["CORRECTNESS"].passed is None


def test_write_result_is_atomic_and_marks_finished(tmp_path: Path):
    out = tmp_path / "r"
    result = candidate_failure(0, _spec(tmp_path), "boom", "parent")
    write_result(out, result)
    assert json.loads((out / "result.json").read_text())["candidate"] == 0
    assert (out / "_FINISHED").exists()
    assert not (out / "result.json.tmp").exists()


def _write_manifest(tmp_path: Path, spec: CandidateSpec) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    (run_dir / "config.resolved.json").write_text("{}", encoding="utf-8")
    manifest = {**spec.to_dict(), "run_dir": str(run_dir)}
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
    monkeypatch.setattr(
        worker_mod,
        "run_candidate",
        lambda deps, spec: candidate_failure(
            spec.candidate_id, spec, "test terminal", spec.parent_sha,
        ),
    )
    rc = main(["--manifest", str(manifest), "--job-id", "123.4"])
    assert rc == 0
    result_dir = Path(spec.result_dir)
    assert (result_dir / "_FINISHED").exists()
    result = json.loads((result_dir / "result.json").read_text())
    # result.json is pure business; telemetry/audit live in the sidecar.
    assert result["candidate"] == 7
    assert result["status"] == "WORKER_FAILED"
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
    assert failure.status is CandidateStatus.WORKER_FAILED
    assert failure.parent_sha == "parent"
    assert failure.gate.passed is False
    assert failure.eligible is False
    # No executor ran, so there is no self-report.
    assert failure.execution.self_report is None


# ---------------------------------------------------------------------------
# Executor SELF_REPORT: the executor's only channel to tell the loop whether
# it finished and why a change is absent/incomplete. Best-effort parse; the
# objective/effort partition is the cut that lets a future proposer-feed treat
# "target doesn't exist" (objective, safe to relay) differently from "too hard"
# (effort, a merit judgment that must stay record-only).
# ---------------------------------------------------------------------------
class TestParseSelfReport:
    def test_valid_completed(self):
        text = ('did the work\n```json\n{"outcome": "completed", '
                '"summary": "cached the constants"}\n```')
        assert parse_self_report(text) == {
            "outcome": "completed", "blocked_reason_kind": None,
            "summary": "cached the constants",
        }

    def test_blocked_objective_kind(self):
        text = ('```json\n{"outcome": "blocked", "blocked_reason_kind": '
                '"objective", "summary": "target fn absent"}\n```')
        r = parse_self_report(text)
        assert r["outcome"] == "blocked"
        assert r["blocked_reason_kind"] == "objective"

    def test_effort_kind(self):
        text = ('```json\n{"outcome": "partial", "blocked_reason_kind": '
                '"effort", "summary": "ran out of budget"}\n```')
        assert parse_self_report(text)["blocked_reason_kind"] == "effort"

    def test_invalid_outcome_returns_none(self):
        text = '```json\n{"outcome": "done", "summary": "x"}\n```'
        assert parse_self_report(text) is None

    def test_unknown_kind_normalized_to_none(self):
        # "too_hard" is a merit judgment, not one of the two objective kinds;
        # it must not survive as a structured field (it could later be mistaken
        # for an objective, relayable fact).
        text = ('```json\n{"outcome": "blocked", "blocked_reason_kind": '
                '"too_hard", "summary": "x"}\n```')
        assert parse_self_report(text)["blocked_reason_kind"] is None

    @pytest.mark.parametrize("text", ["just prose, no fence", "", "```json\n[]\n```"])
    def test_missing_or_non_object_returns_none(self, text: str):
        assert parse_self_report(text) is None

    def test_ignores_unrelated_json_without_outcome(self):
        text = '```json\n{"metrics": {"speed": 100}}\n```'
        assert parse_self_report(text) is None

    def test_last_outcome_block_wins(self):
        text = ('```json\n{"outcome": "completed", "summary": "first"}\n```\n'
                'more text\n```json\n{"outcome": "blocked", '
                '"blocked_reason_kind": "objective", "summary": "second"}\n```')
        assert parse_self_report(text)["summary"] == "second"

    def test_summary_truncated(self):
        long_summary = "x" * 1000
        text = ('```json\n{"outcome": "completed", "summary": "'
                + long_summary + '"}\n```')
        assert len(parse_self_report(text)["summary"]) == 600


def test_self_report_flows_into_candidate_record(tmp_path: Path, monkeypatch):
    """A self-report on the ExecResult lands in the candidate dict that reaches
    history (the record channel); it does NOT affect status/eligibility."""
    report = {"outcome": "blocked", "blocked_reason_kind": "objective",
              "summary": "target absent"}

    def fake_execute(*_a, **_k):
        return ExecResult(
            sha=None, reason="executor made no changes", changed_paths=[],
            path_gate_passed=True, path_gate_violations=[],
            self_report=report)

    monkeypatch.setattr(worker_mod.executor_mod, "execute", fake_execute)
    monkeypatch.setattr(worker_mod.evals, "run_eval", lambda *a, **k: None)
    result = run_candidate(_deps(tmp_path), _spec(tmp_path))
    assert result.status is CandidateStatus.NO_CHANGE
    assert result.eligible is False
    assert result.execution.self_report == report


def test_execute_parses_self_report_from_agent_output(tmp_path: Path,
                                                       monkeypatch):
    """execute() runs parse_self_report on the agent's text and attaches it to
    ExecResult on every return path (here: the committed path)."""
    class FakeAgent:
        def run_text(self, _prompt, cwd=None, label=None):
            return ('work\n```json\n{"outcome": "completed", '
                    '"summary": "done"}\n```')

    class FakeWorkspace:
        def changed_paths(self, _wt):
            return ["a.cc"]

        def commit(self, _wt, _rid, _paths):
            return "sha1"

    result = exec_mod.execute(
        FakeAgent(), proposal="p", goal="g",
        workspace=FakeWorkspace(), worktree=tmp_path, round_id="r1")
    assert result.sha == "sha1"
    assert result.self_report == {"outcome": "completed",
                                  "blocked_reason_kind": None,
                                  "summary": "done"}
