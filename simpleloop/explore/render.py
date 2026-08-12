"""Rendering helpers for the Explore search-health report.

Two consumers:

- :func:`render_explore_for_startup` — the long-form block embedded in the
  Proposer's startup pack (findings + families + global + an informational
  POLICY paragraph when ``challenge_required``).
- :func:`render_explore_for_state_header` — compact tags spliced into the
  per-step working-state header.

All output is plain text and informational only — nothing here is a scientific
verdict, and nothing gates or forces a submit.
"""
from __future__ import annotations

from .models import (
    ExploreReport,
    FamilyExploreHealth,
    FindingExploreHealth,
    GlobalExploreHealth,
    PolicySignal,
)


def _active(sigs) -> list[PolicySignal]:
    return [s for s in sigs if s.active]


def render_explore_for_startup(report: ExploreReport | None) -> str:
    """Render the full Explore block for the startup pack.

    Returns ``""`` on the first round or when there is nothing to show.
    Preserves the per-finding line format the Scientist already knows, then
    appends family and global blocks, then — when ``challenge_required`` is set
    — an informational POLICY paragraph reminding the Proposer the signal does
    not gate submit and exists only to prompt harder thinking.
    """
    if report is None or report.first_round:
        return ""
    has_findings = bool(report.findings)
    has_families = bool(report.families)
    has_global = report.global_health is not None
    if not (has_findings or has_families or has_global):
        return ""

    lines: list[str] = [
        "Explore health (ledger-derived search dynamics, NOT scientific "
        "verdicts):",
    ]
    if report.analysis_eligible is False:
        lines.append(
            "  (objective metric unavailable — only fact counts shown, no "
            "classification-derived signals)"
        )

    for fh in report.findings:
        lines.append(_render_finding_line(fh))

    for fam in report.families:
        lines.append(_render_family_line(fam))

    if report.global_health is not None:
        lines.append(_render_global_line(report.global_health))

    if report.challenge_required:
        lines.append("")
        lines.append(
            "POLICY (informational only — does not gate submit): Explore "
            "detects stagnation. The generation boundary steers the Generator "
            "away from these families; you do not need a challenge_response "
            "to submit. Use this as a signal to think harder, not as "
            "paperwork."
        )
    return "\n".join(lines) + "\n"


def _render_finding_line(fh: FindingExploreHealth) -> str:
    out = (
        f"  {fh.finding_id}  attempts={fh.attempts}  "
        f"impl_failures={fh.implementation_failures}  "
        f"eligible_imp/neutral/reg={fh.improvements}/{fh.neutral}/"
        f"{fh.regressions}  selected={fh.selected}"
    )
    block = [out]
    if fh.question:
        block.append(f"    Q: {fh.question}")
    active = _active(fh.policy_signals)
    if active:
        block.append(
            "    policy signal: "
            + ", ".join(f"{s.name} ({s.rule})" for s in active)
        )
    return "\n".join(block)


def _render_family_line(fam: FamilyExploreHealth) -> str:
    head = (
        f"  family {fam.family_id}  attempts={fam.attempts}  "
        f"imp/neutral/reg={fam.improvements}/{fam.neutral}/"
        f"{fam.regressions}  consecutive_no_improve="
        f"{fam.consecutive_no_improve}  selected={fam.selected}"
    )
    block = [head]
    if fam.finding_ids:
        block.append(f"    findings: {', '.join(fam.finding_ids)}")
    active = _active(fam.policy_signals)
    if active:
        block.append(
            "    policy: "
            + ", ".join(
                f"{s.name}[{s.severity}] ({s.rule})" for s in active
            )
        )
    return "\n".join(block)


def _render_global_line(gh: GlobalExploreHealth) -> str:
    mechs = ", ".join(gh.recent_mechanisms) if gh.recent_mechanisms else "(none)"
    head = (
        f"  global  recent_window={gh.recent_window}  "
        f"imp/neutral/reg={gh.recent_improvements}/{gh.recent_neutral}/"
        f"{gh.recent_regressions}  "
        f"consecutive_no_improve_rounds={gh.consecutive_no_improve_rounds}  "
        f"mechanisms_tried=[{mechs}]"
    )
    block = [head]
    active = _active(gh.policy_signals)
    if active:
        block.append(
            "    policy: "
            + ", ".join(
                f"{s.name}[{s.severity}] ({s.rule})" for s in active
            )
        )
    return "\n".join(block)


def render_explore_for_state_header(report: ExploreReport | None) -> str:
    """Compact active-signal tags for the per-step working-state header.

    Returns ``""`` when there is nothing active. Stays short by design: active
    per-finding signals as ``F-NNN:signal``, families as
    ``family:signal(<region>)``, global as ``global:signal``, plus a trailing
    ``challenge_required: true`` line when set.
    """
    if report is None or report.first_round:
        return ""
    tags: list[str] = []
    for fh in report.findings:
        for s in _active(fh.policy_signals):
            tags.append(f"{fh.finding_id}:{s.name}")
    for fam in report.families:
        for s in _active(fam.policy_signals):
            tags.append(f"family:{s.name}({fam.code_region})")
    if report.global_health is not None:
        for s in _active(report.global_health.policy_signals):
            tags.append(f"global:{s.name}")
    lines: list[str] = []
    if tags:
        lines.append(f"  active_explore: {', '.join(tags)}")
    if report.challenge_required:
        lines.append("  challenge_required: true")
    return "\n".join(lines)
