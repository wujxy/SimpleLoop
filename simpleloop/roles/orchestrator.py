"""ProposerOrchestrator: the partner-lane entry point for the Loop.

Runs ``candidates_per_round`` independent 1:1 generator-cognitive lanes. Each
lane binds one Generator to one Cognitive element for its full lifetime:

  (1) Generator → one hypothesis card (history-free, free explorer)
  (2) Cognitive element → sieve + history audit + enrich → submit | block
      ↕ feedback_generator (≤3 regenerations): cognitive feeds history back
        to the Generator, which regenerates; cognitive re-audits

The orchestrator is intentionally ignorant of idea content: it creates lanes,
samples 5 ops per lane, runs them concurrently, and collects results in stable
lane order. No dedup, no frozen prefilter, no cap, no ranking.

The ProposerResult interface is unchanged so loop.py needs no modification.
"""
from __future__ import annotations

import json
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .model import ChatModel
from .hypothesis import HypothesisCard
from .generator import GeneratorAgent
from .proposer import ProposerAgent, ProposerResult, BranchResult
from ..explore.models import ExploreReport
from ..container.runtime import ApptainerRuntime
from ..memory.models import ResearchProposal


@dataclass
class _Mode:
    """Breadth/depth allocation for one round."""
    n_lanes: int
    max_branch_steps: int
    label: str


@dataclass
class LaneState:
    """Per-lane mutable state. Lives only in this round's orchestrator run."""
    lane_id: int
    assigned_ops: tuple[str, ...]
    hypothesis_versions: list[HypothesisCard] = field(default_factory=list)
    regenerations: int = 0
    gen_transcript: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class LaneResult:
    """One lane's outcome. ``outcome`` is one of submit/block/abstain/error."""
    lane_id: int
    assigned_ops: tuple[str, ...] = ()
    hypothesis: HypothesisCard | None = None
    proposal: ResearchProposal | None = None
    outcome: str = "submit"
    reason_kind: str | None = None
    explanation: str | None = None
    block_evidence_refs: tuple[str, ...] = ()
    enrichment_partial: bool = False
    abstain_reason: str | None = None
    deliberation_telemetry: dict = field(default_factory=dict)
    trace: dict = field(default_factory=dict)


# The generative basis (G1-G9) the prompt defines. The scheduler draws a random
# 5-of-9 subset per lane so identical context does not collapse every draw onto
# the same entry-point angle.
_ALL_GENERATIVE_OPS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9")
_SCHEDULED_OP_COUNT = 5

# Max regenerations per lane (cognitive feeds history back to generator).
_MAX_REGENERATIONS = 3

# Step budget for the Generator agent (it scans code + spots a direction).
_GEN_STEPS = 12

# Step budget for the Cognitive element (sieve + history audit + enrich).
_COGNITIVE_STEPS = 36


def _sample_generative_ops() -> tuple[str, ...]:
    """Pick a random 5-of-9 subset of G1-G9 for one generator call."""
    return tuple(random.sample(_ALL_GENERATIVE_OPS, _SCHEDULED_OP_COUNT))


def _select_mode(
    *, first_round: bool, n_experiments: int, max_steps: int,
    candidates_per_round: int,
    branch_steps: int | None = None,
) -> _Mode:
    """Adaptive breadth/depth.

    First round / no history → depth-first (fewer lanes, deeper branches).
    Later rounds → breadth-first (full candidates_per_round lanes).
    """
    if first_round or n_experiments == 0:
        n = min(3, candidates_per_round)
        steps = branch_steps if branch_steps else max(5, max_steps - 6)
        return _Mode(n_lanes=n, max_branch_steps=steps,
                     label="depth-first")
    n = candidates_per_round
    steps = (branch_steps if branch_steps
             else max(5, (max_steps - 6) // min(candidates_per_round, n)))
    return _Mode(n_lanes=n, max_branch_steps=steps,
                 label="breadth-first")


class ProposerOrchestrator:
    """The Loop's proposer entry point. Owns the partner-lane pipeline."""

    def __init__(
        self,
        *,
        model: ChatModel,
        runtime: ApptainerRuntime,
        timeout_seconds: int,
        max_steps: int,
        command_timeout_seconds: int,
        command_output_cap_chars: int,
        branch_steps: int | None = None,
        usage_observer=None,
    ):
        self.model = model
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self.max_steps = max_steps
        self.command_timeout_seconds = command_timeout_seconds
        self.command_output_cap_chars = command_output_cap_chars
        self.branch_steps = branch_steps
        self.usage_observer = usage_observer
        self.generator = GeneratorAgent(
            model=model, runtime=runtime,
            timeout_seconds=timeout_seconds,
            max_steps=max_steps,
            command_timeout_seconds=command_timeout_seconds,
            command_output_cap_chars=command_output_cap_chars,
            usage_observer=usage_observer,
        )
        self.proposer = ProposerAgent(
            model=model, runtime=runtime, timeout_seconds=timeout_seconds,
            max_steps=max_steps,
            command_timeout_seconds=command_timeout_seconds,
            command_output_cap_chars=command_output_cap_chars,
            usage_observer=usage_observer,
        )

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
    ) -> ProposerResult:
        """Run N independent 1:1 generator-cognitive partner lanes."""
        started = time.monotonic()

        # --- Explore (for the Cognitive element only, not the Generator) ---
        try:
            explore = memory_service.analyze_explore(
                current_round=current_round)
        except Exception:
            explore = None
        experiments = memory_service.load_experiments()
        first_round = explore is None or explore.first_round

        mode = _select_mode(
            first_round=first_round, n_experiments=len(experiments),
            max_steps=self.max_steps,
            candidates_per_round=candidates_per_round,
            branch_steps=self.branch_steps,
        )
        n_lanes = mode.n_lanes
        print(
            f"[orchestrator] mode={mode.label} "
            f"lanes={n_lanes} "
            f"branch_steps={mode.max_branch_steps}",
            flush=True,
        )

        # --- History-free context for the Generator ---
        gen_context = memory_service.build_generation_context(
            goal=goal, editable=editable, frozen=frozen,
            base_sha=base_sha, gate_block=gate_block,
        )

        # --- Build lane states ---
        lanes = [
            LaneState(lane_id=i, assigned_ops=_sample_generative_ops())
            for i in range(n_lanes)
        ]

        # --- Run all lanes concurrently ---
        lane_results = self._run_lanes(
            lanes,
            gen_context=gen_context,
            goal=goal, editable=editable, frozen=frozen,
            memory_service=memory_service, base_sha=base_sha,
            source_path=source_path, repo_path=repo_path,
            run_dir=run_dir, current_round=current_round,
            gate_block=gate_block, prompt_dir=prompt_dir,
            hints=hints, explore=explore,
            mode=mode,
        )

        # --- Collect proposals in stable lane order (no selection) ---
        proposals = [
            lr.proposal for lr in lane_results
            if lr.outcome == "submit" and lr.proposal is not None
        ]

        elapsed = time.monotonic() - started
        if not proposals:
            return self._abstain(
                "all lanes blocked, errored, or abstained",
                telemetry=self._telemetry(lane_results, mode, elapsed),
                trace=self._lane_trace(lane_results),
            )
        print(
            f"[orchestrator] {len(proposals)} proposal(s) in {elapsed:.1f}s",
            flush=True,
        )
        return ProposerResult(
            proposals=proposals,
            usage=None,
            deliberation_telemetry=self._telemetry(
                lane_results, mode, elapsed),
            trace=self._lane_trace(lane_results),
        )

    def _run_lanes(
        self, lanes, *, gen_context, goal, editable, frozen, memory_service,
        base_sha, source_path, repo_path, run_dir, current_round,
        gate_block, prompt_dir, hints, explore, mode,
    ) -> list[LaneResult]:
        """Run one partner lane per lane state, concurrently."""
        shared = dict(
            gen_context=gen_context,
            goal=goal, editable=editable, frozen=frozen,
            memory_service=memory_service, base_sha=base_sha,
            source_path=source_path, repo_path=repo_path,
            run_dir=run_dir, current_round=current_round,
            gate_block=gate_block, prompt_dir=prompt_dir,
            hints=hints, explore=explore,
            max_steps=mode.max_branch_steps,
        )
        results: list[LaneResult | None] = [None] * len(lanes)
        workers = min(8, len(lanes)) if lanes else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._run_one_lane, lane, **shared): lane.lane_id
                for lane in lanes
            }
            for future in as_completed(futures):
                i = futures[future]
                try:
                    results[i] = future.result()
                except Exception as exc:
                    print(
                        f"[orchestrator] lane {i} failed: {exc}", flush=True,
                    )
                    results[i] = LaneResult(
                        lane_id=i, outcome="error",
                        abstain_reason=str(exc),
                        deliberation_telemetry={"tool_calls": 0},
                    )
        for lr in results:
            if lr is not None:
                self._log_lane_result(lr)
        return [r for r in results if r is not None]

    def _run_one_lane(
        self, lane: LaneState, *, gen_context, goal, editable, frozen,
        memory_service, base_sha, source_path, repo_path, run_dir,
        current_round, gate_block, prompt_dir, hints, explore, max_steps,
    ) -> LaneResult:
        """Run one lane: generate → cognitive (sieve + history audit + enrich).

        The feedback loop (feedback_generator → regenerate → re-audit) is driven
        by a ``generator_regenerate`` callback passed to ``research_branch``.
        When the Cognitive element issues ``feedback_generator``, the callback
        invokes ``generator.regenerate()``, enforces the ≤3 regeneration budget,
        and returns the new hypothesis. The Cognitive transcript stays alive
        across regenerations.
        """
        # --- INITIAL_GENERATE ---
        gen_result = self.generator.run(
            context=gen_context,
            source_path=source_path, repo_path=repo_path,
            run_dir=run_dir, prompt_dir=prompt_dir,
            assigned_ops=lane.assigned_ops, max_steps=_GEN_STEPS,
        )
        if not gen_result.cards:
            return LaneResult(
                lane_id=lane.lane_id, assigned_ops=lane.assigned_ops,
                outcome="error",
                abstain_reason="generator produced no card",
                deliberation_telemetry={"tool_calls": 0},
            )
        card = gen_result.cards[0]
        lane.hypothesis_versions.append(card)
        lane.gen_transcript.append({"role": "assistant", "content": json.dumps({
            "hypothesis": {
                "generative_op": card.generative_op,
                "region": card.region,
                "mechanism": card.mechanism,
                "intervention_family": card.intervention_family,
                "why_plausible": card.why_plausible,
                "critical_unknown": card.critical_unknown,
                "facts_read": list(card.facts_read),
            },
        })})

        def generator_regenerate(feedback: dict):
            """Callback for research_branch: handle a feedback_generator action."""
            if lane.regenerations >= _MAX_REGENERATIONS:
                raise RuntimeError(
                    f"regeneration budget exhausted (≤{_MAX_REGENERATIONS})")
            lane.regenerations += 1
            regen_result = self.generator.regenerate(
                context=gen_context, feedback=feedback,
                transcript=lane.gen_transcript,
                source_path=source_path, repo_path=repo_path,
                run_dir=run_dir, prompt_dir=prompt_dir,
                assigned_ops=lane.assigned_ops, max_steps=_GEN_STEPS,
            )
            new_card = regen_result.cards[0]
            lane.hypothesis_versions.append(new_card)
            lane.gen_transcript.append({"role": "user", "content": json.dumps({
                "feedback": feedback,
            })})
            lane.gen_transcript.append({"role": "assistant",
                                        "content": json.dumps({
                "hypothesis": {
                    "generative_op": new_card.generative_op,
                    "region": new_card.region,
                    "mechanism": new_card.mechanism,
                    "intervention_family":
                        new_card.intervention_family,
                    "why_plausible": new_card.why_plausible,
                    "critical_unknown": new_card.critical_unknown,
                    "facts_read": list(new_card.facts_read),
                },
            })})
            return new_card

        # --- COGNITIVE_RESEARCH (with feedback loop) ---
        try:
            branch = self.proposer.research_branch(
                hypothesis=card,
                goal=goal, editable=editable, frozen=frozen,
                memory_service=memory_service, base_sha=base_sha,
                source_path=source_path, repo_path=repo_path,
                run_dir=run_dir, current_round=current_round,
                gate_block=gate_block, prompt_dir=prompt_dir,
                hints=hints, explore=explore, max_steps=_COGNITIVE_STEPS,
                generator_regenerate=generator_regenerate,
            )
        except Exception as exc:
            print(
                f"[orchestrator] lane {lane.lane_id} cognitive failed: {exc}",
                flush=True,
            )
            return LaneResult(
                lane_id=lane.lane_id, assigned_ops=lane.assigned_ops,
                hypothesis=card, outcome="error",
                abstain_reason=str(exc),
                deliberation_telemetry={"tool_calls": 0},
            )
        return LaneResult(
            lane_id=lane.lane_id, assigned_ops=lane.assigned_ops,
            hypothesis=branch.hypothesis, proposal=branch.proposal,
            outcome=branch.outcome,
            reason_kind=branch.reason_kind,
            explanation=branch.explanation,
            block_evidence_refs=branch.block_evidence_refs,
            enrichment_partial=branch.enrichment_partial,
            abstain_reason=(branch.explanation
                            if branch.outcome == "abstain" else None),
            deliberation_telemetry=branch.deliberation_telemetry,
            trace=branch.trace,
        )

    @staticmethod
    def _lane_trace(lanes: list[LaneResult]) -> dict:
        """Per-lane observability — recorded for EVERY round."""
        return {"lanes": [
            {"lane_id": lr.lane_id,
             "assigned_ops": list(lr.assigned_ops),
             "sig": lr.hypothesis.signature() if lr.hypothesis else None,
             "outcome": lr.outcome,
             "proposal": bool(lr.proposal),
             "instruction": (lr.proposal.instruction
                             if lr.proposal is not None else None),
             "reason_kind": lr.reason_kind,
             "explanation": lr.explanation,
             "evidence_refs": list(lr.block_evidence_refs),
             "tool_calls": lr.deliberation_telemetry.get("tool_calls", 0),
             "partial": bool(lr.enrichment_partial)}
            for lr in lanes
        ]}

    @staticmethod
    def _log_lane_result(lr: LaneResult) -> None:
        if lr.proposal:
            tag = "partial submit" if lr.enrichment_partial else "proposal"
            print(
                f"[orchestrator] lane {lr.lane_id} → {tag}", flush=True,
            )
        elif lr.outcome == "block":
            print(
                f"[orchestrator] lane {lr.lane_id} → blocked: "
                f"{lr.reason_kind}", flush=True,
            )
        elif lr.outcome == "error":
            print(
                f"[orchestrator] lane {lr.lane_id} → error", flush=True,
            )

    def _abstain(self, reason: str, *, telemetry: dict | None = None,
                 trace: dict | None = None) -> ProposerResult:
        print(f"[orchestrator] abstained: {reason}", flush=True)
        return ProposerResult(
            proposals=[],
            abstained=True,
            abstain_reason=reason,
            deliberation_telemetry=telemetry or {},
            trace=trace or {},
        )

    def _telemetry(self, lanes: list[LaneResult], mode: _Mode,
                   elapsed: float) -> dict:
        return {
            "mode": mode.label,
            "n_lanes": len(lanes),
            "n_proposals": sum(
                1 for lr in lanes
                if lr.proposal and not lr.enrichment_partial),
            "n_partial": sum(1 for lr in lanes if lr.enrichment_partial),
            "n_blocked": sum(1 for lr in lanes if lr.outcome == "block"),
            "n_error": sum(1 for lr in lanes if lr.outcome == "error"),
            "elapsed_seconds": round(elapsed, 1),
            "lane_telemetry": [
                lr.deliberation_telemetry for lr in lanes
            ],
        }
