"""Typed Reflection adapter over the unified worker-job client.

Mirrors ScheduledSelfReviewer (scheduling/rsi.py) for job submission and
ScheduledProposer (scheduling/task.py) for the task-workspace lane: a
reflection round runs in a lane worktree of the CURRENT accepted revision so
the reflecting Scientist reads the same research world a task round would.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ..reflection import ReflectionRecord
from .contracts import InfrastructureError
from .jobs import WorkerJob, WorkerJobs
from .rsi import _one, _same_round, _usage
from .task import _payload_workspace


class ScheduledReflector:
    def __init__(
        self, *, run_dir, workspace, jobs: WorkerJobs, telemetry,
        scientist_steps: int, prompt_dir,
    ):
        self.run_dir = Path(run_dir)
        self.workspace = workspace
        self.jobs = jobs
        self.telemetry = telemetry
        self.scientist_steps = scientist_steps
        self.prompt_dir = Path(prompt_dir) if prompt_dir else None

    def reflect(self, round_id: int, incumbent_sha: str) -> ReflectionRecord:
        record = self.jobs.inflight()
        if record is not None and record.stage == "reflection":
            _same_round(record, round_id, "reflection")
            payload = dict(record.context["payload"])
        else:
            lane = self.workspace.create_lane(
                f"reflect-r{round_id}", incumbent_sha)
            result_dir = (
                self.run_dir / "rounds" / f"r{round_id}" / "reflection"
            )
            payload = {
                "lane_id": 0,
                "round_id": round_id,
                "base_sha": incumbent_sha,
                "run_dir": str(self.run_dir),
                "workspace_id": lane.workspace_id,
                "workspace_path": str(lane.path),
                "result_dir": str(result_dir),
                "prompt_dir": str(self.prompt_dir or ""),
                "proposal_slots": 1,
                "scientist_steps": self.scientist_steps,
                "mode": "reflection",
                "self_repo": "",
            }
        # The lane worktree is a task provider workspace — pass it to the
        # WorkerJob so the supervisor releases it after the round (same as a
        # proposer lane; unlike the self-edit worktrees, which the RSI body
        # store owns).
        envelope = _one(
            self.jobs,
            stage="reflection",
            round_id=round_id,
            context={"payload": payload},
            job=WorkerJob(
                "reflection",
                f"r{round_id}-reflection",
                payload,
                Path(str(payload["result_dir"])),
                _payload_workspace(payload),
            ),
            transition_from=None,
        )
        _usage(self.telemetry, envelope.usage)
        raw = envelope.result.get("reflection")
        if not isinstance(raw, Mapping):
            raise InfrastructureError(
                "reflection worker returned no handoff")
        return ReflectionRecord(
            int(raw.get("round_id") or round_id),
            str(raw.get("handoff") or ""),
            bool(raw.get("self_limitation_suspected")),
            bool(raw.get("abstained")),
            str(raw.get("note") or ""),
        )
