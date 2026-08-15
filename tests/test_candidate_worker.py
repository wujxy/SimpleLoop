"""Candidate handler tests; transport is covered by test_scheduling_worker."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from simpleloop.candidate import CandidateStatus, candidate_failure_from_request
from simpleloop.scheduling.handlers import candidate as handler
from simpleloop.scheduling.handlers.candidate import CandidateSpec
from simpleloop.stages.executor import parse_self_report
from simpleloop.world import MountMode, SourceWorkspace


def _spec(tmp_path: Path, **overrides) -> CandidateSpec:
    values = dict(
        round_id=3, candidate_id=7, parent_sha="abc123",
        proposal="do the thing", worktree_path=str(tmp_path / "wt"),
        result_dir=str(tmp_path / "result"), attempt=2,
    )
    values.update(overrides)
    return CandidateSpec(**values)


def test_spec_serialization_round_trip(tmp_path: Path):
    spec = _spec(tmp_path, prompt_dir=str(tmp_path / "prompts"))
    assert CandidateSpec.from_dict(json.loads(json.dumps(spec.to_dict()))) == spec


def test_spec_tolerates_unknown_payload_fields(tmp_path: Path):
    raw = _spec(tmp_path).to_dict()
    raw["future_field"] = True
    assert CandidateSpec.from_dict(raw).candidate_id == 7


def test_build_ports_passes_external_read_only_binds_to_executor(tmp_path: Path):
    cfg = {
        "runtime_image": tmp_path / "runtime.sif",
        "runtime_binds": [tmp_path / "evaluation-data"],
        "read_only_binds": [tmp_path / "executor-data"],
        "editable_paths": ["src"],
        "repo_path": tmp_path / "repo", "baseline_ref": "HEAD",
    }
    for path in (
        tmp_path / "wt", tmp_path / "wt" / "src",
        tmp_path / "evaluation-data", tmp_path / "executor-data",
    ):
        path.mkdir(parents=True, exist_ok=True)
    workspace = SourceWorkspace("3-c7", tmp_path / "wt", "abc123")

    ports = handler.build_ports(cfg, tmp_path / "run", workspace)

    assert any(
        mount.source == tmp_path / "executor-data"
        and mount.mode is MountMode.READ_ONLY
        for mount in ports.executor.agent.world.sandbox.mounts
    )


def test_handler_delegates_business_to_candidate_pipeline(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.resolved.json").write_text("{}", encoding="utf-8")
    spec = _spec(tmp_path, run_dir=str(run_dir))
    expected = candidate_failure_from_request(spec.to_request(), "delegated")
    seen = []
    monkeypatch.setattr(
        handler, "build_ports",
        lambda *a, **k: SimpleNamespace(
            executor=object(), artifacts=object(), evaluator=object(),
            gate_spec=object(), trace=object(), preflight=lambda: None,
        ),
    )
    monkeypatch.setattr(
        handler, "run_candidate_guarded",
        lambda request, **ports: seen.append(request) or expected,
    )

    result = handler.handle_candidate(spec.to_dict(), lambda row: None)

    assert len(seen) == 1
    assert seen[0].proposal.instruction == "do the thing"
    assert result["status"] == "WORKER_FAILED"


def test_candidate_failure_shape(tmp_path: Path):
    failure = candidate_failure_from_request(
        _spec(tmp_path, candidate_id=3, parent_sha="parent").to_request(), "boom"
    )
    assert failure.status is CandidateStatus.WORKER_FAILED
    assert failure.parent_sha == "parent"
    assert failure.gate.passed is False
    assert failure.eligible is False
    assert failure.execution.self_report is None


class TestParseSelfReport:
    def test_valid_completed(self):
        text = ('did the work\n```json\n{"outcome": "completed", '
                '"summary": "cached the constants"}\n```')
        assert parse_self_report(text) == {
            "outcome": "completed", "blocked_reason_kind": None,
            "summary": "cached the constants",
            "fidelity": "", "local_runs": [],
        }

    def test_blocked_objective_kind(self):
        text = ('```json\n{"outcome": "blocked", "blocked_reason_kind": '
                '"objective", "summary": "target fn absent"}\n```')
        assert parse_self_report(text)["blocked_reason_kind"] == "objective"

    def test_effort_kind(self):
        text = ('```json\n{"outcome": "partial", "blocked_reason_kind": '
                '"effort", "summary": "ran out of budget"}\n```')
        assert parse_self_report(text)["blocked_reason_kind"] == "effort"

    def test_invalid_outcome_returns_none(self):
        assert parse_self_report(
            '```json\n{"outcome": "done", "summary": "x"}\n```'
        ) is None

    def test_unknown_kind_normalized_to_none(self):
        text = ('```json\n{"outcome": "blocked", "blocked_reason_kind": '
                '"too_hard", "summary": "x"}\n```')
        assert parse_self_report(text)["blocked_reason_kind"] is None

    @pytest.mark.parametrize("text", ["just prose", "", "```json\n[]\n```"])
    def test_missing_or_non_object_returns_none(self, text: str):
        assert parse_self_report(text) is None

    def test_last_outcome_block_wins(self):
        text = ('```json\n{"outcome": "completed", "summary": "first"}\n```\n'
                '```json\n{"outcome": "blocked", "summary": "second"}\n```')
        assert parse_self_report(text)["summary"] == "second"

    def test_summary_truncated(self):
        text = ('```json\n{"outcome": "completed", "summary": "'
                + "x" * 1000 + '"}\n```')
        assert len(parse_self_report(text)["summary"]) == 600
