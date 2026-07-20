from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop import loop as loop_mod
from simpleloop import views
from simpleloop.executor import ExecResult
from simpleloop.judger import Judgment
from simpleloop.loop import _run_candidates, _select_winner
from simpleloop.proposer import Proposal
from simpleloop.proposer import _parse_batch
from simpleloop.store import Store


EXAMPLES = Path(__file__).parents[2] / "examples"


def _example_yaml(relative_path: str) -> dict:
    return yaml.safe_load((EXAMPLES / relative_path).read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, loop_block: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    cfg = {
        "kind": "task",
        "task": {"goal": "go faster"},
        "safety": {"editable_paths": ["src/**"], "frozen_paths": []},
        "loop": {"max_rounds": 3, **(loop_block or {})},
        "source": {"path": str(repo), "baseline_ref": "HEAD"},
    }
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def test_config_parallel_defaults(tmp_path: Path):
    cfg = config_mod.load(_write_config(tmp_path))
    assert cfg["candidates_per_round"] == 1
    assert cfg["max_workers"] == 1


def test_tiny_example_uses_parallel_objective_selection():
    raw = _example_yaml("tiny_algo_opt/task.yaml")

    assert raw["loop"]["candidates_per_round"] == 3
    assert raw["loop"]["max_workers"] == 3
    assert raw["eval"]["metrics"] == {
        "objective": {"key": "ms_per_call", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}, {"key": "DRIFT"}],
    }
    commands = "\n".join(raw["eval"]["commands"])
    assert "CORRECTNESS=PASS" in commands
    assert "CORRECTNESS=FAIL" in commands
    assert "DRIFT=PASS" in commands
    assert "DRIFT=FAIL" in commands


def test_omilrec_v100_example_uses_parallel_speed_selection():
    raw = _example_yaml("omilrec-v100.yaml")

    assert raw["loop"]["candidates_per_round"] == 3
    assert raw["loop"]["max_workers"] == 3
    assert raw["eval"]["metrics"] == {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}, {"key": "EVAL_RESULT"}],
    }


@pytest.mark.parametrize("field", ["candidates_per_round", "max_workers"])
@pytest.mark.parametrize("value", [0, -1, "2"])
def test_config_parallel_rejects_invalid_values(tmp_path: Path, field: str, value):
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(_write_config(tmp_path, {field: value}))


def test_parse_batch_accepts_legacy_single_when_k_is_one():
    batch = _parse_batch(
        {"reflection": "r", "decision": "continue", "proposal": "do one thing"},
        candidates_per_round=1,
    )
    assert batch.reflection == "r"
    assert len(batch.proposals) == 1
    assert batch.proposals[0].proposal == "do one thing"
    assert batch.proposals[0].decision == "continue"
    assert batch.proposals[0].family == "single"
    assert batch.warnings == []


def test_parse_batch_accepts_new_shape():
    batch = _parse_batch(
        {
            "reflection": "r",
            "proposals": [
                {"family": "layout", "decision": "switch", "proposal": "p0"},
                {"family": "hoist", "decision": "continue", "proposal": "p1"},
            ],
        },
        candidates_per_round=2,
    )
    assert [p.family for p in batch.proposals] == ["layout", "hoist"]
    assert [p.proposal for p in batch.proposals] == ["p0", "p1"]
    assert batch.warnings == []


def test_parse_batch_degrades_legacy_when_k_is_greater_than_one():
    batch = _parse_batch(
        {"reflection": "r", "decision": "continue", "proposal": "only one"},
        candidates_per_round=3,
    )
    assert len(batch.proposals) == 1
    assert "returned legacy single-proposal JSON" in batch.warnings[0]


def test_parse_batch_rejects_empty_batch():
    with pytest.raises(ValueError):
        _parse_batch({"reflection": "r", "proposals": []}, candidates_per_round=3)


def test_selector_uses_objective_and_filters_gates_and_risk():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}, {"key": "EVAL_RESULT"}],
    }
    candidates = [
        {"candidate": 0, "sha": "slow", "risk": "low", "score": 0.9,
         "metrics": {"SPEED_MS": 700.0, "CORRECTNESS": True, "EVAL_RESULT": True}},
        {"candidate": 1, "sha": "fast-risky", "risk": "high", "score": 1.0,
         "metrics": {"SPEED_MS": 100.0, "CORRECTNESS": True, "EVAL_RESULT": True}},
        {"candidate": 2, "sha": "fast-fail", "risk": "low", "score": 0.8,
         "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": False, "EVAL_RESULT": True}},
        {"candidate": 3, "sha": "winner", "risk": "medium", "score": 0.3,
         "metrics": {"SPEED_MS": 650.0, "CORRECTNESS": True, "EVAL_RESULT": True}},
    ]
    assert _select_winner(candidates, schema)["sha"] == "winner"


def test_selector_uses_score_only_as_tiebreaker():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {"candidate": 0, "sha": "a", "risk": "low", "score": 0.2,
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True}},
        {"candidate": 1, "sha": "b", "risk": "low", "score": 0.9,
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True}},
    ]
    assert _select_winner(candidates, schema)["sha"] == "b"


def test_store_records_generation_candidates_and_proposer_view(tmp_path: Path):
    store = Store(tmp_path, metrics_schema={
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    })
    candidates = [
        {"candidate": 0, "family": "hoist", "proposal": "p0", "sha": "a",
         "score": 0.5, "risk": "low", "feedback": "short0",
         "feedback_for_report": "Implemented: p0. Result: worse. Analysis: no win.",
         "metrics": {"SPEED_MS": 600.0, "CORRECTNESS": True},
         "changed_paths": ["a.cc"], "accepted": True, "selected": False},
        {"candidate": 1, "family": "layout", "proposal": "p1", "sha": "b",
         "score": 0.7, "risk": "low", "feedback": "short1",
         "feedback_for_report": "Implemented: p1. Result: better. Analysis: cache locality.",
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True},
         "changed_paths": ["b.cc"], "accepted": True, "selected": True},
    ]
    store.append_generation(0, parent_sha="base", selected_candidate=1,
                            selected_sha="b", candidates=candidates,
                            reflection="batch reflection")
    rows = store.history()
    assert rows[0]["selected_sha"] == "b"
    assert rows[0]["candidates"][1]["selected"] is True
    assert store.best_sha == "b"

    projected = views.for_proposer(rows)
    assert projected[0]["selected_sha"] == "b"
    assert projected[0]["candidates"][0]["feedback_for_report"].startswith("Implemented:")


def test_run_candidates_uses_same_parent_for_all_worktrees(monkeypatch, tmp_path: Path):
    class FakeWorkspace:
        def __init__(self):
            self.added = []
            self.removed = []

        def add_worktree(self, round_id, parent_sha):
            self.added.append((round_id, parent_sha))
            return tmp_path / str(round_id)

        def remove_worktree(self, round_id):
            self.removed.append(round_id)

        def diff(self, parent_sha, sha):
            return f"diff {parent_sha}..{sha}"

    def fake_execute(agent, *, proposal, goal, editable, frozen, workspace, worktree, round_id):
        return ExecResult(sha=f"sha-{round_id}", reason=None, changed_paths=[f"{round_id}.cc"])

    def fake_run_eval(commands, cwd, metrics_schema=None):
        cid = int(str(cwd).rsplit("c", 1)[-1])
        return "eval", {"SPEED_MS": 100.0 + cid, "CORRECTNESS": True}

    def fake_judge(agent, **kwargs):
        return Judgment(score=0.5, risk="low", feedback="LANDED_STATE: not-implemented PASS",
                        feedback_for_report="Implemented: x. Result: y. Analysis: z.")

    monkeypatch.setattr(loop_mod.executor_mod, "execute", fake_execute)
    monkeypatch.setattr(loop_mod.judger_mod, "run_eval", fake_run_eval)
    monkeypatch.setattr(loop_mod.judger_mod, "judge", fake_judge)

    workspace = FakeWorkspace()
    proposals = [
        Proposal(proposal="p0", family="f0"),
        Proposal(proposal="p1", family="f1"),
        Proposal(proposal="p2", family="f2"),
    ]
    schema = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
              "gates": [{"key": "CORRECTNESS"}]}
    candidates = _run_candidates(
        proposals, 7, "parent", {
            "goal": "g", "editable_paths": ["src/**"], "frozen_paths": [],
            "eval_commands": ["eval"], "max_workers": 1,
        }, workspace, object(), object(), {"SPEED_MS": 150.0}, {"SPEED_MS": 200.0}, schema)

    assert workspace.added == [("7-c0", "parent"), ("7-c1", "parent"), ("7-c2", "parent")]
    assert workspace.removed == ["7-c0", "7-c1", "7-c2"]
    assert _select_winner(candidates, schema)["candidate"] == 0
