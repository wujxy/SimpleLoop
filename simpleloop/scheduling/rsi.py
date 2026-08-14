"""Typed RSI adapters over the unified worker-job client."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping

from ..rsi.models import (
    SelfChange,
    SelfDecision,
    SelfDecisionKind,
    SelfEditRequest,
    SelfEditResult,
    SelfReviewRequest,
    ViabilityRequest,
    ViabilityResult,
)
from ..world import SourceWorkspace
from .contracts import InfrastructureError
from .envelope import WorkerStatus
from .jobs import WorkerJob, WorkerJobs


class ScheduledSelfReviewer:
    def __init__(
        self, *, run_dir, jobs: WorkerJobs, telemetry,
        scientist_steps: int, prompt_dir,
    ):
        self.run_dir = Path(run_dir)
        self.jobs = jobs
        self.telemetry = telemetry
        self.scientist_steps = scientist_steps
        self.prompt_dir = Path(prompt_dir) if prompt_dir else None

    def review(self, request: SelfReviewRequest) -> SelfDecision:
        record = self.jobs.inflight()
        if record is not None and record.stage == "self_review":
            _same_round(record, request.round_id, "self-review")
            payload = dict(record.context["payload"])
        else:
            result_dir = (
                self.run_dir / "rounds" / f"r{request.round_id}" / "self_review"
            )
            payload = {
                "lane_id": 0,
                "round_id": request.round_id,
                "base_sha": request.incumbent_sha,
                "run_dir": str(self.run_dir),
                "workspace_path": "",
                "result_dir": str(result_dir),
                "prompt_dir": str(self.prompt_dir or ""),
                "scientist_steps": self.scientist_steps,
                "mode": "self",
                "self_repo": str(request.self_repo),
                "reviews_path": str(request.reviews_path),
                "incumbent_self_sha": request.incumbent_sha,
            }
        envelope = _one(
            self.jobs,
            stage="self_review",
            round_id=request.round_id,
            context={"payload": payload},
            job=WorkerJob(
                "self_review",
                f"r{request.round_id}-self",
                payload,
                Path(str(payload["result_dir"])),
            ),
            transition_from=None,
        )
        _usage(self.telemetry, envelope.usage)
        raw = envelope.result.get("self_review")
        if not isinstance(raw, Mapping):
            raise InfrastructureError("self-review worker returned no decision")
        try:
            return _decode_decision(raw)
        except (TypeError, ValueError) as exc:
            raise InfrastructureError(f"invalid self-review decision: {exc}") from exc


class ScheduledSelfEditor:
    def __init__(
        self, *, run_dir, jobs: WorkerJobs, telemetry, prompt_dir,
    ):
        self.run_dir = Path(run_dir)
        self.jobs = jobs
        self.telemetry = telemetry
        self.prompt_dir = Path(prompt_dir) if prompt_dir else None

    def edit(self, request: SelfEditRequest) -> SelfEditResult:
        record = self.jobs.inflight()
        if record is not None and record.stage == "self_edit":
            _same_round(record, request.round_id, "self-edit")
            payload = dict(record.context["payload"])
            transition = None
        else:
            payload = {
                "round_id": request.round_id,
                "run_dir": str(self.run_dir),
                "worktree_path": str(request.workspace.path),
                "result_dir": str(
                    self.run_dir / "rounds" / f"r{request.round_id}" / "self_edit"
                ),
                "prompt_dir": str(self.prompt_dir or ""),
                "change": _encode_change(request.change),
            }
            transition = (
                "self_review"
                if record is not None and record.stage == "self_review"
                else None
            )
        workspace = SourceWorkspace(
            request.workspace.workspace_id,
            Path(str(payload["worktree_path"])),
            request.workspace.base_sha,
        )
        envelope = _one(
            self.jobs,
            stage="self_edit",
            round_id=request.round_id,
            context={"payload": payload},
            job=WorkerJob(
                "self_edit",
                f"r{request.round_id}-self-edit",
                payload,
                Path(str(payload["result_dir"])),
                workspace,
            ),
            transition_from=transition,
        )
        _usage(self.telemetry, envelope.usage)
        raw = envelope.result.get("self_edit")
        if not isinstance(raw, Mapping):
            raise InfrastructureError("self-edit worker returned no result")
        return SelfEditResult(
            str(raw.get("status") or "EDITOR_FAILED"),
            str(raw.get("output") or ""),
            str(raw.get("reason") or ""),
        )


class ScheduledViabilityChecker:
    def __init__(
        self, *, run_dir, jobs: WorkerJobs, telemetry, scientist_steps: int = 20,
    ):
        self.run_dir = Path(run_dir)
        self.jobs = jobs
        self.telemetry = telemetry
        self.scientist_steps = scientist_steps

    def check(self, request: ViabilityRequest) -> ViabilityResult:
        record = self.jobs.inflight()
        if record is not None and record.stage == "viability":
            _same_round(record, request.round_id, "viability")
            payload = dict(record.context["payload"])
            transition = None
        else:
            smoke_run = (
                self.run_dir / "self" / "viability" / f"r{request.round_id}"
            )
            smoke_run.mkdir(parents=True, exist_ok=True)
            resolved = self.run_dir / "config.resolved.json"
            if not resolved.is_file():
                return ViabilityResult(False, "config.resolved.json is missing")
            shutil.copy2(resolved, smoke_run / "config.resolved.json")
            result_dir = smoke_run / "result"
            payload = {
                "lane_id": 0,
                "round_id": request.round_id,
                "base_sha": request.candidate_sha,
                "run_dir": str(smoke_run),
                "workspace_path": str(request.self_repo),
                "result_dir": str(result_dir),
                "prompt_dir": "",
                "proposal_slots": 1,
                "scientist_steps": self.scientist_steps,
                "attempt": 1,
                "mode": "task",
                "self_repo": str(request.self_repo),
            }
            transition = (
                "self_edit"
                if record is not None and record.stage == "self_edit"
                else None
            )
        workspace = SourceWorkspace(
            f"viability-{request.round_id}",
            Path(str(payload["workspace_path"])),
            request.candidate_sha,
        )
        envelope = _one(
            self.jobs,
            stage="viability",
            round_id=request.round_id,
            context={"payload": payload},
            job=WorkerJob(
                "viability",
                f"r{request.round_id}-viability",
                payload,
                Path(str(payload["result_dir"])),
                workspace,
            ),
            transition_from=transition,
        )
        _usage(self.telemetry, envelope.usage)
        raw = envelope.result
        status = raw.get("status")
        if status == "COMPLETED":
            return ViabilityResult(
                True,
                f"smoke COMPLETED (outcome={raw.get('outcome')}, "
                f"n_proposals={len(raw.get('proposals') or [])})",
            )
        return ViabilityResult(
            False,
            f"smoke status={status}: "
            f"{raw.get('explanation') or raw.get('abstain_reason') or ''}",
        )


# Phase-5 names remain absent intentionally; app cuts over in Task 6.


def _one(
    jobs,
    *,
    stage: str,
    round_id: int,
    context,
    job,
    transition_from,
):
    batch = jobs.run(
        stage=stage,
        round_id=round_id,
        context=context,
        jobs=(job,),
        max_parallel=1,
        transition_from=transition_from,
    )
    if not batch.completed:
        raise InfrastructureError(f"{stage} failed on infrastructure")
    envelope = batch.completed[0].result
    if envelope.status is WorkerStatus.FAILED:
        raise InfrastructureError(envelope.error or f"{stage} worker failed")
    return envelope


def _same_round(record, round_id: int, label: str) -> None:
    if record.round_id != round_id:
        raise InfrastructureError(
            f"{label} checkpoint is for round {record.round_id}, not {round_id}"
        )


def _decode_decision(raw: Mapping[str, object]) -> SelfDecision:
    kind = SelfDecisionKind(str(raw.get("decision") or ""))
    change = raw.get("self_change")
    return SelfDecision(
        kind,
        str(raw.get("diagnosis") or ""),
        keep_reason=(
            str(raw["keep_reason"]) if raw.get("keep_reason") is not None else None
        ),
        change=(
            SelfChange(
                str(change.get("target") or ""),
                str(change.get("intent") or ""),
                str(change.get("instruction") or ""),
                tuple(str(item) for item in change.get("evidence_refs") or ()),
            )
            if isinstance(change, Mapping) else None
        ),
        next_review_after_rounds=(
            int(raw["next_review_after_rounds"])
            if raw.get("next_review_after_rounds") is not None else None
        ),
    )


def _encode_change(change: SelfChange) -> dict[str, object]:
    return {
        "target": change.target,
        "intent": change.intent,
        "instruction": change.instruction,
        "evidence_refs": list(change.evidence_refs),
    }


def _usage(telemetry, records) -> None:
    for record in records:
        telemetry.record_usage(record)
