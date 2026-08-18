"""MemoryService: single façade the Loop and Proposer share.

Reads the immutable Experiment Ledger (``history.jsonl``) and the append-only
Finding Archive (``memory/findings.jsonl``); resolves proposal research
targets (allocating new Finding ids when needed); links completed experiments
to their findings after each round; exposes the six memory tools the Proposer
can call (``list_findings / search_findings / inspect_finding /
search_experiments / inspect_episode``). Never mutates the Experiment Ledger.
"""
from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

from .history import read_history, resolve_episode
from .context import build_startup_pack, build_generation_context, build_coverage_pack
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
from .reflection_views import render_reflection_pack


MEMORY_TOOL_CHEATSHEET = (
    "- list_findings(state=active|open|dormant|archived|all, limit=1..20)\n"
    "- search_findings(query, limit=1..20)\n"
    "- inspect_finding(finding_id)\n"
    "- search_experiments(query, limit=1..50, filters={gate_passed?,"
    " selected?, finding_id?, changed_path?, round_min?, round_max?,"
    " status?})\n"
    "- inspect_episode(ref='r<round>c<candidate>')"
)


def read_reflection_records(run_dir: Path) -> list[dict]:
    """Read the Host-owned reflection log (``run_dir/reflection/history.jsonl``)
    as DATA — the proposer never writes it. Tolerant of a missing file (no
    reflections yet) and of torn/blank lines (a crash mid-append must not
    break the next round's context assembly)."""
    path = Path(run_dir) / "reflection" / "history.jsonl"
    if not path.exists():
        return []
    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _read_reflection_handoffs(run_dir: Path) -> list[str]:
    """Kept for callers that want the handoff texts only (the reflection
    pack now consumes the full records directly)."""
    return [
        str(record.get("handoff") or "")
        for record in read_reflection_records(run_dir)
        if record.get("handoff")
    ]


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
        """Project history into Experiment records, then patch each
        ``finding_id`` from the reverse-join of the findings' experiment_refs
        (the proposer-owned association). history.jsonl no longer carries
        finding_id; the association lives in findings.jsonl and is re-derived
        here each read (join-from-history, contract §2.5)."""
        history = read_history(self.history_path)
        experiments = build_experiments(history)
        fmap = self._experiment_finding_map()
        if fmap:
            experiments = [
                replace(e, finding_id=fmap.get(e.experiment_id, e.finding_id))
                for e in experiments
            ]
        return experiments

    def _experiment_finding_map(self) -> dict[str, str]:
        """Invert every finding's ``experiment_refs`` into
        ``{experiment_id: finding_id}``. First writer wins on collision (a ref
        claimed by two findings — e.g. a botched retry — is attributed to the
        first)."""
        out: dict[str, str] = {}
        for fid, finding in self.finding_store.load_all().items():
            for ref in finding.experiment_refs:
                out.setdefault(ref, fid)
        return out

    def _experiments_by_id(self) -> dict[str, Experiment]:
        return {e.experiment_id: e for e in self.load_experiments()}

    @staticmethod
    def _derive_stats(
        finding: Finding, experiments_by_id: dict[str, Experiment],
        obj_key: str | None, lower_is_better: bool,
    ) -> dict:
        """Join finding.experiment_refs -> history outcomes (read-only).
        Refs predicted at emit but not yet in history (round not recorded,
        crashed, or abstained) are skipped — they are not attempts yet."""
        attempts = eligible = selected = 0
        best: float | None = None
        for ref in finding.experiment_refs:
            exp = experiments_by_id.get(ref)
            if exp is None:
                continue
            attempts += 1
            if exp.eligible:
                eligible += 1
            if exp.selected:
                selected += 1
            if obj_key and exp.eligible:
                m = (exp.metrics or {}).get(obj_key)
                if (isinstance(m, (int, float)) and not isinstance(m, bool)
                        and math.isfinite(m)):
                    best = m if best is None else (
                        min(best, m) if lower_is_better else max(best, m))
        return {
            "attempts": attempts,
            "eligible": eligible,
            "selected": selected,
            "best_objective": best,
        }

    def load_findings(self) -> dict[str, Finding]:
        return self.finding_store.load_all()

    def compute_frontier(
        self, *, current_round: int, editable_prefixes: tuple[str, ...] = (),
    ) -> dict:
        experiments = self.load_experiments()
        return compute_frontier(
            self.load_findings(),
            experiments,
            current_round=current_round,
            dormancy_rounds=self.dormancy_rounds,
            editable_prefixes=editable_prefixes,
            experiments_by_id={e.experiment_id: e for e in experiments},
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
            experiments_by_id={e.experiment_id: e for e in experiments},
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
        )

    def build_coverage_pack(
        self, *, current_round: int, recent_rounds: int = 2,
    ) -> str:
        """The lean per-round COVERAGE MAP for the Scientist's wake-up:
        outcomes dashboard + research frontier + recent abstentions. No goal /
        world / gates / tools (those live in the standing system prompt) and no
        direction text — only coverage + outcomes."""
        history = read_history(self.history_path)
        experiments = self.load_experiments()  # reverse-join patched
        findings = self.load_findings()
        frontier = compute_frontier(
            findings, experiments,
            current_round=current_round,
            dormancy_rounds=self.dormancy_rounds,
            editable_prefixes=(),
            experiments_by_id={e.experiment_id: e for e in experiments},
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
        return build_coverage_pack(
            experiments=experiments, frontier=frontier,
            recent_rounds=recent_rounds, recent_abstentions=abstentions,
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

    def build_reflection_pack(self, *, current_round: int) -> str:
        """The aggregate trajectory evidence for one Reflection session.

        Deterministic derived views (mechanism-family concentration,
        expectation↔outcome ledger, incumbent trajectory, attention locality)
        plus the Scientist's previous reflection handoffs. Best-effort on the
        reflection log (a missing file just means no prior reflections); safe
        at round 0 (renders an honest near-empty pack)."""
        history = read_history(self.history_path)
        experiments = self.load_experiments()
        findings = self.load_findings()
        from ..scientist_session import read_expectations
        expectation_rows = read_expectations(self.run_dir)
        reflection_records = read_reflection_records(self.run_dir)
        handoffs = [
            str(record.get("handoff") or "")
            for record in reflection_records
            if record.get("handoff")
        ]
        return render_reflection_pack(
            current_round=current_round,
            experiments=experiments,
            findings=findings,
            history_rows=history,
            expectation_rows=expectation_rows,
            previous_handoffs=handoffs,
            metrics_schema=self.metrics_schema,
            reflection_records=reflection_records,
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

    def commit_proposals(
        self, *, round_id: int, proposals: list[ResearchProposal],
    ) -> list[str | None]:
        """The proposer owns its finding lifecycle. For one round's proposals:
        scrub any stale same-round refs (retry idempotency), resolve each
        proposal's research target (allocate/dedup findings), then record the
        predicted experiment ref ``r{round}c{slot}`` on each finding.

        This records the proposer's INTENT (which finding each slot targets);
        outcomes are never stored here — they are re-derived from history at
        read time (join-from-history, contract §2.5). The slot index equals the
        candidate index in history (the loop builds candidates in proposal
        order), so the predicted ref matches the experiment_id history records.

        A findings.jsonl IO error is swallowed (mirrors _safe_save_meta): the
        Scientist's notes must not fail the round.
        """
        n = len(proposals)
        try:
            slot_refs = {f"r{round_id}c{i}" for i in range(n)}

            # 1. Scrub: remove this round's slot refs from every finding, so a
            #    retried proposer lane does not leave two findings both claiming
            #    r{R}c{i} (which would double-count the outcome in _derive_stats).
            #    The last successful commit for a round wins.
            for fid, finding in self.finding_store.load_all().items():
                if any(r in slot_refs for r in finding.experiment_refs):
                    self.finding_store.append(replace(
                        finding,
                        experiment_refs=tuple(
                            r for r in finding.experiment_refs
                            if r not in slot_refs),
                    ))

            # 2. Resolve targets (allocate new findings / validate existing).
            finding_ids = self.resolve_targets(proposals, round_id=round_id)
            findings = self.finding_store.load_all()

            # 3. Record each slot's predicted ref on its finding.
            for i, fid in enumerate(finding_ids):
                if fid is None:
                    continue
                base = findings.get(fid)
                if base is None:
                    continue
                ref = f"r{round_id}c{i}"
                if ref in base.experiment_refs:
                    continue
                self.finding_store.append(replace(
                    base,
                    experiment_refs=base.experiment_refs + (ref,),
                    last_touched_round=round_id,
                    state="active" if base.state == "open" else base.state,
                ))
            return finding_ids
        except OSError as exc:
            print(f"[memory] commit_proposals IO failed: {exc}", flush=True)
            return [None] * n

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
        experiments_by_id = self._experiments_by_id()
        obj = (self.metrics_schema or {}).get("objective") or {}
        obj_key = obj.get("key")
        lower_is_better = bool(obj.get("lower_is_better"))
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
            # Coverage stats are re-derived from history each read; the stored
            # finding.stats is never authoritative (contract §2.5).
            entry["stats"] = self._derive_stats(
                finding, experiments_by_id, obj_key, lower_is_better)
            # The question text is the Scientist's own open research question;
            # exposing it in the bulk list anchors the proposer to keep
            # drilling the same questions. Drop it here (inspect_finding
            # returns it for deliberate single-item recall).
            entry.pop("question", None)
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
        obj = (self.metrics_schema or {}).get("objective") or {}
        entry = finding.to_dict()
        entry["stats"] = self._derive_stats(
            finding, self._experiments_by_id(),
            obj.get("key"), bool(obj.get("lower_is_better")))
        return entry

    def search_experiments(
        self,
        *,
        query: str,
        filters: dict | None = None,
        limit: int = 10,
        buckets: bool = True,
    ) -> dict | list[dict]:
        """Coverage view over history: locate what ground is already covered
        (and where the gaps are) — NOT a search for directions. Each hit is a
        coverage row (experiment_id, outcome, region, metrics, finding) with
        NO proposal or eval text; those are inspect_episode's job, one
        experiment at a time. ``buckets=True`` (default) returns
        {relevant, contrasting, diverse}; contrasting/diverse point at
        differently-outcomed or uncovered regions (legitimate gap-finding)."""
        limit = max(1, min(int(limit), 50))
        experiments = filter_experiments(
            self.load_experiments(), **(filters or {}),
        )
        if not buckets:
            from .retrieval import rank_experiments
            return [
                self._coverage_row(exp, score)
                for exp, score in rank_experiments(
                    experiments, query=query, limit=limit,
                )
            ]
        # Split ``limit`` across the three buckets while preserving intent.
        rel = max(1, (limit + 2) // 3 + (limit % 3 != 0))
        con = max(1, limit // 3)
        div = max(1, limit - rel - con)
        buckets_result = diverse_experiment_search(
            experiments, query=query,
            relevant=rel, contrasting=con, diverse=div,
        )
        return {
            name: [self._coverage_row(exp, score) for exp, score in hits]
            for name, hits in buckets_result.items()
        }

    @staticmethod
    def _coverage_row(exp, score=None) -> dict:
        """A coverage-only projection of one experiment: NO proposal text and
        NO eval_block (those carry direction semantics and are reserved for
        deliberate single-experiment inspection via inspect_episode)."""
        row = {
            "experiment_id": exp.experiment_id,
            "round": exp.round,
            "candidate": exp.candidate,
            "status": exp.status,
            "gate_passed": exp.gate_passed,
            "eligible": exp.eligible,
            "selected": exp.selected,
            "metrics": dict(exp.metrics),
            "changed_paths": list(exp.changed_paths),
            "finding_id": exp.finding_id,
        }
        if score is not None:
            row["score"] = round(float(score), 4)
        return row

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
