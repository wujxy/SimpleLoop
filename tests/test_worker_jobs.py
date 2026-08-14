from __future__ import annotations

from pathlib import Path

from simpleloop.persistence.journal import JobJournal
from simpleloop.scheduling.contracts import (
    JobBatchResult, ResourceSpec, RetryPolicy,
)
from simpleloop.scheduling.jobs import WorkerJob, WorkerJobPolicy, WorkerJobs


class Supervisor:
    def __init__(self):
        self.requests = []

    def run_batch(self, request, *, scheduler, journal):
        self.requests.append(request)
        return JobBatchResult(())


def _client(tmp_path):
    supervisor = Supervisor()
    journal = JobJournal(tmp_path / "inflight.json")
    client = WorkerJobs(
        scheduler=object(), supervisor=supervisor, journal=journal,
        policy=WorkerJobPolicy(
            "python-worker", RetryPolicy(2, 10, 0), ResourceSpec(2, 1024),
        ),
    )
    return client, supervisor, journal


def test_worker_jobs_builds_shell_free_worker_job(tmp_path):
    client, supervisor, _ = _client(tmp_path)

    client.run(
        stage="candidates", round_id=1, context={"parent": "base"},
        jobs=(WorkerJob(
            "candidate", "r1-c0", {"candidate_id": 0}, tmp_path / "c0",
        ),),
        max_parallel=1,
    )

    batch = supervisor.requests[0]
    spec = batch.jobs[0]
    assert spec.request.kind == "candidate"
    assert spec.request.request_id == "r1-c0"
    assert spec.argv == (
        "python-worker", "-m", "simpleloop.scheduling.worker",
        "--manifest", str(tmp_path / "c0" / "manifest.json"),
    )
    assert spec.resources == ResourceSpec(2, 1024)
    assert batch.context == {"parent": "base"}


def test_worker_jobs_transitions_from_proposer_to_candidates(tmp_path):
    client, _, journal = _client(tmp_path)
    journal.begin("proposer", 1, {}, [])

    client.run(
        stage="candidates", round_id=1,
        context={"proposals": [{"instruction": "p"}]},
        jobs=(WorkerJob(
            "candidate", "r1-c0", {"candidate_id": 0}, tmp_path / "c0",
        ),),
        max_parallel=1, transition_from="proposer",
    )

    record = journal.load()
    assert record.stage == "candidates"
    assert record.jobs[0]["request_id"] == "r1-c0"


def test_worker_jobs_exposes_inflight_and_clear(tmp_path):
    client, _, journal = _client(tmp_path)
    journal.begin("proposer", 3, {}, [])
    assert client.inflight().round_id == 3
    client.clear()
    assert client.inflight() is None
