from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
from simpleloop.execution import build_backend
from simpleloop.execution.backend import WorkerBackend
from simpleloop.scheduling.contracts import JobBatchResult, JobOutcome
from simpleloop.scheduling.envelope import WorkerResult, WorkerStatus
from simpleloop.scheduling.hepjob import HEPJobScheduler
from simpleloop.scheduling.local import LocalScheduler
from simpleloop.persistence.artifacts import encode_candidate_result
from simpleloop.stages.proposer import Proposal, ProposerRequest
from simpleloop.world import SourceWorkspace


class Workspaces:
    def __init__(self, root):
        self.root = root
        self.created = []
        self.removed = []

    def create(self, spec):
        workspace = SourceWorkspace(
            spec.workspace_id, self.root / spec.workspace_id, spec.revision
        )
        workspace.path.mkdir(parents=True, exist_ok=True)
        self.created.append(workspace)
        return workspace

    def create_lane(self, lane_id, revision):
        return self.create(SimpleNamespace(
            workspace_id=f"lane-{lane_id}", revision=revision,
        ))

    def remove(self, workspace):
        self.removed.append(workspace)


def _candidate(candidate_id, proposal, parent):
    return CandidateResult(
        candidate_id,
        f"r2c{candidate_id}",
        Proposal(proposal),
        parent,
        CandidateStatus.COMPLETED,
        ExecutionResult("COMPLETED"),
        CandidateArtifact(parent, f"sha-{candidate_id}"),
        EvaluationResult("", {"OBJ": candidate_id}),
        GateDecision({}, True, True),
    )


class RecordingSupervisor:
    def __init__(self):
        self.requests = []

    def run_batch(self, request, *, scheduler, journal):
        self.requests.append(request)
        outcomes = []
        for job in request.jobs:
            payload = job.request.payload
            if job.request.kind == "candidate":
                inner = encode_candidate_result(_candidate(
                    int(payload["candidate_id"]),
                    str(payload["proposal"]),
                    str(payload["parent_sha"]),
                ))
            elif job.request.kind == "proposer":
                inner = {
                    "status": "COMPLETED",
                    "outcome": "submit",
                    "proposals": [{"instruction": "try cache"}],
                    "reason_kind": None,
                    "telemetry": {},
                }
            elif job.request.kind == "viability":
                inner = {
                    "status": "COMPLETED", "outcome": "abstain", "proposals": [],
                }
            else:
                inner = {"self_review": {"decision": "KEEP"}}
            outcomes.append(JobOutcome(job, WorkerResult(
                job.request.kind,
                job.request.request_id,
                WorkerStatus.COMPLETED,
                inner,
                ({"model": "m"},),
            )))
        return JobBatchResult(tuple(outcomes))


def _context(tmp_path):
    telemetry = SimpleNamespace(records=[], record_usage=lambda row: telemetry.records.append(row))
    return SimpleNamespace(
        cfg={
            "execution_backend": "local",
            "max_workers": 2,
            "candidates_per_round": 2,
            "scientist_steps": 10,
            "hepjob": {
                "max_attempts": 2,
                "run_timeout_seconds": 100,
                "disappearance_grace_seconds": 0,
                "poll_seconds": 5,
                "cpus": 1,
                "memory_mb": 100,
                "python_executable": "python",
            },
        },
        run_dir=tmp_path,
        workspace=Workspaces(tmp_path / "workspaces"),
        telemetry=telemetry,
        prompt_dir=None,
    )


def test_worker_backend_builds_candidate_jobs_and_preserves_order(tmp_path):
    ctx = _context(tmp_path)
    supervisor = RecordingSupervisor()
    backend = WorkerBackend(ctx, scheduler=object(), supervisor=supervisor)
    plans = (
        CandidatePlan(0, "parent-a", Proposal("p0")),
        CandidatePlan(1, "parent-b", Proposal("p1")),
    )

    results = backend.run_candidates(CandidateBatchRequest(2, plans))

    batch = supervisor.requests[0]
    assert batch.stage == "candidates"
    assert batch.max_parallel == 2
    assert [job.request.kind for job in batch.jobs] == ["candidate", "candidate"]
    assert [job.request.payload["parent_sha"] for job in batch.jobs] == [
        "parent-a", "parent-b",
    ]
    assert [result.candidate_id for result in results] == [0, 1]
    assert ctx.telemetry.records == [{"model": "m"}, {"model": "m"}]


def test_worker_backend_builds_proposer_job_and_decodes_batch(tmp_path):
    ctx = _context(tmp_path)
    supervisor = RecordingSupervisor()
    backend = WorkerBackend(ctx, scheduler=object(), supervisor=supervisor)

    batch = backend.run_proposer_lanes(ProposerRequest(3, "goal", "base"))

    request = supervisor.requests[0]
    assert request.stage == "proposer"
    assert request.jobs[0].request.kind == "proposer"
    assert batch.proposals[0].instruction == "try cache"
    assert ctx.telemetry.records == [{"model": "m"}]


def test_build_backend_only_switches_scheduler_adapter(tmp_path):
    local_ctx = _context(tmp_path / "local")
    local_ctx.run_dir.mkdir()
    local = build_backend(local_ctx)
    assert isinstance(local, WorkerBackend)
    assert isinstance(local.scheduler, LocalScheduler)

    remote_ctx = _context(tmp_path / "remote")
    remote_ctx.run_dir.mkdir()
    remote_ctx.cfg["execution_backend"] = "hepjob"
    remote_ctx.cfg["hepjob"].update({
        "schedd_name": "schedd",
        "collector": "collector",
        "accounting_group": "JUNO.juno.default",
        "accounting_group_user": "alice",
        "request_os": "AlmaLinux9",
        "ihep_group": None,
        "submit_cmd": "condor_submit",
        "query_cmd": "condor_q",
        "remove_cmd": "condor_rm",
    })
    remote = build_backend(remote_ctx)
    assert isinstance(remote, WorkerBackend)
    assert isinstance(remote.scheduler, HEPJobScheduler)
    assert remote.scheduler.config.environment_script.is_file()


def test_worker_backend_runs_viability_with_dedicated_kind(tmp_path):
    ctx = _context(tmp_path)
    supervisor = RecordingSupervisor()
    backend = WorkerBackend(ctx, scheduler=object(), supervisor=supervisor)
    payload = {
        "lane_id": 0, "round_id": 0, "base_sha": "base",
        "run_dir": str(tmp_path), "workspace_path": str(tmp_path / "ws"),
        "result_dir": str(tmp_path / "viability"), "self_repo": str(tmp_path / "self"),
    }

    result = backend.run_viability(payload)

    assert supervisor.requests[0].stage == "viability"
    assert supervisor.requests[0].jobs[0].request.kind == "viability"
    assert result["status"] == "COMPLETED"


def test_worker_backend_runs_self_review_with_dedicated_kind(tmp_path):
    ctx = _context(tmp_path)
    supervisor = RecordingSupervisor()
    backend = WorkerBackend(ctx, scheduler=object(), supervisor=supervisor)

    result = backend.run_self_review(round_id=5)

    assert supervisor.requests[0].stage == "self_review"
    assert supervisor.requests[0].jobs[0].request.kind == "self_review"
    assert result == {"decision": "KEEP"}


def test_candidate_resume_uses_persisted_payload_without_new_workspace(tmp_path):
    ctx = _context(tmp_path)
    supervisor = RecordingSupervisor()
    backend = WorkerBackend(ctx, scheduler=object(), supervisor=supervisor)
    payload = {
        "round_id": 2, "candidate_id": 4, "parent_sha": "parent",
        "proposal": "persisted", "evidence_refs": ["ref"],
        "run_dir": str(tmp_path), "worktree_id": "2-c4",
        "worktree_path": str(tmp_path / "existing"),
        "result_dir": str(tmp_path / "result"), "prompt_dir": "",
    }
    backend.journal.begin("candidates", 2, {
        "parent_sha": "parent", "proposals": [{"instruction": "persisted"}],
        "payloads": [payload],
    }, [{"request_id": "r2-c4"}])

    results = backend.run_candidates(CandidateBatchRequest(2, (
        CandidatePlan(4, "parent", Proposal("ignored caller value")),
    )))

    assert ctx.workspace.created == []
    assert supervisor.requests[0].jobs[0].request.payload["proposal"] == "persisted"
    assert results[0].candidate_id == 4


def test_worker_backend_ingests_each_usage_record(tmp_path):
    ctx = _context(tmp_path)

    class UsageSupervisor(RecordingSupervisor):
        def run_batch(self, request, *, scheduler, journal):
            result = super().run_batch(request, scheduler=scheduler, journal=journal)
            outcome = result.outcomes[0]
            wrapped = WorkerResult(
                outcome.result.kind, outcome.result.request_id,
                outcome.result.status, outcome.result.result,
                ({"model": "a"}, {"model": "b"}),
            )
            return JobBatchResult((JobOutcome(outcome.job, wrapped),))

    backend = WorkerBackend(
        ctx, scheduler=object(), supervisor=UsageSupervisor(),
    )

    backend.run_candidates(CandidateBatchRequest(2, (
        CandidatePlan(0, "parent", Proposal("p0")),
    )))

    assert ctx.telemetry.records == [{"model": "a"}, {"model": "b"}]
