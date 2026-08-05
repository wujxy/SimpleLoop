"""DEPRECATED: deliberation signals compat wrapper.

The real implementation now lives in :mod:`simpleloop.explore`. This module
keeps the legacy ``compute_deliberation_signals`` *dict shape* alive so that
older callers and ``tests/test_deliberation_signals.py`` keep working without a
rewrite. New code should call ``MemoryService.analyze_explore(...)`` /
``simpleloop.explore.analyze_explore_health(...)`` and consume the structured
:class:`~simpleloop.explore.ExploreReport` instead.

Why a wrapper rather than ``ExploreReport.to_legacy_signals()``: the legacy
global block uses a *fixed 3-round window* with ``recent_eligible`` semantics,
while the new report uses a run-length ``consecutive_no_improve_rounds`` walk
and a 5-round display window. A pure mapping cannot reproduce both, so the
legacy global view is recomputed here on top of
:func:`simpleloop.explore.classify.classify_experiments` (same classification
rules, same thresholds, same rule strings) — the per-finding entries likewise.
"""
from __future__ import annotations

from typing import Iterable

from ..explore.classify import (
    classify_experiments,
    jaccard_overlap,
    sha_objective_map,
    tokenize,
)
from .experiment_index import Experiment


__all__ = ["compute_deliberation_signals", "tokenize", "jaccard_overlap"]


# --- text utilities (re-exported for the proposer's near-duplicate check) --

# Kept importable from this module for backward compatibility.
__all__ += ["sha_objective_map"]


# --- thresholds (legacy policy rules; rule strings must stay stable) -------

_FEASIBILITY_RISK_MIN_FAILURES = 2
_MECHANISM_CHALLENGE_MIN_NEUTRAL = 2
_CONTRADICTORY_MIN_REGRESSIONS = 1

# Legacy global (cross-finding) stall detection — fixed 3-round window.
_GLOBAL_STALL_WINDOW = 3          # look at the last N rounds
_GLOBAL_STALL_MIN_ELIGIBLE = 3    # need this many classifiable experiments
_GLOBAL_STALL_MIN_REGRESSIONS = 3  # regression_run threshold


# --- cross-finding global stall (legacy fixed-window semantics) -----------

def _compute_global_signal(
    findings: dict,
    experiments: list[Experiment],
    classifications,
    *,
    objective_key: str,
    lower_is_better: bool,
) -> dict | None:
    """Compute the legacy cross-finding view of the most recent 3 rounds.

    Per-finding signals can be evaded by opening a fresh Finding each round;
    this global view looks at the *sequence* of classifiable results across all
    findings in a time window, so it cannot be bypassed by renaming the
    question. Returns ``None`` when fewer than
    :data:`_GLOBAL_STALL_MIN_ELIGIBLE` classifiable experiments exist.
    """
    if not experiments:
        return None
    max_round = max(e.round for e in experiments)
    min_round = max_round - _GLOBAL_STALL_WINDOW + 1

    finding_mechs: dict[str, tuple[str, ...]] = {}
    if isinstance(findings, dict):
        for fid, f in findings.items():
            finding_mechs[fid] = getattr(f, "mechanisms", ()) or ()

    improvements = neutral = regressions = 0
    mechs_seen: dict[str, int] = {}
    for c in classifications:
        if c.round < min_round:
            continue
        if c.kind == "improvement":
            improvements += 1
        elif c.kind == "regression":
            regressions += 1
        elif c.kind == "neutral":
            neutral += 1
        else:
            continue  # unclassified — not counted
        for m in finding_mechs.get(c.finding_id or "", ()):
            mechs_seen[m] = mechs_seen.get(m, 0) + 1

    classifiable = improvements + neutral + regressions
    if classifiable < _GLOBAL_STALL_MIN_ELIGIBLE:
        return None

    top_mechs = sorted(mechs_seen, key=lambda m: (-mechs_seen[m], m))[:5]

    return {
        "recent_window": _GLOBAL_STALL_WINDOW,
        "recent_eligible": classifiable,
        "recent_improvements": improvements,
        "recent_neutral": neutral,
        "recent_regressions": regressions,
        "recent_mechanisms": top_mechs,
        "policy_signals": {
            "global_stall": {
                "active": improvements == 0,
                "rule": f"recent_improvements == 0 and "
                        f"recent_eligible >= {_GLOBAL_STALL_MIN_ELIGIBLE}",
            },
            "regression_run": {
                "active": regressions >= _GLOBAL_STALL_MIN_REGRESSIONS
                          and improvements == 0,
                "rule": f"recent_regressions >= "
                        f"{_GLOBAL_STALL_MIN_REGRESSIONS} and "
                        f"recent_improvements == 0",
            },
        },
    }


def compute_deliberation_signals(
    findings: dict,
    experiments: list[Experiment],
    *,
    current_round: int,
    hints_present: bool,
    objective_key: str | None,
    lower_is_better: bool,
) -> dict:
    """Return ``{first_round, hints_present, findings: [...], global}`` in the
    legacy dict shape. See module docstring for the deprecation note.

    Each finding entry carries ``facts`` (ledger counts) and ``policy_signals``
    (threshold booleans with their rule); ``global`` carries the cross-finding
    fixed-window view (``None`` on early rounds).
    """
    first_round = not experiments or not objective_key
    if first_round:
        return {
            "first_round": True,
            "hints_present": hints_present,
            "findings": [],
        }

    classifications = classify_experiments(
        experiments, objective_key, lower_is_better=lower_is_better,
    )

    # Bucket classifications by finding, preserving experiment order.
    by_finding: dict[str, list] = {}
    for c in classifications:
        if c.finding_id:
            by_finding.setdefault(c.finding_id, []).append(c)

    # Also need raw experiments per finding for attempts / failures / selected.
    exps_by_finding: dict[str, list[Experiment]] = {}
    for exp in experiments:
        if exp.finding_id:
            exps_by_finding.setdefault(exp.finding_id, []).append(exp)

    entries: list[dict] = []
    for fid, cls in by_finding.items():
        exps = exps_by_finding.get(fid, [])
        attempts = len(exps)
        if attempts == 0:
            continue
        implementation_failures = sum(1 for e in exps if not e.gate_passed)
        improvements = sum(1 for c in cls if c.kind == "improvement")
        neutral = sum(1 for c in cls if c.kind == "neutral")
        regressions = sum(1 for c in cls if c.kind == "regression")
        selected = sum(1 for e in exps if e.selected)
        facts = {
            "attempts": attempts,
            "evaluable_attempts": sum(1 for e in exps if e.eligible),
            "implementation_failures": implementation_failures,
            "eligible_improvements": improvements,
            "eligible_neutral": neutral,
            "eligible_regressions": regressions,
            "selected": selected,
        }
        policy_signals = {
            "feasibility_risk": {
                "active": implementation_failures >= _FEASIBILITY_RISK_MIN_FAILURES,
                "rule": f"implementation_failures >= {_FEASIBILITY_RISK_MIN_FAILURES}",
            },
            "mechanism_challenge": {
                "active": neutral >= _MECHANISM_CHALLENGE_MIN_NEUTRAL,
                "rule": f"eligible_neutral >= {_MECHANISM_CHALLENGE_MIN_NEUTRAL}",
            },
            "contradictory_result": {
                "active": regressions >= _CONTRADICTORY_MIN_REGRESSIONS,
                "rule": f"eligible_regressions >= {_CONTRADICTORY_MIN_REGRESSIONS}",
            },
        }
        finding = findings.get(fid) if isinstance(findings, dict) else None
        entries.append({
            "id": fid,
            "question": (finding.question if finding else ""),
            "facts": facts,
            "policy_signals": policy_signals,
            "last_touched_round": (
                finding.last_touched_round if finding else
                max(e.round for e in exps)
            ),
        })

    def any_active(entry: dict) -> bool:
        return any(v["active"] for v in entry["policy_signals"].values())

    entries.sort(key=lambda e: (not any_active(e), -e["facts"]["attempts"], e["id"]))

    global_signal = _compute_global_signal(
        findings, experiments, classifications,
        objective_key=objective_key,
        lower_is_better=lower_is_better,
    )

    return {
        "first_round": False,
        "hints_present": hints_present,
        "findings": entries,
        "global": global_signal,
    }
