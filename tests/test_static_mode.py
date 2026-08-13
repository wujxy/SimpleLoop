"""Static-proposal mode input validation."""
from __future__ import annotations

from simpleloop import loop as loop_mod
import pytest
SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "CORRECTNESS"}],
}


class FakeRuntime:
    def __init__(self, **_kwargs):
        pass

    def summary_lines(self):
        return ()

    def preflight(self):
        pass


def _config(tmp_path, max_rounds=99):
    return {
        "goal": "make it faster",
        "max_rounds": max_rounds,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 10,
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "eval_commands": ["true"],
        "metrics": SCHEMA,
        "runtime_image": tmp_path / "runtime.sif",
        "runtime_binds": [],
    }


def test_static_mode_rejects_continue_combination(monkeypatch, tmp_path):
    monkeypatch.setattr(loop_mod.config_mod, "load",
                        lambda _path: _config(tmp_path))
    monkeypatch.setattr(loop_mod, "ApptainerRuntime", FakeRuntime)

    try:
        loop_mod.run("config.yaml", tmp_path / "run",
                     proposals=["p0"], continue_run=True)
    except ValueError as exc:
        assert "--continue" in str(exc)
    else:
        raise AssertionError("expected ValueError for --continue + --proposals")


def test_agent_mode_requires_researcher_before_context(monkeypatch, tmp_path):
    monkeypatch.setattr(
        loop_mod, "_build_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("context must not be built")
        ),
    )

    with pytest.raises(loop_mod.config_mod.ConfigError, match="researcher"):
        loop_mod._run_locked(
            _config(tmp_path), tmp_path / "run", proposals=None,
            continue_run=False,
        )
