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
from simpleloop.execution.proposer_lanes import (
    read_lane_result,
    read_self_review_result,
)
from simpleloop.harness.store import Store
from simpleloop.loop import _InflightJournal, _load_inflight
from simpleloop.round import RoundResult
from simpleloop.stages.gate import GateSpec
from simpleloop.stages.proposer import Abstention, Proposal, ProposalBatch
from simpleloop.stages.selector import Selection


FIXTURES = Path(__file__).parent / "fixtures" / "phase0"
SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "CORRECTNESS"}],
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _write_result(tmp_path: Path, name: str) -> Path:
    result_dir = tmp_path / name.removesuffix(".json")
    result_dir.mkdir()
    (result_dir / "result.json").write_text(
        json.dumps(load(name), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result_dir


def test_candidate_result_shape(tmp_path: Path):
    class Executor:
        def execute(self, request):
            return ExecutionResult("EXECUTED")

    class Artifacts:
        def inspect(self, worktree):
            return (Path("src/cache.cc"),)

        def commit(self, request):
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
        worktree=tmp_path / "worktree",
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
    result_dir = _write_result(tmp_path, "proposer-lane-result.json")
    assert read_lane_result(result_dir) == load("proposer-lane-result.json")


def test_self_review_result_shape(tmp_path: Path):
    result_dir = _write_result(tmp_path, "self-review-result.json")
    assert read_self_review_result(result_dir) == load("self-review-result.json")


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


def test_inflight_round_shape(tmp_path: Path):
    expected = load("inflight-round.json")
    journal = _InflightJournal(
        tmp_path / "inflight_round.json",
        meta={key: value for key, value in expected.items() if key != "jobs"},
    )
    journal.save(expected["jobs"])

    assert _load_inflight(tmp_path) == expected
