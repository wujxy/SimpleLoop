"""The Explore search-health monitor.

Single entry point :func:`analyze_explore_health` computes an
:class:`ExploreReport` from the immutable Experiment Ledger and the Finding
archive — pure facts and threshold-derived policy signals, never LLM verdicts.

Three aggregation levels (see ``docs/explore_refactor_plan.md``):

- **per-finding** — feasibility / mechanism-challenge / contradictory nudges.
- **family** — cross-finding local-exploitation detection (the new layer).
- **global** — recent-round run-length stall view.

``consecutive_no_improve`` walks are the deliberate alternative to the legacy
fixed-window ``global_stall``: they express an unbroken run of no-improvement
and are reset only by an actual improvement, not by the window sliding.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from .classify import classify_experiments
from .families import assign_families
from .models import (
    KIND_IMPROVEMENT,
    KIND_NEUTRAL,
    KIND_REGRESSION,
    SEVERITY_CHALLENGE,
    SEVERITY_WATCH,
    ExploreReport,
    FamilyExploreHealth,
    FindingExploreHealth,
    GlobalExploreHealth,
    ObjectiveClassification,
    PolicySignal,
)

if TYPE_CHECKING:
    # See classify.py: avoid an import cycle with simpleloop.memory.
    from ..memory.experiment_index import Experiment
    from ..memory.models import Finding


# --- thresholds (policy rules) --------------------------------------------

# per-finding (unchanged semantics)
FINDING_FEASIBILITY_FAILURES = 2
FINDING_MECHANISM_NEUTRAL = 2
FINDING_CONTRADICTORY_REGRESSIONS = 1

# family-level
FAMILY_STALL_CONSECUTIVE_NO_IMPROVE = 3
FAMILY_REGRESSION_MIN = 2
FAMILY_OVEREXPLOITED_ATTEMPTS = 5
FAMILY_FEASIBILITY_FAILURES = 2

# global
GLOBAL_STALL_CONSECUTIVE_NO_IMPROVE_ROUNDS = 2
GLOBAL_REGRESSION_MIN = 3
GLOBAL_RECENT_WINDOW = 5

# An improvement smaller than this fraction of the parent objective does not
# reset the stall counter — it decays by one instead. Prevents marginal
# improvements from masking real stagnation.
MARGINAL_IMPROVEMENT_FRACTION = 0.02

# how many recent rounds a family reports in recent_rounds
FAMILY_RECENT_ROUNDS = 5


def analyze_explore_health(
    findings: dict[str, Finding],
    experiments: list[Experiment],
    *,
    current_round: int,
    objective_key: str | None,
    lower_is_better: bool,
) -> ExploreReport:
    """Compute the full search-health report.

    - ``not experiments`` → ``first_round=True`` (everything else empty).
    - experiments present but no ``objective_key`` → ``analysis_eligible=False``:
      non-classification fact counts are still produced, all signals inactive,
      ``global_health=None``, ``challenge_required=False``.
    - otherwise per-finding / family / global health is computed and
      ``challenge_required`` reflects any active ``challenge``-severity family
      or global signal.
    """
    del current_round  # reserved for future dormancy-aware logic; findings carry rounds.

    if not experiments:
        return ExploreReport(
            first_round=True, analysis_eligible=False,
        )

    first_round = False
    if not objective_key:
        return ExploreReport(
            first_round=first_round,
            analysis_eligible=False,
            findings=_finding_fact_only(findings, experiments),
            families=_family_fact_only(findings, experiments),
            global_health=None,
            challenge_required=False,
        )

    classifications = classify_experiments(
        experiments, objective_key, lower_is_better=lower_is_better,
    )

    finding_health = _build_finding_health(findings, experiments, classifications)
    family_health = _build_family_health(findings, experiments, classifications)
    global_health = _build_global_health(experiments, classifications, findings)

    challenge_reasons: list[str] = []
    for fam in family_health:
        for sig in fam.policy_signals:
            if sig.active and sig.severity == SEVERITY_CHALLENGE:
                challenge_reasons.append(f"{sig.name}:{fam.family_id}")
    if global_health is not None:
        for sig in global_health.policy_signals:
            if sig.active and sig.severity == SEVERITY_CHALLENGE:
                challenge_reasons.append(sig.name)

    challenge_required = bool(challenge_reasons)

    return ExploreReport(
        first_round=first_round,
        analysis_eligible=True,
        findings=finding_health,
        families=family_health,
        global_health=global_health,
        challenge_required=challenge_required,
        challenge_reasons=tuple(challenge_reasons),
    )


# --- per-finding -----------------------------------------------------------

def _finding_fact_only(
    findings: dict[str, Finding], experiments: list[Experiment],
) -> tuple[FindingExploreHealth, ...]:
    """Fact counts that do not need the objective (used when
    ``analysis_eligible`` is False). All policy signals inactive."""
    out: list[FindingExploreHealth] = []
    by_finding = _group_experiments_by_finding(experiments)
    for fid, exps in by_finding.items():
        finding = findings.get(fid)
        out.append(FindingExploreHealth(
            finding_id=fid,
            question=(finding.question if finding else ""),
            attempts=len(exps),
            evaluable_attempts=sum(1 for e in exps if e.eligible),
            implementation_failures=sum(1 for e in exps if not e.gate_passed),
            improvements=0, neutral=0, regressions=0,
            selected=sum(1 for e in exps if e.selected),
            last_touched_round=_last_touched(finding, exps),
            policy_signals=(),
        ))
    return _sort_findings(out)


def _build_finding_health(
    findings: dict[str, Finding],
    experiments: list[Experiment],
    classifications: list[ObjectiveClassification],
) -> tuple[FindingExploreHealth, ...]:
    by_finding = _group_classifications_by_finding(classifications)
    out: list[FindingExploreHealth] = []
    for fid, cls in by_finding.items():
        finding = findings.get(fid)
        attempts = len(cls)
        evaluable = sum(1 for c in cls if c.eligible)
        impl_failures = sum(
            1 for e in experiments
            if e.finding_id == fid and not e.gate_passed
        )
        improvements = sum(1 for c in cls if c.kind == KIND_IMPROVEMENT)
        neutral = sum(1 for c in cls if c.kind == KIND_NEUTRAL)
        regressions = sum(1 for c in cls if c.kind == KIND_REGRESSION)
        selected = sum(
            1 for e in experiments if e.finding_id == fid and e.selected
        )
        signals = (
            PolicySignal(
                name="feasibility_risk",
                active=impl_failures >= FINDING_FEASIBILITY_FAILURES,
                rule=f"implementation_failures >= "
                     f"{FINDING_FEASIBILITY_FAILURES}",
                severity=SEVERITY_WATCH,
            ),
            PolicySignal(
                name="mechanism_challenge",
                active=neutral >= FINDING_MECHANISM_NEUTRAL,
                rule=f"eligible_neutral >= {FINDING_MECHANISM_NEUTRAL}",
                severity=SEVERITY_WATCH,
            ),
            PolicySignal(
                name="contradictory_result",
                active=regressions >= FINDING_CONTRADICTORY_REGRESSIONS,
                rule=f"eligible_regressions >= "
                     f"{FINDING_CONTRADICTORY_REGRESSIONS}",
                severity=SEVERITY_WATCH,
            ),
        )
        out.append(FindingExploreHealth(
            finding_id=fid,
            question=(finding.question if finding else ""),
            attempts=attempts,
            evaluable_attempts=evaluable,
            implementation_failures=impl_failures,
            improvements=improvements,
            neutral=neutral,
            regressions=regressions,
            selected=selected,
            last_touched_round=_last_touched(finding, _exps_for_finding(experiments, fid)),
            policy_signals=signals,
        ))
    return _sort_findings(out)


def _sort_findings(
    findings: list[FindingExploreHealth],
) -> tuple[FindingExploreHealth, ...]:
    def any_active(f: FindingExploreHealth) -> bool:
        return any(s.active for s in f.policy_signals)
    return tuple(sorted(
        findings,
        key=lambda f: (not any_active(f), -f.attempts, f.finding_id),
    ))


# --- family ---------------------------------------------------------------

def _family_fact_only(
    findings: dict[str, Finding], experiments: list[Experiment],
) -> tuple[FamilyExploreHealth, ...]:
    """Family fact counts without classification; all signals inactive."""
    return _build_family_health(findings, experiments, [])


def _build_family_health(
    findings: dict[str, Finding],
    experiments: list[Experiment],
    classifications: list[ObjectiveClassification],
) -> tuple[FamilyExploreHealth, ...]:
    if not findings:
        return ()
    family_keys = assign_families(findings, experiments)
    # Map family_id -> list of (finding_id) and aggregate experiments.
    by_family: dict[str, dict] = {}
    for fid, key in family_keys.items():
        by_family.setdefault(key.family_id, {
            "key": key, "finding_ids": [], "experiments": [],
        })
        by_family[key.family_id]["finding_ids"].append(fid)

    # Attach experiments to their finding's family.
    cls_by_eid = {c.experiment_id: c for c in classifications}
    for exp in experiments:
        fid = exp.finding_id
        if not fid or fid not in family_keys:
            continue
        key = family_keys[fid]
        by_family[key.family_id]["experiments"].append(exp)

    out: list[FamilyExploreHealth] = []
    for family_id, data in by_family.items():
        key = data["key"]
        exps = data["experiments"]
        exps_sorted = sorted(exps, key=lambda e: (e.round, e.candidate))
        attempts = len(exps_sorted)
        evaluable = sum(1 for e in exps_sorted if e.eligible)
        impl_failures = sum(1 for e in exps_sorted if not e.gate_passed)
        improvements = neutral = regressions = 0
        for e in exps_sorted:
            c = cls_by_eid.get(e.experiment_id)
            if c is None:
                continue
            if c.kind == KIND_IMPROVEMENT:
                improvements += 1
            elif c.kind == KIND_NEUTRAL:
                neutral += 1
            elif c.kind == KIND_REGRESSION:
                regressions += 1
        selected = sum(1 for e in exps_sorted if e.selected)
        consecutive = _consecutive_no_improve(exps_sorted, cls_by_eid)
        recent_rounds = _recent_rounds(exps_sorted)
        signals = _family_signals(
            consecutive, regressions, improvements, attempts, selected,
            impl_failures,
        )
        out.append(FamilyExploreHealth(
            family_id=family_id,
            code_region=key.region_bucket,
            mechanisms=(key.mechanism_bucket,),
            finding_ids=tuple(sorted(data["finding_ids"])),
            attempts=attempts,
            evaluable_attempts=evaluable,
            implementation_failures=impl_failures,
            improvements=improvements,
            neutral=neutral,
            regressions=regressions,
            selected=selected,
            consecutive_no_improve=consecutive,
            recent_rounds=recent_rounds,
            policy_signals=signals,
        ))
    return _sort_families(out)


def _family_signals(
    consecutive: int, regressions: int, improvements: int,
    attempts: int, selected: int, impl_failures: int,
) -> tuple[PolicySignal, ...]:
    return (
        PolicySignal(
            name="family_stall",
            active=consecutive >= FAMILY_STALL_CONSECUTIVE_NO_IMPROVE,
            rule=f"consecutive_no_improve >= "
                 f"{FAMILY_STALL_CONSECUTIVE_NO_IMPROVE}",
            severity=SEVERITY_CHALLENGE,
        ),
        PolicySignal(
            name="family_regressing",
            active=regressions >= FAMILY_REGRESSION_MIN and improvements == 0,
            rule=f"regressions >= {FAMILY_REGRESSION_MIN} and "
                 f"improvements == 0",
            severity=SEVERITY_CHALLENGE,
        ),
        PolicySignal(
            name="family_overexploited",
            active=attempts >= FAMILY_OVEREXPLOITED_ATTEMPTS and selected == 0,
            rule=f"attempts >= {FAMILY_OVEREXPLOITED_ATTEMPTS} and "
                 f"selected == 0",
            severity=SEVERITY_WATCH,
        ),
        PolicySignal(
            name="family_feasibility",
            active=(impl_failures >= FAMILY_FEASIBILITY_FAILURES
                    and improvements == 0),
            rule=f"implementation_failures >= "
                 f"{FAMILY_FEASIBILITY_FAILURES} and improvements == 0",
            severity=SEVERITY_WATCH,
        ),
    )


def _sort_families(
    families: list[FamilyExploreHealth],
) -> tuple[FamilyExploreHealth, ...]:
    def has_challenge(f: FamilyExploreHealth) -> bool:
        return any(
            s.active and s.severity == SEVERITY_CHALLENGE
            for s in f.policy_signals
        )
    return tuple(sorted(
        families,
        key=lambda f: (not has_challenge(f), -f.consecutive_no_improve, f.family_id),
    ))


def _consecutive_no_improve(
    exps_sorted: list[Experiment],
    cls_by_eid: dict[str, ObjectiveClassification],
) -> int:
    """Walk attempts newest-first; stop at the first significant improvement.

    A marginal improvement (< MARGINAL_IMPROVEMENT_FRACTION of parent) decays
    the counter by one instead of stopping. ``unclassified`` attempts
    (unparseable parent) are *skipped* — they neither reset nor increment the
    run. Everything else walked (neutral, regression, non-eligible/gate-failed)
    counts +1. Gaps across rounds do not reset.
    """
    count = 0
    for e in reversed(exps_sorted):
        c = cls_by_eid.get(e.experiment_id)
        if c is None:
            count += 1
            continue
        if c.kind == KIND_IMPROVEMENT:
            if (c.objective is not None and c.parent_objective
                    and abs(c.parent_objective) > 1e-12):
                frac = abs(c.objective - c.parent_objective) / abs(c.parent_objective)
                if frac < MARGINAL_IMPROVEMENT_FRACTION:
                    count = max(0, count - 1)
                    continue
            break
        if c.kind == KIND_NEUTRAL or c.kind == KIND_REGRESSION:
            count += 1
            continue
        continue
    return count


def _recent_rounds(exps_sorted: list[Experiment]) -> tuple[int, ...]:
    rounds = sorted({e.round for e in exps_sorted})
    if len(rounds) > FAMILY_RECENT_ROUNDS:
        rounds = rounds[-FAMILY_RECENT_ROUNDS:]
    return tuple(rounds)


# --- global ---------------------------------------------------------------

def _build_global_health(
    experiments: list[Experiment],
    classifications: list[ObjectiveClassification],
    findings: dict[str, Finding],
) -> GlobalExploreHealth | None:
    if not experiments:
        return None
    cls_by_eid = {c.experiment_id: c for c in classifications}
    max_round = max(e.round for e in experiments)
    min_round = max_round - GLOBAL_RECENT_WINDOW + 1

    # Recent-window counts (display + regression_run basis).
    recent_imp = recent_neu = recent_reg = 0
    mechs_seen: dict[str, int] = {}
    finding_mechs = {
        fid: tuple(f.mechanisms or ()) for fid, f in findings.items()
    }
    for c in classifications:
        if c.round < min_round:
            continue
        if c.kind == KIND_IMPROVEMENT:
            recent_imp += 1
        elif c.kind == KIND_NEUTRAL:
            recent_neu += 1
        elif c.kind == KIND_REGRESSION:
            recent_reg += 1
        for m in finding_mechs.get(c.finding_id or "", ()):
            mechs_seen[m] = mechs_seen.get(m, 0) + 1

    # Run-length walk over rounds (latest first). A *significant* improvement
    # (>= MARGINAL_IMPROVEMENT_FRACTION of the parent objective) stops the walk.
    # A *marginal* improvement decays the counter by one instead of stopping —
    # it does not reset the stall, but it is not fully ignored either. Rounds
    # with no classifiable experiment are skipped (neither reset nor increment).
    by_round: dict[int, list[ObjectiveClassification]] = {}
    for c in classifications:
        if c.kind in (KIND_IMPROVEMENT, KIND_NEUTRAL, KIND_REGRESSION):
            by_round.setdefault(c.round, []).append(c)
    consecutive_rounds = 0
    for r in sorted(by_round.keys(), reverse=True):
        kinds = [c.kind for c in by_round[r]]
        if KIND_IMPROVEMENT in kinds:
            # Check whether the best improvement this round is marginal.
            best_frac = 0.0
            for c in by_round[r]:
                if (c.kind == KIND_IMPROVEMENT
                        and c.objective is not None
                        and c.parent_objective
                        and abs(c.parent_objective) > 1e-12):
                    frac = abs(c.objective - c.parent_objective) / abs(c.parent_objective)
                    best_frac = max(best_frac, frac)
            if best_frac >= MARGINAL_IMPROVEMENT_FRACTION:
                break  # significant improvement — stop the walk
            # marginal improvement — decay but continue
            consecutive_rounds = max(0, consecutive_rounds - 1)
            continue
        consecutive_rounds += 1

    top_mechs = sorted(
        mechs_seen, key=lambda m: (-mechs_seen[m], m),
    )[:5]

    signals = (
        PolicySignal(
            name="global_stall",
            active=(consecutive_rounds
                    >= GLOBAL_STALL_CONSECUTIVE_NO_IMPROVE_ROUNDS),
            rule=f"consecutive_no_improve_rounds >= "
                 f"{GLOBAL_STALL_CONSECUTIVE_NO_IMPROVE_ROUNDS}",
            severity=SEVERITY_CHALLENGE,
        ),
        PolicySignal(
            name="global_regression_run",
            active=(recent_reg >= GLOBAL_REGRESSION_MIN
                    and recent_imp == 0),
            rule=f"recent_regressions >= {GLOBAL_REGRESSION_MIN} and "
                 f"recent_improvements == 0",
            severity=SEVERITY_CHALLENGE,
        ),
    )

    evaluable = sum(1 for e in experiments if e.eligible)
    return GlobalExploreHealth(
        attempts=len(experiments),
        evaluable_attempts=evaluable,
        recent_window=GLOBAL_RECENT_WINDOW,
        recent_improvements=recent_imp,
        recent_neutral=recent_neu,
        recent_regressions=recent_reg,
        consecutive_no_improve_rounds=consecutive_rounds,
        recent_mechanisms=tuple(top_mechs),
        policy_signals=signals,
    )


# --- shared helpers --------------------------------------------------------

def _group_experiments_by_finding(
    experiments: list[Experiment],
) -> dict[str, list[Experiment]]:
    by_finding: dict[str, list[Experiment]] = {}
    for exp in experiments:
        fid = exp.finding_id
        if not fid:
            continue
        by_finding.setdefault(fid, []).append(exp)
    return by_finding


def _group_classifications_by_finding(
    classifications: list[ObjectiveClassification],
) -> dict[str, list[ObjectiveClassification]]:
    by_finding: dict[str, list[ObjectiveClassification]] = {}
    for c in classifications:
        fid = c.finding_id
        if not fid:
            continue
        by_finding.setdefault(fid, []).append(c)
    return by_finding


def _exps_for_finding(
    experiments: list[Experiment], fid: str,
) -> list[Experiment]:
    return [e for e in experiments if e.finding_id == fid]


def _last_touched(finding: Finding | None, exps: Iterable[Experiment]) -> int:
    if finding is not None and finding.last_touched_round is not None:
        exp_rounds = [e.round for e in exps]
        candidates = [finding.last_touched_round, *exp_rounds]
        if candidates:
            return max(candidates)
    return max((e.round for e in exps), default=0)
