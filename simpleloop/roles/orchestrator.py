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
    all_cards: tuple[HypothesisCard, ...] = ()  # all generator cards this lane
    proposal: ResearchProposal | None = None
    proposals: tuple[ResearchProposal, ...] = ()  # batch mode: K proposals
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

# --- Funnel architecture constants (internal, not user-configurable) ---
# Each lane's generator produces _HYPOTHESES_PER_LANE seed hypotheses
# (_SCHEDULED_OP_COUNT lenses × _IDEAS_PER_LENS ideas each).
_IDEAS_PER_LENS = 2
_HYPOTHESES_PER_LANE = _SCHEDULED_OP_COUNT * _IDEAS_PER_LENS  # 5 × 2 = 10
# Each lane's cognitive element selects _SELECT_PER_LANE for enrichment.
# K=2: one hotspot (recent improvement) + one new_direction (low coverage).
_SELECT_PER_LANE = 2
# Max concurrent lane workers.
_MAX_LANE_WORKERS = 8


def _sample_generative_ops() -> tuple[str, ...]:
    """Pick a random 5-of-9 subset of G1-G9 for one generator call."""
    return tuple(random.sample(_ALL_GENERATIVE_OPS, _SCHEDULED_OP_COUNT))


def lane_quotas(
    candidates_per_round: int, select_per_lane: int = _SELECT_PER_LANE,
) -> list[int]:
    """Derive lane count and per-lane quotas from N and K.

    N = candidates_per_round (total evals), K = select_per_lane (per lane).
    n_lanes = ceil(N / K). Quotas are [K, K, ..., K, remainder].

    E.g. N=4, K=2 → [2, 2]. N=5, K=2 → [2, 2, 1]. N=4, K=1 → [1, 1, 1, 1].

    Public so the loop can pre-compute ``n_lanes`` (to create one workspace per
    lane) without duplicating the formula; ``select_per_lane`` defaults to the
    internal funnel constant.
    """
    if select_per_lane <= 0:
        return []
    n_lanes = -(-candidates_per_round // select_per_lane)  # ceil
    base, extra = divmod(candidates_per_round, n_lanes)
    return [base + (1 if i < extra else 0) for i in range(n_lanes)]


class ProposerOrchestrator:
    """The Loop's proposer entry point. Owns the partner-lane pipeline."""

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
        self.model = model
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.command_output_cap_chars = command_output_cap_chars
        self.usage_observer = usage_observer
        self.generator = GeneratorAgent(
            model=model, runtime=runtime,
            timeout_seconds=timeout_seconds,
            max_steps=1,  # placeholder; actual budget passed per-run
            command_timeout_seconds=command_timeout_seconds,
            command_output_cap_chars=command_output_cap_chars,
            usage_observer=usage_observer,
        )
        self.proposer = ProposerAgent(
            model=model, runtime=runtime, timeout_seconds=timeout_seconds,
            max_steps=1,  # placeholder; actual budget passed per-run
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
        workspaces: list[Path],
        repo_path: Path,
        run_dir: Path,
        current_round: int,
        candidates_per_round: int,
        gate_block: str,
        prompt_dir: Path | None,
        hints: list[str] | None = None,
        gen_steps: int = 216,
        cognitive_steps: int = 148,
    ) -> ProposerResult:
        """Run N independent generator-cognitive partner lanes (funnel mode).

        Each lane's generator produces _HYPOTHESES_PER_LANE seed hypotheses;
        the cognitive element audits them in batch and selects
        _SELECT_PER_LANE for enrichment. Lane count is derived:
        n_lanes = ceil(candidates_per_round / _SELECT_PER_LANE).
        """
        started = time.monotonic()

        # --- Explore (for the Cognitive element only, not the Generator) ---
        try:
            explore = memory_service.analyze_explore(
                current_round=current_round)
        except Exception:
            explore = None

        # --- Derive lanes and quotas from N and K ---
        quotas = lane_quotas(candidates_per_round)
        n_lanes = len(quotas)
        assert len(workspaces) == n_lanes, (
            f"expected {n_lanes} lane workspace(s) (one per lane), "
            f"got {len(workspaces)}")
        mode = _Mode(n_lanes=n_lanes, max_branch_steps=cognitive_steps,
                     label="funnel")
        print(
            f"[orchestrator] lanes={n_lanes} quotas={quotas} "
            f"hypotheses_per_lane={_HYPOTHESES_PER_LANE} "
            f"ideas_per_lens={_IDEAS_PER_LENS}",
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
            workspaces=workspaces, repo_path=repo_path,
            run_dir=run_dir, current_round=current_round,
            gate_block=gate_block, prompt_dir=prompt_dir,
            hints=hints, explore=explore,
            mode=mode,
            quotas=quotas,
            gen_steps=gen_steps,
            cognitive_steps=cognitive_steps,
        )

        # --- Collect proposals in stable lane order (no selection) ---
        proposals = []
        for lr in lane_results:
            if lr.outcome != "submit":
                continue
            if lr.proposals:
                proposals.extend(lr.proposals)
            elif lr.proposal is not None:
                # Fallback for single-proposal branch results.
                proposals.append(lr.proposal)

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
        base_sha, workspaces, repo_path, run_dir, current_round,
        gate_block, prompt_dir, hints, explore, mode,
        quotas=None,
        gen_steps=216, cognitive_steps=148,
    ) -> list[LaneResult]:
        """Run one partner lane per lane state, concurrently. Each lane runs
        against its own writable workspace (``workspaces[lane.lane_id]``)."""
        shared = dict(
            gen_context=gen_context,
            goal=goal, editable=editable, frozen=frozen,
            memory_service=memory_service, base_sha=base_sha,
            repo_path=repo_path,
            run_dir=run_dir, current_round=current_round,
            gate_block=gate_block, prompt_dir=prompt_dir,
            hints=hints, explore=explore,
            max_steps=mode.max_branch_steps,
            gen_steps=gen_steps,
            cognitive_steps=cognitive_steps,
        )
        results: list[LaneResult | None] = [None] * len(lanes)
        workers = min(_MAX_LANE_WORKERS, len(lanes)) if lanes else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._run_one_lane, lane,
                    select_quota=(quotas[lane.lane_id] if quotas else 1),
                    source_path=workspaces[lane.lane_id],
                    **shared,
                ): lane.lane_id
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
        select_quota=1,
        gen_steps=216, cognitive_steps=148,
    ) -> LaneResult:
        """Run one lane: generate → cognitive (sieve + history audit + enrich).

        The generator produces _HYPOTHESES_PER_LANE seed hypotheses; the
        cognitive element audits them in batch and selects select_quota for
        enrichment (funnel mode).

        The feedback loop (feedback_generator → regenerate → re-audit) is driven
        by a ``generator_regenerate`` callback. When the Cognitive element issues
        ``feedback_generator``, the callback invokes ``generator.regenerate()``,
        enforces the ≤3 regeneration budget, and returns the new hypothesis.
        """
        # --- INITIAL_GENERATE ---
        gen_result = self.generator.run(
            context=gen_context,
            source_path=source_path, repo_path=repo_path,
            run_dir=run_dir, prompt_dir=prompt_dir,
            assigned_ops=lane.assigned_ops, max_steps=gen_steps,
            hypotheses_per_lane=_HYPOTHESES_PER_LANE,
            ideas_per_lens=_IDEAS_PER_LENS,
        )
        if not gen_result.cards:
            return LaneResult(
                lane_id=lane.lane_id, assigned_ops=lane.assigned_ops,
                outcome="error",
                abstain_reason="generator produced no card",
                deliberation_telemetry={"tool_calls": 0},
            )
        cards = gen_result.cards
        for card in cards:
            lane.hypothesis_versions.append(card)
        lane.gen_transcript.append({"role": "assistant", "content": json.dumps({
            "hypotheses": [
                {
                    "generative_op": c.generative_op,
                    "region": c.region,
                    "mechanism": c.mechanism,
                    "intervention_family": c.intervention_family,
                    "why_plausible": c.why_plausible,
                    "critical_unknown": c.critical_unknown,
                    "facts_read": list(c.facts_read),
                }
                for c in cards
            ],
        })})

        def generator_regenerate(feedback: dict):
            """Callback: handle a feedback_generator action."""
            if lane.regenerations >= _MAX_REGENERATIONS:
                raise RuntimeError(
                    f"regeneration budget exhausted (≤{_MAX_REGENERATIONS})")
            lane.regenerations += 1
            regen_result = self.generator.regenerate(
                context=gen_context, feedback=feedback,
                transcript=lane.gen_transcript,
                source_path=source_path, repo_path=repo_path,
                run_dir=run_dir, prompt_dir=prompt_dir,
                assigned_ops=lane.assigned_ops, max_steps=gen_steps,
                hypotheses_per_lane=_HYPOTHESES_PER_LANE,
                ideas_per_lens=_IDEAS_PER_LENS,
            )
            new_cards = regen_result.cards
            for nc in new_cards:
                lane.hypothesis_versions.append(nc)
            lane.gen_transcript.append({"role": "user", "content": json.dumps({
                "feedback": feedback,
            })})
            lane.gen_transcript.append({"role": "assistant",
                                        "content": json.dumps({
                "hypotheses": [
                    {
                        "generative_op": c.generative_op,
                        "region": c.region,
                        "mechanism": c.mechanism,
                        "intervention_family": c.intervention_family,
                        "why_plausible": c.why_plausible,
                        "critical_unknown": c.critical_unknown,
                        "facts_read": list(c.facts_read),
                    }
                    for c in new_cards
                ],
            })})
            return new_cards[0] if new_cards else cards[0]

        # --- COGNITIVE_RESEARCH (with feedback loop) ---
        try:
            branch = self.proposer.research_batch(
                hypotheses=cards,
                select_quota=select_quota,
                goal=goal, editable=editable, frozen=frozen,
                memory_service=memory_service, base_sha=base_sha,
                source_path=source_path, repo_path=repo_path,
                run_dir=run_dir, current_round=current_round,
                gate_block=gate_block, prompt_dir=prompt_dir,
                hints=hints, explore=explore,
                max_steps=cognitive_steps,
                generator_regenerate=generator_regenerate,
            )
        except Exception as exc:
            print(
                f"[orchestrator] lane {lane.lane_id} cognitive failed: {exc}",
                flush=True,
            )
            return LaneResult(
                lane_id=lane.lane_id, assigned_ops=lane.assigned_ops,
                hypothesis=(cards[0] if cards else None), outcome="error",
                all_cards=tuple(cards),
                abstain_reason=str(exc),
                deliberation_telemetry={"tool_calls": 0},
            )
        return LaneResult(
            lane_id=lane.lane_id, assigned_ops=lane.assigned_ops,
            hypothesis=branch.hypothesis,
            all_cards=tuple(cards),
            proposal=branch.proposal,
            proposals=branch.proposals,
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

    def run_lane_episode(
        self, *, lane_id: int, assigned_ops: tuple[str, ...] | list[str],
        workspace: Path, base_sha: str,
        goal: str, editable: list[str], frozen: list[str],
        memory_service, repo_path: Path, run_dir: Path,
        current_round: int, gate_block: str, prompt_dir: Path | None,
        hints: list[str] | None = None,
        select_quota: int = 1, gen_steps: int = 216, cognitive_steps: int = 148,
    ) -> LaneResult:
        """Run exactly ONE lane in ``workspace`` — the single-lane entry a
        proposer lane worker (HEPJob) calls.

        Builds the history-free generation context + explore report from
        ``memory_service``, constructs one ``LaneState``, and runs it through
        ``_run_one_lane``. The local ThreadPoolExecutor path calls
        ``_run_one_lane`` directly via ``_run_lanes``; this method exists so a
        remote worker (which owns one lane) can reuse the exact same per-lane
        logic without re-implementing it. No nesting: one call = one lane."""
        gen_context = memory_service.build_generation_context(
            goal=goal, editable=editable, frozen=frozen,
            base_sha=base_sha, gate_block=gate_block,
        )
        try:
            explore = memory_service.analyze_explore(current_round=current_round)
        except Exception:
            explore = None
        lane = LaneState(lane_id=lane_id, assigned_ops=tuple(assigned_ops))
        return self._run_one_lane(
            lane,
            gen_context=gen_context,
            goal=goal, editable=editable, frozen=frozen,
            memory_service=memory_service, base_sha=base_sha,
            source_path=workspace, repo_path=repo_path,
            run_dir=run_dir, current_round=current_round,
            gate_block=gate_block, prompt_dir=prompt_dir,
            hints=hints, explore=explore,
            max_steps=cognitive_steps,
            select_quota=select_quota,
            gen_steps=gen_steps, cognitive_steps=cognitive_steps,
        )

    @staticmethod
    def _lane_trace(lanes: list[LaneResult]) -> dict:
        """Per-lane observability — recorded for EVERY round."""
        return {"lanes": [
            {"lane_id": lr.lane_id,
             "assigned_ops": list(lr.assigned_ops),
             "all_cards": [
                 {"generative_op": c.generative_op,
                  "region": c.region,
                  "mechanism": c.mechanism,
                  "intervention_family": c.intervention_family}
                 for c in lr.all_cards
             ] if lr.all_cards else [],
             "sig": lr.hypothesis.signature() if lr.hypothesis else None,
             "outcome": lr.outcome,
             "proposal": bool(lr.proposal),
             "n_proposals": len(lr.proposals) if lr.proposals else (
                 1 if lr.proposal else 0),
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
