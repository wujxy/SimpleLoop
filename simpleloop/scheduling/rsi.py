"""Phase-5 RSI transport over the unified worker-job client."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .contracts import InfrastructureError
from .envelope import WorkerStatus
from .jobs import WorkerJob, WorkerJobs


class ScheduledSelfReview:
    def __init__(
        self, *, run_dir, jobs: WorkerJobs, telemetry,
        scientist_steps: int, prompt_dir,
    ):
        self.run_dir = Path(run_dir)
        self.jobs = jobs
        self.telemetry = telemetry
        self.scientist_steps = scientist_steps
        self.prompt_dir = Path(prompt_dir) if prompt_dir else None

    def review(self, round_id: int) -> dict[str, object]:
        record = self.jobs.inflight()
        if record is not None and record.stage == "self_review":
            if record.round_id != round_id:
                raise InfrastructureError(
                    f"self-review checkpoint is for round {record.round_id}, "
                    f"not {round_id}"
                )
            payload = dict(record.context["payload"])
        else:
            result_dir = self.run_dir / "rounds" / f"r{round_id}" / "self_review"
            payload = {
                "lane_id": 0, "round_id": round_id, "base_sha": "",
                "run_dir": str(self.run_dir), "workspace_path": "",
                "result_dir": str(result_dir),
                "prompt_dir": str(self.prompt_dir or ""),
                "scientist_steps": self.scientist_steps, "mode": "self",
            }
        batch = self.jobs.run(
            stage="self_review", round_id=round_id,
            context={"payload": payload},
            jobs=(WorkerJob(
                "self_review", f"r{round_id}-self", payload,
                Path(str(payload["result_dir"])),
            ),),
            max_parallel=1,
        )
        try:
            if not batch.completed:
                raise InfrastructureError("self-review failed on infrastructure")
            envelope = batch.completed[0].result
            _usage(self.telemetry, envelope.usage)
            if envelope.status is WorkerStatus.FAILED:
                raise InfrastructureError(
                    envelope.error or "self-review worker failed"
                )
            result = envelope.result.get("self_review")
            if not isinstance(result, Mapping):
                raise InfrastructureError("self-review worker returned no decision")
            return dict(result)
        finally:
            self.jobs.clear()


class ScheduledViability:
    def __init__(self, *, jobs: WorkerJobs, telemetry):
        self.jobs = jobs
        self.telemetry = telemetry

    def check(
        self,
        payload: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        record = self.jobs.inflight()
        selected = (
            dict(record.context["payload"])
            if record is not None and record.stage == "viability"
            else dict(payload)
        )
        request_id = f"viability-{selected.get('base_sha') or 'candidate'}"
        batch = self.jobs.run(
            stage="viability", round_id=int(selected.get("round_id") or 0),
            context={"payload": selected},
            jobs=(WorkerJob(
                "viability", request_id, selected,
                Path(str(selected["result_dir"])),
            ),),
            max_parallel=1,
        )
        try:
            if not batch.completed:
                return None
            envelope = batch.completed[0].result
            _usage(self.telemetry, envelope.usage)
            if envelope.status is WorkerStatus.FAILED:
                return None
            return dict(envelope.result)
        finally:
            self.jobs.clear()


def _usage(telemetry, records) -> None:
    for record in records:
        telemetry.record_usage(record)
