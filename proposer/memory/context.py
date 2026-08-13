"""Build the Proposer's round-start context pack.

The pack is deliberately compact — objective, gates, accepted revision,
the editable (writable) path set + the read-only world statement, the most
recent factual dashboard, the Research Frontier, and a memory-tool cheatsheet.
It carries NO ``ref: note`` full history, and NO instruction to summarize prior
candidates. See design doc §6.1.

Note: the ``frozen`` parameter is retained in the signatures for call-site
compatibility but is no longer rendered — under the mount-world model the
read-only set is "everything not editable", enforced by the container mount
(EROFS), so an explicit frozen list adds nothing.
"""
from __future__ import annotations

import json


def build_generation_context(
    *,
    goal: str,
    editable: list[str],
    frozen: list[str],
    base_sha: str,
    gate_block: str,
) -> str:
    """History-free context for the Generator.

    Only the task definition: objective, gates, paths, base_sha. No dashboard,
    no frontier, no explore health, no exhausted-region list. The Generator is
    a free explorer — it sees the task but not the history.
    """
    return f"""Research objective:
{goal}

Harness Gates:
{gate_block or "(declared in factual records)"}

Current accepted revision: {base_sha}
Editable paths (writable world — mounted :rw): {json.dumps(editable, ensure_ascii=False)}
Read-only world: every other path is mounted :ro — the whole repo is visible
and runnable, but edits outside the editable set fail at the filesystem.
"""


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
Editable paths (writable world — mounted :rw): {json.dumps(editable, ensure_ascii=False)}
Read-only world: every other path is mounted :ro — the whole repo is visible
and runnable, but edits outside the editable set fail at the filesystem.

Recent factual dashboard (last {recent_rounds} round(s), authoritative harness output):
{dashboard}
{abstentions_block}
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
            # The finding's QUESTION text is deliberately NOT shown here — it
            # is the Scientist's own open research question, and surfacing it
            # in the coverage map anchors the proposer to keep drilling the
            # same questions. Use inspect_finding to recall one deliberately.
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


def build_coverage_pack(
    *,
    experiments,
    frontier: dict,
    recent_rounds: int = 2,
    recent_abstentions: list[dict] | None = None,
) -> str:
    """The lean per-round COVERAGE MAP injected at wake-up: recent outcome
    dashboard + research frontier (coverage) + recent abstentions. Goal /
    world / gates / tools live in the standing system prompt, so they are NOT
    repeated here. Carries no direction text — only coverage and outcomes
    (read it as "what is already covered", never as a direction menu)."""
    dashboard = _render_dashboard(experiments, recent_rounds=recent_rounds)
    abstentions_block = _render_abstentions(recent_abstentions)
    frontier_text = _render_frontier(frontier)
    return (
        "Coverage map — where effort has already been spent (read as WHAT IS "
        "COVERED, not as a menu of directions to reuse):\n"
        f"\nRecent outcomes (last {recent_rounds} round(s), authoritative "
        f"harness output):\n{dashboard}\n"
        f"{abstentions_block}"
        f"Research frontier (open questions and coverage — derived):\n"
        f"{frontier_text}\n"
    )
