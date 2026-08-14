from __future__ import annotations

from pathlib import Path, PurePosixPath

from simpleloop.candidate import (
    CandidateArtifact,
    CandidateRequest,
    CandidateStatus,
    EvaluationResult,
    ExecutionResult,
    candidate_failure_from_request,
    run_candidate,
    run_candidate_guarded,
)
from simpleloop.stages.gate import GateSpec
from simpleloop.stages.proposer import Proposal
from simpleloop.world import SourceWorkspace


SPEC = GateSpec("SPEED_MS", ("CORRECTNESS",))


class FakeExecutor:
    def __init__(self, calls: list[str], result: ExecutionResult):
        self.calls = calls
        self.result = result

    def execute(self, request):
        self.calls.append("execute")
        return self.result


class ExplodingExecutor:
    def execute(self, request):
        raise RuntimeError("agent adapter exploded")


class FakeArtifacts:
    def __init__(
        self,
        calls: list[str],
        paths: tuple[PurePosixPath, ...] = (PurePosixPath("src/a.cc"),),
    ):
        self.calls = calls
        self.paths = paths

    def inspect(self, workspace):
        self.calls.append("inspect")
        return self.paths

    def commit(self, workspace, request):
        self.calls.append("commit")
        return CandidateArtifact(
            request.parent_sha,
            "child",
            request.changed_paths,
        )


class FakeEvaluator:
    def __init__(self, calls: list[str], result: EvaluationResult):
        self.calls = calls
        self.result = result

    def evaluate(self, request):
        self.calls.append("evaluate")
        return self.result


class FakeTrace:
    def __init__(self, calls: list[str]):
        self.calls = calls

    def record_execution(self, request, execution, artifact):
        self.calls.append("execution-trace")

    def record_evaluation(self, request, evaluation, gate, status):
        self.calls.append("evaluation-trace")


def request(tmp_path: Path) -> CandidateRequest:
    return CandidateRequest(
        2, 3, "parent", Proposal("cache it"),
        SourceWorkspace("2-c3", tmp_path, "parent"),
    )


def completed_evaluation() -> EvaluationResult:
    return EvaluationResult(
        "eval",
        {"SPEED_MS": 90.0, "CORRECTNESS": True},
        (0,),
    )


def test_run_candidate_completed_in_explicit_stage_order(tmp_path: Path):
    calls: list[str] = []

    result = run_candidate(
        request(tmp_path),
        executor=FakeExecutor(calls, ExecutionResult("EXECUTED")),
        artifacts=FakeArtifacts(calls),
        evaluator=FakeEvaluator(calls, completed_evaluation()),
        gate_spec=SPEC,
        trace=FakeTrace(calls),
    )

    assert result.status is CandidateStatus.COMPLETED
    assert result.sha == "child"
    assert result.eligible is True
    assert calls == [
        "execute",
        "inspect",
        "commit",
        "execution-trace",
        "evaluate",
        "evaluation-trace",
    ]


def test_executor_failure_stops_before_git(tmp_path: Path):
    calls: list[str] = []

    result = run_candidate(
        request(tmp_path),
        executor=FakeExecutor(
            calls,
            ExecutionResult("EXECUTOR_FAILED", reason="model unavailable"),
        ),
        artifacts=FakeArtifacts(calls),
        evaluator=FakeEvaluator(calls, completed_evaluation()),
        gate_spec=SPEC,
        trace=FakeTrace(calls),
    )

    assert result.status is CandidateStatus.EXECUTOR_FAILED
    assert result.execution.reason == "model unavailable"
    assert result.sha is None
    assert result.gate.results["PATHS"].passed is None
    assert calls == ["execute", "execution-trace"]


def test_no_change_skips_commit_and_evaluation(tmp_path: Path):
    calls: list[str] = []

    result = run_candidate(
        request(tmp_path),
        executor=FakeExecutor(calls, ExecutionResult("EXECUTED")),
        artifacts=FakeArtifacts(calls, paths=()),
        evaluator=FakeEvaluator(calls, completed_evaluation()),
        gate_spec=SPEC,
        trace=FakeTrace(calls),
    )

    assert result.status is CandidateStatus.NO_CHANGE
    assert result.gate.results["EVAL_COMMANDS"].detail == (
        "not run because Executor produced no change"
    )
    assert calls == ["execute", "inspect", "execution-trace"]


def test_eval_error_retains_committed_artifact(tmp_path: Path):
    calls: list[str] = []

    result = run_candidate(
        request(tmp_path),
        executor=FakeExecutor(calls, ExecutionResult("EXECUTED")),
        artifacts=FakeArtifacts(calls),
        evaluator=FakeEvaluator(
            calls,
            EvaluationResult(
                "(eval failed to run: offline)", error="offline",
            ),
        ),
        gate_spec=SPEC,
        trace=FakeTrace(calls),
    )

    assert result.status is CandidateStatus.EVAL_FAILED
    assert result.sha == "child"
    assert result.gate.results["EVAL_COMMANDS"].passed is False
    assert result.evaluation.error == "offline"


def test_nonzero_command_is_gate_rejection(tmp_path: Path):
    calls: list[str] = []

    result = run_candidate(
        request(tmp_path),
        executor=FakeExecutor(calls, ExecutionResult("EXECUTED")),
        artifacts=FakeArtifacts(calls),
        evaluator=FakeEvaluator(
            calls,
            EvaluationResult(
                "failed",
                {"SPEED_MS": 90.0, "CORRECTNESS": True},
                (7,),
            ),
        ),
        gate_spec=SPEC,
        trace=FakeTrace(calls),
    )

    assert result.status is CandidateStatus.GATE_REJECTED
    assert result.gate.results["EVAL_COMMANDS"].passed is False
    assert result.eligible is False


def test_hard_gate_failure_is_gate_rejection(tmp_path: Path):
    calls: list[str] = []

    result = run_candidate(
        request(tmp_path),
        executor=FakeExecutor(calls, ExecutionResult("EXECUTED")),
        artifacts=FakeArtifacts(calls),
        evaluator=FakeEvaluator(
            calls,
            EvaluationResult(
                "gate failed",
                {"SPEED_MS": 90.0, "CORRECTNESS": False},
                (0,),
            ),
        ),
        gate_spec=SPEC,
        trace=FakeTrace(calls),
    )

    assert result.status is CandidateStatus.GATE_REJECTED
    assert result.gate.results["CORRECTNESS"].passed is False


def test_guarded_pipeline_normalizes_unexpected_failure(tmp_path: Path):
    calls: list[str] = []

    result = run_candidate_guarded(
        request(tmp_path),
        executor=ExplodingExecutor(),
        artifacts=FakeArtifacts(calls),
        evaluator=FakeEvaluator(calls, completed_evaluation()),
        gate_spec=SPEC,
        trace=FakeTrace(calls),
    )

    assert result.status is CandidateStatus.WORKER_FAILED
    assert result.parent_sha == "parent"
    assert result.proposal.instruction == "cache it"
    assert result.gate.results["PATHS"].passed is None
    assert "agent adapter exploded" in result.execution.reason


def test_failure_projection_uses_configured_gate_shape(tmp_path: Path):
    result = candidate_failure_from_request(
        request(tmp_path),
        "worker failed before execution",
        gate_spec=SPEC,
    )

    assert tuple(result.gate.results) == (
        "PATHS", "EVAL_COMMANDS", "CORRECTNESS",
    )
