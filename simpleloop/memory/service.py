"""MemoryService: single façade the Loop and Proposer share.

Reads the immutable Experiment Ledger (``history.jsonl``) and the append-only
Finding Archive (``memory/findings.jsonl``); resolves proposal research
targets (allocating new Finding ids when needed); links completed experiments
to their findings after each round; exposes the six memory tools the Proposer
can call (``list_findings / search_findings / inspect_finding /
search_experiments / inspect_episode``). Never mutates the Experiment Ledger.
"""
from __future__ import annotations

import math
from pathlib import Path

from ..explore import ExploreReport, analyze_explore_health
from ..harness.memory import read_history, resolve_episode
from .context import build_startup_pack, build_generation_context
from .experiment_index import (
    Experiment,
    build_experiments,
    filter_experiments,
)
from .finding_store import FindingStore
from .frontier import compute_frontier
from .models import (
    ExistingFindingTarget,
    Finding,
    NewFindingTarget,
    ResearchProposal,
)
from .retrieval import (
    diverse_experiment_search,
    rank_findings,
)


MEMORY_TOOL_CHEATSHEET = (
    "- list_findings(state=active|open|dormant|archived|all, limit=1..20)\n"
    "- search_findings(query, limit=1..20)\n"
    "- inspect_finding(finding_id)\n"
    "- search_experiments(query, limit=1..50, filters={gate_passed?,"
    " selected?, finding_id?, changed_path?, round_min?, round_max?,"
    " status?})\n"
    "- inspect_episode(ref='r<round>c<candidate>')"
)


class MemoryService:
    """Owns the write side of the Finding Archive and the read side of the
    Ledger. Not thread-safe by itself: the Loop's flock protects the run-dir.
    """

    def __init__(
        self,
        run_dir: Path,
        metrics_schema: dict,
        *,
        dormancy_rounds: int = 3,
    ):
        self.run_dir = Path(run_dir)
        self.metrics_schema = metrics_schema or {}
        self.dormancy_rounds = int(dormancy_rounds)
        self.finding_store = FindingStore(self.run_dir)
        self.history_path = self.run_dir / "history.jsonl"

    # --- Public reads used by Loop / Proposer -----------------------------

    def load_experiments(self) -> list[Experiment]:
        history = read_history(self.history_path)
        return build_experiments(history)

    def load_findings(self) -> dict[str, Finding]:
        return self.finding_store.load_all()

    def compute_frontier(
        self, *, current_round: int, editable_prefixes: tuple[str, ...] = (),
    ) -> dict:
        return compute_frontier(
            self.load_findings(),
            self.load_experiments(),
            current_round=current_round,
            dormancy_rounds=self.dormancy_rounds,
            editable_prefixes=editable_prefixes,
        )

    def analyze_explore(self, *, current_round: int) -> ExploreReport:
        """Compute the search-health report from the current Ledger + Finding
        archive. This is the single source of truth the Proposer consults —
        compute it once per wakeup and reuse for the startup pack, the
        per-step state header, the nudges, and the challenge guard."""
        objective = (self.metrics_schema or {}).get("objective") or {}
        return analyze_explore_health(
            self.load_findings(),
            self.load_experiments(),
            current_round=current_round,
            objective_key=objective.get("key"),
            lower_is_better=bool(objective.get("lower_is_better")),
        )

    def build_startup_pack(
        self,
        *,
        goal: str,
        editable: list[str],
        frozen: list[str],
        base_sha: str,
        gate_block: str,
        candidates_per_round: int,
        hints: list[str] | None,
        current_round: int,
        recent_rounds: int = 2,
        explore: ExploreReport | None = None,
    ) -> str:
        history = read_history(self.history_path)
        experiments = build_experiments(history)
        findings = self.load_findings()
        frontier = compute_frontier(
            findings,
            experiments,
            current_round=current_round,
            dormancy_rounds=self.dormancy_rounds,
            editable_prefixes=tuple(editable or ()),
        )
        abstentions = [
            {
                "round": record.get("round", 0),
                "reason": (record.get("abstention") or {}).get("reason"),
                "blocking_unknown": (record.get("abstention") or {}).get(
                    "blocking_unknown"
                ),
            }
            for record in history
            if isinstance(record, dict) and record.get("abstention")
        ][-recent_rounds:]
        if explore is None:
            explore = self.analyze_explore(current_round=current_round)
        return build_startup_pack(
            goal=goal,
            editable=editable,
            frozen=frozen,
            base_sha=base_sha,
            gate_block=gate_block,
            candidates_per_round=candidates_per_round,
            hints=hints,
            experiments=experiments,
            frontier=frontier,
            recent_rounds=recent_rounds,
            tool_cheatsheet=MEMORY_TOOL_CHEATSHEET,
            recent_abstentions=abstentions,
            explore=explore,
        )

    def build_generation_context(
        self,
        *,
        goal: str,
        editable: list[str],
        frozen: list[str],
        base_sha: str,
        gate_block: str,
    ) -> str:
        """History-free context for the Generator (partner design)."""
        return build_generation_context(
            goal=goal, editable=editable, frozen=frozen,
            base_sha=base_sha, gate_block=gate_block,
        )

    # --- Write path: target resolution & experiment linking ---------------

    def resolve_targets(
        self,
        proposals: list[ResearchProposal],
        *,
        round_id: int,
    ) -> list[str]:
        """Turn each proposal's ``research_target`` into a concrete finding
        id. Existing targets are checked for existence; new targets allocate
        a fresh ``F-NNN`` and append an ``open`` Finding record. Returns the
        list of finding ids in proposal order (parallel to ``proposals``)."""
        resolved: list[str] = []
        findings = self.finding_store.load_all()
        for proposal in proposals:
            target = proposal.research_target
            if isinstance(target, ExistingFindingTarget):
                if target.finding_id not in findings:
                    raise ValueError(
                        f"proposal target references unknown finding "
                        f"{target.finding_id!r}"
                    )
                resolved.append(target.finding_id)
            elif isinstance(target, NewFindingTarget):
                new_id = self.finding_store.allocate_next_id()
                finding = Finding(
                    id=new_id,
                    question=target.question,
                    mechanisms=target.mechanisms,
                    code_regions=target.code_regions,
                    state="open",
                    created_round=round_id,
                    last_touched_round=round_id,
                    experiment_refs=(),
                    parent_finding_id=None,
                    stats={
                        "attempts": 0,
                        "eligible": 0,
                        "selected": 0,
                        "best_objective": None,
                    },
                )
                self.finding_store.append(finding)
                findings[new_id] = finding
                resolved.append(new_id)
            else:
                raise TypeError(
                    f"unsupported research target: {type(target).__name__}"
                )
        return resolved

    def link_completed_experiments(
        self,
        *,
        round_id: int,
        candidates: list[dict],
    ) -> None:
        """After the Store persists a round, walk its candidates and update
        each involved Finding's experiment_refs / stats / last_touched_round.
        A candidate with no ``finding_id`` is skipped (nothing to link)."""
        if not candidates:
            return
        findings = self.finding_store.load_all()
        obj = (self.metrics_schema or {}).get("objective") or {}
        obj_key = obj.get("key")
        lower_is_better = bool(obj.get("lower_is_better"))
        by_finding: dict[str, list[dict]] = {}
        for cand in candidates:
            fid = cand.get("finding_id")
            if not fid:
                continue
            by_finding.setdefault(fid, []).append(cand)
        for fid, cands in by_finding.items():
            base = findings.get(fid)
            if base is None:
                raise ValueError(
                    f"cannot link experiments to unknown finding {fid!r}"
                )
            new_refs = list(base.experiment_refs)
            for cand in cands:
                ref = cand.get("experiment_id") or (
                    f"r{round_id}c{cand.get('candidate', 0)}"
                )
                if ref not in new_refs:
                    new_refs.append(ref)
            stats = dict(base.stats or {})
            stats["attempts"] = stats.get("attempts", 0) + len(cands)
            stats["eligible"] = stats.get("eligible", 0) + sum(
                1 for c in cands if c.get("eligible")
            )
            stats["selected"] = stats.get("selected", 0) + sum(
                1 for c in cands if c.get("selected")
            )
            if obj_key:
                stats["best_objective"] = _combine_best(
                    stats.get("best_objective"),
                    cands, obj_key, lower_is_better,
                )
            updated = Finding(
                id=base.id,
                question=base.question,
                mechanisms=base.mechanisms,
                code_regions=base.code_regions,
                state="active",
                created_round=base.created_round,
                last_touched_round=round_id,
                experiment_refs=tuple(new_refs),
                parent_finding_id=base.parent_finding_id,
                stats=stats,
            )
            self.finding_store.append(updated)

    # --- Memory tools exposed to the Proposer -----------------------------

    def list_findings(
        self,
        *,
        state: str = "active",
        limit: int = 20,
        current_round: int | None = None,
    ) -> list[dict]:
        limit = max(1, min(int(limit), 20))
        findings = self.finding_store.load_all()
        out: list[dict] = []
        for finding in findings.values():
            eff_state = (
                self._effective_state(finding, current_round=current_round)
                if current_round is not None
                else finding.state
            )
            if state != "all" and eff_state != state:
                continue
            entry = finding.to_dict()
            entry["state"] = eff_state
            out.append(entry)
        out.sort(
            key=lambda entry: (-int(entry.get("last_touched_round", 0)),
                               entry["id"]),
        )
        return out[:limit]

    def search_findings(
        self, *, query: str, limit: int = 5,
    ) -> list[dict]:
        limit = max(1, min(int(limit), 20))
        findings = list(self.finding_store.load_all().values())
        hits = rank_findings(findings, query=query, limit=limit)
        return [
            {**finding.to_dict(), "score": round(float(score), 4)}
            for finding, score in hits
        ]

    def inspect_finding(self, finding_id: str) -> dict:
        finding = self.finding_store.get(finding_id)
        if finding is None:
            raise ValueError(f"unknown finding: {finding_id}")
        return finding.to_dict()

    def search_experiments(
        self,
        *,
        query: str,
        filters: dict | None = None,
        limit: int = 10,
        buckets: bool = True,
    ) -> dict | list[dict]:
        """When ``buckets`` is True (default), return the design-doc §6.3
        three-bucket view: {relevant, contrasting, diverse}. Otherwise return
        a flat top-K list."""
        limit = max(1, min(int(limit), 50))
        experiments = filter_experiments(
            self.load_experiments(), **(filters or {}),
        )
        if not buckets:
            hits = []
            from .retrieval import rank_experiments
            for exp, score in rank_experiments(
                experiments, query=query, limit=limit,
            ):
                entry = exp.to_dict()
                entry["score"] = round(float(score), 4)
                hits.append(entry)
            return hits
        # Split ``limit`` across the three buckets while preserving intent.
        rel = max(1, (limit + 2) // 3 + (limit % 3 != 0))
        con = max(1, limit // 3)
        div = max(1, limit - rel - con)
        buckets_result = diverse_experiment_search(
            experiments, query=query,
            relevant=rel, contrasting=con, diverse=div,
        )
        return {
            name: [
                {**exp.to_dict(), "score": round(float(score), 4)}
                for exp, score in hits
            ]
            for name, hits in buckets_result.items()
        }

    def inspect_episode(self, ref: str) -> dict:
        history = read_history(self.history_path)
        result = resolve_episode(history, ref)
        # Attach finding_id / experiment_id from the ledger if present.
        return result

    # --- helpers ----------------------------------------------------------

    def _effective_state(
        self, finding: Finding, *, current_round: int | None,
    ) -> str:
        if current_round is None:
            return finding.state
        if finding.state in {"archived", "open", "dormant"}:
            return finding.state
        if current_round - finding.last_touched_round > self.dormancy_rounds:
            return "dormant"
        return "active"


def _combine_best(
    current: float | None,
    candidates: list[dict],
    key: str,
    lower_is_better: bool,
) -> float | None:
    values: list[float] = []
    if isinstance(current, (int, float)) and not isinstance(current, bool):
        if math.isfinite(current):
            values.append(float(current))
    for cand in candidates:
        if not cand.get("eligible"):
            continue
        metric = (cand.get("metrics") or {}).get(key)
        if isinstance(metric, (int, float)) and not isinstance(metric, bool):
            if math.isfinite(metric):
                values.append(float(metric))
    if not values:
        return current
    return min(values) if lower_is_better else max(values)
