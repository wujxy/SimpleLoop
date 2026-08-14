from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from simpleloop.candidate import (
    CandidateArtifact, CandidateBatchRequest, CandidatePlan, CandidateResult,
    CandidateStatus, EvaluationResult, ExecutionResult, GateDecision,
)
from simpleloop.persistence.artifacts import encode_candidate_result
from simpleloop.persistence.journal import JobJournal
from simpleloop.scheduling.envelope import WorkerResult, WorkerStatus
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
        result = self.results.pop(0)
        return SimpleNamespace(
            completed=(SimpleNamespace(result=result),),
            outcomes=(SimpleNamespace(
                result=result, infrastructure_error=None,
            ),),
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
        prompt_dir=None,
    )

    result = proposer.propose(ProposerRequest(4, "goal", "parent"))

    assert result.proposals[0].instruction == "new idea"
    assert telemetry.records == [{"model": "researcher"}]
    assert jobs.calls[0]["stage"] == "proposer"
