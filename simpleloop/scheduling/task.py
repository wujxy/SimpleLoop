"""Typed task-domain adapters over the generic worker-job client."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from ..candidate import (
    NOT_PERFORMED_STATUSES, CandidateBatchRequest, CandidateBatchResult,
    CandidateResult, parse_stop_cause,
)
from ..persistence.artifacts import decode_candidate_result
from ..stages.evaluator import BaselineAcceptanceError, validate_baseline
from ..stages.gate import GateSpec
from ..stages.proposer import (
    Abstention, Proposal, ProposalBatch, ProposerRequest, decode_lane_proposals,
)
from ..world import WorkspaceSpec
from .contracts import InfrastructureError
from .envelope import ProtocolError, WorkerStatus, read_result
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
            payloads, healed = self._heal_dead_sessions(
                request.round_id, payloads,
            )
            if healed:
                # Dead-session outcomes were purged (and their released
                # worktrees resurrected): restart the journal so the batch
                # genuinely re-runs instead of replaying collected results.
                context = {
                    "parent_sha": record.context.get("parent_sha") or (
                        payloads[0]["parent_sha"] if payloads else ""
                    ),
                    "proposals": list(record.context.get("proposals") or ()),
                    "payloads": payloads,
                }
                self.jobs.restart(
                    stage="candidates",
                    round_id=request.round_id,
                    context=context,
                    request_ids=tuple(
                        f"r{payload['round_id']}-c{payload['candidate_id']}"
                        for payload in payloads
                    ),
                )
            else:
                context = record.context
            transition = None
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
        if results and all(
            result.status in NOT_PERFORMED_STATUSES for result in results
        ):
            # Fairness for infrastructure deaths (run-004 r2): a batch in
            # which EVERY experimenter session died (crash/quota/timeout,
            # no SELF_REPORT) performed no experiments — committing it would
            # spend the round and its proposals on an outage. Fail loudly
            # instead: the round is not consumed, and the resume re-runs the
            # executors with the same proposals (_heal_dead_sessions purges
            # the dead results and resurrects the released worktrees). A
            # batch with ANY performed outcome (even NO_CHANGE) is a real
            # research round and commits as before.
            causes = "; ".join(
                f"c{result.candidate_id}:"
                f"{parse_stop_cause(result.execution.reason)}"
                for result in results
            )
            raise InfrastructureError(
                f"round {request.round_id}: every experimenter session died "
                f"without performing the intervention ({causes}); "
                "round not consumed — resume retries the executors with "
                "the same proposals"
            )
        return CandidateBatchResult(
            tuple(sorted(results, key=lambda item: item.candidate_id)),
            self.telemetry.snapshot(persist=True),
        )

    def _heal_dead_sessions(
        self, round_id: int, payloads: list[dict],
    ) -> tuple[list[dict], bool]:
        """Invalidate persisted outcomes of experimenter sessions that died.

        A dead session's result (status in NOT_PERFORMED_STATUSES) is not a
        reusable outcome, and its worktree was released after collection.
        Purge those result files, resurrect missing worktrees (deterministic
        ids, same construction as the fresh path), and report whether any
        healing happened so the caller can restart the journal.
        """
        healed = False
        for payload in payloads:
            result_path = Path(str(payload["result_dir"])) / "result.json"
            if not result_path.exists():
                continue
            if not _is_dead_session_result(result_path):
                continue  # a performed outcome is a usable result: keep it
            result_path.unlink()
            healed = True
            if not Path(str(payload.get("worktree_path") or "")).is_dir():
                workspace = self.workspace.create(WorkspaceSpec(
                    f"{round_id}-c{payload['candidate_id']}",
                    str(payload["parent_sha"]),
                ))
                payload["worktree_id"] = workspace.workspace_id
                payload["worktree_path"] = str(workspace.path)
        return payloads, healed


class ScheduledProposer:
    def __init__(
        self, *, run_dir, workspace, jobs: WorkerJobs, telemetry,
        proposal_slots: int, scientist_steps: int, prompt_dir, self_repo=None,
    ):
        self.run_dir = Path(run_dir)
        self.workspace = workspace
        self.jobs = jobs
        self.telemetry = telemetry
        self.proposal_slots = proposal_slots
        self.scientist_steps = scientist_steps
        self.prompt_dir = Path(prompt_dir) if prompt_dir else None
        self.self_repo = Path(self_repo) if self_repo else None

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
                "self_repo": str(self.self_repo or ""),
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
        if str(raw.get("outcome") or "") == "error":
            # Protocol/worker failure is INFRASTRUCTURE, not research: it
            # must fail the round loudly (no history row, round id not
            # consumed, resumed run retries the same round) instead of being
            # absorbed as an empty research round.
            raise InfrastructureError(
                f"round {request.round_id}: proposer protocol failure: "
                + str(raw.get("abstain_reason") or "unknown error")
            )
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


def _is_dead_session_result(result_path: Path) -> bool:
    """True when a persisted candidate result records a session that died
    without performing the intervention (or is unreadable garbage — that can
    never be a reusable outcome either)."""
    try:
        envelope = read_result(result_path)
        result = decode_candidate_result(envelope.result)
    except ProtocolError:
        return True
    return result.status in NOT_PERFORMED_STATUSES


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
