from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.candidate import (
    CandidateArtifact,
    CandidateRequest,
    CandidateStatus,
    CommitRequest,
    EvaluationResult,
    ExecutionResult,
)
from simpleloop.persistence.candidate_trace import HandoffCandidateTrace
from simpleloop.stages import evaluator as evaluator_mod
from simpleloop.stages.artifacts import GitArtifactWorkspace
from simpleloop.stages.evaluator import (
    BaselineAcceptanceError,
    EvaluationConfig,
    EvaluationRequest,
    HarnessEvaluator,
    validate_baseline,
)
from simpleloop.stages.executor import (
    AgentExecutor,
    ExecutionRequest,
    ExecutorConfig,
    parse_self_report,
)
from simpleloop.stages.gate import GateSpec, apply_gates
from simpleloop.stages.proposer import Proposal
from simpleloop.roles.agent import AgentError


class FakeAgent:
    def __init__(self, output: str = "done"):
        self.output = output
        self.cwd: Path | None = None
        self.label: str | None = None
        self.prompt = ""

    def run_text(self, prompt: str, *, cwd: Path, label: str) -> str:
        self.prompt = prompt
        self.cwd = cwd
        self.label = label
        return self.output


class FakeWorkspace:
    def __init__(self, *, changed: list[str], sha: str):
        self.changed = changed
        self.sha = sha
        self.commit_args = None

    def changed_paths(self, worktree: Path) -> list[str]:
        return list(self.changed)

    def commit(self, worktree: Path, round_id: str, paths: list[str]) -> str:
        self.commit_args = (worktree, round_id, paths)
        return self.sha


def test_agent_executor_only_returns_agent_facts(tmp_path: Path):
    agent = FakeAgent(
        'done\n```json\n{"outcome":"completed","summary":"ok"}\n```'
    )

    result = AgentExecutor(agent, ExecutorConfig("goal")).execute(
        ExecutionRequest(2, 3, Proposal("cache it"), tmp_path)
    )

    assert result.status == "EXECUTED"
    assert result.self_report["summary"] == "ok"
    assert agent.cwd == tmp_path
    assert agent.label == "executor r2-c3"
    assert "cache it" in agent.prompt
    assert "goal" in agent.prompt


def test_agent_executor_normalizes_agent_error(tmp_path: Path):
    class FailingAgent(FakeAgent):
        def run_text(self, prompt: str, *, cwd: Path, label: str) -> str:
            raise AgentError("model unavailable")

    result = AgentExecutor(FailingAgent(), ExecutorConfig("goal")).execute(
        ExecutionRequest(2, 3, Proposal("cache it"), tmp_path)
    )

    assert result.status == "EXECUTOR_FAILED"
    assert result.reason == "model unavailable"


def test_parse_self_report_remains_best_effort():
    assert parse_self_report(
        '```json\n{"outcome":"blocked","blocked_reason_kind":"objective",'
        '"summary":"target absent"}\n```'
    ) == {
        "outcome": "blocked",
        "blocked_reason_kind": "objective",
        "summary": "target absent",
    }
    assert parse_self_report("prose only") is None


def test_git_artifact_workspace_maps_commit_request(tmp_path: Path):
    workspace = FakeWorkspace(changed=["src/a.cc"], sha="child")
    adapter = GitArtifactWorkspace(workspace)

    paths = adapter.inspect(tmp_path)
    artifact = adapter.commit(
        CommitRequest(2, 3, "parent", tmp_path, paths)
    )

    assert artifact == CandidateArtifact("parent", "child", paths)
    assert workspace.commit_args == (
        tmp_path,
        "2-c3",
        ["src/a.cc"],
    )


def test_harness_evaluator_maps_eval_result(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        evaluator_mod.evals,
        "run_eval",
        lambda *args, **kwargs: evaluator_mod.evals.EvalResult(
            "eval", {"SPEED_MS": 90.0, "CORRECTNESS": True}, (0,),
        ),
    )
    evaluator = HarnessEvaluator(
        object(),
        EvaluationConfig(("eval",), "SPEED_MS", ("CORRECTNESS",)),
    )

    result = evaluator.evaluate(EvaluationRequest(tmp_path))

    assert result == EvaluationResult(
        "eval", {"SPEED_MS": 90.0, "CORRECTNESS": True}, (0,),
    )


def test_harness_evaluator_normalizes_runtime_failure(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setattr(
        evaluator_mod.evals,
        "run_eval",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("container unavailable")
        ),
    )
    evaluator = HarnessEvaluator(
        object(), EvaluationConfig(("eval",), "SPEED_MS", ()),
    )

    result = evaluator.evaluate(EvaluationRequest(tmp_path))

    assert result.error == "container unavailable"
    assert "eval failed to run" in result.text


def test_validate_baseline_rejects_missing_objective():
    with pytest.raises(BaselineAcceptanceError, match="missing or not finite"):
        validate_baseline(
            EvaluationResult("bad", {"CORRECTNESS": True}, (0,)),
            GateSpec("SPEED_MS", ("CORRECTNESS",)),
        )


def test_handoff_trace_writes_typed_stage_facts(tmp_path: Path):
    trace = HandoffCandidateTrace(tmp_path)
    request = CandidateRequest(
        2, 3, "parent", Proposal("cache it"), tmp_path / "worktree",
    )
    execution = ExecutionResult(
        "EXECUTED", output="agent response",
        self_report={"outcome": "completed"},
    )
    artifact = CandidateArtifact("parent", "child")
    evaluation = EvaluationResult(
        "eval", {"SPEED_MS": 90.0, "CORRECTNESS": True}, (0,),
    )
    gate = apply_gates(
        evaluation, GateSpec("SPEED_MS", ("CORRECTNESS",)),
    )

    trace.record_execution(request, execution, artifact)
    trace.record_evaluation(
        request, evaluation, gate, CandidateStatus.COMPLETED,
    )

    directory = tmp_path / "handoffs" / "r2"
    executor_row = json.loads(
        (directory / "2-c3.executor.json").read_text(encoding="utf-8")
    )
    eval_row = json.loads(
        (directory / "2-c3.eval.json").read_text(encoding="utf-8")
    )
    assert executor_row["sha"] == "child"
    assert executor_row["executor_response"] == "agent response"
    assert eval_row["status"] == "COMPLETED"
    assert eval_row["gate_passed"] is True
