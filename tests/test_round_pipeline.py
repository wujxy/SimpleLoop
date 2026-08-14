from __future__ import annotations

from simpleloop.candidate import (
    CandidateArtifact, CandidateBatchResult, CandidateResult, CandidateStatus,
    EvaluationResult, ExecutionResult, GateDecision,
)
from simpleloop.round import RoundRequest, SelectionPolicy, run_round
from simpleloop.stages.proposer import Abstention, Proposal, ProposalBatch


def _candidate(candidate_id: int, value: float) -> CandidateResult:
    return CandidateResult(
        candidate_id, f"r2c{candidate_id}", Proposal(f"p{candidate_id}"),
        "parent", CandidateStatus.COMPLETED, ExecutionResult("COMMITTED"),
        CandidateArtifact("parent", f"sha-{candidate_id}"),
        EvaluationResult("", {"OBJ": value}), GateDecision({}, True, True),
    )


def _request(*, improve: bool = True) -> RoundRequest:
    return RoundRequest(
        2, "goal", "parent", {"OBJ": 100.0},
        SelectionPolicy("OBJ", True, improve),
    )


class FixedProposer:
    def __init__(self, batch):
        self.batch = batch
        self.requests = []

    def propose(self, request):
        self.requests.append(request)
        return self.batch


class FakeCandidates:
    def __init__(self, results, events=None):
        self.results = tuple(results)
        self.requests = []
        self.events = events

    def run(self, request):
        self.requests.append(request)
        if self.events is not None:
            self.events.append("candidates")
        return CandidateBatchResult(self.results, {"seconds": 3})


class Recorder:
    def __init__(self, events=None):
        self.records = []
        self.events = events

    def record_proposals(self, request, proposals):
        self.records.append((request, proposals))
        if self.events is not None:
            self.events.append("record")


def test_round_fans_out_proposals_and_selects_best_candidate():
    proposer = FixedProposer(ProposalBatch((Proposal("a"), Proposal("b"))))
    candidates = FakeCandidates((_candidate(0, 90), _candidate(1, 80)))
    recorder = Recorder()

    result = run_round(
        _request(), proposer=proposer, candidates=candidates, recorder=recorder,
    )

    plans = candidates.requests[0].candidates
    assert [plan.proposal.instruction for plan in plans] == ["a", "b"]
    assert all(plan.parent_sha == "parent" for plan in plans)
    assert result.selection.candidate_id == 1
    assert result.next_sha == "sha-1"
    assert result.telemetry == {"seconds": 3}


def test_round_records_proposals_before_candidate_launch():
    events = []
    run_round(
        _request(),
        proposer=FixedProposer(ProposalBatch((Proposal("a"),))),
        candidates=FakeCandidates((_candidate(0, 90),), events),
        recorder=Recorder(events),
    )
    assert events == ["record", "candidates"]


def test_round_abstention_records_and_skips_candidates():
    abstention = Abstention("nothing worth trying")
    candidates = FakeCandidates(())
    recorder = Recorder()

    result = run_round(
        _request(),
        proposer=FixedProposer(ProposalBatch((), abstention)),
        candidates=candidates,
        recorder=recorder,
    )

    assert candidates.requests == []
    assert recorder.records[0][1].abstention == abstention
    assert result.candidates == ()
    assert result.selection.reason == "no_eligible_candidate"


def test_round_static_policy_accepts_non_improving_candidate():
    result = run_round(
        _request(improve=False),
        proposer=FixedProposer(ProposalBatch((Proposal("controlled"),))),
        candidates=FakeCandidates((_candidate(0, 110),)),
        recorder=Recorder(),
    )
    assert result.selection.candidate_id == 0
