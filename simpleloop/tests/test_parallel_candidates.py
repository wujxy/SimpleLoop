from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop import loop as loop_mod
from simpleloop import memory as memory_mod
from simpleloop import views
from simpleloop.agent import Agent, AgentError, AgentResult
from simpleloop.executor import ExecResult
from simpleloop.judger import EvalResult, Judgment, _parse as parse_judgment
from simpleloop.loop import _run_candidates, _select_winner
from simpleloop.proposer import Proposal, ProposalBatch
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
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"SIF-test-double")
    cfg = {
        "kind": "task",
        "task": {"goal": "go faster"},
        "safety": {"editable_paths": ["src/**"], "frozen_paths": []},
        "loop": {"max_rounds": 3, **(loop_block or {})},
        "runtime": {"image": "runtime.sif"},
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
    cfg = _example_yaml("omilrec-post-v107-opt/task.yaml")

    assert cfg["source"]["path"].endswith("/omilrec-v100-postv107-gated")
    assert cfg["eval"]["commands"] == [
        "bash scripts/sl_eval_post_v107.sh --evtmax 10"
    ]
    gates = cfg["eval"]["metrics"]["gates"]
    assert [g["key"] for g in gates] == ["FCN", "CONSISTENCY", "EVAL_RESULT"]
    assert all("description" in g for g in gates), "every gate declares a description"
    assert "/omilrec/scripts/" not in "\n".join(cfg["eval"]["commands"])
    assert "/omilrec-v100/scripts/" not in "\n".join(cfg["eval"]["commands"])


@pytest.mark.parametrize("field", ["candidates_per_round", "max_workers"])
@pytest.mark.parametrize("value", [0, -1, "2"])
def test_config_parallel_rejects_invalid_values(tmp_path: Path, field: str, value):
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(_write_config(tmp_path, {field: value}))


def test_proposer_schema_keeps_structure_without_text_max_lengths():
    schema = _proposer_schema(3)
    proposals = schema["properties"]["proposals"]
    assert proposals["minItems"] == proposals["maxItems"] == 3
    assert schema["required"] == [
        "reflection", "insight", "insight_refs", "proposals",
    ]
    assert schema["additionalProperties"] is False
    assert "maxLength" not in schema["properties"]["reflection"]
    assert "maxLength" not in schema["properties"]["insight"]
    assert "maxLength" not in schema["properties"]["insight_refs"]["items"]
    assert schema["properties"]["reflection"]["type"] == "string"
    assert schema["properties"]["insight"]["type"] == "string"
    assert schema["properties"]["insight_refs"]["items"]["type"] == "string"
    proposal_items = proposals["items"]
    assert proposal_items["additionalProperties"] is False
    assert proposal_items["required"] == ["family", "decision", "proposal"]
    item_properties = proposals["items"]["properties"]
    assert "maxLength" not in item_properties["family"]
    assert "maxLength" not in item_properties["proposal"]
    assert item_properties["family"]["type"] == "string"
    assert item_properties["family"]["minLength"] == 1
    assert item_properties["proposal"]["type"] == "string"
    assert item_properties["proposal"]["minLength"] == 1
    assert item_properties["proposal"]["pattern"] == r"\S"
    assert item_properties["family"]["pattern"] == r"\S"
    assert item_properties["decision"]["enum"] == ["continue", "switch"]


def test_proposer_schema_uses_batch_shape_when_k_is_one():
    schema = _proposer_schema(1)
    proposals = schema["properties"]["proposals"]
    assert proposals["minItems"] == 1
    assert proposals["maxItems"] == 1


def test_parse_batch_rejects_legacy_single_when_k_is_one():
    with pytest.raises(ValueError, match="only reflection, insight, insight_refs, and proposals"):
        _parse_batch(
            {"reflection": "r", "decision": "continue", "proposal": "do one thing"},
            candidates_per_round=1,
        )


def test_parse_batch_accepts_new_shape():
    batch = _parse_batch(
        {
            "reflection": "r",
            "insight": "Sparse gathers benefit from packing.",
            "insight_refs": ["r0c0", "r1c1"],
            "proposals": [
                {"family": "layout", "decision": "switch", "proposal": "p0"},
                {"family": "hoist", "decision": "continue", "proposal": "p1"},
            ],
        },
        candidates_per_round=2,
    )
    assert [p.family for p in batch.proposals] == ["layout", "hoist"]
    assert [p.proposal for p in batch.proposals] == ["p0", "p1"]
    assert batch.insight == "Sparse gathers benefit from packing."
    assert batch.insight_refs == ["r0c0", "r1c1"]


def test_parse_batch_accepts_n_plus_500_without_warning(capsys):
    batch = _parse_batch(
        {
            "reflection": "r" * 1100,
            "insight": "i" * 1000,
            "insight_refs": ["x" * 532],
            "proposals": [
                {
                    "family": "f" * 564,
                    "decision": "switch",
                    "proposal": "p" * 1300,
                },
            ],
        },
        candidates_per_round=1,
    )

    assert len(batch.reflection) == 1100
    assert len(batch.insight) == 1000
    assert len(batch.insight_refs[0]) == 532
    assert len(batch.proposals[0].family) == 564
    assert len(batch.proposals[0].proposal) == 1300
    assert capsys.readouterr().out == ""


def test_parse_batch_warns_and_truncates_every_free_text_field(capsys):
    batch = _parse_batch(
        {
            "reflection": "r" * 1131,
            "insight": "i" * 1001,
            "insight_refs": ["x" * 533],
            "proposals": [
                {
                    "family": "f" * 565,
                    "decision": "continue",
                    "proposal": "p" * 1301,
                },
            ],
        },
        candidates_per_round=1,
    )

    assert len(batch.reflection) == 1100
    assert len(batch.insight) == 1000
    assert len(batch.insight_refs[0]) == 532
    assert len(batch.proposals[0].family) == 564
    assert len(batch.proposals[0].proposal) == 1300
    output = capsys.readouterr().out
    assert "reflection length 1131 exceeds 1100" in output
    assert "insight length 1001 exceeds 1000" in output
    assert "insight_refs[0] length 533 exceeds 532" in output
    assert "proposals[0].family length 565 exceeds 564" in output
    assert "proposals[0].proposal length 1301 exceeds 1300" in output


def test_parse_batch_rejects_families_equal_after_truncation():
    prefix = "x" * 564
    with pytest.raises(ValueError, match="duplicate family"):
        _parse_batch(
            {
                "reflection": "r",
                "insight": "",
                "insight_refs": [],
                "proposals": [
                    {
                        "family": prefix + "a",
                        "decision": "switch",
                        "proposal": "p0",
                    },
                    {
                        "family": prefix + "b",
                        "decision": "continue",
                        "proposal": "p1",
                    },
                ],
            },
            candidates_per_round=2,
        )


def test_parse_batch_rejects_legacy_when_k_is_greater_than_one():
    with pytest.raises(ValueError, match="only reflection, insight, insight_refs, and proposals"):
        _parse_batch(
            {"reflection": "r", "decision": "continue", "proposal": "only one"},
            candidates_per_round=3,
        )


def test_parse_batch_rejects_empty_batch():
    with pytest.raises(ValueError, match="expected exactly 3"):
        _parse_batch({"reflection": "r", "insight": "", "insight_refs": [], "proposals": []}, candidates_per_round=3)


@pytest.mark.parametrize("count", [1, 2, 4])
def test_parse_batch_rejects_wrong_candidate_count(count: int):
    data = {
        "reflection": "r",
        "insight": "",
        "insight_refs": [],
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
                "insight": "",
                "insight_refs": [],
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
                "insight": "",
                "insight_refs": [],
                "proposals": [
                    {"family": "layout", "decision": "maybe", "proposal": "p0"},
                ],
            },
            candidates_per_round=1,
        )


@pytest.mark.parametrize(
    "proposal",
    [
        {"family": "", "decision": "switch", "proposal": "p"},
        {"family": "   ", "decision": "switch", "proposal": "p"},
        {"family": 7, "decision": "switch", "proposal": "p"},
        {"family": "f", "decision": "switch", "proposal": ""},
        {"family": "f", "decision": "switch", "proposal": "   "},
        {"family": "f", "decision": "switch", "proposal": 7},
        {"family": "f", "decision": "switch", "proposal": "p", "extra": True},
        {"family": "f", "decision": "switch"},
    ],
)
def test_parse_batch_rejects_invalid_nested_contract(proposal):
    with pytest.raises(ValueError):
        _parse_batch(
            {
                "reflection": "r",
                "insight": "",
                "insight_refs": [],
                "proposals": [proposal],
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
                "reflection": "r" * 900,
                "insight": "",
                "insight_refs": [],
                "proposals": [
                    {"family": "a" * 364, "decision": "switch", "proposal": "p" * 1100},
                    {"family": "b" * 364, "decision": "switch", "proposal": "q" * 1100},
                    {"family": "c" * 364, "decision": "switch", "proposal": "s" * 1100},
                ],
            }

    agent = CapturingAgent()
    batch = propose(
        agent,
        goal="make it faster",
        editable=["src/**"],
        frozen=["tests/**"],
        history=[],
        insights=[{
            "id": "I4",
            "text": "Hoisting behind cold gates measured as noise.",
            "refs": ["r0c0", "r3c0"],
        }],
        base_sha="base-sha",
        cwd=tmp_path,
        candidates_per_round=3,
    )

    assert agent.schema == _proposer_schema(3)
    assert len(batch.reflection) == 900
    assert [len(item.family) for item in batch.proposals] == [364, 364, 364]
    assert [len(item.proposal) for item in batch.proposals] == [1100, 1100, 1100]
    prompt = " ".join(agent.prompt.split())
    assert "Accumulated search insights:" in prompt
    assert "[I4] Hoisting behind cold gates measured as noise." in prompt
    assert "Evidence: r0c0, r3c0" in prompt
    assert "simpleloop memory show <ref>" in prompt
    assert "`reflection`:" in agent.prompt
    assert "`insight`:" in agent.prompt
    assert "`insight_refs`:" in agent.prompt
    assert "`decision`:" in agent.prompt
    assert "[\"r0c0\", \"r1c1\"]" in agent.prompt
    assert "one or two concise, generalizing sentences" in prompt
    assert (
        "previous evidence -> reflection -> optional insight "
        "-> decision -> proposal"
    ) in prompt
    assert "grounded hypothesis, not an implementation conclusion" in prompt
    assert "The EXECUTOR investigates implementation details" in prompt
    assert "The JUDGER evaluates the resulting diff" in prompt
    assert "batch-level search rationale" in prompt
    assert "not a single continue/switch verdict for the whole batch" in prompt
    assert "Once the EXECUTOR has enough to take over" in prompt
    assert "objective change relative to the direct accepted parent" in prompt
    assert "git show base-sha:<path>" in prompt
    assert "Propose exactly 3 experiments" in prompt
    assert "At most 600 characters" in prompt
    assert "at most 64 characters" in prompt
    assert "at most 800 characters" in prompt
    assert "highest-value" not in prompt
    assert "stalled/exhausted" not in prompt
    assert "check whether a mechanism was already attempted" not in prompt


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
         "feedback_for_proposer": "Hoisting did not help this workload.",
         "metrics": {"SPEED_MS": 600.0, "CORRECTNESS": True},
         "changed_paths": ["a.cc"], "accepted": True, "selected": False},
        {"candidate": 1, "family": "layout", "proposal": "p1", "sha": "b",
         "score": 0.7, "risk": "low",
         "feedback": "LANDED_STATE: not-implemented\nImplemented: p1\nResult: better\nAnalysis: cache locality.",
         "feedback_for_proposer": "Layout remains a promising direction.",
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True},
         "changed_paths": ["b.cc"], "accepted": True, "selected": True},
    ]
    store.append_generation(0, parent_sha="base", selected_candidate=1,
                            selected_sha="b", candidates=candidates,
                            reflection="batch reflection")
    rows = store.history()
    assert rows[0]["selected_sha"] == "b"
    assert rows[0]["candidates"][1]["selected"] is True
    assert rows[0]["feedback_for_proposer"] == "Layout remains a promising direction."
    assert rows[0]["candidates"][0]["feedback_for_proposer"] == (
        "Hoisting did not help this workload."
    )
    assert store.best_sha == "b"

    projected = views.for_proposer(rows)
    assert projected[0]["selected_sha"] == "b"
    assert projected[0]["candidates"][0]["feedback_for_proposer"] == (
        "Hoisting did not help this workload."
    )
    assert "feedback" not in projected[0]["candidates"][0]


def test_proposer_prompt_uses_only_feedback_for_proposer(tmp_path: Path):
    class CapturingAgent:
        prompt = ""

        def run_json(self, prompt, **_kwargs):
            self.prompt = prompt
            return {
                "reflection": "r",
                "insight": "",
                "insight_refs": [],
                "proposals": [
                    {"family": "layout", "decision": "switch", "proposal": "p"},
                ],
            }

    agent = CapturingAgent()
    propose(
        agent,
        goal="g",
        editable=["src/**"],
        frozen=[],
        history=[{
            "round": 0,
            "proposal": "old proposal",
            "sha": "old-sha",
            "score": 0.5,
            "feedback": "FULL_TECHNICAL_SENTINEL",
            "feedback_for_proposer": "SHORT_SEARCH_SENTINEL",
        }],
        insights=[],
        base_sha="base",
        cwd=tmp_path,
    )

    assert "SHORT_SEARCH_SENTINEL" in agent.prompt
    assert "FULL_TECHNICAL_SENTINEL" not in agent.prompt
    assert "feedback_for_proposer=" in agent.prompt


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

    def fake_run_eval(commands, cwd, runtime, metrics_schema=None):
        cid = int(str(cwd).rsplit("c", 1)[-1])
        return EvalResult(
            "eval",
            {"SPEED_MS": 100.0 + cid, "CORRECTNESS": True},
            (0,),
        )

    judger_labels = []

    def fake_judge(agent, **kwargs):
        judger_labels.append(kwargs["label"])
        return Judgment(score=0.5, risk="low",
                        feedback="LANDED_STATE: not-implemented\nImplemented: x\nResult: y\nAnalysis: z.",
                        feedback_for_proposer="The mechanism remains plausible.")

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
        }, workspace, object(), object(), {"SPEED_MS": 150.0},
        {"SPEED_MS": 200.0}, schema, "", object(),
    )

    assert workspace.added == [("7-c0", "parent"), ("7-c1", "parent"), ("7-c2", "parent")]
    assert workspace.removed == ["7-c0", "7-c1", "7-c2"]
    assert judger_labels == ["judger r7-c0", "judger r7-c1", "judger r7-c2"]
    assert _select_winner(candidates, schema)["candidate"] == 0


def test_run_candidates_logs_candidate_local_failure(monkeypatch, tmp_path: Path, capsys):
    class FakeWorkspace:
        def add_worktree(self, round_id, parent_sha):
            return tmp_path / str(round_id)

        def remove_worktree(self, round_id):
            pass

        def diff(self, parent_sha, sha):
            return "diff"

    def fake_execute(*_args, **_kwargs):
        return ExecResult(sha="candidate", reason=None, changed_paths=["a.cc"])

    def fake_judge(*_args, **_kwargs):
        return parse_judgment({
            "score": 0.5,
            "risk": "low",
            "feedback": "FULL_TECHNICAL_SENTINEL",
            "feedback_for_proposer": "",
        })

    monkeypatch.setattr(loop_mod.executor_mod, "execute", fake_execute)
    monkeypatch.setattr(loop_mod.judger_mod, "judge", fake_judge)

    candidates = _run_candidates(
        [Proposal(proposal="p0", family="f0")], 2, "parent", {
            "goal": "g", "editable_paths": ["src/**"], "frozen_paths": [],
            "eval_commands": [], "max_workers": 1,
        }, FakeWorkspace(), object(), object(), {}, {}, None, "", object(),
    )

    assert candidates[0]["score"] == 0.0
    assert candidates[0]["feedback_for_proposer"] == (
        "[loop failure] candidate failed before a usable result was produced"
    )
    assert "FULL_TECHNICAL_SENTINEL" not in candidates[0]["feedback_for_proposer"]
    assert "FULL_TECHNICAL_SENTINEL" in candidates[0]["feedback"]
    assert "candidate r2-c0 failed:" in capsys.readouterr().out


def test_run_candidates_logs_outer_parallel_worker_failure(monkeypatch, capsys):
    def fail_worker(candidate_id, *_args, **_kwargs):
        raise RuntimeError(f"worker {candidate_id} exploded")

    monkeypatch.setattr(loop_mod, "_run_one_candidate", fail_worker)
    candidates = _run_candidates(
        [
            Proposal(proposal="p0", family="f0"),
            Proposal(proposal="p1", family="f1"),
        ],
        3,
        "parent",
        {"max_workers": 2},
        object(), object(), object(), {}, {}, None, "", object(),
    )

    assert [candidate["score"] for candidate in candidates] == [0.0, 0.0]
    out = capsys.readouterr().out
    assert "candidate r3-c0 worker failed: worker 0 exploded" in out
    assert "candidate r3-c1 worker failed: worker 1 exploded" in out


def test_agent_structured_json_uses_validated_output(monkeypatch, tmp_path: Path):
    agent = Agent(runtime=object())
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
    agent = Agent(runtime=object())

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

def _run_insight_integration(
    monkeypatch, tmp_path, *, insight, refs, existing_insights=None,
    corrupt_insights=False, proposer_calls=None,
):
    run_dir = tmp_path / "run"
    seed = Store(run_dir)
    seed.append_generation(
        0,
        parent_sha="baseline-sha",
        selected_candidate=0,
        selected_sha="seed-sha",
        candidates=[{
            "candidate": 0,
            "family": "seed",
            "proposal": "seed proposal",
            "sha": "seed-sha",
            "score": 0.5,
            "risk": "low",
            "feedback": "seed feedback",
            "accepted": True,
        }],
    )
    insights_path = run_dir / "insights.jsonl"
    for record in existing_insights or []:
        memory_mod.append_insight(
            insights_path,
            int(record["id"][1:]),
            record["text"],
            record["refs"],
        )
    if corrupt_insights:
        insights_path.write_text("{not-json\n", encoding="utf-8")
    cfg = {
        "goal": "go faster",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "max_rounds": 2,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 10,
        "metrics": None,
        "repo_path": tmp_path / "source",
        "baseline_ref": "HEAD",
        "eval_commands": [],
    }

    class FakeAgent:
        def __init__(self, **_kwargs):
            self.runtime = object()

    class FakeWorkspace:
        def __init__(self, *, run_dir, **_kwargs):
            self.run_dir = run_dir
            self.repo = run_dir / "repo"

        def setup(self):
            self.repo.mkdir(parents=True, exist_ok=True)

        def baseline_sha(self):
            return "baseline-sha"

    executed = []

    def fake_propose(*_args, **kwargs):
        if proposer_calls is not None:
            proposer_calls.append(True)
        assert kwargs["insights"] == (existing_insights or [])
        assert [row["round"] for row in kwargs["history"]] == [0]
        return ProposalBatch(
            reflection="historical result narrows the useful mechanism",
            insight=insight,
            insight_refs=refs,
            proposals=[Proposal(
                family="layout",
                decision="continue",
                proposal="test another sparse gather",
            )],
        )

    def fake_run_candidates(*_args, **_kwargs):
        executed.append(True)
        return [{
            "candidate": 0,
            "family": "layout",
            "decision": "continue",
            "proposal": "test another sparse gather",
            "sha": None,
            "score": 0.0,
            "risk": "high",
            "feedback": "no improvement",
            "accepted": False,
        }]

    monkeypatch.setattr(config_mod, "load", lambda _path: cfg)
    monkeypatch.setattr(loop_mod, "Agent", FakeAgent)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)
    monkeypatch.setattr(loop_mod, "_eval_baseline", lambda *_args: ("", {}))
    monkeypatch.setattr(loop_mod.proposer_mod, "propose", fake_propose)
    monkeypatch.setattr(loop_mod, "_run_candidates", fake_run_candidates)
    monkeypatch.setattr(loop_mod, "_select_winner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(loop_mod, "_refresh_progress_plot", lambda *_args: None)

    loop_mod.run("config.yaml", run_dir, continue_run=True)
    return run_dir, executed


def test_run_persists_valid_insight_after_generation(monkeypatch, tmp_path):
    run_dir, executed = _run_insight_integration(
        monkeypatch,
        tmp_path,
        insight="Sparse gathers benefit from packing.",
        refs=["r0c0"],
    )

    assert executed == [True]
    assert json.loads((run_dir / "insights.jsonl").read_text()) == {
        "id": "I1",
        "text": "Sparse gathers benefit from packing.",
        "refs": ["r0c0"],
    }
    assert [row["round"] for row in Store(run_dir).history()] == [0, 1]


def test_run_skips_invalid_insight_without_skipping_generation(
    monkeypatch, tmp_path, capsys
):
    run_dir, executed = _run_insight_integration(
        monkeypatch,
        tmp_path,
        insight="Unsupported lesson.",
        refs=["r99c0"],
    )

    assert executed == [True]
    assert not (run_dir / "insights.jsonl").exists()
    assert [row["round"] for row in Store(run_dir).history()] == [0, 1]
    output = capsys.readouterr().out
    assert "insight skipped: memory reference not found: r99c0" in output


def test_continue_loads_existing_insights(monkeypatch, tmp_path):
    existing = [{
        "id": "I0",
        "text": "The seed established a reusable constraint.",
        "refs": ["r0c0"],
    }]
    run_dir, executed = _run_insight_integration(
        monkeypatch,
        tmp_path,
        insight="",
        refs=[],
        existing_insights=existing,
    )

    assert executed == [True]
    assert memory_mod.load_insights(run_dir / "insights.jsonl") == existing


def test_existing_identical_round_insight_is_idempotent(monkeypatch, tmp_path):
    existing = [{
        "id": "I1",
        "text": "Sparse gathers benefit from packing.",
        "refs": ["r0c0"],
    }]
    run_dir, executed = _run_insight_integration(
        monkeypatch,
        tmp_path,
        insight=existing[0]["text"],
        refs=existing[0]["refs"],
        existing_insights=existing,
    )

    assert executed == [True]
    assert memory_mod.load_insights(run_dir / "insights.jsonl") == existing


def test_corrupt_insights_abort_before_proposer(monkeypatch, tmp_path):
    proposer_calls = []
    with pytest.raises(ValueError, match="could not read insight memory"):
        _run_insight_integration(
            monkeypatch,
            tmp_path,
            insight="",
            refs=[],
            corrupt_insights=True,
            proposer_calls=proposer_calls,
        )

    assert proposer_calls == []


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
