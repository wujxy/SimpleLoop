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

from ..container.runtime import ApptainerRuntime, MountMap
from .model import ChatModel
from .proposer import (
    ContextPolicy,
    ProposerError,
    ProposerResult,
    SCIENTIST_PROMPT_VERSION,
    ScientistAgent,
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
            return LaneResult(
                lane_id=lane_id, outcome="error", abstain_reason=str(exc),
                deliberation_telemetry={"tool_calls": 0},
            )
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
