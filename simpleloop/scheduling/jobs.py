"""Domain-neutral construction of unified worker jobs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..persistence.journal import JobJournal, JournalRecord
from ..world import SourceWorkspace
from .contracts import (
    JobBatchRequest, JobBatchResult, JobSpec, ResourceSpec, RetryPolicy,
)
from .envelope import WorkerRequest


@dataclass(frozen=True)
class WorkerJob:
    kind: str
    request_id: str
    payload: Mapping[str, object]
    result_dir: Path
    workspace: SourceWorkspace | None = None


@dataclass(frozen=True)
class WorkerJobPolicy:
    python: str
    retry: RetryPolicy
    resources: ResourceSpec


class WorkerJobs:
    def __init__(self, *, scheduler, supervisor, journal: JobJournal, policy):
        self.scheduler = scheduler
        self.supervisor = supervisor
        self.journal = journal
        self.policy: WorkerJobPolicy = policy

    def inflight(self) -> JournalRecord | None:
        return self.journal.load()

    def clear(self) -> None:
        self.journal.clear()

    def restart(
        self,
        *,
        stage: str,
        round_id: int,
        context: Mapping[str, object],
        request_ids: tuple[str, ...],
    ) -> None:
        """Rewrite the current stage's journal with ready jobs.

        A resume that invalidates persisted outcomes (e.g. every experimenter
        session died before performing the intervention) must genuinely
        re-run the batch: reset the job entries to ready/attempt 1 so the
        supervisor resubmits them instead of replaying results or running
        out of retry attempts against outcomes that were already collected.
        Only legal for the stage/round already recorded.
        """
        self.journal.begin(
            stage,
            round_id,
            context,
            [_ready(request_id) for request_id in request_ids],
        )

    def run(
        self,
        *,
        stage: str,
        round_id: int,
        context: Mapping[str, object],
        jobs: tuple[WorkerJob, ...],
        max_parallel: int,
        transition_from: str | None = None,
    ) -> JobBatchResult:
        specs = tuple(self._spec(job) for job in jobs)
        if transition_from is not None:
            self.journal.transition(
                transition_from,
                stage,
                round_id,
                context,
                tuple(_ready(spec.request.request_id) for spec in specs),
            )
        return self.supervisor.run_batch(
            JobBatchRequest(stage, round_id, dict(context), specs, max_parallel),
            scheduler=self.scheduler,
            journal=self.journal,
        )

    def _spec(self, job: WorkerJob) -> JobSpec:
        result_dir = Path(job.result_dir)
        manifest = result_dir / "manifest.json"
        result = result_dir / "result.json"
        return JobSpec(
            WorkerRequest(job.kind, job.request_id, dict(job.payload), result),
            manifest,
            result,
            result_dir / "job.out",
            result_dir / "job.err",
            (
                self.policy.python,
                "-m",
                "simpleloop.scheduling.worker",
                "--manifest",
                str(manifest),
            ),
            self.policy.retry,
            self.policy.resources,
            job.workspace,
        )


def _ready(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "attempt": 1,
        "state": "ready",
        "handle": None,
        "submitted_at": None,
        "running_since": None,
        "gone_since": None,
        "note": "",
    }
