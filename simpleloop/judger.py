"""Judger: looks at the diff + eval output, grades the round, gives feedback.

The harness (not the LLM) computes the diff and runs the eval commands — these
are deterministic and must not be delegated to the agent. The loop runs eval in
the worktree (the real checked-out tree the executor committed) BEFORE calling
the judger, then passes the captured output in as `eval_block`. The judger agent
only judges: it sees goal + proposal + diff + eval stdout and returns a score
and feedback for the next proposer.

The judger runs with cwd = the worktree, so it may `cat`/`grep` the actual
committed source or re-run a command to verify a claim the eval headline makes.
That is verification, not the primary evidence — eval_block is the ground truth.

When there's no SHA (gate rejected / no change), the judger is still called but
shown the rejection reason instead of a diff, and asked for a low score + feedback
telling the proposer to avoid that direction.

Delivers: {"score": 0.0-1.0, "feedback": "..."}.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from .workspace import Workspace


@dataclass
class Judgment:
    score: float
    feedback: str


def judge(agent: Agent, *, goal: str, proposal: str, sha: str | None,
          reason: str | None, parent_sha: str, workspace: Workspace,
          eval_block: str, cwd: Path,
          prior_eval_block: str | None = None,
          baseline_eval_block: str | None = None) -> Judgment:
    """Grade one round. Returns (score, feedback).

    eval_block is the harness-computed eval output (run in the worktree before
    this call). The judger does NOT run eval itself — eval is deterministic and
    harness-owned. Empty eval_block means either no eval configured or no commit.

    prior_eval_block / baseline_eval_block are the eval outputs of the previous
    round and the baseline commit, passed in so the judger has explicit
    comparison axes. Without these the judger only sees the current round's
    single absolute number and tends to compare vs baseline, missing per-round
    regressions (a round that walks back the prior round's gain still "beats
    baseline" and scores high). See memory simpleloop-judger-prior-round-compare.
    """
    if sha is not None:
        diff = workspace.diff(parent_sha, sha)
    else:
        diff = f"(no commit produced this round: {reason})"

    prompt = _build_prompt(goal, proposal, diff, eval_block,
                           prior_eval_block, baseline_eval_block)
    data = agent.run_json(prompt, cwd=cwd, label="judger")
    return _parse(data)


def _build_prompt(goal: str, proposal: str, diff: str, eval_block: str,
                  prior_eval_block: str | None,
                  baseline_eval_block: str | None) -> str:
    has_eval = bool(eval_block)
    eval_section = f"""Eval command output (THIS round):
{eval_block}
""" if has_eval else ""
    eval_guidance = (
        "- The eval output above is the ground truth. If any eval command failed (non-zero exit), "
        "cap the score at 0.50 and say so in feedback.\n"
        "- Reward a real, measured improvement in the eval output; do not credit an improvement that is "
        "only asserted in the diff without eval evidence.\n"
    ) if has_eval else (
        "- There is no eval output this round; judge on the diff alone. Do not claim a measured speedup "
        "you cannot see — cap the score at 0.70 unless the diff clearly shows a correct, low-risk improvement.\n"
    )

    # Comparison axes: prior round + baseline eval, so the judger can compute
    # both deltas instead of guessing from one absolute number. Project-agnostic:
    # we just hand it the labeled text; the judger reads the metric out itself.
    compare_section = ""
    compare_guidance = ""
    if has_eval:
        parts = []
        if prior_eval_block:
            parts.append(f"""Prior round's eval output (the round this one forks from — compare THIS round vs THIS for per-round credit/regression):
{prior_eval_block}""")
        if baseline_eval_block:
            parts.append(f"""Baseline eval output (the unoptimized starting commit — compare THIS round vs THIS for overall trajectory toward the goal):
{baseline_eval_block}""")
        if parts:
            compare_section = "\n" + "\n\n".join(parts) + "\n"
            compare_guidance = (
                "- Two comparison axes are provided above: the prior round's eval and the baseline's eval. "
                "Read the same metric (e.g. ms/evt) out of all three (this round, prior round, baseline) and compute BOTH deltas.\n"
                "- Score the SINGLE round on (this round vs prior round): a round that regresses vs the prior round must score low even if it still beats baseline — it walked back a gain. A round that improves vs prior round scores high.\n"
                "- Flag trajectory in feedback using (this round vs baseline): even if each round beats the prior, if the cumulative result is still far from baseline / the goal, say so — 'beating the prior round while still 1.7x slower than baseline is not achieving the goal'.\n"
            )
    return f"""You are the JUDGER in a serial optimization loop. Grade this round's change.

Task goal:
{goal}

Direction that was attempted:
{proposal}

Change (git diff vs the previous round's result):
```diff
{diff}
```
{eval_section}{compare_section}Scoring rubric (score 0.0 to 1.0):
- 0.90-1.00: clearly exceeds the goal — real measured improvement (from eval) with no regressions and clean code.
- 0.70-0.90: solid improvement in the right direction, low risk, code still correct.
- 0.50-0.70: directionally useful but modest — small gain, or gain without eval proof, or minor risk.
- 0.30-0.50: weak / inconclusive — change happened but unclear benefit, or validation incomplete.
- 0.10-0.30: poor — wrong direction, introduced risk, broke correctness, or mostly duplicate work.
- 0.00-0.10: failed — no real change, broken code, or touched something it shouldn't.

Judging guidance:
- Judge whether the change moves toward the goal, achieves real improvement, introduces risk, and is good-quality code.
{eval_guidance}{compare_guidance}- Penalize unsupported claims, regressions, and changes that break correctness.
- Give concrete, actionable feedback for the next proposer (what to try next, or what to fix).

Final delivery contract (mandatory):
- Your final response MUST be exactly one parseable JSON object: {{"score": 0.0, "feedback": "<feedback>"}}
- A ```json code fence is acceptable; any prose, heading, commentary, or natural-language summary outside the JSON is forbidden.
- If evidence is incomplete or contradictory, still return the JSON object with a low score and feedback explaining the uncertainty.
- Do not ask for more data and do not emit a wrap-up."""


def _parse(data: dict) -> Judgment:
    try:
        score = float(data.get("score"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"judger 'score' must be a number 0-1: {data}") from exc
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"judger 'score' out of range [0,1]: {score}")
    feedback = data.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        raise ValueError(f"judger 'feedback' must be a non-empty string: {data}")
    return Judgment(score=score, feedback=feedback.strip())


def run_eval(commands: list[str], cwd: Path) -> str:
    """Run each eval command in `cwd` (the worktree), collect stdout/stderr.

    Deterministic — the harness owns this, not the judger agent. Must run in the
    worktree (the real checked-out tree the executor committed), not in the bare
    per-run repo clone which has no working tree. Each command's output is capped
    to keep the prompt bounded.

    Cap is 16000 chars. The OMILRECV2 eval (sl_eval.sh) prints ~5KB: a chunk of
    JUNO steering dump from test_consistency.py followed by the result lines the
    judger must read (CORRECTNESS=/SPEED_MS=/EVAL_RESULT=). The old 4000 cap
    landed mid-steering-dump and cut off all three result lines, so the judger
    saw "build OK, 10 events processed" but never the speed number or the final
    PASS — it graded speed as asserted-not-proven. 16000 leaves headroom for the
    steering dump plus the result lines, and is still small enough to bound the
    prompt (the judger prompt also carries the diff, capped separately).
    """
    _OUT_CAP = 16000
    blocks: list[str] = []
    for cmd in commands:
        completed = subprocess.run(
            cmd, shell=True, cwd=str(cwd),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=600, check=False,
        )
        out = completed.stdout.strip()
        err = completed.stderr.strip()
        status = "OK" if completed.returncode == 0 else f"EXIT {completed.returncode}"
        body = out if out else err
        blocks.append(f"$ {cmd}  [{status}]\n{body[:_OUT_CAP]}")
    return "\n\n".join(blocks)
