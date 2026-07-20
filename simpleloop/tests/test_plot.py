from __future__ import annotations

from simpleloop import plot as plot_mod
from simpleloop.plot import build_series


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
