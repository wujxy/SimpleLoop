"""Concurrent multi-lane orchestration for the Scientist-Proposer."""
from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .generative import GENERATIVE_OPS
from .model import ChatModel
from .proposer import ProposerAgent, ProposerResult, ScientistResult
from ..container.runtime import ApptainerRuntime
from ..memory.models import ResearchProposal


@dataclass(frozen=True)
class _Mode:
    n_lanes: int
    scientist_steps: int
    label: str = "scientist"


@dataclass(frozen=True)
class LaneState:
    lane_id: int
    assigned_ops: tuple[str, ...]


@dataclass(frozen=True)
class LaneResult:
    lane_id: int
    assigned_ops: tuple[str, ...] = ()
    proposals: tuple[ResearchProposal, ...] = ()
    outcome: str = "research_incomplete"
    reason: str | None = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


_SCHEDULED_OP_COUNT = 5
_SELECT_PER_LANE = 2
_MAX_LANE_WORKERS = 8


def _sample_generative_ops(rng: random.Random) -> tuple[str, ...]:
    return tuple(rng.sample(GENERATIVE_OPS, _SCHEDULED_OP_COUNT))


def _lane_quotas(candidates_per_round: int, select_per_lane: int) -> list[int]:
    if candidates_per_round < 1 or select_per_lane < 1:
        return []
    n_lanes = -(-candidates_per_round // select_per_lane)
    base, extra = divmod(candidates_per_round, n_lanes)
    return [base + (1 if index < extra else 0) for index in range(n_lanes)]


class ProposerOrchestrator:
    """Run independent Scientist lanes and collect committed proposals."""

    def __init__(
        self,
        *,
        model: ChatModel,
        runtime: ApptainerRuntime,
        timeout_seconds: int,
        command_timeout_seconds: int,
        command_output_cap_chars: int,
        usage_observer=None,
    ):
        self._proposer_kwargs = {
            "model": model, "runtime": runtime,
            "timeout_seconds": timeout_seconds, "max_steps": 1,
            "command_timeout_seconds": command_timeout_seconds,
            "command_output_cap_chars": command_output_cap_chars,
            "usage_observer": usage_observer,
        }

    def _new_proposer(self) -> ProposerAgent:
        return ProposerAgent(**self._proposer_kwargs)

    def run(
        self,
        *,
        goal: str,
        editable: list[str],
        frozen: list[str],
        memory_service,
        base_sha: str,
        source_path: Path,
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        candidates_per_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        hints: list[str] | None = None,
        scientist_steps: int = 364,
        random_seed: int | None = None,
    ) -> ProposerResult:
        started = time.monotonic()
        rng = random.Random(random_seed)
        quotas = _lane_quotas(candidates_per_round, _SELECT_PER_LANE)
        lanes = [
            LaneState(index, _sample_generative_ops(rng))
            for index in range(len(quotas))
        ]
        mode = _Mode(len(lanes), scientist_steps)
        print(
            f"[orchestrator] scientist lanes={len(lanes)} quotas={quotas} "
            f"steps={scientist_steps}",
            flush=True,
        )
        lane_results = self._run_lanes(
            lanes,
            quotas=quotas,
            scientist_steps=scientist_steps,
            goal=goal,
            editable=editable,
            frozen=frozen,
            memory_service=memory_service,
            base_sha=base_sha,
            source_path=source_path,
            repo_path=repo_path,
            run_dir=run_dir,
            current_round=current_round,
            gate_block=gate_block,
            prompt_dir=prompt_dir,
            hints=hints,
        )
        proposals = [
            proposal
            for lane in lane_results if lane.outcome == "proposals"
            for proposal in lane.proposals
        ]
        elapsed = time.monotonic() - started
        telemetry = self._telemetry(lane_results, mode, elapsed)
        trace = self._lane_trace(lane_results)
        if not proposals:
            return self._abstain(
                "all Scientist lanes ended without a committed proposal",
                telemetry=telemetry,
                trace=trace,
            )
        return ProposerResult(
            proposals=proposals,
            usage=None,
            deliberation_telemetry=telemetry,
            trace=trace,
        )

    def _run_lanes(
        self,
        lanes: list[LaneState],
        *,
        quotas: list[int],
        scientist_steps: int,
        **shared,
    ) -> list[LaneResult]:
        results: list[LaneResult | None] = [None] * len(lanes)
        workers = min(_MAX_LANE_WORKERS, len(lanes)) if lanes else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._run_one_lane,
                    lane,
                    select_quota=quotas[lane.lane_id],
                    scientist_steps=scientist_steps,
                    **shared,
                ): lane.lane_id
                for lane in lanes
            }
            for future in as_completed(futures):
                lane_id = futures[future]
                try:
                    results[lane_id] = future.result()
                except Exception as exc:
                    print(
                        f"[orchestrator] lane {lane_id} failed: {exc}",
                        flush=True,
                    )
                    results[lane_id] = LaneResult(
                        lane_id=lane_id,
                        assigned_ops=lanes[lane_id].assigned_ops,
                        outcome="error",
                        reason=str(exc),
                    )
        return [result for result in results if result is not None]

    def _run_one_lane(
        self,
        lane: LaneState,
        *,
        select_quota: int,
        scientist_steps: int,
        **shared,
    ) -> LaneResult:
        result: ScientistResult = self._new_proposer().run_lane(
            assigned_ops=lane.assigned_ops,
            select_quota=select_quota,
            scientist_steps=scientist_steps,
            **shared,
        )
        return LaneResult(
            lane_id=lane.lane_id,
            assigned_ops=lane.assigned_ops,
            proposals=result.proposals,
            outcome=result.outcome,
            reason=result.reason,
            deliberation_telemetry=result.deliberation_telemetry,
            trace=result.trace,
        )

    @staticmethod
    def _lane_trace(lanes: list[LaneResult]) -> dict:
        return {"lanes": [{
            "lane_id": lane.lane_id,
            "assigned_generative_ops": list(lane.assigned_ops),
            "outcome": lane.outcome,
            "reason": lane.reason,
            "n_proposals": len(lane.proposals),
            **lane.trace,
        } for lane in lanes]}

    @staticmethod
    def _telemetry(
        lanes: list[LaneResult], mode: _Mode, elapsed: float,
    ) -> dict:
        return {
            "mode": mode.label,
            "n_lanes": len(lanes),
            "scientist_steps": mode.scientist_steps,
            "n_proposals": sum(len(lane.proposals) for lane in lanes),
            "n_blocked": sum(lane.outcome == "block" for lane in lanes),
            "n_research_incomplete": sum(
                lane.outcome == "research_incomplete" for lane in lanes
            ),
            "n_error": sum(lane.outcome == "error" for lane in lanes),
            "elapsed_seconds": round(elapsed, 1),
            "lane_telemetry": [lane.deliberation_telemetry for lane in lanes],
        }

    @staticmethod
    def _abstain(
        reason: str,
        *,
        telemetry: dict,
        trace: dict,
    ) -> ProposerResult:
        print(f"[orchestrator] abstained: {reason}", flush=True)
        return ProposerResult(
            proposals=[],
            abstained=True,
            abstain_reason=reason,
            deliberation_telemetry=telemetry,
            trace=trace,
        )
