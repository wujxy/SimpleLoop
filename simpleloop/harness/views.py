"""Per-role projection functions.

The store holds one full generation record per round:
    {round, parent_sha, selected_candidate, selected_sha, reflection,
     candidates: [{candidate, family, proposal, sha, score, risk, feedback,
                   feedback_for_proposer, eval_block, metrics, changed_paths,
                   accepted, selected}, ...]}

The proposer sees only the fields it should, via `for_proposer`; the executor
and judger build their prompts from loop-passed locals directly. The store stays
the single source of truth (full record, for the judger's prior/baseline eval
axis); projection happens at prompt-build time.

Why projections instead of a central Context object: SimpleLoop's data model is
flat — one record per round, serial single-parent chain. A Context pool class
would wrap a list[dict] with no added structure. Pure functions are enough
and leave explicit, auditable seams for future mechanisms.

The proposer projection keeps only the `recent_rounds` most recent round
records in full (default 3; overridable via `loop.proposer_recent_rounds`).
The persisted history remains complete and older evidence stays available through
Search Memory references.
"""
from __future__ import annotations

import re


_PROPOSER_RECENT_ROUNDS_DEFAULT = 6

# The judger prefixes each feedback with a `LANDED_STATE: <tag>` token (the
# contract lives in judger.py); we surface it as a structured field in the
# proposer's view instead of forcing the proposer to re-parse prose. The tag
# is the judger's own landing hint — this projection is deterministic, not new
# information, and degrades to None for legacy feedback written before the
# prefix convention existed.
_LANDING_STATE_RE = re.compile(r"LANDED_STATE:\s*([a-z-]+)", re.IGNORECASE)


def _landing_state(feedback: str | None) -> str | None:
    """Extract the judger's LANDED_STATE tag from feedback, or None.

    The tag (not-implemented | already-implemented | gate-rejected) lets the
    proposer tell a real-but-rejected attempt from an executor no-op without
    inferring it from `accepted=false` prose. No prefix (legacy/loop-failure
    feedback) -> None, which the proposer reads as "unlabelled", never as a
    guessed state.
    """
    if not feedback:
        return None
    m = _LANDING_STATE_RE.search(feedback)
    return m.group(1).lower() if m else None


def for_proposer(history: list[dict], *, recent_rounds: int = _PROPOSER_RECENT_ROUNDS_DEFAULT) -> list[dict]:
    """What the proposer sees of each prior round.

    Projects round/generation history with candidate shas, selected state, score,
    risk, metrics, changed_paths, and the concise proposer-facing lesson.

    Includes:
      - sha (full candidate commit): so the proposer can self-audit whether a
        direction is already landed — `git diff <prev>..<sha> -- <editable>`
        shows exactly what a round changed, instead of the proposer guessing
        from feedback prose. (Before, the proposer could not tell a direction
        was already implemented and re-proposed it for 4 empty rounds running.)
      - metrics: the harness-parsed structured dict (e.g. SPEED_MS=485.18,
        CORRECTNESS=true) — NOT eval_block raw text (which hallucinated
        numbers before). Lets the proposer see the payoff trend and judge
        whether a direction's headroom is exhausted.
      - changed_paths: which files a round touched — a cheap landing signal
        that complements sha (proposer can scan it without running git).
      - risk: the judger's latent-correctness band (low|medium|high) — lets the
        proposer tell "direction was sound but didn't land" (low risk + empty)
        from "direction has a latent bug" (high risk) when reflecting on whether
        to continue an area or switch.

    Still excludes eval_block: raw eval text, too noisy, hallucination risk.
    """
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
    """Render the declared gates' list lines, from the config schema only.

    Returns ONLY the per-gate bullet lines (``- <key>: <description>``), joined by
    newlines — no header. The framing sentence (what gates are, how the role should
    treat them) lives in each role's prompt template, next to its other fixed text,
    so all fixed prompt wording stays in the prompt and only the data part is
    rendered here.

    No domain knowledge lives here — every gate's meaning is the config author's
    `description` string, passed through verbatim.

    Returns "" when there are no gates with a description — a task that does not
    declare descriptions gets an empty block (the prompt's fixed header sits above
    an empty list, which is harmless since SimpleLoop always declares gates).
    """
    if not metrics_schema:
        return ""
    gates = metrics_schema.get("gates") or []
    described = [g for g in gates if g.get("description")]
    if not described:
        return ""
    return "\n".join(f"- {g['key']}: {g['description']}" for g in described)


