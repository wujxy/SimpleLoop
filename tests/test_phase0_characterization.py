"""Golden tests for the durable protocol shapes at the Phase 0 boundary."""
from __future__ import annotations

import json
from pathlib import Path

from simpleloop.candidate import (
    CandidateArtifact,
    CandidateRequest,
    EvaluationResult,
    ExecutionResult,
    run_candidate,
)
from simpleloop.persistence.artifacts import (
    decode_candidate_result,
    encode_candidate_result,
)
from simpleloop.persistence.history import Store
from simpleloop.persistence.journal import JobJournal
from simpleloop.round import RoundResult
from simpleloop.stages.gate import GateSpec
from simpleloop.world import SourceWorkspace
from simpleloop.stages.proposer import Abstention, Proposal, ProposalBatch
from simpleloop.stages.selector import Selection


FIXTURES = Path(__file__).parent / "fixtures" / "phase0"
SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "CORRECTNESS"}],
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_candidate_result_shape(tmp_path: Path):
    class Executor:
        def execute(self, request):
            return ExecutionResult("EXECUTED")

    class Artifacts:
        def inspect(self, worktree):
            return (Path("src/cache.cc"),)

        def commit(self, workspace, request):
            return CandidateArtifact(
                request.parent_sha, "child", request.changed_paths,
            )

    class Evaluator:
        def evaluate(self, request):
            return EvaluationResult(
            "SPEED_MS=90\nCORRECTNESS=PASS",
            {"SPEED_MS": 90.0, "CORRECTNESS": True},
            (0,),
            )

    class Trace:
        def record_execution(self, *args):
            pass

        def record_evaluation(self, *args):
            pass

    request = CandidateRequest(
        round_id=2,
        candidate_id=1,
        parent_sha="parent",
        proposal=Proposal("cache the transform"),
        workspace=SourceWorkspace(
            "2-c1", tmp_path / "worktree", "parent",
        ),
    )

    assert encode_candidate_result(run_candidate(
        request,
        executor=Executor(),
        artifacts=Artifacts(),
        evaluator=Evaluator(),
        gate_spec=GateSpec("SPEED_MS", ("CORRECTNESS",)),
        trace=Trace(),
    )) == load(
        "candidate-result.json"
    )


def test_proposer_lane_result_shape(tmp_path: Path):
    result = load("proposer-lane-result.json")
    assert result["status"] == "COMPLETED"
    assert isinstance(result["proposals"], list)
    assert result["lane_id"] == 0


def test_self_review_result_shape(tmp_path: Path):
    result = load("self-review-result.json")
    assert result["status"] == "COMPLETED"
    assert result["mode"] == "self"
    assert isinstance(result["self_review"], dict)


def test_history_round_shape(tmp_path: Path):
    store = Store(tmp_path, metrics_schema=SCHEMA)
    candidate = decode_candidate_result(load("candidate-result.json"))
    store.append_round(RoundResult(
        round_id=2,
        parent_sha="parent",
        proposals=ProposalBatch((candidate.proposal,)),
        candidates=(candidate,),
        selection=Selection(1, "child", "selected"),
    ))

    assert store.history() == [load("history-round.json")]


def test_abstained_round_projection(tmp_path: Path):
    store = Store(tmp_path, metrics_schema=SCHEMA)
    store.append_round(RoundResult(
        round_id=3,
        parent_sha="parent",
        proposals=ProposalBatch(
            (),
            Abstention("no useful experiment", "missing profile"),
            telemetry={"steps": 2},
        ),
        candidates=(),
        selection=Selection(None, None, "no_eligible_candidate"),
        telemetry={"worktime_seconds": 1.0},
    ))

    assert store.history() == [{
        "round": 3,
        "parent_sha": "parent",
        "selected_candidate": None,
        "selected_sha": None,
        "proposal": "",
        "metrics": {},
        "changed_paths": [],
        "base_sha": "parent",
        "candidates": [],
        "telemetry": {"worktime_seconds": 1.0},
        "abstention": {
            "reason": "no useful experiment",
            "blocking_unknown": "missing profile",
        },
        "deliberation_telemetry": {"steps": 2},
    }]


def test_inflight_journal_shape(tmp_path: Path):
    expected = load("inflight-round.json")
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin(
        expected["stage"], expected["round_id"],
        expected["context"], expected["jobs"],
    )

    record = journal.load()
    assert record is not None
    assert {
        "schema": "simpleloop.inflight.v1",
        "round_id": record.round_id,
        "stage": record.stage,
        "context": record.context,
        "jobs": list(record.jobs),
    } == expected
