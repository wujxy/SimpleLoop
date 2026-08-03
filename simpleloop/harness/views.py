"""Compact factual projections over append-only experiment history."""
from __future__ import annotations


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
