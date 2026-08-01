"""Compact factual projections over append-only experiment history."""
from __future__ import annotations

_PROPOSER_RECENT_ROUNDS_DEFAULT = 6

def for_proposer(history: list[dict], *, recent_rounds: int = _PROPOSER_RECENT_ROUNDS_DEFAULT) -> list[dict]:
    """Return recent SHA, Gate, metric, and changed-path evidence."""
    return [
        {
            "round": r["round"],
            "parent_sha": r.get("parent_sha"),
            "selected_candidate": r.get("selected_candidate"),
            "selected_sha": r.get("selected_sha"),
            "base_sha": r.get("base_sha"),
            "candidates": [
                {
                    "candidate": c.get("candidate"),
                    "proposal": c.get("proposal") or "",
                    "parent_sha": c.get("parent_sha") or r.get("parent_sha"),
                    "sha": c.get("sha") or None,
                    "status": c.get("status"),
                    "selected": bool(c.get("selected")),
                    "gate_passed": c.get("gate_passed"),
                    "eligible": c.get("eligible"),
                    "gates": c.get("gates") or {},
                    "metrics": c.get("metrics") or {},
                    "changed_paths": c.get("changed_paths") or [],
                }
                for c in (r.get("candidates") or [])
            ],
        }
        for r in history[-recent_rounds:]
    ]


def gate_block(metrics_schema: dict | None) -> str:
    """Render the declared gates' bullet lines (``- <key>: <description>``);
    "" when no gate declares a description. Framing sentences live in each
    role's prompt template."""
    if not metrics_schema:
        return ""
    gates = metrics_schema.get("gates") or []
    described = [g for g in gates if g.get("description")]
    if not described:
        return ""
    return "\n".join(f"- {g['key']}: {g['description']}" for g in described)
