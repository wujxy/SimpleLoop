from __future__ import annotations

import pytest

from simpleloop.candidate import (
    CandidateArtifact, CandidateResult, CandidateStatus, EvaluationResult,
    ExecutionResult, GateDecision,
)
from simpleloop.loop import (
    LoopRequest, LoopState, RsiResult, run_loop,
)
from simpleloop.round import RoundResult, SelectionPolicy
from simpleloop.scheduling.contracts import InfrastructureError
from simpleloop.stages.proposer import Proposal, ProposalBatch
from simpleloop.stages.selector import Selection


POLICY = SelectionPolicy("OBJ", True)


def _round(round_id=0, *, winner=True):
    candidate = CandidateResult(
        0, f"r{round_id}c0", Proposal("p"), "base",
        CandidateStatus.COMPLETED, ExecutionResult("COMMITTED"),
        CandidateArtifact("base", "winner"),
        EvaluationResult("", {"OBJ": 80.0}), GateDecision({}, True, True),
    )
    return RoundResult(
        round_id, "base", ProposalBatch((Proposal("p"),)), (candidate,),
        Selection(0, "winner", "selected") if winner else
        Selection(None, None, "no_improvement"),
    )


class Rounds:
    def __init__(self, events, *, failure=None, winner=True):
        self.events = events
        self.failure = failure
        self.winner = winner
        self.requests = []

    def run(self, request):
        self.events.append("round")
        self.requests.append(request)
        if self.failure:
            raise self.failure
        return _round(request.round_id, winner=self.winner)


class NeverRsi:
    def due(self, round_id):
        return False

    def run(self, round_id):
        raise AssertionError("RSI must not run")


class OneRsi:
    def __init__(self):
        self.calls = []

    def due(self, round_id):
        return round_id == 0

    def run(self, round_id):
        self.calls.append(round_id)
        return RsiResult(round_id, "KEEP")


class History:
    def __init__(self, events):
        self.events = events

    def append_round(self, result):
        self.events.append("history")


class Checkpoint:
    def __init__(self, events):
        self.events = events
        self.calls = 0

    def clear(self):
        self.calls += 1
        self.events.append("clear")


class Observer:
    def __init__(self, events):
        self.events = events

    def round_committed(self, result):
        self.events.append("observe")


def _request(stop=1):
    return LoopRequest(
        "goal", stop, LoopState(0, "base", {"OBJ": 100.0}), POLICY,
    )


def test_loop_commits_history_before_clearing_checkpoint():
    events = []
    result = run_loop(
        _request(), rounds=Rounds(events), rsi=NeverRsi(),
        history=History(events), checkpoint=Checkpoint(events),
        observer=Observer(events),
    )

    assert events == ["round", "history", "clear", "observe"]
    assert result.state == LoopState(1, "winner", {"OBJ": 80.0})
    assert result.task_rounds == 1


def test_loop_rsi_branch_consumes_round_without_task_history():
    events = []
    rsi = OneRsi()
    result = run_loop(
        _request(stop=2), rounds=Rounds(events), rsi=rsi,
        history=History(events), checkpoint=Checkpoint(events),
        observer=Observer(events),
    )

    assert rsi.calls == [0]
    assert events == ["round", "history", "clear", "observe"]
    assert result.rsi_tally == {"KEEP": 1}
    assert result.state.next_round == 2


def test_loop_notifies_full_observer_of_round_lifecycle():
    events = []

    class FullObserver:
        def round_started(self, round_id, *, rsi):
            events.append(("start", round_id, rsi))

        def round_committed(self, result):
            events.append(("commit", result.round_id))

        def rsi_finished(self, result):
            events.append(("rsi", result.round_id, result.decision))

    rsi = OneRsi()
    run_loop(
        _request(stop=2), rounds=Rounds(events), rsi=rsi,
        history=History(events), checkpoint=Checkpoint(events),
        observer=FullObserver(),
    )

    # Round 0 is an RSI round (banner, decision), round 1 a task round
    # (banner, commit) — the observer sees every stage transition.
    assert [event for event in events if isinstance(event, tuple)] == [
        ("start", 0, True), ("rsi", 0, "KEEP"),
        ("start", 1, False), ("commit", 1),
    ]


def test_loop_no_winner_retains_incumbent_metrics():
    events = []
    result = run_loop(
        _request(), rounds=Rounds(events, winner=False), rsi=NeverRsi(),
        history=History(events), checkpoint=Checkpoint(events),
        observer=Observer(events),
    )
    assert result.state.incumbent_sha == "base"
    assert result.state.incumbent_metrics == {"OBJ": 100.0}


def test_loop_infrastructure_failure_does_not_consume_or_clear_round():
    events = []
    checkpoint = Checkpoint(events)
    result = run_loop(
        _request(),
        rounds=Rounds(events, failure=InfrastructureError("node lost")),
        rsi=NeverRsi(), history=History(events), checkpoint=checkpoint,
        observer=Observer(events),
    )

    assert result.interrupted is True
    assert result.interruption == "node lost"
    assert result.state.next_round == 0
    assert checkpoint.calls == 0


def test_loop_does_not_swallow_protocol_or_programmer_errors():
    events = []
    with pytest.raises(ValueError, match="broken"):
        run_loop(
            _request(), rounds=Rounds(events, failure=ValueError("broken")),
            rsi=NeverRsi(), history=History(events),
            checkpoint=Checkpoint(events), observer=Observer(events),
        )
