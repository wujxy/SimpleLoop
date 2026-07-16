"""Judger: looks at the diff + eval output, grades the round, gives feedback.

The harness (not the LLM) computes the diff and runs the eval commands — these
are deterministic and must not be delegated to the agent. The judger agent only
judges: it sees goal + proposal + diff + eval stdout and returns a score and
feedback for the next proposer.

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
          eval_commands: list[str], cwd: Path) -> Judgment:
    """Grade one round. Returns (score, feedback)."""
    if sha is not None:
        diff = workspace.diff(parent_sha, sha)
        eval_block = _run_eval(eval_commands, workspace.repo) if eval_commands else ""
    else:
        diff = f"(no commit produced this round: {reason})"
        eval_block = ""

    prompt = _build_prompt(goal, proposal, diff, eval_block)
    data = agent.run_json(prompt, cwd=cwd, label="judger")
    return _parse(data)


def _build_prompt(goal: str, proposal: str, diff: str, eval_block: str) -> str:
    eval_section = f"""Eval command output:
{eval_block}
""" if eval_block else ""
    return f"""You are the JUDGER in a serial optimization loop. Grade this round's change.

Task goal:
{goal}

Direction that was attempted:
{proposal}

Change (git diff vs the previous round's result):
```diff
{diff}
```
{eval_section}
Judge whether the change: moves in the right direction, achieves real improvement (use the eval output if present), introduces risk, and is good-quality code. Give a score from 0.0 to 1.0 and concrete feedback for the next proposer.

Return exactly one JSON object: {{"score": 0.0, "feedback": "<feedback>"}}"""


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


def _run_eval(commands: list[str], repo: Path) -> str:
    """Run each eval command in the working repo, collect stdout/stderr.

    Deterministic — the harness owns this, not the judger agent. Each command's
    output is capped to keep the prompt bounded.
    """
    blocks: list[str] = []
    for cmd in commands:
        completed = subprocess.run(
            cmd, shell=True, cwd=str(repo),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=600, check=False,
        )
        out = completed.stdout.strip()
        err = completed.stderr.strip()
        status = "OK" if completed.returncode == 0 else f"EXIT {completed.returncode}"
        body = out if out else err
        blocks.append(f"$ {cmd}  [{status}]\n{body[:4000]}")
    return "\n\n".join(blocks)
