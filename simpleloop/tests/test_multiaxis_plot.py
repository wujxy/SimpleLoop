from __future__ import annotations

import math

import pytest

from simpleloop import plot as plot_mod
from simpleloop.plot import build_series, write_progress_pngs


SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "CORRECTNESS"}],
}
CONTEXT = {
    "baseline_metrics": {"SPEED_MS": 100.0},
    "baseline_telemetry": {
        "worktime_seconds": 10.0,
        "processed_tokens": 0,
    },
}
HISTORY = [{
    "round": 0,
    "selected_candidate": 1,
    "selected_sha": "winner",
    "telemetry": {
        "worktime_seconds": 30.0,
        "processed_tokens": 300,
    },
    "candidates": [
        {
            "candidate": 0,
            "score": 0.4,
            "metrics": {"SPEED_MS": 120.0},
            "telemetry": {
                "worktime_seconds": 20.0,
                "processed_tokens": 200,
            },
        },
        {
            "candidate": 1,
            "score": 0.8,
            "metrics": {"SPEED_MS": 80.0},
            "telemetry": {
                "worktime_seconds": 25.0,
                "processed_tokens": 250,
            },
        },
    ],
}]
DETAIL_OUTPUTS = {
    "progress-score-vs-round.png",
    "progress-score-vs-worktime.png",
    "progress-score-vs-tokens.png",
    "progress-objective-vs-round.png",
    "progress-objective-vs-worktime.png",
    "progress-objective-vs-tokens.png",
    "progress-objective-ratio-vs-round.png",
    "progress-objective-ratio-vs-worktime.png",
    "progress-objective-ratio-vs-tokens.png",
}


def test_build_series_maps_candidate_and_generation_coordinates():
    series = build_series(HISTORY, SCHEMA, CONTEXT)

    selected = series.candidates[1]
    assert selected.round == 1
    assert selected.worktime_hours == 25.0 / 3600.0
    assert selected.processed_tokens == 250
    assert selected.score == 0.8
    assert selected.objective == 80.0
    assert selected.ratio == 0.8
    assert selected.selected is True

    assert series.baseline.objective == 100.0
    assert series.baseline.ratio == 1.0
    assert series.baseline.round == 0
    assert series.incumbents[-1].objective == 80.0
    assert series.incumbents[-1].worktime_hours == 30.0 / 3600.0


@pytest.mark.parametrize("baseline", [0, math.nan, True, "unknown", None])
def test_invalid_baseline_omits_ratio_but_keeps_objective(baseline):
    context = {
        "baseline_metrics": {"SPEED_MS": baseline},
        "baseline_telemetry": {},
    }

    series = build_series(HISTORY, SCHEMA, context)

    assert series.candidates[1].objective == 80.0
    assert series.candidates[1].ratio is None
    assert series.baseline is None


def test_legacy_history_keeps_round_data_without_resource_coordinates():
    series = build_series([{
        "round": 0,
        "accepted": True,
        "score": 0.7,
        "metrics": {"SPEED_MS": 90.0},
    }], SCHEMA)

    point = series.candidates[0]
    assert point.round == 1
    assert point.score == 0.7
    assert point.objective == 90.0
    assert point.worktime_hours is None
    assert point.processed_tokens is None
    assert point.ratio is None


def test_write_progress_pngs_creates_overview_and_nine_details(tmp_path):
    outputs = write_progress_pngs(tmp_path, HISTORY, SCHEMA, CONTEXT)

    assert {path.name for path in outputs} == {"progress.png"} | DETAIL_OUTPUTS
    for path in outputs:
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert path.stat().st_size > 5_000


def test_objective_panel_title_retains_configured_direction():
    plt = plot_mod._prepare_pyplot()
    figure, axis = plt.subplots()
    try:
        plot_mod._render_panel(
            axis,
            build_series(HISTORY, SCHEMA, CONTEXT),
            "objective",
            "round",
        )
        assert "lower is better" in axis.get_title()
    finally:
        plt.close(figure)


def test_one_detail_failure_preserves_old_file_and_other_outputs(
    monkeypatch, tmp_path,
):
    failed = tmp_path / "progress-score-vs-round.png"
    failed.write_bytes(b"previous")
    real_render = plot_mod._render_detail

    def fail_one(series, y_kind, x_kind, output):
        if y_kind == "score" and x_kind == "round":
            raise RuntimeError("boom")
        return real_render(series, y_kind, x_kind, output)

    monkeypatch.setattr(plot_mod, "_render_detail", fail_one)

    outputs = write_progress_pngs(tmp_path, HISTORY, SCHEMA, CONTEXT)

    assert failed.read_bytes() == b"previous"
    assert failed not in outputs
    assert (tmp_path / "progress.png") in outputs
    assert (tmp_path / "progress-objective-vs-tokens.png") in outputs
