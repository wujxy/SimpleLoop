from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from simpleloop.telemetry import RunTelemetry, processed_tokens


class Clock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_processed_tokens_sums_claude_usage_fields():
    assert processed_tokens({
        "input_tokens": 10,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 30,
        "output_tokens": 5,
    }) == 65


def test_processed_tokens_treats_missing_cache_fields_as_zero():
    assert processed_tokens({"input_tokens": 10, "output_tokens": 5}) == 15


@pytest.mark.parametrize("usage", [
    None,
    {},
    {"input_tokens": True, "output_tokens": 1},
    {"input_tokens": -1, "output_tokens": 1},
    {"input_tokens": 1},
])
def test_processed_tokens_rejects_incomplete_or_invalid_usage(usage):
    assert processed_tokens(usage) is None


def test_fresh_tracker_records_active_time_tokens_and_baseline(tmp_path):
    clock = Clock(10.0)
    tracker = RunTelemetry(tmp_path, clock=clock)
    clock.now = 15.0
    tracker.record_usage({"input_tokens": 4, "output_tokens": 1})
    tracker.set_baseline({"SPEED_MS": 100.0})

    assert tracker.snapshot() == {
        "worktime_seconds": 5.0,
        "processed_tokens": 5,
    }
    assert tracker.plot_context() == {
        "baseline_metrics": {"SPEED_MS": 100.0},
        "baseline_telemetry": {
            "worktime_seconds": 5.0,
            "processed_tokens": 5,
        },
    }


def test_missing_usage_makes_tokens_permanently_unavailable(tmp_path):
    tracker = RunTelemetry(tmp_path, clock=Clock())
    tracker.record_usage(None)
    tracker.record_usage({"input_tokens": 4, "output_tokens": 1})
    assert tracker.snapshot()["processed_tokens"] is None


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
    assert tracker.snapshot()["processed_tokens"] == 300


def test_persistence_failure_warns_without_losing_in_memory_state(
    monkeypatch, tmp_path, capsys,
):
    tracker = RunTelemetry(tmp_path, clock=Clock())

    def fail_replace(*_args):
        raise OSError("boom")

    monkeypatch.setattr("simpleloop.telemetry.os.replace", fail_replace)
    tracker.record_usage({"input_tokens": 2, "output_tokens": 1})

    assert tracker.snapshot()["processed_tokens"] == 3
    assert "[telemetry] warning:" in capsys.readouterr().out


def test_missing_resume_state_keeps_resource_axes_unavailable(tmp_path):
    tracker = RunTelemetry(tmp_path, resume=True, clock=Clock())
    assert tracker.snapshot() == {
        "worktime_seconds": None,
        "processed_tokens": None,
    }
