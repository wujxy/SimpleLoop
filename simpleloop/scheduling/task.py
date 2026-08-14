"""Typed task-domain adapters over the generic worker-job client."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from ..candidate import (
    CandidateBatchRequest, CandidateBatchResult, CandidateResult,
)
from ..persistence.artifacts import decode_candidate_result
from ..stages.evaluator import BaselineAcceptanceError, validate_baseline
from ..stages.gate import GateSpec
from ..stages.proposer import (
    Abstention, Proposal, ProposalBatch, ProposerRequest, decode_lane_proposals,
)
from ..world import WorkspaceSpec
from .contracts import InfrastructureError
from .envelope import WorkerStatus
from .jobs import WorkerJob, WorkerJobs


@dataclass(frozen=True)
class BaselineRequest:
    sha: str


@dataclass(frozen=True)
class BaselineResult:
    text: str
    metrics: Mapping[str, object]


class ScheduledCandidates:
    def __init__(
        self, *, run_dir, workspace, jobs: WorkerJobs, telemetry,
        max_parallel: int, prompt_dir,
    ):
        self.run_dir = Path(run_dir)
        self.workspace = workspace
        self.jobs = jobs
        self.telemetry = telemetry
        self.max_parallel = max_parallel
        self.prompt_dir = Path(prompt_dir) if prompt_dir else None

    def run(self, request: CandidateBatchRequest) -> CandidateBatchResult:
        record = self.jobs.inflight()
        if record is not None and record.stage == "candidates":
            if record.round_id != request.round_id:
                raise InfrastructureError(
                    f"candidate checkpoint is for round {record.round_id}, "
                    f"not {request.round_id}"
                )
            payloads = _payloads(record.context)
            transition = None
            context = record.context
        else:
            payloads = []
            created = []
            try:
                for plan in request.candidates:
                    workspace = self.workspace.create(WorkspaceSpec(
                        f"{request.round_id}-c{plan.candidate_id}",
                        plan.parent_sha,
                    ))
                    created.append(workspace)
                    result_dir = (
                        self.run_dir / "rounds" / f"r{request.round_id}"
                        / "candidates" / f"c{plan.candidate_id}"
                    )
                    payloads.append({
                        "round_id": request.round_id,
                        "candidate_id": plan.candidate_id,
                        "parent_sha": plan.parent_sha,
                        "proposal": plan.proposal.instruction,
                        "evidence_refs": list(plan.proposal.evidence_refs),
                        "run_dir": str(self.run_dir),
                        "worktree_id": workspace.workspace_id,
                        "worktree_path": str(workspace.path),
                        "result_dir": str(result_dir),
                        "prompt_dir": str(self.prompt_dir or ""),
                    })
            except Exception:
                for workspace in created:
                    self.workspace.remove(workspace)
                raise
            context = {
                "parent_sha": (
                    request.candidates[0].parent_sha if request.candidates else ""
                ),
                "proposals": [
                    {
                        "instruction": plan.proposal.instruction,
                        "evidence_refs": list(plan.proposal.evidence_refs),
                    }
                    for plan in request.candidates
                ],
                "payloads": payloads,
            }
            transition = (
                "proposer"
                if record is not None and record.stage == "proposer" else None
            )
        batch = self.jobs.run(
            stage="candidates",
            round_id=request.round_id,
            context=context,
            jobs=tuple(_candidate_job(payload) for payload in payloads),
            max_parallel=max(1, min(self.max_parallel, len(payloads) or 1)),
            transition_from=transition,
        )
        results = []
        for outcome in batch.completed:
            envelope = outcome.result
            _record_usage(self.telemetry, envelope.usage)
            if envelope.status is WorkerStatus.FAILED:
                continue
            result = decode_candidate_result(envelope.result)
            _record_usage(self.telemetry, result.usage)
            results.append(replace(
                result,
                usage=(),
                telemetry=self.telemetry.snapshot(persist=True),
            ))
        if not results and payloads:
            raise InfrastructureError(
                f"round {request.round_id}: all candidate workers failed: "
                + _failure_text(batch.outcomes)
            )
        return CandidateBatchResult(
            tuple(sorted(results, key=lambda item: item.candidate_id)),
            self.telemetry.snapshot(persist=True),
        )


class ScheduledProposer:
    def __init__(
        self, *, run_dir, workspace, jobs: WorkerJobs, telemetry,
        proposal_slots: int, scientist_steps: int, prompt_dir,
    ):
        self.run_dir = Path(run_dir)
        self.workspace = workspace
        self.jobs = jobs
        self.telemetry = telemetry
        self.proposal_slots = proposal_slots
        self.scientist_steps = scientist_steps
        self.prompt_dir = Path(prompt_dir) if prompt_dir else None

    def propose(self, request: ProposerRequest) -> ProposalBatch:
        record = self.jobs.inflight()
        if record is not None and record.stage == "candidates":
            if record.round_id != request.round_id:
                raise InfrastructureError(
                    f"candidate checkpoint is for round {record.round_id}, "
                    f"not {request.round_id}"
                )
            proposals = decode_lane_proposals(record.context.get("proposals") or ())
            return ProposalBatch(proposals)
        if record is not None and record.stage == "proposer":
            if record.round_id != request.round_id:
                raise InfrastructureError(
                    f"proposer checkpoint is for round {record.round_id}, "
                    f"not {request.round_id}"
                )
            payload = dict(record.context["payload"])
        else:
            workspace = self.workspace.create_lane(0, request.incumbent_sha)
            result_dir = (
                self.run_dir / "rounds" / f"r{request.round_id}"
                / "lanes" / "l0"
            )
            payload = {
                "lane_id": 0,
                "round_id": request.round_id,
                "base_sha": request.incumbent_sha,
                "run_dir": str(self.run_dir),
                "workspace_id": workspace.workspace_id,
                "workspace_path": str(workspace.path),
                "result_dir": str(result_dir),
                "prompt_dir": str(self.prompt_dir or ""),
                "proposal_slots": self.proposal_slots,
                "scientist_steps": self.scientist_steps,
            }
        result_dir = Path(str(payload["result_dir"]))
        workspace = _payload_workspace(payload)
        batch = self.jobs.run(
            stage="proposer",
            round_id=request.round_id,
            context={"payload": payload},
            jobs=(WorkerJob(
                "proposer", f"r{request.round_id}-l0", payload,
                result_dir, workspace,
            ),),
            max_parallel=1,
        )
        if not batch.completed:
            raise InfrastructureError(
                f"round {request.round_id}: proposer failed: "
                + _failure_text(batch.outcomes)
            )
        envelope = batch.completed[0].result
        _record_usage(self.telemetry, envelope.usage)
        if envelope.status is WorkerStatus.FAILED:
            raise InfrastructureError(envelope.error or "proposer worker failed")
        raw = envelope.result
        proposals = decode_lane_proposals(raw.get("proposals") or ())
        return ProposalBatch(
            proposals,
            Abstention("all lanes abstained/blocked/errored") if not proposals else None,
            telemetry={"lanes": [{
                "lane_id": 0, "telemetry": raw.get("telemetry") or {},
            }]},
            trace={"lanes": [{
                "lane_id": 0,
                "outcome": raw.get("outcome"),
                "n_proposals": len(proposals),
                "reason_kind": raw.get("reason_kind"),
            }]},
        )


class ScheduledBaseline:
    def __init__(
        self, *, run_dir, workspace, jobs: WorkerJobs, telemetry,
        metrics_schema: Mapping[str, object],
    ):
        self.run_dir = Path(run_dir)
        self.workspace = workspace
        self.jobs = jobs
        self.telemetry = telemetry
        self.metrics_schema = metrics_schema

    def evaluate(self, request: BaselineRequest) -> BaselineResult:
        workspace = self.workspace.create(WorkspaceSpec("baseline", request.sha))
        result_dir = self.run_dir / "baseline"
        payload = {
            "round_id": -1, "candidate_id": -1,
            "parent_sha": request.sha, "proposal": "baseline evaluation",
            "run_dir": str(self.run_dir), "worktree_id": workspace.workspace_id,
            "worktree_path": str(workspace.path),
            "result_dir": str(result_dir), "prompt_dir": "", "mode": "baseline",
        }
        try:
            batch = self.jobs.run(
                stage="baseline", round_id=-1, context={},
                jobs=(WorkerJob("candidate", "baseline", payload, result_dir, workspace),),
                max_parallel=1,
            )
        finally:
            self.jobs.clear()
        if not batch.completed:
            raise BaselineAcceptanceError("baseline job failed on infrastructure")
        envelope = batch.completed[0].result
        _record_usage(self.telemetry, envelope.usage)
        result = decode_candidate_result(envelope.result)
        if result.evaluation is None:
            raise BaselineAcceptanceError("baseline result has no evaluation")
        objective = self.metrics_schema.get("objective") or {}
        validate_baseline(result.evaluation, GateSpec(
            str(objective.get("key") or "OBJECTIVE"),
            tuple(
                str(item["key"])
                for item in self.metrics_schema.get("gates") or ()
            ),
        ))
        return BaselineResult(result.evaluation.text, dict(result.metrics))


def _candidate_job(payload: Mapping[str, object]) -> WorkerJob:
    return WorkerJob(
        "candidate",
        f"r{payload['round_id']}-c{payload['candidate_id']}",
        payload,
        Path(str(payload["result_dir"])),
        _payload_workspace(payload),
    )


def _payload_workspace(payload: Mapping[str, object]):
    from ..world import SourceWorkspace
    path = payload.get("workspace_path") or payload.get("worktree_path")
    if not path:
        raise InfrastructureError("worker payload has no workspace path")
    return SourceWorkspace(
        str(payload.get("workspace_id") or payload.get("worktree_id") or "worker"),
        Path(str(path)),
        str(payload.get("base_sha") or payload.get("parent_sha") or ""),
    )


def _payloads(context: Mapping[str, object]) -> list[dict[str, object]]:
    payloads = context.get("payloads")
    if not isinstance(payloads, (list, tuple)) or not all(
        isinstance(payload, Mapping) for payload in payloads
    ):
        raise InfrastructureError("candidate checkpoint has no valid payloads")
    return [dict(payload) for payload in payloads]


def _record_usage(telemetry, records) -> None:
    for record in records:
        telemetry.record_usage(record)


def _failure_text(outcomes) -> str:
    return "; ".join(
        outcome.infrastructure_error
        or (outcome.result.error if outcome.result is not None else None)
        or "worker failed"
        for outcome in outcomes
    )
