from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from simpleloop import app as loop_mod
from simpleloop.candidate import CandidateRequest, candidate_failure_from_request
from simpleloop.persistence.history import Store
from simpleloop.reporting.telemetry import RunTelemetry, processed_tokens
from simpleloop.stages.proposer import Proposal
from simpleloop.world import ProcessResult, SourceWorkspace
from round_helpers import append_round


class Clock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeSandbox:
    def preflight(self, _spec):
        pass


class _FakeWorld:
    def run(self, request):
        return ProcessResult(request.argv, 0, "", "", 0.1)


class _FakeWorldBuilder:
    def __init__(self, _sandbox):
        pass

    def build(self, _workspace, _sandbox, _world):
        return _FakeWorld()


class _FakeWorkspaceProvider:
    def __init__(self, run_dir, *_args, **_kwargs):
        self.repo = Path(run_dir) / "repo"

    def initialize(self):
        self.repo.mkdir(parents=True, exist_ok=True)
        return "baseline"

    def baseline_sha(self):
        return "baseline"

    def create(self, spec):
        path = self.repo.parent / "worktrees" / spec.workspace_id
        path.mkdir(parents=True, exist_ok=True)
        return SourceWorkspace(spec.workspace_id, path, spec.revision)

    def remove(self, _workspace):
        pass


def test_processed_tokens_sums_claude_usage_fields():
    assert processed_tokens({
        "input_tokens": 10,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 30,
        "output_tokens": 5,
    }) == 65


def test_processed_tokens_treats_missing_cache_fields_as_zero():
    assert processed_tokens({"input_tokens": 10, "output_tokens": 5}) == 15


@pytest.mark.parametrize(("usage", "expected"), [
    (None, 0),
    ({}, 0),
    ({"input_tokens": True, "output_tokens": 1}, 1),
    ({"input_tokens": -1, "output_tokens": 1}, 1),
    ({"input_tokens": 1, "output_tokens": None}, 1),
])
def test_processed_tokens_skips_missing_or_invalid_fields(usage, expected):
    assert processed_tokens(usage) == expected


def test_fresh_tracker_records_active_time_tokens_and_baseline(tmp_path):
    clock = Clock(10.0)
    tracker = RunTelemetry(tmp_path, clock=clock)
    clock.now = 15.0
    tracker.record_usage({"input_tokens": 4, "output_tokens": 1})
    tracker.set_baseline({"SPEED_MS": 100.0})

    expected = {
        "worktime_seconds": 5.0,
        "input_tokens": 4,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "processed_tokens": 5,
    }
    assert tracker.snapshot() == expected
    assert tracker.plot_context() == {
        "baseline_metrics": {"SPEED_MS": 100.0},
        "baseline_telemetry": expected,
    }


def test_missing_usage_is_skipped_without_poisoning_later_counts(tmp_path):
    tracker = RunTelemetry(tmp_path, clock=Clock())
    tracker.record_usage(None)
    tracker.record_usage({
        "input_tokens": 4,
        "output_tokens": None,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
    })

    assert tracker.snapshot() == {
        "worktime_seconds": 0.0,
        "input_tokens": 4,
        "output_tokens": 0,
        "cache_creation_input_tokens": 2,
        "cache_read_input_tokens": 3,
        "processed_tokens": 9,
    }
    assert not (tmp_path / "usage.jsonl").exists()


def test_resume_adds_only_new_active_segment_and_keeps_baseline(tmp_path):
    first_clock = Clock(0.0)
    first = RunTelemetry(tmp_path, clock=first_clock)
    first_clock.now = 8.0
    first.set_baseline({"SPEED_MS": 100.0})
    first.snapshot(persist=True)

    resumed_clock = Clock(1000.0)
    resumed = RunTelemetry(tmp_path, resume=True, clock=resumed_clock)
    resumed_clock.now = 1003.0
    resumed.set_baseline({"SPEED_MS": 999.0})

    assert resumed.snapshot()["worktime_seconds"] == 11.0
    assert resumed.plot_context()["baseline_metrics"] == {"SPEED_MS": 100.0}


def test_persisted_state_has_only_mvp_fields(tmp_path):
    tracker = RunTelemetry(tmp_path, clock=Clock())
    tracker.snapshot(persist=True)
    state = json.loads((tmp_path / "telemetry.json").read_text())
    assert state["worktime_seconds"] == 0.0
    assert state["processed_tokens"] == 0
    assert set(state) == {
        "worktime_seconds",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "processed_tokens",
        "baseline_metrics",
        "baseline_telemetry",
    }
    assert not (tmp_path / ".telemetry.tmp.json").exists()


def test_concurrent_usage_is_counted_once(tmp_path):
    tracker = RunTelemetry(tmp_path, clock=Clock())
    usage = {"input_tokens": 2, "output_tokens": 1}
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(tracker.record_usage, [usage] * 100))
    assert tracker.snapshot() == {
        "worktime_seconds": 0.0,
        "input_tokens": 200,
        "output_tokens": 100,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "processed_tokens": 300,
    }


def test_persistence_failure_warns_without_losing_in_memory_state(
    monkeypatch, tmp_path, capsys,
):
    tracker = RunTelemetry(tmp_path, clock=Clock())

    def fail_replace(*_args):
        raise OSError("boom")

    monkeypatch.setattr("simpleloop.reporting.telemetry.os.replace", fail_replace)
    tracker.record_usage({"input_tokens": 2, "output_tokens": 1})

    assert tracker.snapshot()["processed_tokens"] == 3
    assert "[telemetry] warning:" in capsys.readouterr().out


def test_missing_resume_state_keeps_resource_axes_unavailable(tmp_path):
    tracker = RunTelemetry(tmp_path, resume=True, clock=Clock())
    assert tracker.snapshot() == {
        "worktime_seconds": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "processed_tokens": 0,
    }


# ---- loop wiring persists telemetry (merged from test_run_telemetry.py) ----

def test_store_persists_candidate_and_generation_telemetry(tmp_path):
    store = Store(tmp_path, metrics_schema={
        "objective": {"key": "SPEED_MS", "lower_is_better": True}, "gates": []})
    candidate_snapshot = {"worktime_seconds": 2.0, "processed_tokens": 10}
    generation_snapshot = {"worktime_seconds": 3.0, "processed_tokens": 12}

    append_round(store,
        0,
        parent_sha="base",
        selected_candidate=0,
        selected_sha="sha",
        candidates=[{
            "candidate": 0,
            "sha": "sha",
            "telemetry": candidate_snapshot,
        }],
        telemetry=generation_snapshot,
    )

    row = store.history()[0]
    assert row["telemetry"] == generation_snapshot
    assert row["candidates"][0]["telemetry"] == candidate_snapshot
