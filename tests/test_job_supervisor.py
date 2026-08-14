from __future__ import annotations

from pathlib import Path

import pytest

from simpleloop.persistence.journal import JobJournal
from simpleloop.scheduling.contracts import (
    JobBatchRequest,
    JobHandle,
    JobObservation,
    JobSpec,
    JobState,
    ResourceSpec,
    RetryPolicy,
)
from simpleloop.scheduling.envelope import (
    ProtocolError,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
    read_request,
    write_result,
)
from simpleloop.scheduling.supervisor import JobSupervisor
from simpleloop.world import SourceWorkspace


def _job(tmp_path: Path, request_id: str = "r1-c0", *, attempts: int = 2,
         workspace=None) -> JobSpec:
    result = tmp_path / request_id / "result.json"
    request = WorkerRequest("candidate", request_id, {}, result)
    return JobSpec(
        request=request,
        manifest_path=result.parent / "manifest.json",
        result_path=result,
        stdout_path=result.parent / "job.out",
        stderr_path=result.parent / "job.err",
        argv=("worker",),
        retry=RetryPolicy(attempts, 10, 0),
        resources=ResourceSpec(),
        workspace=workspace,
    )


class FakeScheduler:
    name = "fake"

    def __init__(self, *, states=(), complete_on_attempt=None):
        self.states = list(states)
        self.complete_on_attempt = complete_on_attempt
        self.submitted = []
        self.cancelled = []
        self.inspections = 0

    def submit(self, job):
        self.submitted.append(job)
        handle = JobHandle(self.name, str(len(self.submitted)))
        if self.complete_on_attempt == len(self.submitted):
            write_result(job.result_path, WorkerResult(
                job.request.kind,
                job.request.request_id,
                WorkerStatus.COMPLETED,
                {"attempt": len(self.submitted)},
            ))
        return handle

    def inspect(self, handles):
        self.inspections += 1
        state = self.states.pop(0) if self.states else JobState.RUNNING
        return tuple(JobObservation(handle, state) for handle in handles)

    def cancel(self, handle):
        self.cancelled.append(handle)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def _run(tmp_path, scheduler, jobs, *, max_parallel=1, workspaces=None):
    journal = JobJournal(tmp_path / "inflight.json")
    request = JobBatchRequest("candidates", 1, {}, tuple(jobs), max_parallel)
    return JobSupervisor(
        workspace_provider=workspaces,
        clock=Clock(),
        sleep=lambda _: None,
        poll_seconds=0,
    ).run_batch(request, scheduler=scheduler, journal=journal)


def test_supervisor_returns_valid_result_without_waiting_for_queue_exit(tmp_path):
    scheduler = FakeScheduler(complete_on_attempt=1)

    result = _run(tmp_path, scheduler, [_job(tmp_path)])

    assert result.outcomes[0].result.result == {"attempt": 1}
    assert scheduler.inspections == 0


def test_held_job_retries_then_collects(tmp_path):
    scheduler = FakeScheduler(
        states=[JobState.FAILED], complete_on_attempt=2,
    )

    result = _run(tmp_path, scheduler, [_job(tmp_path)])

    assert result.outcomes[0].result.result == {"attempt": 2}
    assert len(scheduler.submitted) == 2
    assert scheduler.cancelled == [JobHandle("fake", "1")]


def test_retry_manifest_carries_current_attempt(tmp_path):
    scheduler = FakeScheduler(
        states=[JobState.FAILED], complete_on_attempt=2,
    )
    attempts = []
    original_submit = scheduler.submit

    def submit(job):
        attempts.append(read_request(job.manifest_path).payload["attempt"])
        return original_submit(job)

    scheduler.submit = submit

    _run(tmp_path, scheduler, [_job(tmp_path)])

    assert attempts == [1, 2]


def test_unknown_query_does_not_consume_an_attempt(tmp_path):
    job = _job(tmp_path)
    scheduler = FakeScheduler(states=[JobState.UNKNOWN, JobState.RUNNING])
    original_inspect = scheduler.inspect

    def inspect(handles):
        observations = original_inspect(handles)
        if scheduler.inspections == 2:
            write_result(job.result_path, WorkerResult(
                "candidate", "r1-c0", WorkerStatus.COMPLETED, {"ok": True}
            ))
        return observations

    scheduler.inspect = inspect

    result = _run(tmp_path, scheduler, [job])

    assert result.outcomes[0].result.result == {"ok": True}
    assert len(scheduler.submitted) == 1


def test_disappeared_job_waits_for_grace_then_retries(tmp_path):
    job = _job(tmp_path)
    job = JobSpec(
        job.request, job.manifest_path, job.result_path, job.stdout_path,
        job.stderr_path, job.argv, RetryPolicy(2, 10, 2), job.resources,
        job.workspace,
    )
    scheduler = FakeScheduler(
        states=[JobState.LOST, JobState.LOST], complete_on_attempt=2,
    )

    result = _run(tmp_path, scheduler, [job])

    assert result.completed[0].result.result == {"attempt": 2}
    assert len(scheduler.submitted) == 2


def test_running_timeout_cancels_and_retries(tmp_path):
    job = _job(tmp_path)
    job = JobSpec(
        job.request, job.manifest_path, job.result_path, job.stdout_path,
        job.stderr_path, job.argv, RetryPolicy(2, 1, 0), job.resources,
        job.workspace,
    )
    scheduler = FakeScheduler(
        states=[JobState.RUNNING, JobState.RUNNING], complete_on_attempt=2,
    )

    result = _run(tmp_path, scheduler, [job])

    assert result.completed[0].result.result == {"attempt": 2}
    assert scheduler.cancelled == [JobHandle("fake", "1")]


def test_resume_inspects_persisted_remote_handle_without_submit(tmp_path):
    job = _job(tmp_path)
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("candidates", 1, {}, [{
        "request_id": "r1-c0", "attempt": 1, "state": "running",
        "handle": {"scheduler": "fake", "value": "remote-1"},
        "submitted_at": 1.0, "running_since": 1.0,
        "gone_since": None, "note": "",
    }])
    scheduler = FakeScheduler(states=[JobState.RUNNING])

    def inspect(handles):
        assert handles == (JobHandle("fake", "remote-1"),)
        write_result(job.result_path, WorkerResult(
            "candidate", "r1-c0", WorkerStatus.COMPLETED, {"resumed": True},
        ))
        return (JobObservation(handles[0], JobState.RUNNING),)

    scheduler.inspect = inspect
    result = JobSupervisor(
        clock=Clock(), sleep=lambda _: None, poll_seconds=0,
    ).run_batch(
        JobBatchRequest("candidates", 1, {}, (job,), 1),
        scheduler=scheduler, journal=journal,
    )

    assert result.completed[0].result.result == {"resumed": True}
    assert scheduler.submitted == []


def test_resume_failed_runtime_without_result_finishes_as_infrastructure(tmp_path):
    job = _job(tmp_path, attempts=2)
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("candidates", 1, {}, [{
        "request_id": "r1-c0", "attempt": 2, "state": "failed",
        "handle": {"scheduler": "fake", "value": "1"},
        "submitted_at": 1.0, "running_since": 1.0,
        "gone_since": None, "note": "node lost",
    }])
    scheduler = FakeScheduler()

    result = JobSupervisor(
        clock=Clock(), sleep=lambda _: None, poll_seconds=0,
    ).run_batch(
        JobBatchRequest("candidates", 1, {}, (job,), 1),
        scheduler=scheduler, journal=journal,
    )

    assert scheduler.submitted == []
    assert len(result.failed) == 1
    assert result.failed[0].infrastructure_error == "node lost"


def test_resume_terminal_runtime_with_attempts_left_is_resubmitted(tmp_path):
    job = _job(tmp_path, attempts=2)
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("candidates", 1, {}, [{
        "request_id": "r1-c0", "attempt": 1, "state": "succeeded",
        "handle": {"scheduler": "fake", "value": "1"},
        "submitted_at": 1.0, "running_since": None,
        "gone_since": None, "note": "",
    }])
    scheduler = FakeScheduler(complete_on_attempt=1)

    result = JobSupervisor(
        clock=Clock(), sleep=lambda _: None, poll_seconds=0,
    ).run_batch(
        JobBatchRequest("candidates", 1, {}, (job,), 1),
        scheduler=scheduler, journal=journal,
    )

    assert len(scheduler.submitted) == 1
    assert result.completed[0].result.result == {"attempt": 1}


def test_malformed_result_is_protocol_error_not_retry(tmp_path):
    job = _job(tmp_path)
    scheduler = FakeScheduler()
    original_submit = scheduler.submit

    def submit(spec):
        handle = original_submit(spec)
        spec.result_path.parent.mkdir(parents=True, exist_ok=True)
        spec.result_path.write_text("{broken")
        return handle

    scheduler.submit = submit

    with pytest.raises(ProtocolError):
        _run(tmp_path, scheduler, [job])
    assert len(scheduler.submitted) == 1


def test_terminal_job_releases_workspace_once(tmp_path):
    workspace = SourceWorkspace("1-c0", tmp_path / "worktree", "base")

    class Workspaces:
        def __init__(self):
            self.removed = []

        def remove(self, item):
            self.removed.append(item)

        def reset(self, item):
            pass

    workspaces = Workspaces()
    scheduler = FakeScheduler(complete_on_attempt=1)

    _run(tmp_path, scheduler, [_job(tmp_path, workspace=workspace)],
         workspaces=workspaces)

    assert workspaces.removed == [workspace]


def test_retry_resets_workspace_before_resubmit(tmp_path):
    workspace = SourceWorkspace("1-c0", tmp_path / "worktree", "base")

    class Workspaces:
        def __init__(self):
            self.removed = []
            self.resets = []

        def remove(self, item):
            self.removed.append(item)

        def reset(self, item):
            self.resets.append(item)

    workspaces = Workspaces()
    scheduler = FakeScheduler(
        states=[JobState.FAILED], complete_on_attempt=2,
    )

    result = _run(tmp_path, scheduler, [_job(tmp_path, workspace=workspace)],
                  workspaces=workspaces)

    # The retried attempt must start from a pristine worktree, and the
    # terminal release still happens exactly once.
    assert workspaces.resets == [workspace]
    assert workspaces.removed == [workspace]
    assert result.completed[0].result.result == {"attempt": 2}


def test_supervisor_limits_parallel_submissions(tmp_path):
    jobs = [_job(tmp_path, f"r1-c{i}") for i in range(2)]
    scheduler = FakeScheduler()

    def submit(job):
        if scheduler.submitted:
            assert scheduler.submitted[-1].result_path.exists()
        scheduler.submitted.append(job)
        write_result(job.result_path, WorkerResult(
            "candidate", job.request.request_id, WorkerStatus.COMPLETED, {}
        ))
        return JobHandle("fake", str(len(scheduler.submitted)))

    scheduler.submit = submit

    result = _run(tmp_path, scheduler, jobs, max_parallel=1)

    assert len(result.outcomes) == 2
    assert len(scheduler.submitted) == 2


def test_partial_success_is_returned_with_exhausted_failure(tmp_path):
    first = _job(tmp_path, "r1-c0", attempts=1)
    second = _job(tmp_path, "r1-c1", attempts=1)

    class PartialScheduler(FakeScheduler):
        def submit(self, job):
            self.submitted.append(job)
            handle = JobHandle(self.name, job.request.request_id)
            if job.request.request_id == "r1-c0":
                write_result(job.result_path, WorkerResult(
                    "candidate", "r1-c0", WorkerStatus.COMPLETED, {"ok": True},
                ))
            return handle

        def inspect(self, handles):
            return tuple(JobObservation(handle, JobState.FAILED, "held")
                         for handle in handles)

    result = _run(
        tmp_path, PartialScheduler(), (first, second), max_parallel=2,
    )

    assert len(result.completed) == 1
    assert result.completed[0].job.request.request_id == "r1-c0"
    assert len(result.failed) == 1
    assert result.failed[0].infrastructure_error == "held"
