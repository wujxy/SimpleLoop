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


def expectation_ledger(
    experiments: list, expectation_rows: dict, *,
    current_round: int, last_k_rounds: int = 8,
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
        if exp.selected:
            outcome = "SELECTED_AS_NEW_INCUMBENT"
        elif exp.gate_passed:
            outcome = "PASSED_GATES_NOT_IMPROVED"
        else:
            outcome = "FAILED_GATES"
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
) -> str:
    """Render the deterministic evidence pack for one Reflection session.

    Facts and derived counts only — no interpretation. What the trajectory
    MEANS is the Reflecting Scientist's judgment; this pack's job is to make
    the trajectory-level patterns visible at all.
    """
    objective = (metrics_schema or {}).get("objective") or {}
    objective_key = objective.get("key")
    lower_is_better = bool(objective.get("lower_is_better"))

    mech = mechanism_family_distribution(
        findings, experiments, current_round=current_round)
    ledger = expectation_ledger(
        experiments, expectation_rows, current_round=current_round)
    trajectory = improvement_trajectory(
        history_rows, objective_key=objective_key,
        lower_is_better=lower_is_better)
    streak_prefix, streak = path_prefix_streak(
        experiments, current_round=current_round)

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
