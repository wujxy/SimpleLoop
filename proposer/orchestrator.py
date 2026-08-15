"""ProposerOrchestrator: the lane adapter between the Loop and the Scientist.

One proposer lane = one persistent Scientist. The orchestrator owns *no*
research logic: it loads/resumes the Scientist session, runs one research
round, persists the session, and maps the outcome to the ``LaneResult`` /
``ProposerResult`` shapes the Loop and execution backends consume.

The Generator→Cognitive pipeline, hypothesis cards, and the feedback loop are
gone. Idea content, judgment, and research direction all live in the Scientist
(``roles/proposer.py``); this module is intentionally a thin adapter.
``LaneResult`` and ``ProposerResult`` shapes are preserved so loop.py and the
HEPJob collector need no modification — that shape is the interface firewall of
this refactor.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .runtime import ApptainerRuntime, MountMap
from .model import ChatModel
from .scientist import (
    ContextPolicy,
    ProposerError,
    ProposerResult,
    ReflectionResult,
    SCIENTIST_PROMPT_VERSION,
    SelfReviewResult,
    ScientistAgent,
    _SELF_REVIEW_DEFAULT_DEFER,
)
from .scientist_session import ScientistSession


@dataclass(frozen=True)
class LaneResult:
    """One lane's outcome. ``outcome`` is one of submit | abstain | error."""

    lane_id: int
    proposals: tuple = ()  # tuple[ResearchProposal, ...]
    outcome: str = "submit"
    reason_kind: str | None = None
    explanation: str | None = None
    abstain_reason: str | None = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


def _safe_save_meta(
    session: ScientistSession, round_id: int, base_sha: str
) -> None:
    try:
        session.save_meta(round_id=round_id, base_sha=base_sha)
    except Exception as exc:  # session IO must not fail the round
        print(f"[orchestrator] session save_meta failed: {exc}", flush=True)


def _commit_proposals_safe(memory_service, round_id: int, result) -> None:
    """Record the round's proposal→finding intent (refs) into the proposer's
    own findings archive. The proposer owns finding allocation + association;
    outcomes are never written here (derived at read time). No-op on abstain
    or when there is no memory_service (static/test paths)."""
    if memory_service is None:
        return
    if getattr(result, "abstained", False) or not result.proposals:
        return
    try:
        memory_service.commit_proposals(
            round_id=round_id, proposals=result.proposals,
        )
    except Exception as exc:  # findings IO must not fail the round
        print(f"[orchestrator] commit_proposals failed: {exc}", flush=True)


class ProposerOrchestrator:
    """The Loop's proposer entry point. Owns the Scientist session lifecycle."""

    def __init__(
        self,
        *,
        model: ChatModel,
        runtime: ApptainerRuntime,
        timeout_seconds: int,
        command_timeout_seconds: int,
        command_output_cap_chars: int,
        usage_observer=None,
        context_policy: ContextPolicy | None = None,
    ):
        self.model = model
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.command_output_cap_chars = command_output_cap_chars
        self.usage_observer = usage_observer
        self.scientist = ScientistAgent(
            model=model, runtime=runtime,
            timeout_seconds=timeout_seconds,
            max_steps=1,  # placeholder; the real budget is passed per-run
            command_timeout_seconds=command_timeout_seconds,
            command_output_cap_chars=command_output_cap_chars,
            usage_observer=usage_observer,
            context_policy=context_policy,
        )

    def run(
        self,
        *,
        goal: str,
        editable: list[str],
        frozen: list[str],
        world_mount: MountMap,
        memory_service,
        base_sha: str,
        workspaces: list[Path],
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        candidates_per_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        hints: list[str] | None = None,
        scientist_steps: int = 200,
    ) -> ProposerResult:
        """Run the single Scientist lane for the round (local backend path).

        ``frozen`` is accepted for call-site compatibility and ignored — the
        read-only world is mount-enforced (EROFS outside editable).
        """
        assert len(workspaces) == 1, (
            "expected exactly one lane workspace, "
            f"got {len(workspaces)}"
        )
        started = time.monotonic()
        print(
            f"[orchestrator] scientist lane=0 proposal_slots="
            f"{candidates_per_round} steps={scientist_steps}",
            flush=True,
        )
        try:
            lane_result = self._run_lane(
                lane_id=0, workspace=workspaces[0], base_sha=base_sha,
                goal=goal, editable=editable, world_mount=world_mount,
                memory_service=memory_service, repo_path=repo_path,
                run_dir=run_dir, current_round=current_round,
                gate_block=gate_block, prompt_dir=prompt_dir, hints=hints,
                proposal_slots=candidates_per_round,
                scientist_steps=scientist_steps,
            )
        except Exception as exc:  # session load itself failed
            print(f"[orchestrator] scientist lane 0 failed: {exc}", flush=True)
            lane_result = LaneResult(
                lane_id=0, outcome="error", abstain_reason=str(exc),
                deliberation_telemetry={"tool_calls": 0},
            )
        self._log_lane_result(lane_result)
        elapsed = time.monotonic() - started

        proposals = list(lane_result.proposals)
        if not proposals:
            return self._abstain(
                lane_result.abstain_reason
                or "the Scientist submitted no directions",
                telemetry=self._telemetry([lane_result], elapsed),
                trace=self._lane_trace([lane_result]),
            )
        print(
            f"[orchestrator] {len(proposals)} proposal(s) in {elapsed:.1f}s",
            flush=True,
        )
        return ProposerResult(
            proposals=proposals,
            usage=None,
            abstained=False,
            deliberation_telemetry=self._telemetry([lane_result], elapsed),
            trace=self._lane_trace([lane_result]),
        )

    def run_lane_episode(
        self,
        *,
        lane_id: int,
        workspace: Path,
        base_sha: str,
        goal: str,
        editable: list[str],
        frozen: list[str],
        world_mount: MountMap,
        memory_service,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        hints: list[str] | None = None,
        proposal_slots: int = 1,
        scientist_steps: int = 200,
    ) -> LaneResult:
        """Run exactly ONE Scientist lane in ``workspace`` — the single-lane
        entry a proposer lane worker (HEPJob) calls."""
        return self._run_lane(
            lane_id=lane_id, workspace=workspace, base_sha=base_sha,
            goal=goal, editable=editable, world_mount=world_mount,
            memory_service=memory_service, repo_path=repo_path,
            run_dir=run_dir, current_round=current_round,
            gate_block=gate_block, prompt_dir=prompt_dir, hints=hints,
            proposal_slots=proposal_slots, scientist_steps=scientist_steps,
        )

    def _run_lane(
        self,
        *,
        lane_id: int,
        workspace: Path,
        base_sha: str,
        goal: str,
        editable: list[str],
        world_mount: MountMap,
        memory_service,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        hints: list[str] | None,
        proposal_slots: int,
        scientist_steps: int,
    ) -> LaneResult:
        """Load/resume the resident Scientist, run one round, persist session."""
        session = ScientistSession.load_or_create(
            run_dir, lane_id, prompt_version=SCIENTIST_PROMPT_VERSION,
        )
        try:
            result = self.scientist.research(
                goal=goal, editable=editable, world_mount=world_mount,
                memory_service=memory_service, base_sha=base_sha,
                source_path=workspace, repo_path=repo_path, run_dir=run_dir,
                current_round=current_round, gate_block=gate_block,
                prompt_dir=prompt_dir, proposal_slots=proposal_slots,
                hints=hints, session=session, max_steps=scientist_steps,
            )
        except (ProposerError, Exception) as exc:
            print(
                f"[orchestrator] scientist lane {lane_id} research failed: "
                f"{exc}",
                flush=True,
            )
            _safe_save_meta(session, current_round, base_sha)
            # The deliberation loop attaches its trace (incl. the model's
            # last raw reply) to the exception on protocol death — keep it
            # as the lane's trace so the cause is durable in the artifacts.
            return LaneResult(
                lane_id=lane_id, outcome="error", abstain_reason=str(exc),
                deliberation_telemetry={"tool_calls": 0},
                trace=getattr(exc, "proposer_trace", None) or {},
            )
        _commit_proposals_safe(memory_service, current_round, result)
        _safe_save_meta(session, current_round, base_sha)
        outcome = "abstain" if result.abstained else "submit"
        return LaneResult(
            lane_id=lane_id,
            proposals=tuple(result.proposals),
            outcome=outcome,
            abstain_reason=result.abstain_reason,
            deliberation_telemetry=result.deliberation_telemetry,
            trace=result.trace,
        )

    def run_self_review(
        self, *,
        self_repo: Path,
        run_dir: Path,
        reviews_path: Path,
        incumbent_self_sha: str,
        goal: str,
        objective_key: str | None,
        current_round: int,
        prompt_dir: Path | None,
        memory_service=None,
        scientist_steps: int = 200,
    ) -> SelfReviewResult:
        """Run ONE self-review (RSI S3c): the resident Scientist studies itself as
        the research system and emits a KEEP/CHANGE decision. Mirrors
        ``run_lane_episode`` for the self-review path. CHANGE records intent only
        — the self-executor (S3d) does HOW."""
        return self._run_self_review(
            self_repo=self_repo, run_dir=run_dir, reviews_path=reviews_path,
            incumbent_self_sha=incumbent_self_sha, goal=goal,
            objective_key=objective_key, current_round=current_round,
            prompt_dir=prompt_dir, memory_service=memory_service,
            scientist_steps=scientist_steps,
        )

    def _run_self_review(
        self, *,
        self_repo: Path,
        run_dir: Path,
        reviews_path: Path,
        incumbent_self_sha: str,
        goal: str,
        objective_key: str | None,
        current_round: int,
        prompt_dir: Path | None,
        memory_service,
        scientist_steps: int,
    ) -> SelfReviewResult:
        """Load/resume the resident Scientist, run one self-review, persist the
        session. Same session continuity as a task round (the Scientist's
        autobiography persists across task AND self rounds — semantics §9/§20)."""
        session = ScientistSession.load_or_create(
            run_dir, 0, prompt_version=SCIENTIST_PROMPT_VERSION,
        )
        try:
            result = self.scientist.self_review(
                goal=goal, self_repo=self_repo, run_dir=run_dir,
                reviews_path=reviews_path, incumbent_self_sha=incumbent_self_sha,
                objective_key=objective_key, current_round=current_round,
                prompt_dir=prompt_dir, session=session,
                memory_service=memory_service, max_steps=scientist_steps,
            )
        except (ProposerError, Exception) as exc:
            print(f"[orchestrator] self-review failed: {exc}", flush=True)
            _safe_save_meta(session, current_round, incumbent_self_sha)
            # Crash → KEEP with a short defer so self-attention re-opens soon,
            # not never.
            return SelfReviewResult(
                decision="KEEP",
                diagnosis=f"self-review crashed before a decision: {exc}",
                keep_reason="self-review did not complete; defaulting to KEEP "
                            "pending a successful review",
                next_review_after_rounds=_SELF_REVIEW_DEFAULT_DEFER,
                abstained=True,
            )
        _safe_save_meta(session, current_round, incumbent_self_sha)
        return result

    def run_reflection(
        self, *,
        goal: str,
        editable: list[str],
        world_mount: MountMap,
        memory_service,
        base_sha: str,
        workspace: Path,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        prompt_dir: Path | None,
        scientist_steps: int = 200,
    ) -> ReflectionResult:
        """Run ONE Reflection round: the resident Scientist audits its recent
        research trajectory in the research world and emits a handoff. Mirrors
        ``run_self_review``'s session continuity (the autobiography persists
        across task, self, AND reflection rounds).

        Exceptions are NOT converted into a fabricated result (unlike
        self-review's KEEP crash-default, which protects the authoritative
        reviews stream): a failed reflection is infrastructure — the worker
        envelope reports FAILED and the supervisor retries it. Nothing here
        writes the reflection log; the Host owns that."""
        session = ScientistSession.load_or_create(
            run_dir, 0, prompt_version=SCIENTIST_PROMPT_VERSION,
        )
        try:
            result = self.scientist.reflection(
                goal=goal, editable=editable, world_mount=world_mount,
                memory_service=memory_service, base_sha=base_sha,
                source_path=workspace, repo_path=repo_path, run_dir=run_dir,
                current_round=current_round, prompt_dir=prompt_dir,
                session=session, max_steps=scientist_steps,
            )
        except Exception as exc:
            _safe_save_meta(session, current_round, base_sha)
            raise
        _safe_save_meta(session, current_round, base_sha)
        return result

    @staticmethod
    def _log_lane_result(lr: LaneResult) -> None:
        if lr.proposals:
            print(
                f"[orchestrator] lane {lr.lane_id} → {len(lr.proposals)} "
                f"proposal(s)",
                flush=True,
            )
        elif lr.outcome == "abstain":
            print(
                f"[orchestrator] lane {lr.lane_id} → abstained: "
                f"{lr.abstain_reason}",
                flush=True,
            )
        elif lr.outcome == "error":
            print(f"[orchestrator] lane {lr.lane_id} → error", flush=True)

    def _abstain(
        self, reason: str, *, telemetry: dict | None = None,
        trace: dict | None = None,
    ) -> ProposerResult:
        print(f"[orchestrator] abstained: {reason}", flush=True)
        return ProposerResult(
            proposals=[],
            abstained=True,
            abstain_reason=reason,
            deliberation_telemetry=telemetry or {},
            trace=trace or {},
        )

    def _telemetry(self, lanes: list[LaneResult], elapsed: float) -> dict:
        return {
            "n_lanes": len(lanes),
            "n_proposals": sum(len(lr.proposals) for lr in lanes),
            "n_abstain": sum(1 for lr in lanes if lr.outcome == "abstain"),
            "n_error": sum(1 for lr in lanes if lr.outcome == "error"),
            "elapsed_seconds": round(elapsed, 1),
            "lane_telemetry": [lr.deliberation_telemetry for lr in lanes],
        }

    @staticmethod
    def _lane_trace(lanes: list[LaneResult]) -> dict:
        """Per-lane observability — recorded for every round."""
        return {"lanes": [
            {
                "lane_id": lr.lane_id,
                "outcome": lr.outcome,
                "n_proposals": len(lr.proposals),
                "reason_kind": lr.reason_kind,
                "abstain_reason": lr.abstain_reason,
                "tool_calls": lr.deliberation_telemetry.get("tool_calls", 0),
            }
            for lr in lanes
        ]}
