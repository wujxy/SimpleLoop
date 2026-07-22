from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop import loop as loop_mod
from simpleloop import views
from simpleloop.agent import Agent, AgentError, AgentResult
from simpleloop.executor import ExecResult
from simpleloop.judger import Judgment
from simpleloop.loop import _run_candidates, _select_winner
from simpleloop.proposer import Proposal
from simpleloop.proposer import _parse_batch
from simpleloop.proposer import _proposer_schema
from simpleloop.proposer import propose
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
    gates = raw["eval"]["metrics"]["gates"]
    assert [g["key"] for g in gates] == ["CORRECTNESS", "DRIFT"]
    assert all("description" in g for g in gates), "every gate declares a description"
    commands = "\n".join(raw["eval"]["commands"])
    assert "CORRECTNESS=PASS" in commands
    assert "CORRECTNESS=FAIL" in commands
    assert "DRIFT=PASS" in commands
    assert "DRIFT=FAIL" in commands


def test_omilrec_v100_example_uses_parallel_speed_selection():
    raw = _example_yaml("omilrec-opt/task.yaml")

    assert raw["loop"]["candidates_per_round"] == 3
    assert raw["loop"]["max_workers"] == 3
    gates = raw["eval"]["metrics"]["gates"]
    assert [g["key"] for g in gates] == ["CORRECTNESS", "EVAL_RESULT"]
    assert all("description" in g for g in gates), "every gate declares a description"


def test_omilrec_v100_postv107_gated_example_uses_new_package_only():
    cfg = config_mod.load(EXAMPLES / "omilrec-post-v107-opt/task.yaml")

    assert cfg["repo_path"].endswith("/omilrec-v100-postv107-gated")
    assert cfg["eval_commands"] == ["bash scripts/sl_eval_post_v107.sh --evtmax 10"]
    gates = cfg["metrics"]["gates"]
    assert [g["key"] for g in gates] == ["FCN", "CONSISTENCY", "EVAL_RESULT"]
    assert all("description" in g for g in gates), "every gate declares a description"
    assert "/omilrec/scripts/" not in "\n".join(cfg["eval_commands"])
    assert "/omilrec-v100/scripts/" not in "\n".join(cfg["eval_commands"])


@pytest.mark.parametrize("field", ["candidates_per_round", "max_workers"])
@pytest.mark.parametrize("value", [0, -1, "2"])
def test_config_parallel_rejects_invalid_values(tmp_path: Path, field: str, value):
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(_write_config(tmp_path, {field: value}))


def test_proposer_schema_requires_exact_candidate_count():
    schema = _proposer_schema(3)
    proposals = schema["properties"]["proposals"]
    assert proposals["minItems"] == 3
    assert proposals["maxItems"] == 3
    assert schema["required"] == ["reflection", "proposals"]
    assert schema["additionalProperties"] is False


def test_proposer_schema_uses_batch_shape_when_k_is_one():
    schema = _proposer_schema(1)
    proposals = schema["properties"]["proposals"]
    assert proposals["minItems"] == 1
    assert proposals["maxItems"] == 1


def test_parse_batch_rejects_legacy_single_when_k_is_one():
    with pytest.raises(ValueError, match="only reflection and proposals"):
        _parse_batch(
            {"reflection": "r", "decision": "continue", "proposal": "do one thing"},
            candidates_per_round=1,
        )


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


def test_parse_batch_rejects_legacy_when_k_is_greater_than_one():
    with pytest.raises(ValueError, match="only reflection and proposals"):
        _parse_batch(
            {"reflection": "r", "decision": "continue", "proposal": "only one"},
            candidates_per_round=3,
        )


def test_parse_batch_rejects_empty_batch():
    with pytest.raises(ValueError, match="expected exactly 3"):
        _parse_batch({"reflection": "r", "proposals": []}, candidates_per_round=3)


@pytest.mark.parametrize("count", [1, 2, 4])
def test_parse_batch_rejects_wrong_candidate_count(count: int):
    data = {
        "reflection": "r",
        "proposals": [
            {"family": f"family_{i}", "decision": "switch", "proposal": f"p{i}"}
            for i in range(count)
        ],
    }
    with pytest.raises(ValueError, match="expected exactly 3"):
        _parse_batch(data, candidates_per_round=3)


def test_parse_batch_rejects_duplicate_families():
    with pytest.raises(ValueError, match="duplicate family"):
        _parse_batch(
            {
                "reflection": "r",
                "proposals": [
                    {"family": "Layout", "decision": "switch", "proposal": "p0"},
                    {"family": " layout ", "decision": "continue", "proposal": "p1"},
                ],
            },
            candidates_per_round=2,
        )


def test_parse_batch_rejects_invalid_decision():
    with pytest.raises(ValueError, match="decision must be continue or switch"):
        _parse_batch(
            {
                "reflection": "r",
                "proposals": [
                    {"family": "layout", "decision": "maybe", "proposal": "p0"},
                ],
            },
            candidates_per_round=1,
        )


def test_proposer_passes_hard_schema_and_keeps_prompt_semantic(tmp_path: Path):
    class CapturingAgent:
        prompt = ""
        schema = None

        def run_json(self, prompt, json_schema=None, **_kwargs):
            self.prompt = prompt
            self.schema = json_schema
            return {
                "reflection": "",
                "proposals": [
                    {"family": "one", "decision": "switch", "proposal": "p1"},
                    {"family": "two", "decision": "switch", "proposal": "p2"},
                    {"family": "three", "decision": "switch", "proposal": "p3"},
                ],
            }

    agent = CapturingAgent()
    propose(
        agent,
        goal="make it faster",
        editable=["src/**"],
        frozen=["tests/**"],
        history=[],
        base_sha="base-sha",
        cwd=tmp_path,
        candidates_per_round=3,
    )

    assert agent.schema == _proposer_schema(3)


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


@pytest.mark.parametrize(
    ("lower_is_better", "prior_value", "candidate_values"),
    [
        (True, 100.0, [100.0, 120.0, 110.0]),
        (False, 100.0, [100.0, 80.0, 90.0]),
    ],
)
def test_selector_keeps_incumbent_when_no_candidate_improves_objective(
    lower_is_better: bool,
    prior_value: float,
    candidate_values: list[float],
):
    schema = {
        "objective": {"key": "OBJECTIVE", "lower_is_better": lower_is_better},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {
            "candidate": i,
            "sha": f"candidate-{i}",
            "risk": "low",
            "score": 1.0,
            "metrics": {"OBJECTIVE": value, "CORRECTNESS": True},
        }
        for i, value in enumerate(candidate_values)
    ]

    assert _select_winner(
        candidates,
        schema,
        prior_metrics={"OBJECTIVE": prior_value},
    ) is None


@pytest.mark.parametrize(
    ("lower_is_better", "prior_value", "candidate_values", "winner"),
    [
        (True, 100.0, [110.0, 90.0, 95.0], "candidate-1"),
        (False, 100.0, [90.0, 105.0, 101.0], "candidate-1"),
    ],
)
def test_selector_advances_only_when_best_candidate_improves_objective(
    lower_is_better: bool,
    prior_value: float,
    candidate_values: list[float],
    winner: str,
):
    schema = {
        "objective": {"key": "OBJECTIVE", "lower_is_better": lower_is_better},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {
            "candidate": i,
            "sha": f"candidate-{i}",
            "risk": "low",
            "score": 0.5,
            "metrics": {"OBJECTIVE": value, "CORRECTNESS": True},
        }
        for i, value in enumerate(candidate_values)
    ]

    selected = _select_winner(
        candidates,
        schema,
        prior_metrics={"OBJECTIVE": prior_value},
    )

    assert selected is not None
    assert selected["sha"] == winner


def test_selector_uses_best_candidate_when_prior_objective_is_missing():
    schema = {
        "objective": {"key": "OBJECTIVE", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {"candidate": 0, "sha": "slow", "risk": "low", "score": 0.5,
         "metrics": {"OBJECTIVE": 20.0, "CORRECTNESS": True}},
        {"candidate": 1, "sha": "fast", "risk": "low", "score": 0.5,
         "metrics": {"OBJECTIVE": 10.0, "CORRECTNESS": True}},
    ]

    assert _select_winner(candidates, schema, prior_metrics={})["sha"] == "fast"


def test_store_records_generation_candidates_and_proposer_view(tmp_path: Path):
    store = Store(tmp_path, metrics_schema={
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    })
    candidates = [
        {"candidate": 0, "family": "hoist", "proposal": "p0", "sha": "a",
         "score": 0.5, "risk": "low",
         "feedback": "LANDED_STATE: not-implemented\nImplemented: p0\nResult: worse\nAnalysis: no win.",
         "metrics": {"SPEED_MS": 600.0, "CORRECTNESS": True},
         "changed_paths": ["a.cc"], "accepted": True, "selected": False},
        {"candidate": 1, "family": "layout", "proposal": "p1", "sha": "b",
         "score": 0.7, "risk": "low",
         "feedback": "LANDED_STATE: not-implemented\nImplemented: p1\nResult: better\nAnalysis: cache locality.",
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
    assert "Implemented:" in projected[0]["candidates"][0]["feedback"]


def test_store_keeps_parent_and_best_when_generation_has_no_winner(tmp_path: Path):
    store = Store(tmp_path, metrics_schema={
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    })
    winner = {
        "candidate": 0, "family": "winner", "proposal": "p0", "sha": "best",
        "score": 0.8, "risk": "low", "feedback": "improved",
        "metrics": {"SPEED_MS": 100.0, "CORRECTNESS": True},
        "accepted": True, "selected": True,
    }
    store.append_generation(
        0,
        parent_sha="baseline",
        selected_candidate=0,
        selected_sha="best",
        candidates=[winner],
    )
    regressed = {
        "candidate": 0, "family": "regressed", "proposal": "p1", "sha": "slower",
        "score": 0.2, "risk": "low", "feedback": "regressed",
        "metrics": {"SPEED_MS": 120.0, "CORRECTNESS": True},
        "accepted": True, "selected": False,
    }
    store.append_generation(
        1,
        parent_sha="best",
        selected_candidate=None,
        selected_sha=None,
        candidates=[regressed],
    )

    generation = store.history()[1]
    assert generation["selected_candidate"] is None
    assert generation["selected_sha"] is None
    assert generation["base_sha"] == "best"
    assert generation["candidates"][0]["sha"] == "slower"
    assert generation["candidates"][0]["selected"] is False
    assert store.best_sha == "best"


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

    def fake_execute(agent, *, proposal, goal, editable, frozen, workspace, worktree, round_id, gate_block=""):
        return ExecResult(sha=f"sha-{round_id}", reason=None, changed_paths=[f"{round_id}.cc"])

    def fake_run_eval(commands, cwd, metrics_schema=None):
        cid = int(str(cwd).rsplit("c", 1)[-1])
        return "eval", {"SPEED_MS": 100.0 + cid, "CORRECTNESS": True}

    def fake_judge(agent, **kwargs):
        return Judgment(score=0.5, risk="low",
                        feedback="LANDED_STATE: not-implemented\nImplemented: x\nResult: y\nAnalysis: z.")

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
        }, workspace, object(), object(), {"SPEED_MS": 150.0}, {"SPEED_MS": 200.0}, schema, "")

    assert workspace.added == [("7-c0", "parent"), ("7-c1", "parent"), ("7-c2", "parent")]
    assert workspace.removed == ["7-c0", "7-c1", "7-c2"]
    assert _select_winner(candidates, schema)["candidate"] == 0


def test_agent_structured_json_uses_validated_output(monkeypatch, tmp_path: Path):
    agent = Agent()
    expected = {"reflection": "", "proposals": []}

    def fake_run(*_args, **_kwargs):
        return AgentResult(text="ignored", data=expected)

    monkeypatch.setattr(agent, "_run", fake_run)

    assert agent.run_json(
        "prompt",
        cwd=tmp_path,
        label="proposer",
        json_schema={"type": "object"},
    ) is expected


def test_agent_structured_json_rejects_prose_wrapped_json(monkeypatch, tmp_path: Path):
    agent = Agent()

    def fake_run(*_args, **_kwargs):
        return AgentResult(
            text='explanation before {"reflection":"","proposals":[]}',
            data={},
        )

    monkeypatch.setattr(agent, "_run", fake_run)

    with pytest.raises(AgentError, match="not an exact JSON object"):
        agent.run_json(
            "prompt",
            cwd=tmp_path,
            label="proposer",
            json_schema={"type": "object"},
        )


def test_run_aborts_before_executor_when_proposer_contract_fails(
    monkeypatch, tmp_path: Path
):
    cfg = {
        "goal": "go faster",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "max_rounds": 1,
        "candidates_per_round": 3,
        "max_workers": 3,
        "agent_timeout_seconds": 10,
        "metrics": None,
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "eval_commands": [],
    }

    class FakeAgent:
        def __init__(self, **_kwargs):
            pass

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.run_dir = run_dir
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline-sha"

    class FakeStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def history(self):
            return []

        def append(self, *_args, **_kwargs):
            raise AssertionError("proposer failure must not be written as a round")

    executor_called = False

    def fail_proposer(*_args, **_kwargs):
        raise ValueError("invalid proposer batch")

    def fail_if_executor_runs(*_args, **_kwargs):
        nonlocal executor_called
        executor_called = True
        raise AssertionError("executor must not run")

    monkeypatch.setattr(config_mod, "load", lambda _path: cfg)
    monkeypatch.setattr(loop_mod, "Agent", FakeAgent)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)
    monkeypatch.setattr(loop_mod, "Store", FakeStore)
    monkeypatch.setattr(loop_mod, "_eval_baseline", lambda *_args: ("", {}))
    monkeypatch.setattr(loop_mod.proposer_mod, "propose", fail_proposer)
    monkeypatch.setattr(loop_mod, "_run_candidates", fail_if_executor_runs)

    with pytest.raises(ValueError, match="invalid proposer batch"):
        loop_mod.run("config.yaml", tmp_path / "run")

    assert executor_called is False
