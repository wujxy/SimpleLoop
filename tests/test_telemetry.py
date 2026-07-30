from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from simpleloop import loop as loop_mod
from simpleloop.loop import RunContext, _finalize_candidates
from simpleloop.harness.store import Store
from simpleloop.reporting.telemetry import RunTelemetry, processed_tokens


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


def test_resume_from_legacy_null_total_starts_known_category_counts(tmp_path):
    (tmp_path / "telemetry.json").write_text(json.dumps({
        "worktime_seconds": 10.0,
        "processed_tokens": None,
    }))
    tracker = RunTelemetry(tmp_path, resume=True, clock=Clock())

    tracker.record_usage({"output_tokens": 7})

    assert tracker.snapshot()["output_tokens"] == 7
    assert tracker.snapshot()["processed_tokens"] == 7


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

def test_fresh_run_wires_agents_and_persists_fixed_baseline(
    monkeypatch, tmp_path,
):
    run_dir = tmp_path / "run"
    config = {
        "goal": "make it faster",
        "max_rounds": 0,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 10,
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "eval_commands": ["eval"],
        "metrics": {
            "objective": {"key": "SPEED_MS", "lower_is_better": True},
            "gates": [],
        },
        "runtime_image": tmp_path / "runtime.sif",
        "runtime_binds": [],
    }
    observers = []

    class FakeRuntime:
        def __init__(self, **_kwargs):
            pass

        def summary_lines(self):
            return ()

        def preflight(self):
            pass

    class FakeAgent:
        def __init__(self, **kwargs):
            observers.append(kwargs.get("usage_observer"))

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline"

    monkeypatch.setattr(loop_mod.config_mod, "load", lambda _path: config)
    monkeypatch.setattr(loop_mod, "ApptainerRuntime", FakeRuntime)
    monkeypatch.setattr(loop_mod, "Agent", FakeAgent)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)
    monkeypatch.setattr(
        loop_mod,
        "_eval_baseline",
        lambda *_args: ("baseline eval", {"SPEED_MS": 100.0}),
    )

    loop_mod.run("config.yaml", run_dir)

    assert len(observers) == 3
    assert all(callable(observer) for observer in observers)
    state = json.loads((run_dir / "telemetry.json").read_text())
    assert state["baseline_metrics"] == {"SPEED_MS": 100.0}
    assert state["baseline_telemetry"]["processed_tokens"] == 0

    observers[0]({"input_tokens": 2, "output_tokens": 1})
    updated = json.loads((run_dir / "telemetry.json").read_text())
    assert updated["processed_tokens"] == 3


# ---- store/candidate telemetry integration (merged from test_telemetry_integration.py) ----

class SnapshotTracker:
    def __init__(self):
        self.value = 0
        self.persist_flags = []
        self.recorded = []

    def record_usage(self, usage):
        self.recorded.append(usage)

    def snapshot(self, *, persist=False):
        self.value += 1
        self.persist_flags.append(persist)
        return {
            "worktime_seconds": float(self.value),
            "processed_tokens": self.value * 10,
        }

    def plot_context(self):
        return {}


def test_store_persists_candidate_and_generation_telemetry(tmp_path):
    store = Store(tmp_path, metrics_schema={
        "objective": {"key": "SPEED_MS", "lower_is_better": True}, "gates": []})
    candidate_snapshot = {"worktime_seconds": 2.0, "processed_tokens": 10}
    generation_snapshot = {"worktime_seconds": 3.0, "processed_tokens": 12}

    store.append_generation(
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


def test_finalize_candidates_ingests_usage_and_stamps_snapshots():
    """The loop (not the backend) owns telemetry: worker-reported usage is
    popped from the candidate and recorded, then each candidate gets a
    persisted snapshot for its history row. On ihep_scale this is the
    HEPJobBackend usage-ingest path (main removed it because it has no
    remote backend)."""
    tracker = SnapshotTracker()
    ctx = RunContext(cfg={}, telemetry=tracker)
    candidates = [
        {"candidate": 0, "score": 0.5,
         "usage": [{"input_tokens": 3, "output_tokens": 1}]},
        {"candidate": 1, "score": 0.4,
         "usage": [{"input_tokens": 5, "output_tokens": 2}]},
    ]

    _finalize_candidates(ctx, candidates)

    assert tracker.recorded == [
        {"input_tokens": 3, "output_tokens": 1},
        {"input_tokens": 5, "output_tokens": 2},
    ]
    assert all("usage" not in c for c in candidates)
    assert {
        c["telemetry"]["worktime_seconds"] for c in candidates
    } == {1.0, 2.0}
    assert tracker.persist_flags == [True, True]
