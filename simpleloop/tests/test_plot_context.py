from __future__ import annotations

from simpleloop import loop as loop_mod
from simpleloop.store import Store
from simpleloop.telemetry import RunTelemetry


SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [],
}


def test_refresh_progress_plot_passes_persisted_context(
    monkeypatch, tmp_path,
):
    store = Store(tmp_path, metrics_schema=SCHEMA)
    store.append(
        0,
        "proposal",
        "sha",
        0.8,
        "feedback",
        eval_metrics={"SPEED_MS": 90.0},
        accepted=True,
    )
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
        return []

    monkeypatch.setattr(loop_mod.plot_mod, "write_progress_pngs", capture)

    loop_mod._refresh_progress_plot(store, context)

    assert captured == {
        "run_dir": tmp_path,
        "history": store.history(),
        "metrics_schema": SCHEMA,
        "plot_context": context,
    }


def test_noop_continue_refreshes_with_loaded_baseline_context(
    monkeypatch, tmp_path,
):
    run_dir = tmp_path / "run"
    store = Store(run_dir, metrics_schema=SCHEMA)
    store.append(
        0,
        "proposal",
        "sha",
        0.8,
        "feedback",
        eval_metrics={"SPEED_MS": 90.0},
        accepted=True,
    )
    telemetry = RunTelemetry(run_dir)
    telemetry.set_baseline({"SPEED_MS": 100.0})
    expected = telemetry.plot_context()
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
    contexts = []

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline"

    monkeypatch.setattr(loop_mod.config_mod, "load", lambda _path: config)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)
    monkeypatch.setattr(
        loop_mod,
        "_refresh_progress_plot",
        lambda _store, context: contexts.append(context),
    )

    loop_mod.run("config.yaml", run_dir, continue_run=True)

    assert contexts == [expected]
