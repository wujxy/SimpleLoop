"""Static-proposal mode goes through the unified generation pipeline.

Static mode is a one-candidate generation per round with a different acceptance
rule: hard gates alone decide chain advance (no improvement / risk requirement),
so a regressing-but-valid experiment still lands and the next static proposal
builds on it. These tests fake the candidate pipeline and assert the loop-level
semantics: rounds = len(proposals), generation-shaped records with
decision="static", and gate-only chain advance.
"""
from __future__ import annotations

from simpleloop import loop as loop_mod
from simpleloop.store import Store

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


class FakeWorkspace:
    def __init__(self, *, run_dir, **_kwargs):
        self.repo = run_dir / "repo"

    def setup(self):
        self.repo.mkdir(parents=True, exist_ok=True)

    def baseline_sha(self):
        return "baseline"


def _config(tmp_path, max_rounds=99):
    return {
        "goal": "make it faster",
        "max_rounds": max_rounds,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 10,
        "proposer_recent_rounds": 6,
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "eval_commands": ["true"],
        "metrics": SCHEMA,
        "runtime_image": tmp_path / "runtime.sif",
        "runtime_binds": [],
    }


def test_static_mode_accepts_by_gates_alone_and_records_generations(
    monkeypatch, tmp_path
):
    run_dir = tmp_path / "run"
    monkeypatch.setattr(loop_mod.config_mod, "load",
                        lambda _path: _config(tmp_path))
    monkeypatch.setattr(loop_mod, "ApptainerRuntime", FakeRuntime)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)
    monkeypatch.setattr(loop_mod, "_eval_baseline",
                        lambda *_args: ("", {"SPEED_MS": 100.0,
                                             "CORRECTNESS": True}))
    monkeypatch.setattr(loop_mod, "_refresh_progress_plot",
                        lambda *_args, **_kwargs: None)

    seen_parents = []
    # Three canned outcomes:
    #   round 0: gates pass but the objective REGRESSES and risk is high —
    #            _select_winner would reject it; static mode must accept it.
    #   round 1: gate fails — chain must stay on round 0's sha.
    #   round 2: gates pass — must build on round 0's sha.
    outcomes = [
        {"sha": "sha0", "accepted": True, "risk": "high",
         "metrics": {"SPEED_MS": 200.0, "CORRECTNESS": True}},
        {"sha": "sha1", "accepted": False, "risk": "low",
         "metrics": {"SPEED_MS": 50.0, "CORRECTNESS": False}},
        {"sha": "sha2", "accepted": True, "risk": "low",
         "metrics": {"SPEED_MS": 150.0, "CORRECTNESS": True}},
    ]

    def fake_candidate(_ctx, candidate_id, proposal, round_id, parent_sha,
                       *_args):
        seen_parents.append(parent_sha)
        outcome = outcomes[round_id]
        return {
            "candidate": candidate_id,
            "family": proposal.family,
            "decision": proposal.decision,
            "proposal": proposal.proposal,
            "sha": outcome["sha"],
            "score": 0.5,
            "risk": outcome["risk"],
            "feedback": "LANDED_STATE: not-implemented\nImplemented: x\n"
                        "Result: y\nAnalysis: z",
            "feedback_for_proposer": "lesson",
            "eval_block": "raw",
            "metrics": outcome["metrics"],
            "changed_paths": ["src/a.cc"],
            "accepted": outcome["accepted"],
            "selected": False,
        }

    monkeypatch.setattr(loop_mod, "_run_one_candidate", fake_candidate)

    summary = loop_mod.run(
        "config.yaml", run_dir,
        proposals=["direction 0", "direction 1", "direction 2"],
    )

    # rounds = len(proposals), max_rounds (99) ignored
    assert summary["rounds"] == 3
    # gate-only chain advance: r0 lands despite regression+high risk; r1's gate
    # failure leaves the chain on sha0; r2 builds on sha0.
    assert seen_parents == ["baseline", "sha0", "sha0"]

    history = Store(run_dir, metrics_schema=SCHEMA).history()
    assert [r.get("selected_sha") for r in history] == ["sha0", None, "sha2"]
    assert [r.get("base_sha") for r in history] == ["sha0", "sha0", "sha2"]
    for record in history:
        assert "candidates" in record and len(record["candidates"]) == 1
        assert record["candidates"][0]["decision"] == "static"
        assert record["reflection"] == ""
    assert [r["candidates"][0]["proposal"] for r in history] == [
        "direction 0", "direction 1", "direction 2",
    ]
    assert [r["candidates"][0]["selected"] for r in history] == [
        True, False, True,
    ]


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
