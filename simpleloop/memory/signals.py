"""Deterministic deliberation signals for the Proposer Scientist.

These are *facts and threshold-derived policy nudges* computed from the
immutable Experiment Ledger and the Finding archive — never LLM judgments,
never scientific verdicts. They exist to give the Scientist, at wakeup, an
honest picture of which directions have stalled, regressed, or repeatedly
failed to even become evaluable, so it can challenge or reframe instead of
producing another small variant.

Design notes (see docs/simpleloop_scientist_deliberation_policy_design.md and
the refactor plan):

- ``facts`` are ledger counts; ``policy_signals`` are threshold-derived booleans
  with their rule attached. The two are kept separate so the Scientist never
  mistakes a harness heuristic for an experimental conclusion.
- A gate failure is treated as *feasibility* (the experiment did not
  effectively test the mechanism), not a mechanism refutation: it may mean the
  change was infeasible, too large, or broke a constraint.
- ``selected`` is contaminated by sibling competition, so eligible attempts are
  classified by ``objective`` vs the *parent* objective they were built on, not
  by whether they won the round.
- Parent objective is recovered from a ``sha -> objective`` map built from
  candidate rows; experiments whose parent (e.g. the baseline) is not in the
  map are left unclassified rather than force-fit.
"""
from __future__ import annotations

import re
from typing import Iterable

from .experiment_index import Experiment


# --- text utilities (also used by the proposer's near-duplicate check) ------

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> frozenset[str]:
    """Coarse, deterministic tokenization for overlap comparison.

    Lowercased alphanumeric tokens; stop-word filtering is intentionally
    absent so the comparison stays a blunt lexical signal, not a semantic one.
    """
    if not text:
        return frozenset()
    return frozenset(_TOKEN_RE.findall(text.lower()))


def jaccard_overlap(a: str, b: str) -> float:
    """Jaccard similarity over token sets. 0.0 when either is empty."""
    sa, sb = tokenize(a), tokenize(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


# --- per-experiment objective classification ------------------------------

def _sha_objective_map(
    experiments: Iterable[Experiment], objective_key: str,
) -> dict[str, float]:
    """Map each candidate sha to its objective value. Only candidates that
    produced a sha and a numeric objective contribute; the parent of a later
    experiment is resolved through this map."""
    out: dict[str, float] = {}
    for exp in experiments:
        sha = exp.candidate_sha
        if not sha:
            continue
        value = exp.metrics.get(objective_key)
        if isinstance(value, (int, float)):
            out[sha] = float(value)
    return out


def _classify_objective(
    obj: float, parent_obj: float, *, lower_is_better: bool,
) -> str:
    """Return ``improvement`` / ``neutral`` / ``regression`` for an eligible
    experiment's objective relative to its parent, using a relative tolerance
    so float noise is not read as a change."""
    eps = abs(parent_obj) * 1e-6 + 1e-9
    delta = obj - parent_obj
    if lower_is_better:
        improved = delta < -eps
        regressed = delta > eps
    else:
        improved = delta > eps
        regressed = delta < -eps
    if improved:
        return "improvement"
    if regressed:
        return "regression"
    return "neutral"


# --- thresholds (policy rules) --------------------------------------------

_FEASIBILITY_RISK_MIN_FAILURES = 2
_MECHANISM_CHALLENGE_MIN_NEUTRAL = 2
_CONTRADICTORY_MIN_REGRESSIONS = 1

# Global (cross-finding) stall detection — see plan §修复 1.
_GLOBAL_STALL_WINDOW = 3          # look at the last N rounds
_GLOBAL_STALL_MIN_ELIGIBLE = 3    # need this many classifiable experiments
_GLOBAL_STALL_MIN_REGRESSIONS = 3  # regression_run threshold


# --- cross-finding global stall -------------------------------------------

def _compute_global_signal(
    findings: dict,
    experiments: list[Experiment],
    sha_obj: dict[str, float],
    *,
    objective_key: str,
    lower_is_better: bool,
) -> dict | None:
    """Compute a cross-finding view of the most recent rounds.

    Per-finding signals can be evaded by opening a fresh Finding each round
    (each finding then has only one experiment, so per-finding neutral counts
    never reach the mechanism_challenge threshold). This global view looks at
    the *sequence* of eligible results across all findings in a time window,
    so it cannot be bypassed by renaming the question.

    Returns ``None`` when there is not enough classifiable history (early
    rounds, or too few eligible experiments). Otherwise returns ``{recent_window,
    recent_eligible, recent_improvements, recent_neutral, recent_regressions,
    recent_mechanisms, policy_signals}``.
    """
    if not experiments:
        return None
    # Round range of the last _GLOBAL_STALL_WINDOW rounds.
    max_round = max(e.round for e in experiments)
    min_round = max_round - _GLOBAL_STALL_WINDOW + 1
    # Map finding_id -> mechanisms for mechanism collection.
    finding_mechs: dict[str, tuple[str, ...]] = {}
    if isinstance(findings, dict):
        for fid, f in findings.items():
            finding_mechs[fid] = getattr(f, "mechanisms", ()) or ()

    recent_eligible = [
        e for e in experiments
        if e.eligible and e.round >= min_round
    ]
    improvements = neutral = regressions = 0
    mechs_seen: dict[str, int] = {}
    for e in recent_eligible:
        obj = e.metrics.get(objective_key)
        parent_obj = sha_obj.get(e.parent_sha)
        if not isinstance(obj, (int, float)) or parent_obj is None:
            continue
        kind = _classify_objective(
            float(obj), float(parent_obj), lower_is_better=lower_is_better,
        )
        if kind == "improvement":
            improvements += 1
        elif kind == "regression":
            regressions += 1
        else:
            neutral += 1
        for m in finding_mechs.get(e.finding_id or "", ()):
            mechs_seen[m] = mechs_seen.get(m, 0) + 1

    classifiable = improvements + neutral + regressions
    if classifiable < _GLOBAL_STALL_MIN_ELIGIBLE:
        return None

    # Top-5 mechanisms by frequency, to keep the startup pack compact.
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
    """Return ``{first_round, hints_present, findings: [...], global}`` where
    each finding entry carries ``facts`` (ledger counts) and ``policy_signals``
    (threshold booleans with their rule), and ``global`` carries a
    cross-finding view of the most recent rounds (``None`` on early rounds).

    The global signal exists because per-finding signals can be evaded by
    opening a fresh Finding each round — each finding then has only one
    experiment, so per-finding neutral counts never reach the
    ``mechanism_challenge`` threshold. The global view looks at the sequence
    of eligible results across *all* findings in a time window, so it cannot
    be bypassed by renaming the question.

    Brand-new findings with no experiments are omitted: they have no signal
    yet. Findings are returned with policy-signal findings first, then by
    descending attempts.
    """
    first_round = not experiments
    if first_round or not objective_key:
        return {
            "first_round": True,
            "hints_present": hints_present,
            "findings": [],
        }

    sha_obj = _sha_objective_map(experiments, objective_key)

    # Bucket experiments by finding.
    by_finding: dict[str, list[Experiment]] = {}
    for exp in experiments:
        if exp.finding_id:
            by_finding.setdefault(exp.finding_id, []).append(exp)

    entries: list[dict] = []
    for fid, exps in by_finding.items():
        exps_sorted = sorted(exps, key=lambda e: (e.round, e.candidate))
        attempts = len(exps_sorted)
        if attempts == 0:
            continue
        implementation_failures = sum(1 for e in exps_sorted if not e.gate_passed)
        eligible_exps = [e for e in exps_sorted if e.eligible]
        improvements = neutral = regressions = 0
        for e in eligible_exps:
            obj = e.metrics.get(objective_key)
            parent_obj = sha_obj.get(e.parent_sha)
            if not isinstance(obj, (int, float)) or parent_obj is None:
                continue  # unclassifiable parent (e.g. baseline) — not force-fit
            kind = _classify_objective(
                float(obj), float(parent_obj), lower_is_better=lower_is_better,
            )
            if kind == "improvement":
                improvements += 1
            elif kind == "regression":
                regressions += 1
            else:
                neutral += 1
        selected = sum(1 for e in exps_sorted if e.selected)
        facts = {
            "attempts": attempts,
            "evaluable_attempts": len(eligible_exps),
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
                max(e.round for e in exps_sorted)
            ),
        })

    def any_active(entry: dict) -> bool:
        return any(v["active"] for v in entry["policy_signals"].values())

    entries.sort(key=lambda e: (not any_active(e), -e["facts"]["attempts"], e["id"]))

    global_signal = _compute_global_signal(
        findings, experiments, sha_obj,
        objective_key=objective_key,
        lower_is_better=lower_is_better,
    )

    return {
        "first_round": False,
        "hints_present": hints_present,
        "findings": entries,
        "global": global_signal,
    }
