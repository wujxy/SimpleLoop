"""Proposer: reads the source repo (read-only) + history -> proposes one direction.

Sees: task goal, safety (editable/frozen), the round history (proposal/score/feedback
for each prior round). Its cwd is the ORIGINAL source repo, so it can `cat`/`grep`
the source code and run read-only `git log`/`git diff` to ground its direction in
the actual code. It must NOT edit any file — the executor does that, in the
working repo.

Delivers: {"proposal": "<a rough direction>"} — one key, short.
"""
from __future__ import annotations

from pathlib import Path

from .agent import Agent


def propose(agent: Agent, *, goal: str, editable: list[str], frozen: list[str],
            history: list[dict], cwd: Path) -> str:
    """Return the proposal text for the next round."""
    if history:
        hist_lines = []
        for r in history:
            hist_lines.append(
                f"  round {r['round']}: proposal=\"{r['proposal']}\" "
                f"score={r['score']} feedback=\"{r['feedback']}\""
            )
        hist_block = "\n".join(hist_lines)
    else:
        hist_block = "  (none yet — this is the first round)"

    prompt = f"""You are the PROPOSER in a serial optimization loop. Propose the next round's direction.

Task goal:
{goal}

You are running in the source repository (read-only). You may `cat`, `grep`, or run
read-only `git log`/`git diff` to inspect the code and ground your direction in it.
Do NOT edit any file — that is the executor's job.

Safety:
- editable_paths (only these may be changed by the executor): {editable}
- frozen_paths (must never be touched): {frozen}

Prior rounds (proposal / judger score / feedback):
{hist_block}

Propose exactly one direction for the next round. Keep it short — a rough direction, not a long plan. Point at specific functions/modules/files when you can. Do not repeat directions that prior feedback says failed.

Return exactly one JSON object: {{"proposal": "<your direction>"}}"""
    data = agent.run_json(prompt, cwd=cwd, label="proposer")
    proposal = data.get("proposal")
    if not isinstance(proposal, str) or not proposal.strip():
        raise ValueError(f"proposer did not return a non-empty 'proposal' string: {data}")
    return proposal.strip()
