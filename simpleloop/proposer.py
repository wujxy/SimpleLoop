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

from .agent import Agent, AgentError


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

Guidance:
- Propose exactly one direction for the next round.
- Size is not a virtue and not a sin — let it be whatever the payoff demands. If your read of the code says the biggest real win is a systematic change that spans many call sites (e.g. rewiring every time-PDF call site, or index-partitioning the whole PMT loop), propose THAT, even though it is large; do not shrink a high-payoff direction into a small one just to be safe. Point at the specific functions/modules/files and the call sites it touches.
- The real constraint on size is verifiability, not smallness: the change must be implementable and evaluable within one round. That rules out sprawling multi-system rewrites, but a single coherent large refactor (one mechanism, many sites) is fine and often the highest-value kind.
- Do not repeat directions that prior feedback says failed; mutate from the best-scoring round's idea when there is one — but if the best round is local hill-climbing and a bigger lever exists elsewhere, prefer the bigger lever.
- Do not assume task facts that are not visible in the source code or prior feedback.

Final delivery contract (mandatory):
- Your final response MUST be exactly one parseable JSON object: {{"proposal": "<your direction>"}}
- A ```json code fence is acceptable; any prose, heading, commentary, or natural-language wrap-up outside the JSON is forbidden — this includes reasoning/analysis before the JSON. Do ALL your thinking via tool calls (cat/grep/git), then emit ONLY the JSON.
- If you are uncertain or blocked, still return the JSON object with a conservative, specific direction.
- Do not ask for more data and do not emit a summary.

Example of the ONLY acceptable final output shape:
{{"proposal": "In count_pairs, sort points by x and break the inner loop once xj-xi > radius."}}"""
    try:
        data = agent.run_json(prompt, cwd=cwd, label="proposer")
    except AgentError as exc:
        # The agent returned prose instead of JSON. A rough direction in prose is
        # still a usable proposal — degrade gracefully instead of wasting a round.
        fallback = _prose_fallback(exc.raw_output)
        if not fallback:
            raise
        print(f"[proposer] JSON parse failed; using prose fallback as proposal", flush=True)
        return fallback
    proposal = data.get("proposal")
    if not isinstance(proposal, str) or not proposal.strip():
        raise ValueError(f"proposer did not return a non-empty 'proposal' string: {data}")
    return proposal.strip()


def _prose_fallback(raw: str) -> str | None:
    """If the agent emitted prose instead of JSON, salvage it as the proposal.

    Returns the non-empty prose (trimmed) or None if there's nothing usable. We
    don't try to parse a JSON object here — run_json already tried and failed.
    """
    if not raw:
        return None
    # drop a leading ```json fence if the agent half-fenced prose
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    return text or None
