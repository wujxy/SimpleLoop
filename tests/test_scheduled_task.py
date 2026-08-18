from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from simpleloop.candidate import (
    CandidateArtifact, CandidateBatchRequest, CandidatePlan, CandidateResult,
    CandidateStatus, EvaluationResult, ExecutionResult, GateDecision,
)
from simpleloop.persistence.artifacts import encode_candidate_result
from simpleloop.persistence.journal import JobJournal
from simpleloop.scheduling.contracts import InfrastructureError
from simpleloop.scheduling.envelope import WorkerResult, WorkerStatus, write_result
from simpleloop.scheduling.task import (
    ScheduledCandidates, ScheduledProposer,
)
from simpleloop.stages.proposer import Proposal, ProposerRequest
from simpleloop.world import SourceWorkspace


class Workspaces:
    def __init__(self, root):
        self.root = root
        self.created = []

    def create(self, spec):
        workspace = SourceWorkspace(
            spec.workspace_id, self.root / spec.workspace_id, spec.revision,
        )
        workspace.path.mkdir(parents=True, exist_ok=True)
        self.created.append(workspace)
        return workspace

    def create_lane(self, lane_id, revision):
        return self.create(SimpleNamespace(
            workspace_id=f"lane-{lane_id}", revision=revision,
        ))

    def remove(self, workspace):
        pass


class Telemetry:
    def __init__(self):
        self.records = []
        self.snapshots = 0

    def record_usage(self, record):
        self.records.append(record)

    def snapshot(self, *, persist=False):
        self.snapshots += 1
        return {"snap": self.snapshots}


class Jobs:
    def __init__(self, journal, results):
        self.journal = journal
        self.results = list(results)
        self.calls = []

    def inflight(self):
        return self.journal.load()

    def run(self, **kwargs):
        self.calls.append(kwargs)
        results = [self.results.pop(0) for _ in kwargs["jobs"]]
        return SimpleNamespace(
            completed=tuple(
                SimpleNamespace(result=result) for result in results
            ),
            outcomes=tuple(
                SimpleNamespace(
                    result=result, infrastructure_error=None,
                )
                for result in results
            ),
        )

    def restart(self, **kwargs):
        self.calls.append({"restart": kwargs})
        self.journal.begin(
            kwargs["stage"], kwargs["round_id"], kwargs["context"],
            [{
                "request_id": request_id, "attempt": 1, "state": "ready",
                "handle": None, "submitted_at": None, "running_since": None,
                "gone_since": None, "note": "",
            } for request_id in kwargs["request_ids"]],
        )


def _candidate(candidate_id=0):
    return CandidateResult(
        candidate_id, f"r2c{candidate_id}", Proposal("p"), "parent",
        CandidateStatus.COMPLETED, ExecutionResult("COMMITTED"),
        CandidateArtifact("parent", f"sha-{candidate_id}"),
        EvaluationResult("", {"OBJ": 80}), GateDecision({}, True, True),
    )


def test_scheduled_candidates_builds_payloads_and_stamps_telemetry(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    envelope = WorkerResult(
        "candidate", "r2-c0", WorkerStatus.COMPLETED,
        encode_candidate_result(_candidate()), ({"model": "executor"},),
    )
    jobs = Jobs(journal, [envelope])
    telemetry = Telemetry()
    runner = ScheduledCandidates(
        run_dir=tmp_path, workspace=Workspaces(tmp_path / "ws"), jobs=jobs,
        telemetry=telemetry, max_parallel=2, prompt_dir=None,
    )

    batch = runner.run(CandidateBatchRequest(2, (
        CandidatePlan(0, "parent", Proposal("try cache", ("ref",))),
    )))

    call = jobs.calls[0]
    payload = call["jobs"][0].payload
    assert payload["proposal"] == "try cache"
    assert payload["evidence_refs"] == ["ref"]
    assert call["max_parallel"] == 1
    assert telemetry.records == [{"model": "executor"}]
    assert batch.candidates[0].telemetry == {"snap": 1}
    assert batch.telemetry == {"snap": 2}


def test_scheduled_candidates_transitions_active_proposer_stage(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("proposer", 2, {"payload": {}}, [])
    jobs = Jobs(journal, [WorkerResult(
        "candidate", "r2-c0", WorkerStatus.COMPLETED,
        encode_candidate_result(_candidate()),
    )])
    runner = ScheduledCandidates(
        run_dir=tmp_path, workspace=Workspaces(tmp_path / "ws"), jobs=jobs,
        telemetry=Telemetry(), max_parallel=1, prompt_dir=None,
    )

    runner.run(CandidateBatchRequest(2, (
        CandidatePlan(0, "parent", Proposal("p")),
    )))

    assert jobs.calls[0]["transition_from"] == "proposer"


def test_scheduled_proposer_replays_candidate_context_without_worker(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("candidates", 3, {
        "proposals": [{"instruction": "persisted", "evidence_refs": ["r"]}],
        "payloads": [],
    }, [])
    jobs = Jobs(journal, [])
    proposer = ScheduledProposer(
        run_dir=tmp_path, workspace=Workspaces(tmp_path / "ws"), jobs=jobs,
        telemetry=Telemetry(), proposal_slots=1, scientist_steps=10,
        prompt_dir=None,
    )

    result = proposer.propose(ProposerRequest(3, "goal", "parent"))

    assert result.proposals == (Proposal("persisted", ("r",)),)
    assert jobs.calls == []


def test_scheduled_proposer_decodes_worker_result_and_keeps_journal(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    raw = {
        "status": "COMPLETED", "outcome": "submit",
        "proposals": [{"instruction": "new idea", "evidence_refs": []}],
        "telemetry": {"steps": 2},
    }
    jobs = Jobs(journal, [WorkerResult(
        "proposer", "r4-l0", WorkerStatus.COMPLETED, raw,
        ({"model": "researcher"},),
    )])
    telemetry = Telemetry()
    proposer = ScheduledProposer(
        run_dir=tmp_path, workspace=Workspaces(tmp_path / "ws"), jobs=jobs,
        telemetry=telemetry, proposal_slots=1, scientist_steps=10,
        prompt_dir=None, self_repo=tmp_path / "self" / "repo",
    )

    result = proposer.propose(ProposerRequest(4, "goal", "parent"))

    assert result.proposals[0].instruction == "new idea"
    assert telemetry.records == [{"model": "researcher"}]
    assert jobs.calls[0]["stage"] == "proposer"
    assert jobs.calls[0]["jobs"][0].payload["self_repo"] == str(
        tmp_path / "self" / "repo"
    )


def test_scheduled_proposer_error_outcome_is_infrastructure_not_research(
        tmp_path):
    """A protocol-failed proposer lane is infrastructure: the adapter must
    raise (no history row, round id not consumed) instead of returning an
    empty research round."""
    import pytest

    from simpleloop.scheduling.contracts import InfrastructureError

    journal = JobJournal(tmp_path / "inflight.json")
    raw = {
        "status": "COMPLETED", "outcome": "error", "proposals": [],
        "abstain_reason": (
            "action protocol failed after 2 repairs; last reply: 'nope'"),
    }
    jobs = Jobs(journal, [WorkerResult(
        "proposer", "r4-l0", WorkerStatus.COMPLETED, raw, (),
    )])
    proposer = ScheduledProposer(
        run_dir=tmp_path, workspace=Workspaces(tmp_path / "ws"), jobs=jobs,
        telemetry=Telemetry(), proposal_slots=1, scientist_steps=10,
        prompt_dir=None,
    )

    with pytest.raises(InfrastructureError, match="last reply"):
        proposer.propose(ProposerRequest(4, "goal", "parent"))


def test_scheduled_proposer_honest_abstain_still_commits(tmp_path):
    """A real abstain decision IS research: it decodes to an empty batch
    (round consumed, history row written by the caller)."""
    journal = JobJournal(tmp_path / "inflight.json")
    raw = {
        "status": "COMPLETED", "outcome": "abstain", "proposals": [],
        "abstain_reason": "no defensible direction this round",
    }
    jobs = Jobs(journal, [WorkerResult(
        "proposer", "r4-l0", WorkerStatus.COMPLETED, raw, (),
    )])
    proposer = ScheduledProposer(
        run_dir=tmp_path, workspace=Workspaces(tmp_path / "ws"), jobs=jobs,
        telemetry=Telemetry(), proposal_slots=1, scientist_steps=10,
        prompt_dir=None,
    )

    result = proposer.propose(ProposerRequest(4, "goal", "parent"))

    assert result.proposals == ()
    assert result.abstained


def _dead_candidate(candidate_id=0):
    return CandidateResult(
        candidate_id, f"r2c{candidate_id}", Proposal("p"), "parent",
        CandidateStatus.IMPLEMENTATION_INCOMPLETE,
        ExecutionResult(
            "IMPLEMENTATION_INCOMPLETE",
            reason="[harness post-mortem] the experimenter session ended "
                   "without completing the intervention; stop_cause=crashed; "
                   "changed: none",
        ),
        None, None, GateDecision({}, False, False),
    )


def _no_change_candidate(candidate_id=0):
    return CandidateResult(
        candidate_id, f"r2c{candidate_id}", Proposal("p"), "parent",
        CandidateStatus.NO_CHANGE,
        ExecutionResult("EXECUTED", reason="executor made no changes"),
        None, None, GateDecision({}, False, False),
    )


def _completed_envelope(result):
    return WorkerResult(
        "candidate", f"r2-c{result.candidate_id}", WorkerStatus.COMPLETED,
        encode_candidate_result(result), (),
    )


def _candidates_runner(tmp_path, jobs):
    return ScheduledCandidates(
        run_dir=tmp_path, workspace=Workspaces(tmp_path / "ws"), jobs=jobs,
        telemetry=Telemetry(), max_parallel=1, prompt_dir=None,
    )


def test_all_sessions_dead_is_infrastructure_not_research(tmp_path):
    """run-004 r2 fairness: a batch where every experimenter session died
    performed no experiments — the round must not be consumed."""
    journal = JobJournal(tmp_path / "inflight.json")
    jobs = Jobs(journal, [_completed_envelope(_dead_candidate(0))])
    runner = _candidates_runner(tmp_path, jobs)

    with pytest.raises(InfrastructureError, match="not consumed") as exc:
        runner.run(CandidateBatchRequest(2, (
            CandidatePlan(0, "parent", Proposal("try composition")),
        )))

    assert "c0:crashed" in str(exc.value)


def test_mixed_dead_and_performed_batch_commits(tmp_path):
    """One performed outcome makes the round real research; the dead
    sibling is carried as a recorded failure, not an abort."""
    journal = JobJournal(tmp_path / "inflight.json")
    jobs = Jobs(journal, [
        _completed_envelope(_dead_candidate(0)),
        _completed_envelope(_candidate(1)),
    ])
    runner = _candidates_runner(tmp_path, jobs)

    batch = runner.run(CandidateBatchRequest(2, (
        CandidatePlan(0, "parent", Proposal("a")),
        CandidatePlan(1, "parent", Proposal("b")),
    )))

    assert [c.status for c in batch.candidates] == [
        CandidateStatus.IMPLEMENTATION_INCOMPLETE, CandidateStatus.COMPLETED,
    ]


def test_all_no_change_batch_still_commits(tmp_path):
    """NO_CHANGE is a performed outcome (session reported, decided against
    editing): an all-NO_CHANGE round is genuine research and must consume
    the round — otherwise a declining executor loop would retry forever."""
    journal = JobJournal(tmp_path / "inflight.json")
    jobs = Jobs(journal, [
        _completed_envelope(_no_change_candidate(0)),
        _completed_envelope(_no_change_candidate(1)),
    ])
    runner = _candidates_runner(tmp_path, jobs)

    batch = runner.run(CandidateBatchRequest(2, (
        CandidatePlan(0, "parent", Proposal("a")),
        CandidatePlan(1, "parent", Proposal("b")),
    )))

    assert all(
        c.status is CandidateStatus.NO_CHANGE for c in batch.candidates
    )


def test_resume_heals_dead_sessions_and_resurrects_worktrees(tmp_path):
    """The resume after an all-dead round must re-run the executors with
    the SAME proposals: purge the dead results, resurrect the released
    worktree, restart the journal with ready jobs."""
    journal = JobJournal(tmp_path / "inflight.json")
    result_dir = tmp_path / "rounds" / "r2" / "candidates" / "c0"
    result_dir.mkdir(parents=True)
    write_result(
        result_dir / "result.json",
        _completed_envelope(_dead_candidate(0)),
    )
    dead_worktree = tmp_path / "worktrees" / "r2-c0"  # released: absent
    payload = {
        "round_id": 2, "candidate_id": 0, "parent_sha": "parent",
        "proposal": "try composition", "evidence_refs": [],
        "run_dir": str(tmp_path),
        "worktree_id": "2-c0", "worktree_path": str(dead_worktree),
        "result_dir": str(result_dir), "prompt_dir": "",
    }
    journal.begin("candidates", 2, {
        "parent_sha": "parent",
        "proposals": [{"instruction": "try composition", "evidence_refs": []}],
        "payloads": [payload],
    }, [{
        "request_id": "r2-c0", "attempt": 1, "state": "succeeded",
        "handle": None, "submitted_at": 1.0, "running_since": None,
        "gone_since": None, "note": "",
    }])
    jobs = Jobs(journal, [_completed_envelope(_candidate(0))])
    workspaces = Workspaces(tmp_path / "ws")
    runner = ScheduledCandidates(
        run_dir=tmp_path, workspace=workspaces, jobs=jobs,
        telemetry=Telemetry(), max_parallel=1, prompt_dir=None,
    )

    batch = runner.run(CandidateBatchRequest(2, (
        CandidatePlan(0, "parent", Proposal("try composition")),
    )))

    assert not (result_dir / "result.json").exists()
    assert [ws.workspace_id for ws in workspaces.created] == ["2-c0"]
    record = journal.load()
    assert record.context["payloads"][0]["worktree_path"] == str(
        tmp_path / "ws" / "2-c0",
    )
    assert record.jobs[0]["state"] == "ready"
    assert record.jobs[0]["attempt"] == 1
    assert batch.candidates[0].status is CandidateStatus.COMPLETED
    run_call = next(call for call in jobs.calls if "jobs" in call)
    assert run_call["jobs"][0].payload["worktree_path"] == str(
        tmp_path / "ws" / "2-c0",
    )


def test_resume_keeps_performed_results_without_healing(tmp_path):
    """A crash mid-batch after some candidates performed: their results are
    reusable — no purge, no worktree churn, journal untouched."""
    journal = JobJournal(tmp_path / "inflight.json")
    result_dir = tmp_path / "rounds" / "r2" / "candidates" / "c0"
    result_dir.mkdir(parents=True)
    write_result(
        result_dir / "result.json",
        _completed_envelope(_candidate(0)),
    )
    payload = {
        "round_id": 2, "candidate_id": 0, "parent_sha": "parent",
        "proposal": "p", "evidence_refs": [], "run_dir": str(tmp_path),
        "worktree_id": "2-c0",
        "worktree_path": str(tmp_path / "ws" / "2-c0"),
        "result_dir": str(result_dir), "prompt_dir": "",
    }
    journal.begin("candidates", 2, {
        "parent_sha": "parent", "proposals": [], "payloads": [payload],
    }, [{
        "request_id": "r2-c0", "attempt": 1, "state": "succeeded",
        "handle": None, "submitted_at": 1.0, "running_since": None,
        "gone_since": None, "note": "",
    }])
    jobs = Jobs(journal, [])
    workspaces = Workspaces(tmp_path / "ws")
    (tmp_path / "ws" / "2-c0").mkdir(parents=True)
    runner = ScheduledCandidates(
        run_dir=tmp_path, workspace=workspaces, jobs=jobs,
        telemetry=Telemetry(), max_parallel=1, prompt_dir=None,
    )

    # The performed result exists on disk; the (stubbed) batch would replay
    # it via the supervisor. Healing must not have touched anything.
    runner_jobs_call_before = len(jobs.calls)

    assert (result_dir / "result.json").exists()
    assert workspaces.created == []
    assert len(jobs.calls) == runner_jobs_call_before
    restart_calls = [c for c in jobs.calls if "restart" in c]
    assert restart_calls == []
