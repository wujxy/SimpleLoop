"""The sole submit/poll/timeout/retry/resume/result lifecycle."""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping

from ..persistence.journal import JobJournal
from .contracts import (
    JobBatchRequest,
    JobBatchResult,
    JobHandle,
    JobOutcome,
    JobSpec,
    JobState,
    Scheduler,
)
from .envelope import (
    ProtocolError, WorkerResult, WorkerStatus, read_result, write_request,
)


_ACTIVE = {"pending", "running"}
_TERMINAL = {"succeeded", "failed"}


def is_failure_envelope(result: WorkerResult) -> bool:
    """True when a persisted worker result records a failure.

    Two shapes count: an envelope the worker itself marked FAILED, and a
    COMPLETED envelope whose handler converted an internal failure (LLM
    429/quota exhaustion, network error, bad credentials) into the
    proposer-family ``outcome: "error"`` convention. Both mean the stage
    failed; neither may be replayed as a final result on resume.
    """
    if result.status is not WorkerStatus.COMPLETED:
        return True
    return result.result.get("outcome") == "error"


def purge_failure_result(path: Path) -> bool:
    """Delete a persisted failure result so its job re-runs on resume.

    Returns True when the file was purged. An unreadable result file is
    purged too: it cannot be a valid reusable outcome, and collecting it
    would raise a protocol error on every future resume of the round.
    """
    if not path.exists():
        return False
    try:
        result = read_result(path)
    except ProtocolError:
        result = None
    if result is not None and not is_failure_envelope(result):
        return False
    path.unlink()
    return True


@dataclass
class _Runtime:
    request_id: str
    attempt: int = 1
    state: str = "ready"
    handle: JobHandle | None = None
    submitted_at: float | None = None
    running_since: float | None = None
    gone_since: float | None = None
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "attempt": self.attempt,
            "state": self.state,
            "handle": (
                {"scheduler": self.handle.scheduler, "value": self.handle.value}
                if self.handle else None
            ),
            "submitted_at": self.submitted_at,
            "running_since": self.running_since,
            "gone_since": self.gone_since,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "_Runtime":
        handle_raw = raw.get("handle")
        handle = None
        if isinstance(handle_raw, dict):
            scheduler = handle_raw.get("scheduler")
            value = handle_raw.get("value")
            if isinstance(scheduler, str) and isinstance(value, str):
                handle = JobHandle(scheduler, value)
        return cls(
            request_id=str(raw["request_id"]),
            attempt=int(raw.get("attempt") or 1),
            state=str(raw.get("state") or "ready"),
            handle=handle,
            submitted_at=_number(raw.get("submitted_at")),
            running_since=_number(raw.get("running_since")),
            gone_since=_number(raw.get("gone_since")),
            note=str(raw.get("note") or ""),
        )


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class JobSupervisor:
    def __init__(
        self,
        *,
        workspace_provider=None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 5.0,
    ):
        self.workspace_provider = workspace_provider
        self.clock = clock
        self.sleep = sleep
        self.poll_seconds = poll_seconds

    def run_batch(
        self,
        request: JobBatchRequest,
        *,
        scheduler: Scheduler,
        journal: JobJournal,
    ) -> JobBatchResult:
        by_id = {job.request.request_id: job for job in request.jobs}
        record = journal.load()
        if record is None:
            runtimes = {
                request_id: _Runtime(request_id) for request_id in by_id
            }
            journal.begin(
                request.stage,
                request.round_id,
                request.context,
                [runtime.to_dict() for runtime in runtimes.values()],
            )
            # A fresh batch must never replay a fossil: a failure result
            # left in a reused result_dir (a journal dropped after a failed
            # stage) would be collected as final and re-fail the stage with
            # the previous invocation's error instead of retrying it with
            # the current configuration.
            for job in request.jobs:
                purge_failure_result(job.result_path)
        else:
            if record.stage != request.stage or record.round_id != request.round_id:
                raise ValueError(
                    "persisted batch does not match requested stage/round: "
                    f"{record.stage}/r{record.round_id} != "
                    f"{request.stage}/r{request.round_id}"
                )
            persisted_ids = {str(item.get("request_id")) for item in record.jobs}
            if persisted_ids != set(by_id):
                raise ValueError(
                    "persisted job ids do not match the requested batch: "
                    f"{sorted(persisted_ids)} != {sorted(by_id)}"
                )
            runtimes = {
                runtime.request_id: runtime
                for runtime in (_Runtime.from_dict(item) for item in record.jobs)
            }

        outcomes: dict[str, JobOutcome] = {}
        released: set[str] = set()

        # A crash can persist a terminal runtime whose result was never
        # collected (or has since vanished). Such a job must not stall the
        # batch: retry it when attempts remain, otherwise finish it as an
        # infrastructure failure.
        for request_id, runtime in runtimes.items():
            if runtime.state not in _TERMINAL:
                continue
            job = by_id[request_id]
            if job.result_path.exists():
                continue  # collected by the main loop below
            if runtime.attempt < job.retry.max_attempts:
                self._reset_workspace(job)
                runtime.state = "ready"
                runtime.handle = None
                runtime.submitted_at = None
                runtime.running_since = None
                runtime.gone_since = None
            else:
                outcomes[request_id] = JobOutcome(
                    job,
                    infrastructure_error=(
                        runtime.note
                        or "job ended without a result before the run stopped"
                    ),
                )
                self._release(job, released)

        while len(outcomes) < len(request.jobs):
            changed = False

            for request_id, runtime in runtimes.items():
                if request_id in outcomes:
                    continue
                job = by_id[request_id]
                if job.result_path.exists():
                    result = read_result(job.result_path, expected=job.request)
                    runtime.state = "succeeded"
                    outcomes[request_id] = JobOutcome(job, result=result)
                    self._release(job, released)
                    changed = True

            active = sum(
                runtime.state in _ACTIVE
                for request_id, runtime in runtimes.items()
                if request_id not in outcomes
            )
            for request_id, runtime in runtimes.items():
                if active >= request.max_parallel:
                    break
                if request_id in outcomes or runtime.state != "ready":
                    continue
                job = by_id[request_id]
                attempt_request = replace(
                    job.request,
                    payload={**job.request.payload, "attempt": runtime.attempt},
                )
                write_request(job.manifest_path, attempt_request)
                now = self.clock()
                try:
                    runtime.handle = scheduler.submit(job)
                except Exception as exc:  # scheduler launch is infrastructure
                    self._retry_or_finish(
                        runtime, job, outcomes, released, scheduler,
                        f"submit failed: {exc}",
                    )
                else:
                    runtime.state = "pending"
                    runtime.submitted_at = now
                    runtime.running_since = None
                    runtime.gone_since = None
                    runtime.note = ""
                    if job.result_path.exists():
                        result = read_result(job.result_path, expected=job.request)
                        runtime.state = "succeeded"
                        outcomes[request_id] = JobOutcome(job, result=result)
                        self._release(job, released)
                    else:
                        active += 1
                changed = True

            handles = tuple(
                runtime.handle
                for request_id, runtime in runtimes.items()
                if request_id not in outcomes
                and runtime.state in _ACTIVE
                and runtime.handle is not None
            )
            if handles:
                observations = {
                    item.handle: item for item in scheduler.inspect(handles)
                }
                now = self.clock()
                for request_id, runtime in runtimes.items():
                    if request_id in outcomes or runtime.handle not in observations:
                        continue
                    observation = observations[runtime.handle]
                    job = by_id[request_id]
                    if observation.state is JobState.UNKNOWN:
                        continue
                    if observation.state is JobState.PENDING:
                        runtime.state = "pending"
                        runtime.gone_since = None
                    elif observation.state is JobState.RUNNING:
                        runtime.state = "running"
                        runtime.gone_since = None
                        if runtime.running_since is None:
                            runtime.running_since = now
                        if now - runtime.running_since > job.retry.timeout_seconds:
                            self._retry_or_finish(
                                runtime, job, outcomes, released, scheduler,
                                f"running timeout after {job.retry.timeout_seconds}s",
                            )
                    elif observation.state is JobState.FAILED:
                        self._retry_or_finish(
                            runtime, job, outcomes, released, scheduler,
                            observation.detail or "scheduler reported failure",
                        )
                    elif observation.state in (JobState.LOST, JobState.SUCCEEDED):
                        if runtime.gone_since is None:
                            runtime.gone_since = now
                        elif (
                            now - runtime.gone_since
                            >= job.retry.disappearance_grace_seconds
                        ):
                            self._retry_or_finish(
                                runtime, job, outcomes, released, scheduler,
                                observation.detail or "job left scheduler without result",
                            )
                    changed = True

            journal.save_jobs([runtime.to_dict() for runtime in runtimes.values()])
            if len(outcomes) < len(request.jobs):
                self.sleep(self.poll_seconds)

        return JobBatchResult(tuple(
            outcomes[job.request.request_id] for job in request.jobs
        ))

    def _retry_or_finish(
        self,
        runtime: _Runtime,
        job: JobSpec,
        outcomes: dict[str, JobOutcome],
        released: set[str],
        scheduler: Scheduler,
        note: str,
    ) -> None:
        if runtime.handle is not None:
            scheduler.cancel(runtime.handle)
        runtime.note = note
        if runtime.attempt < job.retry.max_attempts:
            runtime.attempt += 1
            # Never hand the next attempt a possibly-dirty worktree.
            self._reset_workspace(job)
            runtime.state = "ready"
            runtime.handle = None
            runtime.submitted_at = None
            runtime.running_since = None
            runtime.gone_since = None
            return
        runtime.state = "failed"
        outcomes[runtime.request_id] = JobOutcome(
            job, infrastructure_error=note,
        )
        self._release(job, released)

    def _release(self, job: JobSpec, released: set[str]) -> None:
        request_id = job.request.request_id
        if (
            request_id in released
            or job.workspace is None
            or self.workspace_provider is None
        ):
            return
        self.workspace_provider.remove(job.workspace)
        released.add(request_id)

    def _reset_workspace(self, job: JobSpec) -> None:
        if job.workspace is None or self.workspace_provider is None:
            return
        reset = getattr(self.workspace_provider, "reset", None)
        if callable(reset):
            reset(job.workspace)
