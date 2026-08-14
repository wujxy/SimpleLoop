from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop import app as loop_mod
from simpleloop.candidate import (
    CandidateArtifact,
    CandidateBatchRequest,
    CandidatePlan,
    CandidateResult,
    CandidateStatus,
    EvaluationResult,
    ExecutionResult,
    GateDecision,
)
from proposer.memory import MemoryService
from simpleloop.stages.agent import Agent, AgentError, AgentResult
from simpleloop.persistence.history import Store, best_candidate
from simpleloop.stages.proposer import Proposal, ProposalBatch, ProposerRequest, StaticProposer
from simpleloop.stages.selector import select_candidate
from simpleloop.world import ProcessResult, SourceWorkspace
from round_helpers import append_round


EXAMPLES = Path(__file__).parents[1] / "examples"

_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
           "gates": [{"key": "CORRECTNESS"}]}


def _typed(rows: list[dict]) -> tuple[CandidateResult, ...]:
    """Translate compact policy-test facts into the production contract."""
    results = []
    for row in rows:
        candidate_id = int(row.get("candidate", 0))
        parent_sha = str(row.get("parent_sha", "parent"))
        sha = row.get("sha")
        status = CandidateStatus(row.get(
            "status",
            "COMPLETED" if row.get("gate_passed") else "GATE_REJECTED",
        ))
        metrics = row.get("metrics") or {}
        results.append(CandidateResult(
            candidate_id=candidate_id,
            experiment_id=str(row.get("experiment_id", f"r0c{candidate_id}")),
            proposal=Proposal(str(row.get("proposal", f"p{candidate_id}"))),
            parent_sha=parent_sha,
            status=status,
            execution=ExecutionResult(status.value),
            artifact=(
                CandidateArtifact(parent_sha, str(sha)) if sha else None
            ),
            evaluation=(
                EvaluationResult("", metrics) if metrics else None
            ),
            gate=GateDecision(
                {}, bool(row.get("gate_passed")), bool(row.get("eligible")),
            ),
            usage=tuple(row.get("usage") or ()),
            telemetry=row.get("telemetry") or {},
        ))
    return tuple(results)


def _select(candidates, schema, prior_metrics=None):
    objective = schema["objective"]
    prior = (prior_metrics or {}).get(objective["key"])
    return select_candidate(
        candidates=tuple(candidates),
        objective_key=objective["key"],
        lower_is_better=objective["lower_is_better"],
        incumbent_value=(
            float(prior)
            if isinstance(prior, (int, float)) and not isinstance(prior, bool)
            else None
        ),
    )


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
        "safety": {"editable_paths": ["src/**"]},
        "loop": {"max_rounds": 3, **(loop_block or {})},
        "runtime": {"image": "runtime.sif"},
        "source": {"path": str(repo), "baseline_ref": "HEAD"},
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [{"key": "CORRECTNESS"}],
            },
        },
    }
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


def test_config_parallel_defaults(tmp_path: Path):
    cfg = config_mod.load(_write_config(tmp_path))
    assert cfg["candidates_per_round"] == 1
    assert cfg["max_workers"] == 1


@pytest.mark.parametrize("relative_path", [
    "tiny_algo_opt/task.yaml",
    "omilrec-opt/task.yaml",
    "omilrec-post-v107-opt/task.yaml",
])
def test_fanout_examples_declare_gate_descriptions_and_matched_workers(
    relative_path: str,
):
    raw = _example_yaml(relative_path)

    # Real conformance invariants (not example-specific pins): every gate
    # declares a description the harness renders, and parallel fanout runs as
    # many workers as candidates per round.
    gates = raw["evaluation"]["gates"]
    assert gates and all("description" in g for g in gates)
    loop = raw["loop"]
    assert (
        loop["candidates_per_round"]
        == loop["max_parallel_candidates"]
        >= 1
    )


@pytest.mark.parametrize("relative_path", [
    "omilrec-opt/task.yaml",
    "omilrec-v100-opt/task.yaml",
    "omilrec-post-v107-opt/task.yaml",
])
def test_default_omilrec_tasks_define_outcomes_not_research_methods(
    relative_path: str,
):
    raw = _example_yaml(relative_path)
    goal = raw["goal"].lower()

    assert "speed_ms" in goal
    assert "every configured gate" in goal
    for prescribed in (
        "hoisting", "caching", "soa", "safe", "forbidden",
        "second likelihood",
    ):
        assert prescribed not in goal
    assert "OMILRECV2/src" in raw["world"]["writable"]
    assert "OMILRECV2/CMakeLists.txt" in raw["world"]["writable"]
    # frozen_paths AND read_only_paths are both gone — the read-only world is
    # the whole worktree minus editable, enforced by the mount (ro base + :rw
    # overlay for editable), not by an explicit frozen list.
    assert "frozen_paths" not in raw["world"]
    assert "read_only_paths" not in raw["world"]


def test_runtime_architecture_has_no_judger_module_or_packaged_prompt():
    root = EXAMPLES.parent

    assert not (root / "simpleloop/roles/judger.py").exists()
    assert not (root / "simpleloop/prompts/judger.md").exists()
    for path in (root / "simpleloop").rglob("*.py"):
        assert "roles.judger" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("field", ["candidates_per_round", "max_workers"])
@pytest.mark.parametrize("value", [0, -1, "2"])
def test_config_parallel_rejects_invalid_values(tmp_path: Path, field: str, value):
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(_write_config(tmp_path, {field: value}))


def test_selector_uses_objective_and_filters_ineligible_candidates():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}, {"key": "EVAL_RESULT"}],
    }
    candidates = [
        {"candidate": 0, "sha": "slow", "gate_passed": True, "eligible": True,
         "metrics": {"SPEED_MS": 700.0, "CORRECTNESS": True, "EVAL_RESULT": True}},
        {"candidate": 1, "sha": "fast-but-rejected", "gate_passed": False,
         "eligible": False,
         "metrics": {"SPEED_MS": 100.0, "CORRECTNESS": True, "EVAL_RESULT": True}},
        {"candidate": 2, "sha": "fast-fail", "gate_passed": False,
         "eligible": False,
         "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": False, "EVAL_RESULT": True}},
        {"candidate": 3, "sha": "winner", "gate_passed": True, "eligible": True,
         "metrics": {"SPEED_MS": 650.0, "CORRECTNESS": True, "EVAL_RESULT": True}},
    ]
    assert _select(_typed(candidates), schema).sha == "winner"


def test_selector_uses_only_eligibility_objective_and_candidate_order():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {"candidate": 1, "sha": "b", "gate_passed": True, "eligible": True,
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True}},
        {"candidate": 0, "sha": "a", "gate_passed": True, "eligible": True,
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True}},
        {"candidate": 2, "sha": "faster-but-rejected",
         "gate_passed": False, "eligible": False,
         "metrics": {"SPEED_MS": 400.0, "CORRECTNESS": False}},
    ]
    assert _select(_typed(candidates), schema).sha == "a"


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
            "gate_passed": True,
            "eligible": True,
            "metrics": {"OBJECTIVE": value, "CORRECTNESS": True},
        }
        for i, value in enumerate(candidate_values)
    ]

    assert _select(
        _typed(candidates),
        schema,
        prior_metrics={"OBJECTIVE": prior_value},
    ).sha is None


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
            "gate_passed": True,
            "eligible": True,
            "metrics": {"OBJECTIVE": value, "CORRECTNESS": True},
        }
        for i, value in enumerate(candidate_values)
    ]

    selected = _select(
        _typed(candidates),
        schema,
        prior_metrics={"OBJECTIVE": prior_value},
    )

    assert selected.sha is not None
    assert selected.sha == winner


def test_selector_uses_best_candidate_when_prior_objective_is_missing():
    schema = {
        "objective": {"key": "OBJECTIVE", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {"candidate": 0, "sha": "slow", "gate_passed": True, "eligible": True,
         "metrics": {"OBJECTIVE": 20.0, "CORRECTNESS": True}},
        {"candidate": 1, "sha": "fast", "gate_passed": True, "eligible": True,
         "metrics": {"OBJECTIVE": 10.0, "CORRECTNESS": True}},
    ]

    assert _select(
        _typed(candidates), schema, prior_metrics={}
    ).sha == "fast"


def test_round_performance_reports_best_eligible_value_and_relative_improvement(
    capsys,
):
    schema = {
        "objective": {"key": "OBJECTIVE", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }
    candidates = [
        {"candidate": 0, "sha": "valid", "gate_passed": True,
         "eligible": True,
         "metrics": {"OBJECTIVE": 110.0, "CORRECTNESS": True}},
        {"candidate": 1, "sha": "rejected", "gate_passed": False,
         "eligible": False,
         "metrics": {"OBJECTIVE": 50.0, "CORRECTNESS": False}},
    ]

    loop_mod._print_round_performance(
        round_id=2,
        candidates=_typed(candidates),
        metrics_schema=schema,
        prior_metrics={"OBJECTIVE": 100.0},
    )

    output = capsys.readouterr().out
    # Best eligible objective is reported; the rejected candidate is excluded;
    # an improvement indicator is shown. Assert behavior, not exact format.
    assert "harness performance round" in output
    assert "OBJECTIVE" in output
    assert "110" in output
    assert "OBJECTIVE=50" not in output
    assert "improvement" in output.lower()


def test_store_records_generation_candidates_and_proposer_view(tmp_path: Path):
    store = Store(tmp_path, metrics_schema={
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    })
    candidates = [
        {"candidate": 0, "proposal": "p0", "parent_sha": "base", "sha": "a",
         "status": "COMPLETED", "gate_passed": True, "eligible": True,
         "gates": {"CORRECTNESS": {"passed": True, "detail": ""}},
         "metrics": {"SPEED_MS": 600.0, "CORRECTNESS": True},
         "changed_paths": ["a.cc"], "selected": False},
        {"candidate": 1, "proposal": "p1", "parent_sha": "base", "sha": "b",
         "status": "COMPLETED", "gate_passed": True, "eligible": True,
         "gates": {"CORRECTNESS": {"passed": True, "detail": ""}},
         "metrics": {"SPEED_MS": 500.0, "CORRECTNESS": True},
         "changed_paths": ["b.cc"], "selected": True},
    ]
    append_round(store, 0, parent_sha="base", selected_candidate=1,
                            selected_sha="b", candidates=candidates)
    rows = store.history()
    assert rows[0]["selected_sha"] == "b"
    assert rows[0]["candidates"][1]["selected"] is True
    assert rows[0]["candidates"][0]["gates"]["CORRECTNESS"]["passed"] is True
    assert best_candidate(rows, store.metrics_schema)["sha"] == "b"


def test_store_keeps_parent_and_best_when_generation_has_no_winner(tmp_path: Path):
    store = Store(tmp_path, metrics_schema={
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    })
    winner = {
        "candidate": 0, "proposal": "p0", "sha": "best",
        "status": "COMPLETED", "gate_passed": True, "eligible": True,
        "metrics": {"SPEED_MS": 100.0, "CORRECTNESS": True},
        "selected": True,
    }
    append_round(store,
        0,
        parent_sha="baseline",
        selected_candidate=0,
        selected_sha="best",
        candidates=[winner],
    )
    regressed = {
        "candidate": 0, "proposal": "p1", "sha": "slower",
        "status": "COMPLETED", "gate_passed": True, "eligible": True,
        "metrics": {"SPEED_MS": 120.0, "CORRECTNESS": True},
        "selected": False,
    }
    append_round(store,
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
    assert best_candidate(
        store.history(), store.metrics_schema,
    )["sha"] == "best"


def test_agent_structured_json_uses_validated_output(monkeypatch, tmp_path: Path):
    agent = Agent(world=object())
    expected = {"proposals": []}

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
    agent = Agent(world=object())

    def fake_run(*_args, **_kwargs):
        return AgentResult(
            text='explanation before {"proposals":[]}',
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


def test_next_proposals_static_mode_returns_host_proposal(tmp_path):
    result = StaticProposer(("fixed",)).propose(
        ProposerRequest(0, "goal", "parent")
    )

    assert len(result.proposals) == 1
    assert result.proposals[0].instruction == "fixed"
    assert not hasattr(result.proposals[0], "research_target")
