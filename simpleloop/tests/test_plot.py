from __future__ import annotations

import math

from simpleloop import loop as loop_mod
from simpleloop import plot as plot_mod
from simpleloop.judger import _parse as parse_judgment
from simpleloop.plot import build_series
from simpleloop.store import Store


SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "CORRECTNESS"}],
}


def test_build_series_tracks_parallel_candidates_selected_and_incumbent():
    history = [
        {
            "round": 0,
            "selected_candidate": 1,
            "selected_sha": "winner",
            "candidates": [
                {
                    "candidate": 0,
                    "score": 0.4,
                    "metrics": {"SPEED_MS": 120.0},
                },
                {
                    "candidate": 1,
                    "score": 0.8,
                    "metrics": {"SPEED_MS": 90.0},
                },
            ],
        },
        {
            "round": 1,
            "selected_candidate": None,
            "selected_sha": None,
            "candidates": [
                {
                    "candidate": 0,
                    "score": 0.3,
                    "metrics": {"SPEED_MS": 110.0},
                },
            ],
        },
    ]

    series = build_series(history, SCHEMA)

    assert series.rounds == [1, 2]
    assert series.score_points == [(1, 0.4), (1, 0.8), (2, 0.3)]
    assert series.selected_scores == [(1, 0.8)]
    assert series.objective_points == [(1, 120.0), (1, 90.0), (2, 110.0)]
    assert series.selected_objectives == [(1, 90.0)]
    assert series.incumbent_objective == [(1, 90.0), (2, 90.0)]
    assert series.objective_key == "SPEED_MS"
    assert series.lower_is_better is True


def test_build_series_supports_serial_history_and_missing_values():
    history = [
        {
            "round": 0,
            "accepted": True,
            "score": 0.7,
            "metrics": {"QUALITY": 10.0},
        },
        {
            "round": 1,
            "accepted": False,
            "score": None,
            "metrics": {"QUALITY": "unknown"},
        },
        {
            "round": 2,
            "accepted": True,
            "score": True,
            "metrics": {"QUALITY": 12},
        },
    ]
    schema = {
        "objective": {"key": "QUALITY", "lower_is_better": False},
        "gates": [],
    }

    series = build_series(history, schema)

    assert series.rounds == [1, 2, 3]
    assert series.score_points == [(1, 0.7)]
    assert series.selected_scores == [(1, 0.7)]
    assert series.objective_points == [(1, 10.0), (3, 12.0)]
    assert series.selected_objectives == [(1, 10.0), (3, 12.0)]
    assert series.incumbent_objective == [(1, 10.0), (2, 10.0), (3, 12.0)]
    assert series.objective_key == "QUALITY"
    assert series.lower_is_better is False


def test_build_series_without_objective_schema_still_tracks_scores():
    history = [
        {
            "round": 0,
            "accepted": True,
            "score": 0.6,
            "metrics": {"SPEED_MS": 100.0},
        },
    ]

    series = build_series(history, None)

    assert series.score_points == [(1, 0.6)]
    assert series.selected_scores == [(1, 0.6)]
    assert series.objective_points == []
    assert series.incumbent_objective == []
    assert series.objective_key is None
    assert series.lower_is_better is None


def test_build_series_requires_selected_sha_to_advance_parallel_incumbent():
    history = [
        {
            "round": 0,
            "selected_candidate": 0,
            "selected_sha": None,
            "candidates": [
                {
                    "candidate": 0,
                    "score": 0.8,
                    "metrics": {"SPEED_MS": 90.0},
                },
            ],
        },
    ]

    series = build_series(history, SCHEMA)

    assert series.selected_scores == [(1, 0.8)]
    assert series.selected_objectives == [(1, 90.0)]
    assert series.incumbent_objective == []


def test_build_series_skips_non_finite_values():
    history = [
        {
            "round": 0,
            "accepted": True,
            "score": math.nan,
            "metrics": {"SPEED_MS": math.inf},
        },
    ]

    series = build_series(history, SCHEMA)

    assert series.score_points == []
    assert series.objective_points == []
    assert series.incumbent_objective == []


def test_write_progress_png_creates_valid_png(tmp_path):
    history = [
        {
            "round": 0,
            "selected_candidate": 0,
            "selected_sha": "winner",
            "candidates": [
                {
                    "candidate": 0,
                    "score": 0.8,
                    "metrics": {"SPEED_MS": 90.0},
                },
                {
                    "candidate": 1,
                    "score": 0.4,
                    "metrics": {"SPEED_MS": 120.0},
                },
            ],
        },
    ]

    output = plot_mod.write_progress_png(tmp_path, history, SCHEMA)

    assert output == tmp_path / "progress.png"
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert output.stat().st_size > 10_000


def test_render_failure_preserves_previous_png(monkeypatch, tmp_path):
    output = tmp_path / "progress.png"
    output.write_bytes(b"previous image")

    def fail_render(_series, _output):
        raise RuntimeError("render failed")

    monkeypatch.setattr(plot_mod, "_render_progress_png", fail_render)

    result = plot_mod.write_progress_png(tmp_path, [], SCHEMA)

    assert result is None
    assert output.read_bytes() == b"previous image"
    assert not (tmp_path / ".progress.tmp.png").exists()


def test_atomic_replace_failure_preserves_previous_png(monkeypatch, tmp_path):
    output = tmp_path / "progress.png"
    output.write_bytes(b"previous image")

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(plot_mod.os, "replace", fail_replace)

    result = plot_mod.write_progress_png(tmp_path, [], SCHEMA)

    assert result is None
    assert output.read_bytes() == b"previous image"
    assert not (tmp_path / ".progress.tmp.png").exists()


def test_refresh_progress_plot_uses_persisted_history(monkeypatch, tmp_path):
    store = Store(tmp_path, metrics_schema=SCHEMA)
    store.append(
        0,
        "proposal",
        "sha",
        0.8,
        "feedback",
        eval_metrics={"SPEED_MS": 90.0, "CORRECTNESS": True},
        risk="low",
        accepted=True,
        base_sha="sha",
    )
    captured = {}

    def capture(run_dir, history, metrics_schema):
        captured["run_dir"] = run_dir
        captured["history"] = history
        captured["metrics_schema"] = metrics_schema
        return tmp_path / "progress.png"

    monkeypatch.setattr(loop_mod.plot_mod, "write_progress_png", capture)

    loop_mod._refresh_progress_plot(store)

    assert captured == {
        "run_dir": tmp_path,
        "history": store.history(),
        "metrics_schema": SCHEMA,
    }


def test_refresh_progress_plot_swallows_history_read_failure(monkeypatch, tmp_path, capsys):
    store = Store(tmp_path, metrics_schema=SCHEMA)

    def fail_history():
        raise OSError("history unavailable")

    monkeypatch.setattr(store, "history", fail_history)

    loop_mod._refresh_progress_plot(store)

    assert "[plot] warning:" in capsys.readouterr().out


def test_record_failure_refreshes_progress_plot(monkeypatch, tmp_path):
    store = Store(tmp_path, metrics_schema=SCHEMA)
    refreshed = []
    monkeypatch.setattr(loop_mod, "_refresh_progress_plot", refreshed.append)

    try:
        parse_judgment({
            "score": 0.5,
            "risk": "low",
            "feedback": "FULL_TECHNICAL_SENTINEL",
            "feedback_for_proposer": "",
        })
    except ValueError as exc:
        loop_mod._record_failure(
            store,
            round_id=0,
            proposal="proposal",
            reason="judger failed: " + str(exc),
            base_sha="base",
        )

    assert refreshed == [store]
    history = store.history()
    assert len(history) == 1
    assert history[0]["feedback_for_proposer"] == (
        "[loop failure] round failed before a usable result was produced"
    )
    assert "FULL_TECHNICAL_SENTINEL" not in history[0]["feedback_for_proposer"]
    assert "FULL_TECHNICAL_SENTINEL" in history[0]["feedback"]


def test_noop_continue_refreshes_plots_without_report(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    store = Store(run_dir, metrics_schema=SCHEMA)
    store.append(
        0,
        "proposal",
        "sha",
        0.8,
        "feedback",
        eval_metrics={"SPEED_MS": 90.0, "CORRECTNESS": True},
        risk="low",
        accepted=True,
        base_sha="sha",
    )
    config = {
        "goal": "make it faster",
        "max_rounds": 1,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 10,
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "eval_commands": [],
        "metrics": SCHEMA,
    }

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline"

    monkeypatch.setattr(loop_mod.config_mod, "load", lambda _path: config)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)

    summary = loop_mod.run("config.yaml", run_dir, continue_run=True)

    assert summary["rounds"] == 1
    assert (run_dir / "progress.png").exists()
    assert not (run_dir / "final_report.md").exists()
