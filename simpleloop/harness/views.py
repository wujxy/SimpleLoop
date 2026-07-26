"""Per-role projections over history records: the proposer sees only the fields
it should (via for_proposer, limited to the recent_rounds most recent rounds);
the store stays the full single source of truth."""
from __future__ import annotations

import re


_PROPOSER_RECENT_ROUNDS_DEFAULT = 6

# The judger prefixes feedback with `LANDED_STATE: <tag>` (contract in
# judger.py); surface it as a structured field instead of prose re-parsing.
_LANDING_STATE_RE = re.compile(r"LANDED_STATE:\s*([a-z-]+)", re.IGNORECASE)


def _landing_state(feedback: str | None) -> str | None:
    """Extract the judger's LANDED_STATE tag from feedback, or None (unlabelled)."""
    if not feedback:
        return None
    m = _LANDING_STATE_RE.search(feedback)
    return m.group(1).lower() if m else None


def for_proposer(history: list[dict], *, recent_rounds: int = _PROPOSER_RECENT_ROUNDS_DEFAULT) -> list[dict]:
    """What the proposer sees of each prior round: candidate shas, selected
    state, score, risk, parsed metrics, changed_paths and the proposer-facing
    lesson — never the raw eval_block (noisy, hallucination risk)."""
    return [
        {
            "round": r["round"],
            "parent_sha": r.get("parent_sha"),
            "selected_candidate": r.get("selected_candidate"),
            "selected_sha": r.get("selected_sha"),
            "base_sha": r.get("base_sha"),
            "reflection": r.get("reflection", ""),
            "candidates": [
                {
                    "candidate": c.get("candidate"),
                    "family": c.get("family"),
                    "proposal": c.get("proposal") or "",
                    "sha": c.get("sha") or None,
                    "selected": bool(c.get("selected")),
                    "accepted": c.get("accepted"),
                    "score": c.get("score"),
                    "risk": c.get("risk"),
                    "metrics": c.get("metrics") or {},
                    "changed_paths": c.get("changed_paths") or [],
                    "landing_state": _landing_state(c.get("feedback", "")),
                    "feedback_for_proposer": (
                        c.get("feedback_for_proposer") or ""
                    ),
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


