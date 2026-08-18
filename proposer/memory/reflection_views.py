"""Deterministic aggregate trajectory views for the Reflection session.

Reflection audits the RECENT Scientist's research behavior. Its evidence is
not round-granularity history (each round looks locally reasonable — that is
exactly the failure mode) but trajectory-level aggregates that are invisible
at round granularity: mechanism-family concentration, the pre-registered
expectation↔outcome ledger, the incumbent-improvement trajectory, and
same-region streaks.

Like frontier.py, this module is a pure derived read — deterministic, no IO,
never a source of truth, never written back. v1 deliberately uses exact
mechanism-tag matching (no normalization/clustering): tags are LLM-authored
free text, and a wrong cluster is worse than a coarse count.
"""
from __future__ import annotations

from .frontier import _bucket_prefix

# Wire-format mirrors of simpleloop/candidate.py (the proposer runs as a
# standalone CLI in its own process and must not import the harness; these
# parse the history rows the harness writes). Keep in sync with the source.
NOT_PERFORMED_STATUSES = frozenset({
    "IMPLEMENTATION_INCOMPLETE", "EXECUTOR_FAILED", "WORKER_FAILED",
})

# Outcomes under which a pre-registered ``would_weaken`` clause is plausibly
# engaged: the experiment did not become the new incumbent, so the commitment
# the Scientist wrote at submission time is awaiting adjudication. Firing is a
# SEMANTIC judgment the reflecting Scientist owns — this set only marks the
# rows worth adjudicating.
WATCH_OUTCOMES = frozenset({
    "FAILED_GATES",
    "PASSED_GATES_NOT_IMPROVED",
    "PASSED_GATES_IMPROVED_NOT_SELECTED",
    "INTERVENTION_NOT_PERFORMED",
})


def parse_stop_cause(block: str) -> str:
    """Read the ``stop_cause=`` prefix the harness executor stage writes
    into post-mortem eval blocks. Mirror of simpleloop.candidate."""
    if "stop_cause=" in block:
        cause = block.split("stop_cause=", 1)[1].split(";", 1)[0].strip()
        if cause:
            return cause
    return "crashed"


def mechanism_family_distribution(
    findings: dict, experiments: list, *,
    current_round: int, last_k_rounds: int = 8,
) -> dict[str, int]:
    """Count mechanism tags over experiments from the last ``last_k_rounds``
    rounds (joined to findings via ``experiment.finding_id``).

    A concentration here is not by itself a finding — mechanisms repeat
    because they work — but a distribution dominated by one family while
    returns stagnate is the continuation-inertia signature Reflection exists
    to see.
    """
    window_start = current_round - last_k_rounds
    counts: dict[str, int] = {}
    for exp in experiments:
        if exp.round < window_start or exp.round >= current_round:
            continue
        fid = getattr(exp, "finding_id", None)
        finding = findings.get(fid) if fid else None
        if finding is None:
            continue
        for mech in finding.mechanisms:
            counts[mech] = counts.get(mech, 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])))


def classify_improvement(
    metrics, incumbent, objective_key, lower_is_better,
) -> bool | None:
    """Three-valued improvement fact: True = beat the incumbent objective,
    False = did not, None = unknowable (no objective key, no incumbent
    value, or a non-numeric metric).

    Callers MUST render None as a neutral "not selected", never as "not
    improved": in a multi-candidate round a passing sibling that loses to a
    better sibling still improved on the incumbent, and labeling it
    NOT_IMPROVED matches the Scientist's own pre-registered weakening
    clause ("passes but does not improve") on false grounds (tiny-test r2c0:
    0.5919 → 0.2971 ms, labeled NOT_IMPROVED because r2c1 won).
    """
    if not objective_key or incumbent is None:
        return None
    value = (metrics or {}).get(objective_key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(incumbent, bool) or not isinstance(incumbent, (int, float)):
        return None
    return value < incumbent if lower_is_better else value > incumbent


def _incumbent_objective_before(experiments, objective_key, round_id):
    """The incumbent objective entering ``round_id``: the objective of the
    most recent selected candidate in a strictly earlier round (None when no
    such value exists)."""
    best = None
    if not objective_key:
        return None
    for exp in experiments:
        if not exp.selected or exp.round >= round_id:
            continue
        value = (exp.metrics or {}).get(objective_key)
        if (isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (best is None or exp.round > best[0])):
            best = (exp.round, value)
    return best[1] if best is not None else None


def _outcome_of(exp, experiments, objective_key, lower_is_better) -> str:
    """The five-valued outcome vocabulary shared by the expectation ledger
    and the commitment watchlist. Selection dominates; an intervention that
    never ran leaves its expectation UNTESTED, not weakened."""
    status = str(getattr(exp, "status", "") or "")
    if exp.selected:
        return "SELECTED_AS_NEW_INCUMBENT"
    if status in NOT_PERFORMED_STATUSES:
        return "INTERVENTION_NOT_PERFORMED"
    if status == "EVAL_FAILED":
        return "EVALUATION_FAILED"
    if exp.gate_passed:
        improved = classify_improvement(
            exp.metrics,
            _incumbent_objective_before(experiments, objective_key, exp.round),
            objective_key, lower_is_better)
        if improved is True:
            return "PASSED_GATES_IMPROVED_NOT_SELECTED"
        if improved is False:
            return "PASSED_GATES_NOT_IMPROVED"
        return "PASSED_GATES_NOT_SELECTED"
    return "FAILED_GATES"


def expectation_ledger(
    experiments: list, expectation_rows: dict, *,
    current_round: int, last_k_rounds: int = 8,
    objective_key: str | None = None, lower_is_better: bool = False,
) -> list[dict]:
    """Pair each recent experiment with the pre-registered expectation for
    its (round, slot), verbatim.

    ``preregistered=False`` entries (capture failed / predates the mechanism)
    are kept, not dropped: an outcome that cannot be checked against a prior
    commitment is itself evidence about how the recent Scientist commissioned
    experiments.
    """
    window_start = current_round - last_k_rounds
    rows: list[dict] = []
    for exp in sorted(experiments, key=lambda e: (e.round, e.candidate)):
        if exp.round <= window_start or exp.round >= current_round:
            continue
        outcome = _outcome_of(exp, experiments, objective_key, lower_is_better)
        row = (expectation_rows.get(exp.round) or {}).get("expectations") or []
        match = next(
            (item for item in row
             if isinstance(item, dict) and item.get("slot") == exp.candidate),
            None,
        )
        rows.append({
            "experiment_id": exp.experiment_id,
            "round": exp.round,
            "slot": exp.candidate,
            "preregistered": match is not None,
            "expectation": (match or {}).get("expectation"),
            "would_weaken": (match or {}).get("would_weaken"),
            "outcome": outcome,
            "gate_passed": exp.gate_passed,
            "selected": exp.selected,
            "metrics": dict(exp.metrics or {}),
        })
    return rows


def execution_outcomes(history_rows: list) -> dict:
    """Execution-cost aggregate: how many commissioned candidates actually
    reached evaluation, and what stopped the ones that did not (by status /
    post-mortem cause). Information only — what it means for direction is
    the reflecting Scientist's judgment."""
    performed = 0
    by_cause: dict[str, int] = {}
    for record in history_rows:
        if not isinstance(record, dict):
            continue
        rnd = record.get("round")
        if not isinstance(rnd, int):
            continue
        for cand in record.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            status = str(cand.get("status") or "")
            if status in NOT_PERFORMED_STATUSES:
                cause = status
                block = str(cand.get("eval_block") or "")
                if "stop_cause=" in block:
                    cause = f"{status}/{parse_stop_cause(block)}"
                by_cause[cause] = by_cause.get(cause, 0) + 1
            else:
                performed += 1
    return {"performed": performed, "not_performed_by_cause": by_cause}


def improvement_trajectory(
    history_rows: list, *,
    objective_key: str | None, lower_is_better: bool,
    last_k_rounds: int = 12,
) -> list[dict]:
    """Per-round best-eligible objective + running incumbent, over the last
    ``last_k_rounds`` task rounds.

    Best-eligible is computed directly from candidate metrics (selector
    semantics); ``require_improvement`` affects which candidate is SELECTED,
    not what the best measurable value was. ``no_incumbent_streak`` counts
    trailing consecutive rounds in which no candidate was selected — the
    diminishing-returns signature, as a number instead of a feeling.
    """
    rounds: list[dict] = []
    for record in history_rows:
        if not isinstance(record, dict):
            continue
        rnd = record.get("round")
        if not isinstance(rnd, int):
            continue
        best = None
        selected = False
        for cand in record.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            if cand.get("selected"):
                selected = True
            if not cand.get("eligible") or not objective_key:
                continue
            value = (cand.get("metrics") or {}).get(objective_key)
            if (isinstance(value, (int, float)) and not isinstance(value, bool)):
                if best is None or (
                    value < best if lower_is_better else value > best
                ):
                    best = value
        rounds.append({
            "round": rnd,
            "best_eligible": best,
            "selected": selected,
        })
    rounds.sort(key=lambda r: r["round"])
    rounds = rounds[-last_k_rounds:]

    incumbent = None
    streak = 0
    for entry in rounds:
        if entry["selected"] and entry["best_eligible"] is not None:
            incumbent = entry["best_eligible"]
            streak = 0
        else:
            streak += 1
        entry["incumbent_after"] = incumbent
        entry["no_incumbent_streak"] = streak
    return rounds


def commitment_watchlist(
    experiments: list, expectation_rows: dict, reflection_records: list, *,
    current_round: int, last_k_rounds: int = 8,
    objective_key: str | None = None, lower_is_better: bool = False,
) -> list[dict]:
    """Pre-registered weakening clauses whose outcomes plausibly engaged them
    and that the record does not show being revisited.

    The harness never adjudicates a clause (firing is semantic). What it CAN
    compute is the bookkeeping the Scientist would otherwise redo by hand at
    every reflection: which clause-bearing commitments ended in a
    non-incumbent outcome, and what the ledger shows since — later attempts
    on the same finding, later reflections. A row with zero follow-up is the
    r3c3/r5c0 pattern: the commitment fired and nobody noticed for rounds.
    """
    window_start = current_round - last_k_rounds
    reflection_rounds = sorted(
        int(r["round_id"]) for r in reflection_records
        if isinstance(r, dict) and isinstance(r.get("round_id"), int))
    attempts_after: dict[str, int] = {}
    for exp in experiments:
        fid = getattr(exp, "finding_id", None)
        if fid:
            attempts_after.setdefault(fid, []).append(exp.round)
    rows: list[dict] = []
    for exp in sorted(experiments, key=lambda e: (e.round, e.candidate)):
        if exp.round <= window_start or exp.round >= current_round:
            continue
        outcome = _outcome_of(exp, experiments, objective_key, lower_is_better)
        if outcome not in WATCH_OUTCOMES:
            continue
        match = next(
            (item for item in
             ((expectation_rows.get(exp.round) or {}).get("expectations") or [])
             if isinstance(item, dict) and item.get("slot") == exp.candidate),
            None)
        if match is None or not str(match.get("would_weaken") or "").strip():
            continue
        fid = getattr(exp, "finding_id", None)
        later = [r for r in (attempts_after.get(fid) or []) if r > exp.round]
        rows.append({
            "experiment_id": exp.experiment_id,
            "round": exp.round,
            "outcome": outcome,
            "would_weaken": str(match["would_weaken"]).strip(),
            "finding_id": fid,
            "attempts_on_finding_since": len(later),
            "reflections_since": sum(
                1 for r in reflection_rounds if r > exp.round),
        })
    return rows


def prescription_followthrough(
    reflection_records: list, experiments: list, *,
    current_round: int,
) -> list[dict]:
    """Each past reflection's structured prescriptions with the deterministic
    deltas since: experiments run, selections made, reflections held. The
    prescription's MEANING stays in its free text; this view only answers
    'what has the trajectory done since you wrote it down'."""
    rows: list[dict] = []
    for record in reflection_records:
        if not isinstance(record, dict):
            continue
        rnd = record.get("round_id")
        prescriptions = record.get("prescriptions")
        if not isinstance(rnd, int) or not isinstance(prescriptions, list):
            continue
        for i, text in enumerate(prescriptions):
            if not isinstance(text, str) or not text.strip():
                continue
            later_exps = [
                e.experiment_id for e in experiments
                if rnd < e.round < current_round]
            selected = [
                e.experiment_id for e in experiments
                if rnd < e.round < current_round and e.selected]
            rows.append({
                "id": f"P-r{rnd}-{i}",
                "round": rnd,
                "text": text.strip(),
                "rounds_elapsed": current_round - rnd,
                "experiments_since": later_exps,
                "selections_since": selected,
                "reflections_since": sum(
                    1 for other in reflection_records
                    if isinstance(other, dict)
                    and isinstance(other.get("round_id"), int)
                    and rnd < other["round_id"] < current_round),
            })
    return rows


def path_prefix_streak(
    experiments: list, *, current_round: int,
) -> tuple[str | None, int]:
    """The largest count of trailing experiment rounds whose changed-path
    buckets share a common region (intersection non-empty). Long streaks mean
    the research attention has not moved even when the questions claim to."""
    by_round: dict[int, set[str]] = {}
    for exp in experiments:
        if exp.round >= current_round:
            continue
        buckets = {_bucket_prefix(p) for p in exp.changed_paths}
        by_round.setdefault(exp.round, set()).update(buckets)
    if not by_round:
        return None, 0
    rounds_desc = sorted(by_round, reverse=True)
    common = set(by_round[rounds_desc[0]])
    streak = 0
    streak_prefix = None
    for rnd in rounds_desc:
        common = common & by_round[rnd]
        if not common:
            break
        streak += 1
        streak_prefix = sorted(common)[0]
    return streak_prefix, streak


def render_reflection_pack(
    *,
    current_round: int,
    experiments: list,
    findings: dict,
    history_rows: list,
    expectation_rows: dict,
    previous_handoffs: list[str],
    metrics_schema: dict,
    reflection_records: list | None = None,
) -> str:
    """Render the deterministic evidence pack for one Reflection session.

    Facts and derived counts only — no interpretation. What the trajectory
    MEANS is the Reflecting Scientist's judgment; this pack's job is to make
    the trajectory-level patterns visible at all.
    """
    objective = (metrics_schema or {}).get("objective") or {}
    objective_key = objective.get("key")
    lower_is_better = bool(objective.get("lower_is_better"))
    records = [
        r for r in (reflection_records or []) if isinstance(r, dict)]

    mech = mechanism_family_distribution(
        findings, experiments, current_round=current_round)
    ledger = expectation_ledger(
        experiments, expectation_rows, current_round=current_round,
        objective_key=objective_key, lower_is_better=lower_is_better)
    trajectory = improvement_trajectory(
        history_rows, objective_key=objective_key,
        lower_is_better=lower_is_better)
    streak_prefix, streak = path_prefix_streak(
        experiments, current_round=current_round)
    exec_outcomes = execution_outcomes(history_rows)
    watchlist = commitment_watchlist(
        experiments, expectation_rows, records, current_round=current_round,
        objective_key=objective_key, lower_is_better=lower_is_better)
    watch_by_id = {row["experiment_id"]: row for row in watchlist}
    prescriptions = prescription_followthrough(
        records, experiments, current_round=current_round)

    lines = [
        f"REFLECTION EVIDENCE PACK (as of round {current_round})",
        "Deterministic aggregate views over the experiment ledger — facts and",
        "counts, no interpretation. Cite these by experiment id / round when",
        "you challenge the recent trajectory.",
        "",
        "## Mechanism families over recent experiments",
    ]
    if mech:
        for name, count in mech.items():
            lines.append(f"  {count:>3}x  {name}")
    else:
        lines.append("  (no mechanism-tagged experiments in the recent window)")
    lines += [
        "",
        "## Pre-registered expectations vs outcomes",
    ]
    if ledger:
        for row in ledger:
            marker = "r" if row["preregistered"] else "-"
            lines.append(
                f"  [{marker}] {row['experiment_id']}  {row['outcome']}")
            if row["preregistered"]:
                lines.append(
                    f"        expected: {row['expectation']}")
                if row["would_weaken"]:
                    lines.append(
                        f"        said would weaken if: {row['would_weaken']}")
                watch = watch_by_id.get(row["experiment_id"])
                if watch is not None:
                    # Clause-shaped outcome, adjudication still pending —
                    # annotated in place; what it means is your judgment.
                    lines.append(
                        f"        clause-shaped outcome, +{current_round - watch['round']} "
                        f"rounds ago; attempts on "
                        f"{watch['finding_id'] or '—'} since: "
                        f"{watch['attempts_on_finding_since']}; "
                        f"reflections since: {watch['reflections_since']}")
            else:
                lines.append(
                    "        no pre-registered expectation (capture failed "
                    "or predates the mechanism)")
        missed = sum(
            1 for row in ledger
            if row["preregistered"] and not row["selected"]
        )
        total = sum(1 for row in ledger if row["preregistered"])
        lines.append(
            f"  preregistered experiments not selected as new incumbent: "
            f"{missed}/{total}")
    else:
        lines.append("  (no experiments in the recent window)")
    lines += [
        "",
        "## Your past prescriptions and what happened since",
    ]
    if prescriptions:
        for row in prescriptions:
            exps = ", ".join(row["experiments_since"]) or "none"
            sels = ", ".join(row["selections_since"]) or "none"
            lines.append(
                f"  {row['id']} (+{row['rounds_elapsed']} rounds; "
                f"experiments since: {exps}; selections since: {sels}; "
                f"reflections since: {row['reflections_since']})")
            lines.append(f"        you wrote: {row['text']}")
    else:
        lines.append("  (no structured prescriptions recorded yet)")
    lines += [
        "",
        "## Experimenter session outcomes",
        f"  commissioned candidates that reached evaluation: "
        f"{exec_outcomes['performed']}",
    ]
    if exec_outcomes["not_performed_by_cause"]:
        lines.append(
            "  experimenter sessions that ended without completing the "
            "intervention, by cause:")
        for cause, count in sorted(
                exec_outcomes["not_performed_by_cause"].items()):
            lines.append(f"    {count:>3}x  {cause}")
    else:
        lines.append(
            "  experimenter sessions that ended without completing the "
            "intervention: none")
    lines += [
        "",
        "## Incumbent-improvement trajectory "
        f"(objective: {objective_key or '—'})",
    ]
    if trajectory:
        for entry in trajectory:
            best = (
                f"{entry['best_eligible']:.6g}"
                if isinstance(entry["best_eligible"], (int, float))
                else "—"
            )
            inc = (
                f"{entry['incumbent_after']:.6g}"
                if isinstance(entry["incumbent_after"], (int, float))
                else "—"
            )
            sel = "selected" if entry["selected"] else "no selection"
            lines.append(
                f"  round {entry['round']:>3}: best-eligible {best:>12}   "
                f"incumbent {inc:>12}   {sel}   "
                f"(no-new-incumbent streak: {entry['no_incumbent_streak']})"
            )
    else:
        lines.append("  (no recorded task rounds yet)")
    lines += [
        "",
        "## Attention locality",
        f"  changed-path streak: {streak} consecutive recent experiment "
        f"round(s) in the same region"
        + (f" ({streak_prefix})" if streak_prefix else ""),
        "",
    ]
    if previous_handoffs:
        lines += [
            "## Your previous reflection handoffs (your own past judgments —",
            "audit whether you acted on them, and whether they were right)",
        ]
        for text in previous_handoffs[-3:]:
            lines.append(f"  --- {text}")
        lines.append("")
    return "\n".join(lines)
