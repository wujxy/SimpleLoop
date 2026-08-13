"""Deterministic Research Frontier view.

Frontier is a derived read over Findings + Experiments — it is never a source
of truth and never written back. See design doc §7.
"""
from __future__ import annotations

from .experiment_index import Experiment
from .models import Finding


def compute_frontier(
    findings: dict[str, Finding],
    experiments: list[Experiment],
    *,
    current_round: int,
    dormancy_rounds: int,
    editable_prefixes: tuple[str, ...] = (),
    experiments_by_id: dict[str, Experiment] | None = None,
) -> dict:
    """Return {active_findings, dormant_count, archived_count,
    experiment_count, coverage{code_regions, mechanisms}}.

    - ``active_findings``: state==active, sorted by last_touched desc.
    - ``dormant_count`` / ``archived_count``: bookkeeping counts, not
      inlined so the startup pack stays compact.
    - ``coverage.code_regions``: experiment count per editable prefix
      (falls back to top-2-segment buckets when no prefixes are supplied).
    - ``coverage.mechanisms``: finding-count per mechanism tag.

    ``attempts`` is the count of a finding's experiment_refs that have
    actually landed in history (run + recorded). When no
    ``experiments_by_id`` is supplied (e.g. unit tests), it falls back to
    ``len(experiment_refs)`` (predicted attempts, including not-yet-run).
    """
    active: list[dict] = []
    dormant = 0
    archived = 0
    for finding in findings.values():
        state = _effective_state(
            finding,
            current_round=current_round,
            dormancy_rounds=dormancy_rounds,
        )
        if state == "active":
            refs = finding.experiment_refs
            if experiments_by_id is not None:
                attempts = sum(1 for r in refs if r in experiments_by_id)
            else:
                attempts = len(refs)
            active.append({
                "id": finding.id,
                "question": finding.question,
                "attempts": attempts,
                "last_touched_round": finding.last_touched_round,
                "mechanisms": list(finding.mechanisms),
                "code_regions": list(finding.code_regions),
            })
        elif state == "dormant":
            dormant += 1
        elif state == "archived":
            archived += 1
    active.sort(
        key=lambda entry: (-entry["last_touched_round"], entry["id"]),
    )

    coverage_regions = _coverage_by_region(experiments, editable_prefixes)
    coverage_mechanisms = _coverage_by_mechanism(findings.values())

    return {
        "active_findings": active,
        "dormant_count": dormant,
        "archived_count": archived,
        "experiment_count": len(experiments),
        "coverage": {
            "code_regions": coverage_regions,
            "mechanisms": coverage_mechanisms,
        },
    }


def _effective_state(
    finding: Finding, *, current_round: int, dormancy_rounds: int,
) -> str:
    """Compute the state the frontier should present, layering a dormancy
    check on top of the stored state. Stored ``archived`` and ``open`` pass
    through; ``active`` falls back to ``dormant`` when idle long enough."""
    if finding.state in {"archived", "open"}:
        return finding.state
    if finding.state == "dormant":
        return "dormant"
    # active
    if current_round - finding.last_touched_round > dormancy_rounds:
        return "dormant"
    return "active"


def _coverage_by_region(
    experiments: list[Experiment],
    editable_prefixes: tuple[str, ...],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    if editable_prefixes:
        for exp in experiments:
            hits: set[str] = set()
            for path in exp.changed_paths:
                for prefix in editable_prefixes:
                    if path == prefix or path.startswith(prefix):
                        hits.add(prefix)
                        break
            for prefix in hits:
                counts[prefix] = counts.get(prefix, 0) + 1
        # Include declared prefixes even when unexplored, so gaps are visible.
        for prefix in editable_prefixes:
            counts.setdefault(prefix, 0)
    else:
        for exp in experiments:
            for path in exp.changed_paths:
                bucket = _bucket_prefix(path)
                counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def _coverage_by_mechanism(findings) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        for mech in finding.mechanisms:
            counts[mech] = counts.get(mech, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def _bucket_prefix(path: str) -> str:
    parts = path.split("/")
    if len(parts) <= 2:
        return path
    return "/".join(parts[:2])
