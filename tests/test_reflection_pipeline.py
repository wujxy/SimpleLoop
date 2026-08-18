"""Tests for the host-side Reflection scheduling (workstream B2/B3).

The reflection log is the only durable state; scheduling is derived from it;
the loop's third branch consumes a round id without touching the incumbent or
task history.
"""
from __future__ import annotations

import json

import pytest

from simpleloop.loop import LoopRequest, LoopState, run_loop
from simpleloop.reflection import (
    JsonlReflectionLog,
    ReflectionPipeline,
    ReflectionRecord,
    next_reflection_round,
)
from simpleloop.round import RoundResult, SelectionPolicy

POLICY = SelectionPolicy("OBJ", True)


# ---------------- log ----------------

def test_log_append_read_and_last_round(tmp_path):
    log = JsonlReflectionLog(tmp_path / "reflection")
    assert log.records() == ()
    assert log.last_round() is None
    log.append(ReflectionRecord(4, "stop anchoring", True, False, "ctx"))
    log.append(ReflectionRecord(12, "second", False, True, ""))
    assert log.last_round() == 12
    assert log.has_round(4) and not log.has_round(5)
    records = log.records()
    assert records[0].handoff == "stop anchoring"
    assert records[0].self_limitation_suspected is True
    assert records[1].abstained is True


def test_log_tolerates_torn_lines(tmp_path):
    path = tmp_path / "reflection" / "history.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"round_id": 2, "handoff": "ok"}) + "\n"
        + "{torn line\n"
        + "\n"
        + "not json at all\n"
    )
    log = JsonlReflectionLog(tmp_path / "reflection")
    assert log.last_round() == 2


def test_next_reflection_round_derived_from_log(tmp_path):
    log = JsonlReflectionLog(tmp_path / "reflection")
    assert next_reflection_round(
        log=log, interval_rounds=8, first_reflection_round=8) == 8
    log.append(ReflectionRecord(8, "first"))
    assert next_reflection_round(
        log=log, interval_rounds=8, first_reflection_round=8) == 16
    log.append(ReflectionRecord(16, "second"))
    assert next_reflection_round(
        log=log, interval_rounds=8, first_reflection_round=8) == 24


# ---------------- pipeline ----------------

class _Workspace:
    def __init__(self, sha="baseline-sha"):
        self._sha = sha

    def baseline_sha(self) -> str:
        return self._sha


class _Reflector:
    def __init__(self):
        self.calls = []

    def reflect(self, round_id, incumbent_sha):
        self.calls.append((round_id, incumbent_sha))
        return ReflectionRecord(round_id, f"handoff-{round_id}")


class _Checkpoint:
    def __init__(self, record=None):
        self.record = record
        self.cleared = 0

    def inflight(self):
        return self.record

    def clear(self):
        self.cleared += 1
        self.record = None


class _Inflight:
    def __init__(self, stage, round_id):
        self.stage = stage
        self.round_id = round_id


def _pipeline(tmp_path, checkpoint=None):
    log = JsonlReflectionLog(tmp_path / "reflection")
    return ReflectionPipeline(
        run_dir=tmp_path, workspace=_Workspace(),
        reflector=_Reflector(), log=log,
        checkpoint=checkpoint or _Checkpoint(),
        interval_rounds=8, first_reflection_round=8,
    )


def test_pipeline_due_schedule(tmp_path):
    pipeline = _pipeline(tmp_path)
    assert pipeline.due(0) is False
    assert pipeline.due(7) is False
    assert pipeline.due(8) is True
    pipeline.run(8)
    assert pipeline.due(9) is False
    assert pipeline.due(15) is False
    assert pipeline.due(16) is True


def test_pipeline_run_appends_and_is_idempotent(tmp_path):
    pipeline = _pipeline(tmp_path)
    record = pipeline.run(8)
    assert record.handoff == "handoff-8"
    assert pipeline.log.last_round() == 8
    # a retried round already logged returns the record without reflecting
    again = pipeline.run(8)
    assert again.round_id == 8
    assert len(pipeline.reflector.calls) == 1


def test_pipeline_incumbent_from_history_else_baseline(tmp_path):
    pipeline = _pipeline(tmp_path)
    assert pipeline.incumbent_sha() == "baseline-sha"
    (tmp_path / "history.jsonl").write_text(
        json.dumps({"round": 0, "selected_sha": "sha-a"}) + "\n"
        + json.dumps({"round": 1, "selected_sha": None}) + "\n"
        + json.dumps({"round": 2, "selected_sha": "sha-b"}) + "\n"
    )
    assert pipeline.incumbent_sha() == "sha-b"


def test_pipeline_prepare_clears_logged_inflight_only(tmp_path):
    # inflight for a round already logged -> cleared
    checkpoint = _Checkpoint(_Inflight("reflection", 8))
    pipeline = _pipeline(tmp_path, checkpoint)
    pipeline.log.append(ReflectionRecord(8, "done"))
    pipeline.prepare()
    assert checkpoint.cleared == 1
    # inflight for a round NOT yet logged -> kept for the reflector to reuse
    checkpoint2 = _Checkpoint(_Inflight("reflection", 9))
    pipeline2 = _pipeline(tmp_path, checkpoint2)
    pipeline2.prepare()
    assert checkpoint2.cleared == 0


# ---------------- loop third branch ----------------

def _task_round(round_id):
    from simpleloop.candidate import (
        CandidateArtifact, CandidateResult, CandidateStatus, EvaluationResult,
        ExecutionResult, GateDecision,
    )
    from simpleloop.stages.proposer import Proposal, ProposalBatch
    from simpleloop.stages.selector import Selection
    candidate = CandidateResult(
        0, f"r{round_id}c0", Proposal("p"), "base",
        CandidateStatus.COMPLETED, ExecutionResult("COMMITTED"),
        CandidateArtifact("base", "winner"),
        EvaluationResult("", {"OBJ": 80.0}), GateDecision({}, True, True),
    )
    return RoundResult(
        round_id, "base", ProposalBatch((Proposal("p"),)), (candidate,),
        Selection(0, "winner", "selected"),
    )


def test_loop_reflection_branch_consumes_round_without_task_history():
    events: list[str] = []
    appended: list = []

    class Rounds:
        def run(self, request):
            events.append("round")
            appended.append(_task_round(request.round_id))
            return appended[-1]

    class NeverRsi:
        def due(self, round_id):
            return False

        def run(self, round_id):
            raise AssertionError("RSI must not run")

    class OneReflection:
        def __init__(self):
            self.calls = []

        def due(self, round_id):
            return round_id == 0

        def run(self, round_id):
            self.calls.append(round_id)
            return round_id

    class History:
        def append_round(self, result):
            events.append("history")

    class Checkpoint:
        def clear(self):
            events.append("clear")

    class Observer:
        def round_committed(self, result):
            events.append("observe")

    reflection = OneReflection()
    result = run_loop(
        LoopRequest("goal", 2, LoopState(0, "base", {"OBJ": 100.0}), POLICY),
        rounds=Rounds(), rsi=NeverRsi(),
        history=History(), checkpoint=Checkpoint(), observer=Observer(),
        reflection=reflection,
    )
    assert reflection.calls == [0]
    # only ONE task round ran (r1); r0 was consumed by reflection and never
    # committed to task history
    assert events.count("round") == 1
    assert appended[0].round_id == 1
    assert result.reflection_rounds == 1
    assert result.task_rounds == 1
    assert result.state.next_round == 2
    # the incumbent moved only via the task round's selection ("winner"),
    # never via the reflection round
    assert result.state.incumbent_sha == "winner"


def test_loop_without_reflection_runner_is_unchanged():
    events: list[str] = []

    class Rounds:
        def run(self, request):
            events.append("round")
            return _task_round(request.round_id)

    class NeverRsi:
        def due(self, round_id):
            return False

        def run(self, round_id):
            raise AssertionError("RSI must not run")

    class History:
        def append_round(self, result):
            events.append("history")

    class Checkpoint:
        def clear(self):
            events.append("clear")

    class Observer:
        def round_committed(self, result):
            events.append("observe")

    result = run_loop(
        LoopRequest("goal", 1, LoopState(0, "base", {"OBJ": 100.0}), POLICY),
        rounds=Rounds(), rsi=NeverRsi(),
        history=History(), checkpoint=Checkpoint(), observer=Observer(),
    )
    assert result.reflection_rounds == 0
    assert events == ["round", "history", "clear", "observe"]


def test_loop_notifies_reflection_observer_hooks():
    seen: list[tuple] = []

    class Rounds:
        def run(self, request):
            return _task_round(request.round_id)

    class NeverRsi:
        def due(self, round_id):
            return False

        def run(self, round_id):
            raise AssertionError("RSI must not run")

    class History:
        def append_round(self, result):
            pass

    class Checkpoint:
        def clear(self):
            pass

    class Observer:
        def reflection_started(self, round_id):
            seen.append(("started", round_id))

        def reflection_finished(self, round_id):
            seen.append(("finished", round_id))

    class EveryReflection:
        def due(self, round_id):
            return True

        def run(self, round_id):
            return round_id

    run_loop(
        LoopRequest("goal", 1, LoopState(0, "base", {"OBJ": 100.0}), POLICY),
        rounds=Rounds(), rsi=NeverRsi(),
        history=History(), checkpoint=Checkpoint(), observer=Observer(),
        reflection=EveryReflection(),
    )
    assert seen == [("started", 0), ("finished", 0)]



# ---------------- infra failures fail fast, consume no round id -------------

def test_loop_reflection_failure_interrupts_without_consuming_round():
    from simpleloop.scheduling.contracts import InfrastructureError

    task_rounds: list = []
    clears: list = []

    class Rounds:
        def run(self, request):
            task_rounds.append(request.round_id)
            return _task_round(request.round_id)

    class NeverRsi:
        def due(self, round_id):
            return False

        def run(self, round_id):
            raise AssertionError("RSI must not run")

    class FailingReflection:
        def due(self, round_id):
            return True

        def run(self, round_id):
            raise InfrastructureError(
                "action protocol failed after 2 repairs; last reply: 'x'")

    class History:
        def append_round(self, result):
            pass

    class Checkpoint:
        def clear(self):
            # The failed reflection's journaled stage must be dropped, or
            # the next launch's first batch refuses to start
            # ("persisted batch does not match requested stage/round").
            clears.append(1)

    class Observer:
        def round_committed(self, result):
            pass

        def reflection_started(self, round_id):
            pass

    result = run_loop(
        LoopRequest("goal", 9, LoopState(0, "base", {"OBJ": 100.0}), POLICY),
        rounds=Rounds(), rsi=NeverRsi(),
        history=History(), checkpoint=Checkpoint(), observer=Observer(),
        reflection=FailingReflection(),
    )
    # infrastructure, not research: the run stops, no round id is consumed
    # (a resume retries round 0), no history row was committed, and the
    # stale journal was cleared for a clean restart
    assert result.interrupted
    assert "action protocol failed" in (result.interruption or "")
    assert task_rounds == []
    assert result.state.next_round == 0
    assert clears == [1]


def test_loop_stops_after_two_consecutive_all_dead_rounds():
    """A round whose candidates all died before evaluation consumes its
    round id (execution cost is real) — but two in a row stop the run."""
    from simpleloop.candidate import (
        CandidateArtifact, CandidateResult, CandidateStatus, EvaluationResult,
        ExecutionResult, GateDecision,
    )
    from simpleloop.stages.proposer import Proposal, ProposalBatch
    from simpleloop.stages.selector import Selection

    def _dead_round(round_id):
        dead = CandidateResult(
            0, f"r{round_id}c0", Proposal("p"), "base",
            CandidateStatus.IMPLEMENTATION_INCOMPLETE,
            ExecutionResult("IMPLEMENTATION_INCOMPLETE", reason="pm"),
            CandidateArtifact("base", f"child{round_id}"),
            None, GateDecision({}, False, False),
        )
        return RoundResult(
            round_id, "base", ProposalBatch((Proposal("p"),)), (dead,),
            Selection(0, "", "no_eligible_candidate"),
        )

    class Rounds:
        def run(self, request):
            return _dead_round(request.round_id)

    class NeverRsi:
        def due(self, round_id):
            return False

        def run(self, round_id):
            raise AssertionError("RSI must not run")

    class History:
        def append_round(self, result):
            pass

    class Checkpoint:
        def clear(self):
            pass

    class Observer:
        def round_committed(self, result):
            pass

    result = run_loop(
        LoopRequest("goal", 20, LoopState(0, "base", {"OBJ": 100.0}), POLICY),
        rounds=Rounds(), rsi=NeverRsi(),
        history=History(), checkpoint=Checkpoint(), observer=Observer(),
    )
    assert result.interrupted
    assert "no performed experiments" in result.interruption
    # both rounds committed (ids consumed), stopped right after the second
    assert result.task_rounds == 2
    assert result.state.next_round == 2


def test_loop_partial_death_resets_all_dead_streak():
    from simpleloop.candidate import (
        CandidateArtifact, CandidateResult, CandidateStatus, EvaluationResult,
        ExecutionResult, GateDecision,
    )
    from simpleloop.stages.proposer import Proposal, ProposalBatch
    from simpleloop.stages.selector import Selection

    def _mixed_round(round_id):
        dead = CandidateResult(
            0, f"r{round_id}c0", Proposal("p"), "base",
            CandidateStatus.IMPLEMENTATION_INCOMPLETE,
            ExecutionResult("IMPLEMENTATION_INCOMPLETE", reason="pm"),
            CandidateArtifact("base", f"dead{round_id}"),
            None, GateDecision({}, False, False),
        )
        alive = CandidateResult(
            1, f"r{round_id}c1", Proposal("q"), "base",
            CandidateStatus.GATE_REJECTED,
            ExecutionResult("COMMITTED"),
            CandidateArtifact("base", f"child{round_id}"),
            EvaluationResult("", {"OBJ": 90.0}),
            GateDecision({}, False, True),
        )
        return RoundResult(
            round_id, "base", ProposalBatch((Proposal("p"), Proposal("q"))),
            (dead, alive),
            Selection(0, "", "no_eligible_candidate"),
        )

    class Rounds:
        def run(self, request):
            return _mixed_round(request.round_id)

    class NeverRsi:
        def due(self, round_id):
            return False

        def run(self, round_id):
            raise AssertionError("RSI must not run")

    class History:
        def append_round(self, result):
            pass

    class Checkpoint:
        def clear(self):
            pass

    class Observer:
        def round_committed(self, result):
            pass

    result = run_loop(
        LoopRequest("goal", 5, LoopState(0, "base", {"OBJ": 100.0}), POLICY),
        rounds=Rounds(), rsi=NeverRsi(),
        history=History(), checkpoint=Checkpoint(), observer=Observer(),
    )
    # any performed candidate keeps the run alive
    assert not result.interrupted
    assert result.task_rounds == 5


# ---------------- structured byproducts & agent-owned cadence -------------

def test_log_roundtrips_structured_fields(tmp_path):
    log = JsonlReflectionLog(tmp_path / "reflection")
    log.append(ReflectionRecord(
        4, "audit", False, False, "",
        prescriptions=("test the eval-count family", "watch RemoveDN"),
        next_reflection_after_rounds=3))
    record = log.records()[0]
    assert record.prescriptions == (
        "test the eval-count family", "watch RemoveDN")
    assert record.next_reflection_after_rounds == 3


def test_log_omits_absent_structured_keys(tmp_path):
    root = tmp_path / "reflection"
    log = JsonlReflectionLog(root)
    log.append(ReflectionRecord(4, "plain old row"))
    row = json.loads(
        (root / "history.jsonl").read_text().splitlines()[0])
    assert set(row) == {
        "round_id", "handoff", "self_limitation_suspected", "abstained",
        "note"}


def test_next_reflection_round_agent_defer_and_calendar_cap(tmp_path):
    log = JsonlReflectionLog(tmp_path / "reflection")
    log.append(ReflectionRecord(8, "first"))
    # no defer -> the calendar interval rules
    assert next_reflection_round(
        log=log, interval_rounds=8, first_reflection_round=8) == 16
    # an earlier request is honored
    log.append(ReflectionRecord(
        16, "second", next_reflection_after_rounds=2))
    assert next_reflection_round(
        log=log, interval_rounds=8, first_reflection_round=8) == 18
    # a later request is capped by the calendar interval
    log.append(ReflectionRecord(
        18, "third", next_reflection_after_rounds=99))
    assert next_reflection_round(
        log=log, interval_rounds=8, first_reflection_round=8) == 26
    # a non-positive defer is ignored
    log.append(ReflectionRecord(
        26, "fourth", next_reflection_after_rounds=0))
    assert next_reflection_round(
        log=log, interval_rounds=8, first_reflection_round=8) == 34
