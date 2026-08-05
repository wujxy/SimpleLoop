"""ProposerOrchestrator: the branch-then-deepen entry point for the Loop.

Replaces ProposerAgent.run as the Loop's proposer. It runs the pipeline:

  (1) Generator → N hypothesis cards (wide, evidence-free)
  (2) dedup_by_signature → K distinct niches
  (3) Per-branch ProposerAgent.research_branch → 0-1 proposals each
  (4) list-wise selection → 1..candidates_per_round proposals

The orchestrator owns the breadth/depth trade-off: on the first round (no
history, frontier empty) it runs depth-first — fewer hypotheses, deeper
branches — because the model has no evidence to spread across wide branches
and most wide hypotheses would be prior-duplicates. On later rounds it runs
breadth-first — more hypotheses, shallower branches — because Explore's
negative feedback can steer the generator away from exhausted families.

There is no probe/gate between generator and branch. An earlier version had a
shallow grep-based probe to filter cards before spending branch budget, but it
suffered a vocabulary mismatch: the generator writes English mechanism
descriptions, the code contains CamelCase C++ identifiers, so literal grep
produced systematic false negatives — killing good seeds. The branch
researcher itself is the filter: its first few run_research_command steps
confirm whether the mechanism exists, and if not it abandons (producing a
finding that feeds Explore). This is both more accurate (the branch has task
context and understanding, not a blind keyword match) and task-agnostic.

The ProposerResult interface is unchanged so loop.py needs no modification.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .model import ChatModel
from .hypothesis import HypothesisCard, dedup_by_signature, distinct_niches
from .generator import GeneratorAgent
from .proposer import ProposerAgent, ProposerResult, BranchResult
from ..explore.models import ExploreReport
from ..container.runtime import ApptainerRuntime
from ..memory.models import ResearchProposal, NewFindingTarget


@dataclass
class _Mode:
    """Breadth/depth allocation for one round."""
    n_hypotheses: int
    max_branch_steps: int
    label: str


def _select_mode(
    *, first_round: bool, n_experiments: int, max_steps: int,
    hypothesis_count: int, branch_count: int,
) -> _Mode:
    """Adaptive breadth/depth (Snell 2024 — fixed strategies are dominated).

    First round / no history → depth-first: few hypotheses, deep branches.
    The model has no evidence to spread wide; most wide hypotheses would be
    prior-duplicates. Better to deep-probe one or two directions, discover
    where the real bottleneck is, then branch next round.

    Later rounds → breadth-first: more hypotheses, shallower branches.
    Explore's negative feedback steers the generator away from exhausted
    families, so wide generation produces genuine diversity.
    """
    if first_round or n_experiments == 0:
        # Depth-first: 2-3 hypotheses, deep branches (most of the budget).
        n = min(3, hypothesis_count)
        branch_steps = max(5, max_steps - 6)  # reserve ~6 for generation
        return _Mode(n_hypotheses=n, max_branch_steps=branch_steps,
                     label="depth-first")
    # Breadth-first: full hypothesis count, split budget across branches.
    n = hypothesis_count
    branch_steps = max(5, (max_steps - 6) // min(branch_count, n))
    return _Mode(n_hypotheses=n, max_branch_steps=branch_steps,
                 label="breadth-first")


class ProposerOrchestrator:
    """The Loop's proposer entry point. Owns the branch-then-deepen pipeline."""

    def __init__(
        self,
        *,
        model: ChatModel,
        runtime: ApptainerRuntime,
        timeout_seconds: int,
        max_steps: int,
        command_timeout_seconds: int,
        command_output_cap_chars: int,
        hypothesis_count: int = 8,
        branch_count: int = 3,
        frame_free_ratio: float = 0.33,
        usage_observer=None,
    ):
        self.model = model
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self.max_steps = max_steps
        self.command_timeout_seconds = command_timeout_seconds
        self.command_output_cap_chars = command_output_cap_chars
        self.hypothesis_count = hypothesis_count
        self.branch_count = branch_count
        self.frame_free_ratio = frame_free_ratio
        self.usage_observer = usage_observer
        self.generator = GeneratorAgent(
            model=model, timeout_seconds=timeout_seconds,
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
        """Run the full branch-then-deepen pipeline. Returns ProposerResult
        with the same interface as the old ProposerAgent.run."""
        started = time.monotonic()

        # --- Explore (shared across generator + branches) ---
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
            hypothesis_count=self.hypothesis_count,
            branch_count=self.branch_count,
        )
        print(
            f"[orchestrator] mode={mode.label} "
            f"n_hypotheses={mode.n_hypotheses} "
            f"branch_steps={mode.max_branch_steps}",
            flush=True,
        )

        # --- (1) Generate ---
        startup_pack = memory_service.build_startup_pack(
            goal=goal, editable=editable, frozen=frozen,
            base_sha=base_sha, gate_block=gate_block,
            candidates_per_round=candidates_per_round, hints=hints,
            current_round=current_round, explore=explore,
        )
        gen_result = self.generator.run(
            n=mode.n_hypotheses,
            frame_free_ratio=self.frame_free_ratio,
            context=startup_pack,
            explore=explore,
            prompt_dir=prompt_dir,
        )
        cards = gen_result.cards
        print(
            f"[orchestrator] generated {len(cards)} cards, "
            f"{distinct_niches(cards)} distinct niches",
            flush=True,
        )

        # --- (2) Dedup by structural signature ---
        cards = dedup_by_signature(cards, per_bin=1)
        # Cap at branch_count.
        cards = cards[:self.branch_count]
        print(
            f"[orchestrator] after dedup: {len(cards)} cards "
            f"({distinct_niches(cards)} niches)",
            flush=True,
        )

        if not cards:
            return self._abstain("generator produced no usable hypotheses")

        # --- (3) Per-branch deep research ---
        # No probe/gate: every dedup'd card enters a branch. The branch's first
        # few run_research_command steps are the real filter — if the mechanism
        # doesn't exist in the code, the branch abandons (producing a finding
        # for Explore). This is task-agnostic and avoids the vocabulary-mismatch
        # false negatives of a keyword-grep probe.
        branch_results = self._run_branches(
            cards,
            goal=goal, editable=editable, frozen=frozen,
            memory_service=memory_service, base_sha=base_sha,
            source_path=source_path, repo_path=repo_path,
            run_dir=run_dir, current_round=current_round,
            gate_block=gate_block, prompt_dir=prompt_dir,
            hints=hints, explore=explore,
            mode=mode,
        )

        # --- (4) Collect proposals (list-wise selection) ---
        proposals = [
            b.proposal for b in branch_results if b.proposal is not None
        ]
        # Cap at candidates_per_round. List-wise: keep proposals from branches
        # with the most tool calls (deepest research) first, as a proxy for
        # evidence weight. Tie-break by branch order.
        if len(proposals) > candidates_per_round:
            scored = sorted(
                enumerate(proposals),
                key=lambda iv: (
                    -branch_results[
                        [b for b, r in enumerate(branch_results)
                         if r.proposal is not None][iv[0]]
                    ].deliberation_telemetry.get("tool_calls", 0),
                    iv[0],
                ),
            )
            proposals = [p for _, p in scored[:candidates_per_round]]

        elapsed = time.monotonic() - started
        if not proposals:
            return self._abstain(
                "all branches abandoned or produced no proposal",
                telemetry=self._telemetry(branch_results, mode, elapsed),
            )
        print(
            f"[orchestrator] {len(proposals)} proposal(s) in {elapsed:.1f}s",
            flush=True,
        )
        return ProposerResult(
            proposals=proposals,
            usage=None,
            deliberation_telemetry=self._telemetry(
                branch_results, mode, elapsed),
            trace={"branches": [
                {"sig": b.hypothesis.signature(),
                 "proposal": bool(b.proposal),
                 "instruction": (b.proposal.instruction
                                  if b.proposal is not None else None),
                 "abandoned": b.abandoned}
                for b in branch_results
            ]},
        )

    def _run_branches(
        self, cards, *, goal, editable, frozen, memory_service, base_sha,
        source_path, repo_path, run_dir, current_round, gate_block,
        prompt_dir, hints, explore, mode,
    ) -> list[BranchResult]:
        """Run one research_branch per card, concurrently.

        Mirrors ``loop._run_candidates``: a branch that raises becomes an
        abandoned result (so one failure never kills the others) and results are
        indexed by original card position so the post-collection tie-break and
        trace stay stable. Concurrency is already bounded — ``cards`` is capped
        at ``branch_count`` upstream.
        """
        shared = dict(
            goal=goal, editable=editable, frozen=frozen,
            memory_service=memory_service, base_sha=base_sha,
            source_path=source_path, repo_path=repo_path,
            run_dir=run_dir, current_round=current_round,
            gate_block=gate_block, prompt_dir=prompt_dir,
            hints=hints, explore=explore,
            max_steps=mode.max_branch_steps,
        )
        results: list[BranchResult | None] = [None] * len(cards)
        with ThreadPoolExecutor(max_workers=len(cards)) as pool:
            futures = {
                pool.submit(
                    self.proposer.research_branch, hypothesis=card, **shared,
                ): i
                for i, card in enumerate(cards)
            }
            for future in as_completed(futures):
                i = futures[future]
                try:
                    results[i] = future.result()
                except Exception as exc:
                    card = cards[i]
                    print(
                        f"[orchestrator] branch {card.generative_op} "
                        f"{card.signature()} failed: {exc}", flush=True,
                    )
                    results[i] = BranchResult(
                        hypothesis=card, abandoned=True,
                        abandon_reason=f"branch worker failed: {exc}",
                        deliberation_telemetry={"tool_calls": 0},
                    )
        for branch in results:
            if branch is not None:
                self._log_branch_result(branch)
        return [r for r in results if r is not None]

    @staticmethod
    def _log_branch_result(branch: BranchResult) -> None:
        card = branch.hypothesis
        if branch.proposal:
            print(
                f"[orchestrator] branch {card.generative_op} "
                f"{card.signature()} → proposal", flush=True,
            )
        elif branch.abandoned:
            print(
                f"[orchestrator] branch {card.generative_op} "
                f"{card.signature()} → abandoned: "
                f"{(branch.abandon_reason or '')[:60]}", flush=True,
            )

    def _abstain(self, reason: str, *, telemetry: dict | None = None) -> ProposerResult:
        print(f"[orchestrator] abstained: {reason}", flush=True)
        return ProposerResult(
            proposals=[],
            abstained=True,
            abstain_reason=reason,
            deliberation_telemetry=telemetry or {},
        )

    def _telemetry(self, branches: list[BranchResult], mode: _Mode,
                   elapsed: float) -> dict:
        return {
            "mode": mode.label,
            "n_hypotheses": mode.n_hypotheses,
            "n_branches": len(branches),
            "n_proposals": sum(1 for b in branches if b.proposal),
            "n_abandoned": sum(1 for b in branches if b.abandoned),
            "elapsed_seconds": round(elapsed, 1),
            "branch_telemetry": [
                b.deliberation_telemetry for b in branches
            ],
        }
