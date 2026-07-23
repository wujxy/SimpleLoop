from __future__ import annotations

import json

from simpleloop import loop as loop_mod


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
    }
    observers = []

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
