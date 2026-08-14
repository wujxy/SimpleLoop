"""Static-proposal mode input validation."""
from __future__ import annotations

from simpleloop import app as app_mod
from simpleloop.stages.proposer import ProposerRequest, StaticProposer
import pytest
SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "CORRECTNESS"}],
}


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
    monkeypatch.setattr(app_mod.config_mod, "load",
                        lambda _path: _config(tmp_path))
    try:
        app_mod.run("config.yaml", tmp_path / "run",
                    proposals=["p0"], continue_run=True)
    except ValueError as exc:
        assert "--continue" in str(exc)
    else:
        raise AssertionError("expected ValueError for --continue + --proposals")


def test_agent_mode_requires_researcher_before_context(monkeypatch, tmp_path):
    with pytest.raises(app_mod.config_mod.ConfigError, match="proposer"):
        app_mod._run_locked(
            _config(tmp_path), tmp_path / "run", proposals=None,
            continue_run=False,
        )


def test_static_proposer_has_explicit_request_to_proposal_mapping():
    proposer = StaticProposer(("first", "second"))

    result = proposer.propose(ProposerRequest(1, "goal", "base"))

    assert [proposal.instruction for proposal in result.proposals] == ["second"]
