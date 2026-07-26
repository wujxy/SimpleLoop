from __future__ import annotations

from simpleloop import loop as loop_mod
from simpleloop.harness.store import Store


def test_store_has_no_final_report_generator():
    assert not hasattr(Store, "write_final_report")


def test_fresh_run_does_not_create_final_report(monkeypatch, tmp_path):
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
        "eval_commands": [],
        "metrics": None,
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

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline"

    monkeypatch.setattr(loop_mod.config_mod, "load", lambda _path: config)
    monkeypatch.setattr(loop_mod, "ApptainerRuntime", FakeRuntime)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)
    monkeypatch.setattr(loop_mod, "_eval_baseline", lambda *_args: ("", {}))

    summary = loop_mod.run("config.yaml", run_dir)

    assert summary["rounds"] == 0
    assert not (run_dir / "final_report.md").exists()
