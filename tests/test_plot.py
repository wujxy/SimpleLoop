from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

from simpleloop import app as loop_mod
from simpleloop.reporting import plot as plot_mod
from simpleloop.reporting.plot import build_series
from simpleloop.persistence.history import Store
from simpleloop.reporting.telemetry import RunTelemetry
from simpleloop.world import ProcessResult, SourceWorkspace
from round_helpers import append_round


_RESEARCHER = {
    "model": "gpt-5.5", "base_url": "https://example.invalid",
    "command_timeout_seconds": 2,
    "command_output_cap_chars": 1000,
}
_EXECUTOR = {
    "model": "glm-5", "base_url": "https://example.invalid",
}


class _FakeSandbox:
    def __init__(self, **_kwargs):
        pass

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


# The six detail images are drawn by the offline script, not the package.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "plot_details.py"
_spec = importlib.util.spec_from_file_location("plot_details", _SCRIPT)
plot_details = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plot_details)
write_detail_pngs = plot_details.write_detail_pngs


def _single_candidate_record(round_id: int, *, metrics,
                             selected: bool = True,
                             telemetry: dict | None = None) -> dict:
    """A one-candidate generation record (the only shape the loop writes)."""
    record = {
        "round": round_id,
        "selected_candidate": 0 if selected else None,
        "selected_sha": f"sha-{round_id}" if selected else None,
        "candidates": [{
            "candidate": 0,
            "metrics": metrics,
        }],
    }
    if telemetry is not None:
        record["telemetry"] = telemetry
        record["candidates"][0]["telemetry"] = telemetry
    return record


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
                    "metrics": {"SPEED_MS": 120.0},
                },
                {
                    "candidate": 1,
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
                    "metrics": {"SPEED_MS": 110.0},
                },
            ],
        },
    ]

    series = build_series(history, SCHEMA)

    assert series.rounds == [1, 2]
    assert plot_mod._Y_KINDS == ("objective", "ratio")
    assert not hasattr(series, "score_points")
    assert series.objective_points == [(1, 120.0), (1, 90.0), (2, 110.0)]
    assert series.selected_objectives == [(1, 90.0)]
    assert series.incumbent_objective == [(1, 90.0), (2, 90.0)]
    assert series.objective_key == "SPEED_MS"
    assert series.lower_is_better is True


def test_build_series_supports_single_candidate_history_and_missing_values():
    history = [
        _single_candidate_record(0, metrics={"QUALITY": 10.0}),
        _single_candidate_record(
            1, metrics={"QUALITY": "unknown"}, selected=False),
        _single_candidate_record(2, metrics={"QUALITY": 12}),
    ]
    schema = {
        "objective": {"key": "QUALITY", "lower_is_better": False},
        "gates": [],
    }

    series = build_series(history, schema)

    assert series.rounds == [1, 2, 3]
    assert not hasattr(series, "selected_scores")
    assert series.objective_points == [(1, 10.0), (3, 12.0)]
    assert series.selected_objectives == [(1, 10.0), (3, 12.0)]
    assert series.incumbent_objective == [(1, 10.0), (2, 10.0), (3, 12.0)]
    assert series.objective_key == "QUALITY"
    assert series.lower_is_better is False


def test_build_series_requires_selected_sha_to_advance_parallel_incumbent():
    history = [
        {
            "round": 0,
            "selected_candidate": 0,
            "selected_sha": None,
            "candidates": [
                {
                    "candidate": 0,
                    "metrics": {"SPEED_MS": 90.0},
                },
            ],
        },
    ]

    series = build_series(history, SCHEMA)

    assert series.selected_objectives == [(1, 90.0)]
    assert series.incumbent_objective == []


def test_build_series_skips_non_finite_values():
    history = [
        _single_candidate_record(
            0, metrics={"SPEED_MS": math.inf}),
    ]

    series = build_series(history, SCHEMA)

    assert series.objective_points == []
    assert series.incumbent_objective == []


def test_cost_uses_weighted_input_output_and_cache_hit_rates():
    telemetry = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 7,
        "cache_read_input_tokens": 20,
    }

    assert plot_mod._cost_rmb(telemetry) == pytest.approx(
        (10 + 5) * 28 / 1_000_000 + 20 * 2 / 1_000_000
    )


def test_write_progress_png_creates_valid_png(tmp_path):
    history = [
        {
            "round": 0,
            "selected_candidate": 0,
            "selected_sha": "winner",
            "candidates": [
                {
                    "candidate": 0,
                    "metrics": {"SPEED_MS": 90.0},
                },
                {
                    "candidate": 1,
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
    append_round(store,
        0, parent_sha="parent", selected_candidate=0, selected_sha="sha",
        candidates=[{
            "candidate": 0, "proposal": "proposal", "sha": "sha",
            "status": "COMPLETED", "gate_passed": True, "eligible": True,
            "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": True},
        }])
    captured = {}

    def capture(run_dir, history, metrics_schema, plot_context):
        captured["run_dir"] = run_dir
        captured["history"] = history
        captured["metrics_schema"] = metrics_schema
        captured["plot_context"] = plot_context
        return tmp_path / "progress.png"

    monkeypatch.setattr(loop_mod.plot_mod, "write_progress_png", capture)

    loop_mod._refresh_progress_plot(store)

    assert captured == {
        "run_dir": tmp_path,
        "history": store.history(),
        "metrics_schema": SCHEMA,
        "plot_context": None,
    }


def test_refresh_progress_plot_swallows_history_read_failure(monkeypatch, tmp_path, capsys):
    store = Store(tmp_path, metrics_schema=SCHEMA)

    def fail_history():
        raise OSError("history unavailable")

    monkeypatch.setattr(store, "history", fail_history)

    loop_mod._refresh_progress_plot(store)

    assert "[plot] warning:" in capsys.readouterr().out


def test_noop_continue_refreshes_plots_without_report(monkeypatch, tmp_path):
    run_dir = tmp_path / "run"
    store = Store(run_dir, metrics_schema=SCHEMA)
    append_round(store,
        0, parent_sha="parent", selected_candidate=0, selected_sha="sha",
        candidates=[{
            "candidate": 0, "proposal": "proposal", "sha": "sha",
            "status": "COMPLETED", "gate_passed": True, "eligible": True,
            "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": True},
        }])
    config = {
        "goal": "make it faster",
        "max_rounds": 1,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 10,
        "roles": {"researcher": _RESEARCHER, "executor": _EXECUTOR},
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "eval_commands": [],
        "metrics": SCHEMA,
        "runtime_image": tmp_path / "runtime.sif",
        "runtime_binds": [],
    }

    class FakeRuntime:
        def __init__(self, **_kwargs):
            pass

        def summary_lines(self):
            return ()

        def preflight(self):
            pass

        def executor_preflight(self, **_kwargs):
            pass

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline"

        def add_worktree(self, worktree_id, _parent_sha):
            path = self.repo.parent / "worktrees" / worktree_id
            path.mkdir(parents=True)
            return path

        def remove_worktree(self, _worktree_id):
            pass

    monkeypatch.setattr(loop_mod.config_mod, "load", lambda _path: config)
    monkeypatch.setattr(loop_mod, "ApptainerSandbox", _FakeSandbox)
    monkeypatch.setattr(loop_mod, "WorldBuilder", _FakeWorldBuilder)
    monkeypatch.setattr(loop_mod, "GitWorkspaceProvider", _FakeWorkspaceProvider)

    summary = loop_mod.run("config.yaml", run_dir, continue_run=True)

    assert summary["rounds"] == 1
    assert (run_dir / "progress.png").exists()
    # detail images are offline-only (`simpleloop plot`), never loop-written
    assert not (run_dir / "progress-score-vs-round.png").exists()
    assert not (run_dir / "final_report.md").exists()


# ---- multi-axis progress plots (merged from test_multiaxis_plot.py) ----

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
            "metrics": {"SPEED_MS": 120.0},
            "telemetry": {
                "worktime_seconds": 20.0,
                "processed_tokens": 200,
            },
        },
        {
            "candidate": 1,
            "metrics": {"SPEED_MS": 80.0},
            "telemetry": {
                "worktime_seconds": 25.0,
                "processed_tokens": 250,
            },
        },
    ],
}]
DETAIL_OUTPUTS = {
    "progress-objective-vs-round.png",
    "progress-objective-vs-worktime.png",
    "progress-objective-vs-tokens.png",
    "progress-objective-ratio-vs-round.png",
    "progress-objective-ratio-vs-worktime.png",
    "progress-objective-ratio-vs-tokens.png",
    "progress-cost-vs-round.png",
    "progress-objective-vs-cost.png",
    "progress-objective-speedup-vs-round.png",
    "progress-objective-ratio-vs-cost.png",
}


def test_build_series_maps_candidate_and_generation_coordinates():
    series = build_series(HISTORY, SCHEMA, CONTEXT)

    selected = series.candidates[1]
    assert selected.round == 1
    assert selected.worktime_hours == 25.0 / 3600.0
    assert selected.processed_tokens == 250
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


def test_coordinates_always_use_processed_tokens():
    worktime, tokens = plot_mod._coordinates({
        "worktime_seconds": 3600.0,
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 40,
        "processed_tokens": 999,
    })

    assert worktime == 1.0
    assert tokens == 999


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


def test_write_detail_pngs_creates_six_details(tmp_path):
    outputs = write_detail_pngs(tmp_path, HISTORY, SCHEMA, CONTEXT)

    assert {path.name for path in outputs} == DETAIL_OUTPUTS
    for path in outputs:
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert path.stat().st_size > 5_000
    # the offline detail writer never touches the loop-owned overview
    assert not (tmp_path / "progress.png").exists()


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
            {"candidate": 0, "metrics": {"QUALITY": 12.0}},
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


def test_objective_panel_has_paper_reference_line_for_speed_ms():
    """Every SPEED_MS objective panel gets a dashed 177.7 ms reference line."""
    plt = plot_mod._prepare_pyplot()
    figure, axis = plt.subplots()
    try:
        series = build_series(HISTORY, SCHEMA, CONTEXT)
        plot_mod._render_panel(axis, series, "objective", "round")
        labels = [line.get_label() for line in axis.get_lines()]
        assert "Paper v1.12.0" in labels
        ref_line = next(
            line for line in axis.get_lines() if line.get_label() == "Paper v1.12.0"
        )
        # axhline stores the y-intercept as the first y-data element.
        assert pytest.approx(ref_line.get_ydata()[0]) == 177.7
    finally:
        plt.close(figure)


def test_ratio_panel_has_no_paper_reference_line():
    """The 177.7 ms reference line must not appear on ratio panels."""
    plt = plot_mod._prepare_pyplot()
    figure, axis = plt.subplots()
    try:
        series = build_series(HISTORY, SCHEMA, CONTEXT)
        plot_mod._render_panel(axis, series, "ratio", "round")
        labels = [line.get_label() for line in axis.get_lines()]
        assert "Paper v1.12.0" not in labels
    finally:
        plt.close(figure)


def test_one_detail_failure_preserves_old_file_and_other_outputs(
    monkeypatch, tmp_path,
):
    failed = tmp_path / "progress-objective-vs-round.png"
    failed.write_bytes(b"previous")
    real_render = plot_details._render_detail

    def fail_one(series, y_kind, x_kind, output):
        if y_kind == "objective" and x_kind == "round":
            raise RuntimeError("boom")
        return real_render(series, y_kind, x_kind, output)

    monkeypatch.setattr(plot_details, "_render_detail", fail_one)

    outputs = write_detail_pngs(tmp_path, HISTORY, SCHEMA, CONTEXT)

    assert failed.read_bytes() == b"previous"
    assert failed not in outputs
    assert (tmp_path / "progress-objective-vs-tokens.png") in outputs
    assert len(outputs) == len(DETAIL_OUTPUTS) - 1


# ---- persisted plot context / continue-mode plots (merged from test_plot_context.py) ----

SCHEMA_NO_GATES = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [],
}


def test_refresh_progress_plot_passes_persisted_context(
    monkeypatch, tmp_path,
):
    store = Store(tmp_path, metrics_schema=SCHEMA_NO_GATES)
    append_round(store,
        0, parent_sha="parent", selected_candidate=0, selected_sha="sha",
        candidates=[{
            "candidate": 0, "proposal": "proposal", "sha": "sha",
            "status": "COMPLETED", "gate_passed": True, "eligible": True,
            "metrics": {"SPEED_MS": 90.0},
        }])
    context = {
        "baseline_metrics": {"SPEED_MS": 100.0},
        "baseline_telemetry": {
            "worktime_seconds": 1.0,
            "processed_tokens": 0,
        },
    }
    captured = {}

    def capture(run_dir, history, metrics_schema, plot_context):
        captured.update({
            "run_dir": run_dir,
            "history": history,
            "metrics_schema": metrics_schema,
            "plot_context": plot_context,
        })
        return run_dir / "progress.png"

    monkeypatch.setattr(loop_mod.plot_mod, "write_progress_png", capture)

    loop_mod._refresh_progress_plot(store, context)

    assert captured == {
        "run_dir": tmp_path,
        "history": store.history(),
        "metrics_schema": SCHEMA_NO_GATES,
        "plot_context": context,
    }


def test_noop_continue_refreshes_with_loaded_baseline_context(
    monkeypatch, tmp_path,
):
    run_dir = tmp_path / "run"
    store = Store(run_dir, metrics_schema=SCHEMA_NO_GATES)
    append_round(store,
        0, parent_sha="parent", selected_candidate=0, selected_sha="sha",
        candidates=[{
            "candidate": 0, "proposal": "proposal", "sha": "sha",
            "status": "COMPLETED", "gate_passed": True, "eligible": True,
            "metrics": {"SPEED_MS": 90.0},
        }])
    telemetry = RunTelemetry(run_dir)
    telemetry.set_baseline({"SPEED_MS": 100.0})
    expected = telemetry.plot_context()
    config = {
        "goal": "make it faster",
        "max_rounds": 1,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 10,
        "roles": {"researcher": _RESEARCHER, "executor": _EXECUTOR},
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "eval_commands": [],
        "metrics": SCHEMA_NO_GATES,
        "runtime_image": tmp_path / "runtime.sif",
        "runtime_binds": [],
    }
    contexts = []

    class FakeRuntime:
        def __init__(self, **_kwargs):
            pass

        def summary_lines(self):
            return ()

        def preflight(self):
            pass

        def executor_preflight(self, **_kwargs):
            pass

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline"

        def add_worktree(self, worktree_id, _parent_sha):
            path = self.repo.parent / "worktrees" / worktree_id
            path.mkdir(parents=True)
            return path

        def remove_worktree(self, _worktree_id):
            pass

    monkeypatch.setattr(loop_mod.config_mod, "load", lambda _path: config)
    monkeypatch.setattr(loop_mod, "ApptainerSandbox", _FakeSandbox)
    monkeypatch.setattr(loop_mod, "WorldBuilder", _FakeWorldBuilder)
    monkeypatch.setattr(loop_mod, "GitWorkspaceProvider", _FakeWorkspaceProvider)
    monkeypatch.setattr(
        loop_mod,
        "_refresh_progress_plot",
        lambda _store, context: contexts.append(context),
    )

    loop_mod.run("config.yaml", run_dir, continue_run=True)

    assert contexts == [expected]


# ---- offline plotting entry: simpleloop plot ----

def test_plot_command_redraws_overview_offline(tmp_path, capsys):
    import json
    import yaml

    from simpleloop import cli as cli_mod
    from simpleloop.reporting import telemetry as telemetry_mod

    # a real minimal config (the plot command loads it for eval.metrics)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"SIF-test-double")
    config_path = tmp_path / "task.yaml"
    config_path.write_text(yaml.safe_dump({
        "kind": "task",
        "task": {"goal": "go faster"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": {"max_rounds": 1},
        "runtime": {"image": "runtime.sif"},
        "source": {"path": str(repo)},
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [{"key": "CORRECTNESS"}],
            },
        },
    }), encoding="utf-8")

    # a run dir as the loop leaves it: history.jsonl + telemetry.json
    run_dir = tmp_path / "run"
    store = Store(run_dir, metrics_schema=SCHEMA)
    append_round(store,
        0, parent_sha="parent", selected_candidate=1, selected_sha="winner",
        candidates=HISTORY[0]["candidates"],
        telemetry=HISTORY[0]["telemetry"],
    )
    (run_dir / "telemetry.json").write_text(json.dumps({
        "worktime_seconds": 30.0,
        "processed_tokens": 300,
        "baseline_metrics": CONTEXT["baseline_metrics"],
        "baseline_telemetry": CONTEXT["baseline_telemetry"],
    }), encoding="utf-8")

    assert telemetry_mod.load_plot_context(run_dir) == CONTEXT

    cli_mod.main([
        "plot", "--config", str(config_path), "--run-dir", str(run_dir),
    ])

    written = {p.name for p in run_dir.glob("progress*.png")}
    assert written == {"progress.png"}
    out = capsys.readouterr().out
    assert out.count("Wrote ") == 1

    # the offline script draws the six detail images on top
    plot_details.main([
        "--config", str(config_path), "--run-dir", str(run_dir),
    ])
    written = {p.name for p in run_dir.glob("progress*.png")}
    assert written == {"progress.png"} | DETAIL_OUTPUTS


def test_plot_command_requires_existing_history(tmp_path):
    import yaml

    from simpleloop import cli as cli_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (tmp_path / "runtime.sif").write_bytes(b"SIF-test-double")
    config_path = tmp_path / "task.yaml"
    config_path.write_text(yaml.safe_dump({
        "kind": "task",
        "task": {"goal": "go faster"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": {"max_rounds": 1},
        "runtime": {"image": "runtime.sif"},
        "source": {"path": str(repo)},
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [],
            },
        },
    }), encoding="utf-8")

    with pytest.raises(SystemExit):
        cli_mod.main([
            "plot", "--config", str(config_path),
            "--run-dir", str(tmp_path / "no-such-run"),
        ])
