"""ProposerOrchestrator: the branch-then-deepen entry point for the Loop.

Runs the pipeline:

  (1) Generator → N independent calls (n=2 each) → 2N hypothesis cards
  (2) dedup_by_signature → K distinct niches, minus frozen-region cards
  (3) Per-branch cognitive element (Sieve + Enricher) → 0-1 proposals each
  (4) list-wise selection → 1..candidates_per_round proposals

The cognitive element (per branch) is NOT a reviewer — it never judges merit.
It only sieves on objective bars (and blocks with a source ref) and enriches
the card into an executor-ready instruction (and submits). There is no
search-space restriction on a judgment: submit is the default terminal; block
is objective and evidence-bound; budget exhaustion submits partial. See
promposer.py / prompts/proposer.md.

The orchestrator owns the breadth/depth trade-off. When ``branch_steps`` is set
explicitly it overrides the derived per-branch budget — the cognitive element's
job is now bounded locate+enrich, which no longer justifies splitting a
merit-judging budget across branches.

The ProposerResult interface is unchanged so loop.py needs no modification.
"""
from __future__ import annotations

import fnmatch
import random
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


# The generative basis (G1-G9) the prompt defines. The scheduler draws a random
# 5-of-9 subset per independent generator call so that identical context does
# not collapse every draw onto the same entry-point angle.
_ALL_GENERATIVE_OPS = ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9")
_SCHEDULED_OP_COUNT = 5


def _sample_generative_ops() -> tuple[str, ...]:
    """Pick a random 5-of-9 subset of G1-G9 for one generator call.

    This is the generative-op scheduler: it narrows the menu the model chooses
    from, so N independent calls each see a different constrained basis rather
    than the full G1-G9 (which a strong prior collapses to one repeated G). The
    model still self-reports which G it used — the scheduler only selects the
    menu, not the choice.
    """
    return tuple(random.sample(_ALL_GENERATIVE_OPS, _SCHEDULED_OP_COUNT))


def _select_mode(
    *, first_round: bool, n_experiments: int, max_steps: int,
    hypothesis_count: int, candidates_per_round: int,
    branch_steps: int | None = None,
) -> _Mode:
    """Adaptive breadth/depth.

    First round / no history → depth-first (few hypotheses, deep branches).
    Later rounds → breadth-first (full hypothesis count).

    The number of branches per round is ``candidates_per_round`` — there is no
    separate branch-count knob — so the breadth-first step budget is split
    across that many branches.

    When ``branch_steps`` is set explicitly it overrides the derived per-branch
    budget for both modes. The cognitive element's job is bounded locate+enrich
    (the old merit-judging budget split no longer applies); an explicit budget
    lets each branch deepen enough to locate and enrich a real target instead
    of budget-exhausting like the old merit chase did.
    """
    if first_round or n_experiments == 0:
        n = min(3, hypothesis_count)
        steps = branch_steps if branch_steps else max(5, max_steps - 6)
        return _Mode(n_hypotheses=n, max_branch_steps=steps,
                     label="depth-first")
    n = hypothesis_count
    steps = (branch_steps if branch_steps
             else max(5, (max_steps - 6) // min(candidates_per_round, n)))
    return _Mode(n_hypotheses=n, max_branch_steps=steps,
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
        branch_steps: int | None = None,
        usage_observer=None,
    ):
        self.model = model
        self.runtime = runtime
        self.timeout_seconds = timeout_seconds
        self.max_steps = max_steps
        self.command_timeout_seconds = command_timeout_seconds
        self.command_output_cap_chars = command_output_cap_chars
        self.hypothesis_count = hypothesis_count
        self.branch_steps = branch_steps
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
        """Run the full branch-then-deepen pipeline."""
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
            candidates_per_round=candidates_per_round,
            branch_steps=self.branch_steps,
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
        call_count = self.hypothesis_count
        cards = self._generate_independent_hypotheses(
            call_count=call_count,
            context=startup_pack,
            explore=explore,
            prompt_dir=prompt_dir,
        )
        print(
            f"[orchestrator] independent hypothesis calls={call_count} "
            f"→ {len(cards)} cards, {distinct_niches(cards)} distinct niches",
            flush=True,
        )

        # --- (2) Dedup by structural signature, drop frozen regions ---
        cards = dedup_by_signature(cards, per_bin=1)
        cards = self._split_frozen(cards, frozen)
        cards = cards[:candidates_per_round]
        print(
            f"[orchestrator] after dedup: {len(cards)} cards "
            f"({distinct_niches(cards)} niches)",
            flush=True,
        )

        if not cards:
            return self._abstain("generator produced no usable hypotheses")

        # --- (3) Per-branch cognitive element (sieve + enrich) ---
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
        # Floor: a zero-read partial submit carries no enrichment, so it is
        # dropped before selection (orchestrator-level filter, NOT a cognitive-
        # element merit gate — the element submitted it; the orchestrator simply
        # does not spend an executor+harness pass on a card it never located).
        eligible = [
            b for b in branch_results
            if b.proposal is not None
            and not (b.enrichment_partial
                     and b.deliberation_telemetry.get("source_reads", 0) == 0)
        ]
        # Cap at candidates_per_round, preferring deeper research (tool_calls);
        # stable sort preserves branch order among ties.
        if len(eligible) > candidates_per_round:
            eligible = sorted(
                eligible,
                key=lambda b: -b.deliberation_telemetry.get("tool_calls", 0),
            )[:candidates_per_round]
        proposals = [b.proposal for b in eligible]

        elapsed = time.monotonic() - started
        if not proposals:
            return self._abstain(
                "all branches blocked, errored, or produced no eligible proposal",
                telemetry=self._telemetry(branch_results, mode, elapsed),
                trace=self._branch_trace(branch_results),
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
            trace=self._branch_trace(branch_results),
        )

    def _generate_independent_hypotheses(
        self, *, call_count: int, context: str, explore, prompt_dir,
    ) -> list[HypothesisCard]:
        """Fire ``call_count`` independent two-card Generator calls in parallel.

        Each call is ``self.generator.run(n=2, ...)`` against the same
        configured model with identical context, Explore boundary, and prompt
        directory. With ``hypothesis_count=N`` this issues N calls producing
        2N cards total — half the call count of one-card calls at the same
        yield, so each call does more work but the draws are still independent.

        Generative-op scheduling: each call gets a random 5-of-9 subset of
        G1-G9 injected into its prompt (replacing the full basis), so the model
        chooses from a different constrained menu each time. This counters the
        collapse where identical context makes every independent draw pick the
        same G. The model still self-reports ``generative_op`` — the delivery
        contract is unchanged; the scheduler only narrows the menu.

        Concurrency is bounded at eight (no new configuration) so large N
        stays practical. Results are stored by submission index and flattened
        in index order after completion, so dedup (which keeps the first card
        per signature) and downstream selection stay deterministic regardless
        of completion order.
        """
        slots: list[list[HypothesisCard] | None] = [None] * call_count
        workers = min(8, call_count) if call_count else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self.generator.run,
                    # n=2 ⇒ each call produces 2 cards. frame_free_ratio=0.0
                    # still yields 1 free slot (the generator's max(1, ...)
                    # floor), so one card respects the Explore boundary and
                    # one ignores it — a guided+free pair per call.
                    n=2, frame_free_ratio=0.0,
                    context=context, explore=explore, prompt_dir=prompt_dir,
                    assigned_ops=_sample_generative_ops(),
                ): i
                for i in range(call_count)
            }
            for future in as_completed(futures):
                # Keyed by submission index, not completion order — see docstring.
                slots[futures[future]] = future.result().cards
        cards: list[HypothesisCard] = []
        for slot in slots:
            if slot:
                cards.extend(slot)
        return cards

    @staticmethod
    def _branch_trace(branches: list[BranchResult]) -> dict:
        """Per-branch observability — recorded for EVERY round, including
        rounds where all branches blocked/errored (so a block is diagnosable
        from the trace, not silently lost)."""
        return {"branches": [
            {"sig": b.hypothesis.signature(),
             "outcome": b.outcome,
             "proposal": bool(b.proposal),
             "instruction": (b.proposal.instruction
                             if b.proposal is not None else None),
             "reason_kind": b.reason_kind,
             "explanation": b.explanation,
             "evidence_refs": list(b.block_evidence_refs),
             "tool_calls": b.deliberation_telemetry.get("tool_calls", 0),
             "partial": bool(b.enrichment_partial)}
            for b in branches
        ]}

    @staticmethod
    def _split_frozen(cards: list[HypothesisCard],
                      frozen: list[str]) -> list[HypothesisCard]:
        """Best-effort deterministic prefilter: drop cards whose ``region``
        names a path under a frozen glob. ``region`` is often prose, so this
        only catches the obvious path-like cases; the branch sieve's frozen
        check is the real gate for prose regions. Dropped cards are naturally
        backfilled because this runs before the candidates_per_round cap."""
        if not frozen:
            return list(cards)
        kept = []
        for card in cards:
            region = (card.region or "").strip()
            token = region.split()[0] if region else ""
            if token and any(fnmatch.fnmatch(token, pat) for pat in frozen):
                print(
                    f"[orchestrator] dropped frozen-region card "
                    f"{card.signature()}", flush=True,
                )
                continue
            kept.append(card)
        return kept

    def _run_branches(
        self, cards, *, goal, editable, frozen, memory_service, base_sha,
        source_path, repo_path, run_dir, current_round, gate_block,
        prompt_dir, hints, explore, mode,
    ) -> list[BranchResult]:
        """Run one research_branch per card, concurrently.

        A branch that raises becomes an error result (so one failure never
        kills the others) and results are indexed by original card position so
        selection and trace stay stable. Concurrency is bounded — ``cards`` is
        capped at ``candidates_per_round`` upstream.
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
                        hypothesis=card, outcome="error",
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
            tag = "partial submit" if branch.enrichment_partial else "proposal"
            print(
                f"[orchestrator] branch {card.generative_op} "
                f"{card.signature()} → {tag}", flush=True,
            )
        elif branch.outcome == "block":
            print(
                f"[orchestrator] branch {card.generative_op} "
                f"{card.signature()} → blocked: {branch.reason_kind}",
                flush=True,
            )
        elif branch.outcome == "error":
            print(
                f"[orchestrator] branch {card.generative_op} "
                f"{card.signature()} → error", flush=True,
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

    def _telemetry(self, branches: list[BranchResult], mode: _Mode,
                   elapsed: float) -> dict:
        return {
            "mode": mode.label,
            "n_hypotheses": mode.n_hypotheses,
            "n_branches": len(branches),
            "n_proposals": sum(
                1 for b in branches
                if b.proposal and not b.enrichment_partial),
            "n_partial": sum(1 for b in branches if b.enrichment_partial),
            "n_blocked": sum(1 for b in branches if b.outcome == "block"),
            "n_error": sum(1 for b in branches if b.outcome == "error"),
            "elapsed_seconds": round(elapsed, 1),
            "branch_telemetry": [
                b.deliberation_telemetry for b in branches
            ],
        }
