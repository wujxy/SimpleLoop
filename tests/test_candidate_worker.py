"""CandidateWorker unit tests: spec serialization, run_candidate business
status, the standalone CLI's atomic result contract, and the catch-all
invariant (any business failure still yields result.json + _FINISHED)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from simpleloop import candidate_worker as worker_mod
from simpleloop.candidate import (
    CandidateStatus,
    candidate_failure_from_request,
)
from simpleloop.candidate_worker import (
    CandidateSpec,
    main,
    write_result,
)
from simpleloop.stages.executor import parse_self_report


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

    ports = worker_mod.build_ports(cfg, tmp_path / "run")

    assert ports.executor.agent.mounts.external_ro == (
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


def test_write_result_is_atomic_and_marks_finished(tmp_path: Path):
    out = tmp_path / "r"
    result = candidate_failure_from_request(
        _spec(tmp_path, candidate_id=0, parent_sha="parent").to_request(),
        "boom",
    )
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
    expected = candidate_failure_from_request(
        spec.to_request(), "test terminal",
    )
    monkeypatch.setattr(
        worker_mod,
        "build_ports",
        lambda cfg, run_dir, usage_observer=None, prompt_dir=None: (
            SimpleNamespace(
                executor=object(), artifacts=object(), evaluator=object(),
                gate_spec=object(), trace=object(), preflight=lambda: None,
            )
        ),
    )
    monkeypatch.setattr(
        worker_mod,
        "run_candidate_guarded",
        lambda request, **ports: expected,
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


def test_worker_delegates_business_to_shared_pipeline(
    tmp_path: Path,
    monkeypatch,
):
    spec = _spec(tmp_path)
    manifest = _write_manifest(tmp_path, spec)
    seen = []
    expected = candidate_failure_from_request(
        spec.to_request(), "delegated",
    )

    monkeypatch.setattr(
        worker_mod,
        "build_ports",
        lambda cfg, run_dir, usage_observer=None, prompt_dir=None: (
            SimpleNamespace(
                executor=object(), artifacts=object(), evaluator=object(),
                gate_spec=object(), trace=object(), preflight=lambda: None,
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        worker_mod,
        "run_candidate_guarded",
        lambda request, **ports: seen.append(request) or expected,
        raising=False,
    )

    assert main(["--manifest", str(manifest)]) == 0
    assert len(seen) == 1
    assert seen[0].proposal.instruction == "do the thing"
    assert seen[0].parent_sha == "abc123"


def test_cli_catch_all_still_finishes(tmp_path: Path, monkeypatch):
    """A non-AgentError bug mid-run is a BUSINESS failure: result.json +
    _FINISHED must still appear so the backend never mistakes it for a
    lost job and wastes a retry."""
    spec = _spec(tmp_path)
    manifest = _write_manifest(tmp_path, spec)
    monkeypatch.setattr(
        worker_mod,
        "build_ports",
        lambda cfg, run_dir, usage_observer=None, prompt_dir=None: (
            SimpleNamespace(
                executor=object(), artifacts=object(), evaluator=object(),
                gate_spec=None, trace=object(), preflight=lambda: None,
            )
        ),
    )

    def explode(request, **ports):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(worker_mod, "run_candidate_guarded", explode)
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


def test_cli_malformed_manifest_with_result_dir_still_finishes(tmp_path: Path):
    result_dir = tmp_path / "result"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.resolved.json").write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "candidate_id": 2,
        "round_id": 1,
        "parent_sha": "parent",
        "run_dir": str(run_dir),
        "result_dir": str(result_dir),
    }), encoding="utf-8")

    assert main(["--manifest", str(manifest)]) == 0
    assert (result_dir / "_FINISHED").exists()
    result = json.loads((result_dir / "result.json").read_text())
    assert result["status"] == "WORKER_FAILED"
    assert result["proposal"] == "<missing proposal>"


def test_candidate_failure_shape(tmp_path: Path):
    failure = candidate_failure_from_request(
        _spec(tmp_path, candidate_id=3, parent_sha="parent").to_request(),
        "boom",
    )
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
