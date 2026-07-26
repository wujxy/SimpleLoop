from __future__ import annotations

import math

import pytest

from simpleloop.reporting import plot as plot_mod
from simpleloop.reporting.plot import build_series, write_progress_pngs


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
    # lower_is_better: True inverts the ratio (baseline/objective) so the third row
    # reads as an improvement multiple where higher is better: 100ms/80ms = 1.25x.
    assert selected.ratio == 1.25
    assert selected.selected is True

    assert series.baseline.objective == 100.0
    assert series.baseline.ratio == 1.0
    assert series.baseline.round == 0
    assert series.incumbents[-1].objective == 80.0
    assert series.incumbents[-1].worktime_hours == 30.0 / 3600.0


def test_worktime_rebase_offsets_lifts_resume_drop():
    # Session 1 reaches 3600s; a --continue resume restarts the counter near 0.
    history = [
        {"telemetry": {"worktime_seconds": 3600.0}},
        {"telemetry": {"worktime_seconds": 600.0}},
        {"telemetry": {"worktime_seconds": 900.0}},
    ]
    offsets = plot_mod._worktime_rebase_offsets(history)

    assert offsets[0] == 0.0
    # The resumed session is lifted so its 600s point meets the 3600s ceiling.
    assert offsets[1] == pytest.approx(3000.0 / 3600.0)
    # Same resumed session shares one offset (its internal deltas are already correct).
    assert offsets[2] == pytest.approx(3000.0 / 3600.0)


def test_worktime_stays_continuous_across_continue_resume():
    """The 'vs worktime' axis must not drop at a --continue resume boundary."""
    history = [
        {
            "round": 0,
            "selected_candidate": 0,
            "selected_sha": "a",
            "telemetry": {"worktime_seconds": 3600.0, "processed_tokens": 0},
            "candidates": [{
                "candidate": 0,
                "score": 0.8,
                "metrics": {"SPEED_MS": 80.0},
                "telemetry": {"worktime_seconds": 3600.0, "processed_tokens": 0},
            }],
        },
        {
            "round": 1,
            "selected_candidate": 0,
            "selected_sha": "b",
            # Resumed session restarted its counter at ~0 instead of carrying the
            # prior 3600s forward — without the rebase this round would plot at
            # 600s, below the previous round (the reported display bug).
            "telemetry": {"worktime_seconds": 600.0, "processed_tokens": 0},
            "candidates": [{
                "candidate": 0,
                "score": 0.8,
                "metrics": {"SPEED_MS": 75.0},
                "telemetry": {"worktime_seconds": 600.0, "processed_tokens": 0},
            }],
        },
    ]
    series = build_series(history, SCHEMA, CONTEXT)

    round1_incumbent = next(p for p in series.incumbents if p.round == 1)
    round2_incumbent = next(p for p in series.incumbents if p.round == 2)
    round1_candidate = next(p for p in series.candidates if p.round == 1)
    round2_candidate = next(p for p in series.candidates if p.round == 2)

    assert round1_incumbent.worktime_hours == pytest.approx(3600.0 / 3600.0)
    # The resumed round continues from the prior ceiling instead of dropping back.
    assert round2_incumbent.worktime_hours >= round1_incumbent.worktime_hours
    assert round2_incumbent.worktime_hours == pytest.approx(3600.0 / 3600.0)
    assert round2_candidate.worktime_hours >= round1_candidate.worktime_hours


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


def test_ratio_panel_inverts_for_lower_is_better():
    plt = plot_mod._prepare_pyplot()
    figure, axis = plt.subplots()
    try:
        series = build_series(HISTORY, SCHEMA, CONTEXT)
        plot_mod._render_panel(axis, series, "ratio", "round")
        title = axis.get_title()
        # Lower-is-better objectives report an inverted multiple, so the panel is
        # labelled "multiple" and annotated "higher is better".
        assert "multiple" in title
        assert "higher is better" in title
    finally:
        plt.close(figure)


def test_ratio_panel_keeps_raw_ratio_for_higher_is_better():
    schema = {
        "objective": {"key": "QUALITY", "lower_is_better": False},
        "gates": [],
    }
    context = {
        "baseline_metrics": {"QUALITY": 10.0},
        "baseline_telemetry": {},
    }
    history = [{
        "round": 0,
        "selected_candidate": 0,
        "selected_sha": "winner",
        "candidates": [
            {"candidate": 0, "score": 0.8, "metrics": {"QUALITY": 12.0}},
        ],
    }]
    plt = plot_mod._prepare_pyplot()
    figure, axis = plt.subplots()
    try:
        series = build_series(history, schema, context)
        # No inversion for higher-is-better: 12/10 = 1.2.
        assert series.candidates[0].ratio == 1.2
        plot_mod._render_panel(axis, series, "ratio", "round")
        assert "ratio" in axis.get_title()
        assert "higher is better" in axis.get_title()
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
