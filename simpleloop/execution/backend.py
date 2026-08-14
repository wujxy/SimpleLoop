"""Temporary Phase-5 bridge from domain operations to worker jobs.

This module prepares payloads and decodes domain results. Scheduling lifecycle
policy belongs exclusively to :mod:`simpleloop.scheduling.supervisor`.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

from ..candidate import (
    CandidateBatchRequest,
    CandidatePlan,
    CandidateResult,
)
from ..persistence.artifacts import decode_candidate_result
from ..persistence.journal import JobJournal, JournalRecord
from ..scheduling.contracts import (
    InfrastructureError,
    JobBatchRequest,
    JobSpec,
    ResourceSpec,
    RetryPolicy,
)
from ..scheduling.envelope import WorkerStatus
from ..stages.evaluator import BaselineAcceptanceError, validate_baseline
from ..stages.gate import GateSpec
from ..stages.proposer import (
    Abstention,
    Proposal,
    ProposalBatch,
    ProposerRequest,
    decode_lane_proposals,
)
from ..world import SourceWorkspace, WorkspaceSpec


InfraRoundError = InfrastructureError


class WorkerBackend:
    def __init__(self, ctx, *, scheduler, supervisor):
        self.ctx = ctx
        self.scheduler = scheduler
        self.supervisor = supervisor
        self.run_dir = Path(ctx.run_dir)
        self.journal = JobJournal(self.run_dir / "inflight.json")

    def inflight(self) -> JournalRecord | None:
        return self.journal.load()

    def clear_inflight(self) -> None:
        self.journal.clear()

    def run_candidates(
        self,
        request: CandidateBatchRequest,
        *,
        journal=None,
    ) -> tuple[CandidateResult, ...]:
        record = self.journal.load()
        if record is not None and record.stage == "candidates":
            payloads = _payload_list(record.context)
        else:
            payloads = []
            created = []
            try:
                for plan in request.candidates:
                    workspace = self.ctx.workspace.create(WorkspaceSpec(
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
                        "prompt_dir": str(getattr(self.ctx, "prompt_dir", None) or ""),
                    })
            except Exception:
                for workspace in created:
                    self.ctx.workspace.remove(workspace)
                raise

        jobs = tuple(
            self._job("candidate", f"r{request.round_id}-c{payload['candidate_id']}", payload)
            for payload in payloads
        )
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
        batch = self.supervisor.run_batch(
            JobBatchRequest(
                "candidates",
                request.round_id,
                record.context if record is not None else context,
                jobs,
                max(1, min(int(self.ctx.cfg.get("max_workers", 1)), len(jobs) or 1)),
            ),
            scheduler=self.scheduler,
            journal=self.journal,
        )
        results = []
        for outcome in batch.completed:
            self._record_usage(outcome.result.usage)
            if outcome.result.status is WorkerStatus.FAILED:
                continue
            results.append(decode_candidate_result(outcome.result.result))
        if not results and jobs:
            failures = "; ".join(
                outcome.infrastructure_error or outcome.result.error or "worker failed"
                for outcome in batch.outcomes
            )
            raise InfrastructureError(
                f"round {request.round_id}: all candidate jobs failed on "
                f"infrastructure: {failures}"
            )
        return tuple(sorted(results, key=lambda item: item.candidate_id))

    def resume_round(
        self,
        jobs_payload,
        *,
        round_id: int,
        parent_sha: str,
        journal=None,
    ) -> tuple[CandidateResult, ...]:
        record = self.journal.load()
        if record is None or record.stage != "candidates":
            raise InfrastructureError("no candidate stage is available to resume")
        plans = tuple(
            CandidatePlan(
                int(payload["candidate_id"]),
                str(payload["parent_sha"]),
                Proposal(
                    str(payload["proposal"]),
                    tuple(str(ref) for ref in payload.get("evidence_refs") or ()),
                ),
            )
            for payload in _payload_list(record.context)
        )
        return self.run_candidates(CandidateBatchRequest(round_id, plans))

    def run_proposer_lanes(self, request: ProposerRequest) -> ProposalBatch:
        record = self.journal.load()
        if record is not None and record.stage == "proposer":
            payload = dict(record.context["payload"])
        else:
            workspace = self.ctx.workspace.create_lane(0, request.incumbent_sha)
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
                "prompt_dir": str(getattr(self.ctx, "prompt_dir", None) or ""),
                "proposal_slots": int(self.ctx.cfg.get("candidates_per_round", 1)),
                "scientist_steps": int(self.ctx.cfg.get("scientist_steps", 200)),
            }
        job = self._job(
            "proposer", f"r{request.round_id}-l0", payload,
            lane=True,
        )
        batch = self.supervisor.run_batch(
            JobBatchRequest(
                "proposer", request.round_id, {"payload": payload}, (job,), 1,
            ),
            scheduler=self.scheduler,
            journal=self.journal,
        )
        completed = batch.completed
        if not completed:
            raise InfrastructureError(
                f"round {request.round_id}: proposer failed on infrastructure"
            )
        outcome = completed[0]
        self._record_usage(outcome.result.usage)
        raw = outcome.result.result
        proposals = decode_lane_proposals(raw.get("proposals") or ())
        result = ProposalBatch(
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
        self.journal.clear()
        return result

    def run_self_review(self, *, round_id: int) -> dict:
        record = self.journal.load()
        if record is not None and record.stage == "self_review":
            payload = dict(record.context["payload"])
        else:
            result_dir = self.run_dir / "rounds" / f"r{round_id}" / "self_review"
            payload = {
                "lane_id": 0,
                "round_id": round_id,
                "base_sha": "",
                "run_dir": str(self.run_dir),
                "workspace_path": "",
                "result_dir": str(result_dir),
                "prompt_dir": str(getattr(self.ctx, "prompt_dir", None) or ""),
                "scientist_steps": int(self.ctx.cfg.get("scientist_steps", 200)),
                "mode": "self",
            }
        job = self._job("self_review", f"r{round_id}-self", payload)
        batch = self.supervisor.run_batch(
            JobBatchRequest(
                "self_review", round_id, {"payload": payload}, (job,), 1,
            ),
            scheduler=self.scheduler,
            journal=self.journal,
        )
        if not batch.completed:
            raise InfrastructureError("self-review failed on infrastructure")
        outcome = batch.completed[0]
        self._record_usage(outcome.result.usage)
        self.journal.clear()
        result = outcome.result.result.get("self_review")
        if not isinstance(result, Mapping):
            raise InfrastructureError("self-review worker returned no decision")
        return dict(result)

    def run_viability(
        self,
        payload: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        record = self.journal.load()
        selected = (
            dict(record.context["payload"])
            if record is not None and record.stage == "viability"
            else dict(payload)
        )
        job = self._job(
            "viability",
            f"viability-{selected.get('base_sha') or 'candidate'}",
            selected,
            release_workspace=False,
        )
        batch = self.supervisor.run_batch(
            JobBatchRequest(
                "viability", int(selected.get("round_id") or 0),
                {"payload": selected}, (job,), 1,
            ),
            scheduler=self.scheduler,
            journal=self.journal,
        )
        self.journal.clear()
        if not batch.completed:
            return None
        outcome = batch.completed[0]
        self._record_usage(outcome.result.usage)
        if outcome.result.status is WorkerStatus.FAILED:
            return None
        return dict(outcome.result.result)

    def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
        workspace = self.ctx.workspace.create(WorkspaceSpec("baseline", baseline_sha))
        result_dir = self.run_dir / "baseline"
        payload = {
            "round_id": -1,
            "candidate_id": -1,
            "parent_sha": baseline_sha,
            "proposal": "baseline evaluation",
            "run_dir": str(self.run_dir),
            "worktree_id": workspace.workspace_id,
            "worktree_path": str(workspace.path),
            "result_dir": str(result_dir),
            "prompt_dir": "",
            "mode": "baseline",
        }
        job = self._job("candidate", "baseline", payload)
        temporary = JobJournal(result_dir / "inflight.json")
        batch = self.supervisor.run_batch(
            JobBatchRequest("baseline", -1, {}, (job,), 1),
            scheduler=self.scheduler,
            journal=temporary,
        )
        temporary.clear()
        if not batch.completed:
            raise BaselineAcceptanceError("baseline job failed on infrastructure")
        outcome = batch.completed[0]
        self._record_usage(outcome.result.usage)
        result = decode_candidate_result(outcome.result.result)
        if result.evaluation is None:
            raise BaselineAcceptanceError("baseline result has no evaluation")
        schema = self.ctx.cfg.get("metrics") or {}
        objective = schema.get("objective") or {}
        validate_baseline(result.evaluation, GateSpec(
            str(objective.get("key") or "OBJECTIVE"),
            tuple(str(item["key"]) for item in schema.get("gates") or ()),
        ))
        return result.evaluation.text, dict(result.metrics)

    def _job(
        self,
        kind: str,
        request_id: str,
        payload: Mapping[str, object],
        *,
        lane: bool = False,
        release_workspace: bool = True,
    ) -> JobSpec:
        result_dir = Path(str(payload["result_dir"]))
        result_path = result_dir / "result.json"
        workspace = None
        workspace_path = str(payload.get("workspace_path") or "")
        if workspace_path and release_workspace:
            workspace = SourceWorkspace(
                str(payload.get("workspace_id") or payload.get("worktree_id") or request_id),
                Path(workspace_path),
                str(payload.get("base_sha") or payload.get("parent_sha") or ""),
            )
        cfg = self.ctx.cfg.get("hepjob") or {}
        python = (
            str(cfg.get("python_executable") or sys.executable)
            if self.ctx.cfg.get("execution_backend") == "hepjob"
            else sys.executable
        )
        retry = RetryPolicy(
            int(cfg.get("max_attempts", 2)),
            int(cfg.get("run_timeout_seconds", 21600)),
            int(cfg.get("disappearance_grace_seconds", 120)),
        )
        return JobSpec(
            request=_worker_request(kind, request_id, payload, result_path),
            manifest_path=result_dir / "manifest.json",
            result_path=result_path,
            stdout_path=result_dir / "job.out",
            stderr_path=result_dir / "job.err",
            argv=(
                python, "-m", "simpleloop.scheduling.worker",
                "--manifest", str(result_dir / "manifest.json"),
            ),
            retry=retry,
            resources=ResourceSpec(
                int(cfg.get("cpus", 1)),
                int(cfg.get("memory_mb", 0)),
                _requirements(self.ctx.cfg),
            ),
            workspace=workspace,
        )

    def _record_usage(self, records) -> None:
        telemetry = getattr(self.ctx, "telemetry", None)
        if telemetry is None:
            return
        for record in records:
            telemetry.record_usage(record)


def _worker_request(kind, request_id, payload, result_path):
    from ..scheduling.envelope import WorkerRequest
    return WorkerRequest(kind, request_id, dict(payload), result_path)


def _payload_list(context: Mapping[str, object]) -> list[dict]:
    payloads = context.get("payloads")
    if not isinstance(payloads, (list, tuple)) or not all(
        isinstance(payload, Mapping) for payload in payloads
    ):
        raise InfrastructureError("candidate journal has no valid payloads")
    return [dict(payload) for payload in payloads]


def _requirements(cfg: Mapping[str, object]) -> str | None:
    hep = cfg.get("hepjob") or {}
    parts = []
    cpu_model = hep.get("cpu_model")
    if cpu_model:
        from ..config import _CPU_MODEL_REQUIREMENTS
        parts.append(_CPU_MODEL_REQUIREMENTS[str(cpu_model)])
    if hep.get("machine_constraint"):
        parts.append(str(hep["machine_constraint"]))
    return " && ".join(parts) or None
