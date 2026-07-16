"""Executor: turns the proposal into a code change. Delivers a commit SHA (or None).

Flow:
  1. Prompt the agent with goal + proposal + safety; tell it to edit files in the
     worktree and NOT to commit (the harness commits).
  2. After the agent returns, read the worktree's changed paths.
  3. Gate: if any changed path is frozen or outside editable, reject (sha=None).
  4. Else harness stages exactly the changed paths in editable and commits -> SHA.
  5. If nothing changed, sha=None with reason "empty".

The executor agent returns no JSON — it just edits files. We only care that it ran.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from . import gate
from .workspace import Workspace


@dataclass
class ExecResult:
    sha: str | None          # None when gate-rejected or no change
    reason: str | None       # None on success; otherwise why there's no SHA
    changed_paths: list[str]


def execute(agent: Agent, *, proposal: str, goal: str, editable: list[str],
            frozen: list[str], workspace: Workspace, worktree: Path,
            round_id: int) -> ExecResult:
    """Run the executor agent and produce (or fail to produce) a commit."""
    prompt = f"""You are the EXECUTOR in a serial optimization loop. Implement the proposed direction by editing source files.

Task goal:
{goal}

Direction to implement:
{proposal}

Safety (hard rules):
- You may only edit files under: {editable}
- You must NOT touch files under: {frozen}  (touching them makes the gate reject this round, voiding it)
- Do NOT run `git commit` / `git add` yourself — the harness stages and commits your edits.
- Do NOT edit files outside the working tree you are in.

Guidance:
- Implement the direction with the smallest correct change you can.
- Make sure the code still runs after your edits — run the build/tests/benchmark yourself if they are available, and fix anything you break.
- Keep the change focused; do not reformat or refactor unrelated code.

When you are done, simply stop. No JSON output is needed — the harness will inspect your file changes."""
    agent.run_text(prompt, cwd=worktree, label=f"executor r{round_id}")

    changed = workspace.changed_paths(worktree)
    if not changed:
        return ExecResult(sha=None, reason="executor made no changes", changed_paths=[])

    ok, violations = gate.check_diff(changed, editable, frozen)
    if not ok:
        return ExecResult(sha=None, reason="gate rejected: " + "; ".join(violations),
                          changed_paths=changed)

    sha = workspace.commit(worktree, round_id, changed)
    return ExecResult(sha=sha, reason=None, changed_paths=changed)
