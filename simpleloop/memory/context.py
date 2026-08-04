"""Build the Proposer's round-start context pack.

The pack is deliberately compact — objective, gates, accepted revision,
editable/frozen paths, the most recent factual dashboard, the Research
Frontier, and a memory-tool cheatsheet. It carries NO ``ref: note`` full
history, and NO instruction to summarize prior candidates. See design doc §6.1.
"""
from __future__ import annotations

import json


def build_startup_pack(
    *,
    goal: str,
    editable: list[str],
    frozen: list[str],
    base_sha: str,
    gate_block: str,
    candidates_per_round: int,
    hints: list[str] | None,
    experiments,               # list[Experiment]
    frontier: dict,
    recent_rounds: int = 2,
    tool_cheatsheet: str = "",
    recent_abstentions: list[dict] | None = None,
    signals: dict | None = None,
) -> str:
    """Return the plain-text user-turn content the Proposer wakes up with."""
    hints_block = ""
    if hints:
        bullets = "\n".join(f"  - {h}" for h in hints)
        hints_block = (
            f"\nGuidance (high-value directions, not requirements):\n"
            f"{bullets}\n"
        )
    dashboard = _render_dashboard(experiments, recent_rounds=recent_rounds)
    abstentions_block = _render_abstentions(recent_abstentions)
    signals_block = _render_signals(signals)
    frontier_text = _render_frontier(frontier)
    tools_block = (
        f"\nMemory tools available (see the Runtime contract for schemas):\n"
        f"{tool_cheatsheet}\n"
        if tool_cheatsheet else ""
    )
    return f"""Research objective:
{goal}
{hints_block}
Harness Gates:
{gate_block or "(declared in factual records)"}

Current accepted revision: {base_sha}
Editable paths: {json.dumps(editable, ensure_ascii=False)}
Frozen paths: {json.dumps(frozen, ensure_ascii=False)}

Recent factual dashboard (last {recent_rounds} round(s), authoritative harness output):
{dashboard}
{abstentions_block}{signals_block}
Research frontier (open questions and search coverage — derived, not a summary):
{frontier_text}
{tools_block}
Submit between 1 and {candidates_per_round} proposal(s) — each one an
experiment the evidence makes worth its execution cost; the budget is a
ceiling, not a quota. If no direction clears that bar, abandon the round
(zero proposals) rather than forcing a weak bet.
Every candidate begins from the accepted revision above. For each proposal
declare its research target: either an existing finding (F-NNN) or a new
question you are opening.
"""


def _render_signals(signals: dict | None) -> str:
    """Render deliberation signals — ledger facts and threshold-derived policy
    nudges, explicitly NOT scientific conclusions. Omitted entirely on the
    first round or when no finding has any experiment yet."""
    if not signals or signals.get("first_round"):
        return ""
    entries = signals.get("findings") or []
    if not entries:
        return ""
    lines = [
        "",
        "Deliberation signals (ledger facts + harness policy signals, NOT "
        "scientific conclusions):",
    ]
    for entry in entries:
        facts = entry.get("facts") or {}
        sig = entry.get("policy_signals") or {}
        lines.append(
            f"  {entry['id']}  attempts={facts.get('attempts', 0)}  "
            f"impl_failures={facts.get('implementation_failures', 0)}  "
            f"eligible_imp/neutral/reg="
            f"{facts.get('eligible_improvements', 0)}/"
            f"{facts.get('eligible_neutral', 0)}/"
            f"{facts.get('eligible_regressions', 0)}  "
            f"selected={facts.get('selected', 0)}"
        )
        q = (entry.get("question") or "").strip()
        if q:
            lines.append(f"    Q: {q}")
        active = {k: v for k, v in sig.items() if (v or {}).get("active")}
        if active:
            tags = ", ".join(
                f"{k} ({v['rule']})" for k, v in active.items()
            )
            lines.append(f"    policy signal: {tags}")
    return "\n".join(lines) + "\n"


def _render_abstentions(recent_abstentions: list[dict] | None) -> str:
    """Surface recent zero-candidate rounds as plain facts, so the next
    Scientist sees which directions were judged not worth executing (and
    does not mistake silence for an unexplored gap)."""
    if not recent_abstentions:
        return ""
    lines = [
        "",
        "Recent abstentions (rounds declined as not worth an experiment):",
    ]
    for entry in recent_abstentions:
        round_no = int(entry.get("round", 0)) + 1
        reason = str(entry.get("reason") or "").strip()
        lines.append(f"  round {round_no}: {reason}")
        unknown = (entry.get("blocking_unknown") or "").strip()
        if unknown:
            lines.append(f"    blocking unknown: {unknown}")
    return "\n".join(lines) + "\n"


def _render_dashboard(experiments, *, recent_rounds: int) -> str:
    if not experiments:
        return "(no prior experiments)"
    latest = max(exp.round for exp in experiments)
    threshold = latest - recent_rounds + 1
    rows = [exp for exp in experiments if exp.round >= threshold]
    rows.sort(key=lambda exp: (exp.round, exp.candidate))
    if not rows:
        return "(no prior experiments)"
    lines: list[str] = []
    for exp in rows:
        finding = exp.finding_id or "-"
        gate = "pass" if exp.gate_passed else "fail"
        selected = " selected" if exp.selected else ""
        metrics = _short_metrics(exp.metrics)
        lines.append(
            f"  {exp.experiment_id}  finding={finding}  status={exp.status}  "
            f"gate={gate}{selected}  {metrics}"
        )
    return "\n".join(lines)


def _short_metrics(metrics: dict) -> str:
    if not metrics:
        return "metrics=(none)"
    parts: list[str] = []
    for key, value in metrics.items():
        if isinstance(value, float):
            parts.append(f"{key}={value:g}")
        else:
            parts.append(f"{key}={value}")
    return "metrics=" + ", ".join(parts)


def _render_frontier(frontier: dict) -> str:
    if not frontier or (
        not frontier.get("active_findings")
        and not frontier.get("coverage", {}).get("code_regions")
        and not frontier.get("coverage", {}).get("mechanisms")
    ):
        return "(no findings yet — this is the first Proposer round)"
    lines: list[str] = []
    active = frontier.get("active_findings") or []
    if active:
        lines.append("  active_findings:")
        for entry in active:
            mech = ",".join(entry.get("mechanisms") or []) or "-"
            regions = ",".join(entry.get("code_regions") or []) or "-"
            lines.append(
                f"    - {entry['id']}  attempts={entry['attempts']}  "
                f"last_touched=r{entry['last_touched_round']}  "
                f"mechanisms=[{mech}]  code_regions=[{regions}]"
            )
            lines.append(f"      Q: {entry['question']}")
    else:
        lines.append("  active_findings: (none)")
    lines.append(
        f"  dormant_count: {frontier.get('dormant_count', 0)}   "
        f"archived_count: {frontier.get('archived_count', 0)}   "
        f"experiment_count: {frontier.get('experiment_count', 0)}"
    )
    coverage = frontier.get("coverage") or {}
    regions = coverage.get("code_regions") or {}
    if regions:
        lines.append("  coverage.code_regions:")
        for region, count in regions.items():
            lines.append(f"    {region}: {count}")
    mechanisms = coverage.get("mechanisms") or {}
    if mechanisms:
        lines.append("  coverage.mechanisms:")
        for mech, count in mechanisms.items():
            lines.append(f"    {mech}: {count}")
    return "\n".join(lines)
