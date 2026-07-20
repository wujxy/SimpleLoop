"""Per-role projection functions.

The store holds the full raw record per round:
    {round, proposal, sha, score, risk, feedback, feedback_for_report,
     eval_block, metrics, changed_paths}

Each role sees only the fields it should, via these projections. The store stays
the single source of truth (full record, for the judger's prior/baseline eval
axis and for final_report); projection happens at prompt-build time in each role.

Why projections instead of a central Context object: SimpleLoop's data model is
flat — one record per round, serial single-parent chain. A Context pool class
would wrap a list[dict] with no added structure. Three pure functions are enough
and leave explicit, auditable seams for future mechanisms.

Extension points NOT yet implemented (intentionally — see plan):
  for_proposer is the natural home for (a) periodic exploration rounds — hide the
  best round's proposal every K rounds to fight basin collapse; (b) a sliding
  window — keep the last W rounds full, degrade older rounds to a one-line score
  row. Neither is wired now: Step 2+3 already keep history tiny (~700B/round),
  so neither is justified until evidence demands it. Adding them later means
  editing for_proposer's internals; callers (proposer prompt builder) stay fixed.
"""
from __future__ import annotations


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
    for r in history:
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
                        "proposal": c.get("proposal"),
                        "sha": c.get("sha") or None,
                        "selected": bool(c.get("selected")),
                        "accepted": c.get("accepted"),
                        "score": c.get("score"),
                        "risk": c.get("risk"),
                        "metrics": c.get("metrics") or {},
                        "changed_paths": c.get("changed_paths") or [],
                        "feedback": c.get("feedback", ""),
                        "feedback_for_report": c.get("feedback_for_report", ""),
                    }
                    for c in (r.get("candidates") or [])
                ],
            })
            continue
        out.append({
            "round": r["round"],
            "proposal": r["proposal"],
            "sha": r.get("sha") or None,
            "accepted": r.get("accepted"),
            "base_sha": r.get("base_sha"),
            "score": r["score"],
            "risk": r.get("risk"),
            "metrics": r.get("metrics") or {},
            "changed_paths": r.get("changed_paths") or [],
            "feedback": r["feedback"],
            "feedback_for_report": r.get("feedback_for_report", r.get("feedback", "")),
        })
    return out


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
