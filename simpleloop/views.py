"""Per-role projection functions.

The store holds the full raw record per round:
    {round, proposal, sha, score, risk, feedback, eval_block, metrics,
     changed_paths}

Each role sees only the fields it should, via these projections. The store stays
the single source of truth (full record, for the judger's prior/baseline eval
axis and for final_report); projection happens at prompt-build time in each role.

Why projections instead of a central Context object: SimpleLoop's data model is
flat — one record per round, serial single-parent chain. A Context pool class
would wrap a list[dict] with no added structure. Three pure functions are enough
and leave explicit, auditable seams for future mechanisms.

The proposer projection keeps recent proposals complete and compacts older
proposal text. The persisted history remains complete, so other roles and final
reports are unaffected.
"""
from __future__ import annotations

import re


_PROPOSER_FULL_PROPOSAL_ROUNDS = 6
_PROPOSER_OLD_PROPOSAL_CHARS = 300

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


def _proposal_projection(proposal: str, keep_full: bool) -> dict[str, str]:
    if keep_full:
        return {"proposal": proposal}
    normalized = " ".join(proposal.split())
    suffix = "…" if len(normalized) > _PROPOSER_OLD_PROPOSAL_CHARS else ""
    return {
        "proposal_head":
            normalized[:_PROPOSER_OLD_PROPOSAL_CHARS] + suffix,
    }


def for_proposer(history: list[dict]) -> list[dict]:
    """What the proposer sees of each prior round.

    Projects round/generation history with candidate shas, selected state, score,
    risk, metrics, changed_paths, feedback, and concise diagnostic narrative.

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
    out = []
    full_proposal_start = max(
        0, len(history) - _PROPOSER_FULL_PROPOSAL_ROUNDS
    )
    for record_index, r in enumerate(history):
        keep_full_proposal = record_index >= full_proposal_start
        if "candidates" in r:
            out.append({
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
                        **_proposal_projection(
                            c.get("proposal") or "",
                            keep_full_proposal,
                        ),
                        "sha": c.get("sha") or None,
                        "selected": bool(c.get("selected")),
                        "accepted": c.get("accepted"),
                        "score": c.get("score"),
                        "risk": c.get("risk"),
                        "metrics": c.get("metrics") or {},
                        "changed_paths": c.get("changed_paths") or [],
                        "landing_state": _landing_state(c.get("feedback", "")),
                        "feedback": c.get("feedback", ""),
                    }
                    for c in (r.get("candidates") or [])
                ],
            })
            continue
        out.append({
            "round": r["round"],
            **_proposal_projection(
                r.get("proposal") or "",
                keep_full_proposal,
            ),
            "sha": r.get("sha") or None,
            "accepted": r.get("accepted"),
            "base_sha": r.get("base_sha"),
            "score": r["score"],
            "risk": r.get("risk"),
            "metrics": r.get("metrics") or {},
            "changed_paths": r.get("changed_paths") or [],
            "landing_state": _landing_state(r.get("feedback", "")),
            "feedback": r["feedback"],
        })
    return out


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


def for_executor(proposal: str, goal: str, editable: list[str],
                 frozen: list[str]) -> dict:
    """What the executor sees: the proposal + safety + a goal anchor. No history.

    The executor implements one direction; it must not be influenced by prior
    rounds' outcomes (that would tempt it to "fix" earlier attempts or second-guess
    the proposal). It gets only what the current round's proposal needs.
    """
    return {"proposal": proposal, "goal": goal,
            "editable": editable, "frozen": frozen}


def for_judger(*, goal: str, proposal: str, diff: str, eval_block: str,
               metrics: dict | None, prior_metrics: dict | None,
               baseline_metrics: dict | None,
               metrics_schema: dict | None) -> dict:
    """What the judger sees.

    The parsed metrics (this/prior/baseline) + the declared schema are passed in
    by the loop as locals — they do NOT come from store.history() (which would
    also drag in the proposer's view of other rounds). The judger is the one role
    that legitimately sees raw eval output, but only to verify a specific claim;
    the parsed metrics block is the authoritative numbers it cites (the harness
    parses + computes deltas so the judger never reads numbers out of prose,
    which hallucinated a baseline number across 12 rounds before).
    """
    return {
        "goal": goal,
        "proposal": proposal,
        "diff": diff,
        "eval_block": eval_block,
        "metrics": metrics,
        "prior_metrics": prior_metrics,
        "baseline_metrics": baseline_metrics,
        "metrics_schema": metrics_schema,
    }
