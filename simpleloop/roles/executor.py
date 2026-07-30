"""Executor: turns the proposal into a code change and delivers a commit SHA
(or None when gate-rejected / no change). The agent only edits the worktree;
the harness gates the changed paths and commits them itself."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from ..prompts import load_semantic
from ..harness import gate
from ..harness.workspace import Workspace


@dataclass
class ExecResult:
    sha: str | None          # None when gate-rejected or no change
    reason: str | None       # None on success; otherwise why there's no SHA
    changed_paths: list[str]


def execute(agent: Agent, *, proposal: str, goal: str, editable: list[str],
            frozen: list[str], workspace: Workspace, worktree: Path,
            round_id: int | str,
            gate_block: str = "", prompt_dir: str | Path | None = None) -> ExecResult:
    """Run the executor agent and produce (or fail to produce) a commit."""
    semantic = load_semantic("executor", prompt_dir)
    prompt = f"""{semantic}

Task goal:
{goal}

Direction to implement:
{proposal}

Gates:
{gate_block}

Fixed execution boundaries:
- Editable paths: {editable}
- Frozen paths: {frozen}
- Edits stay inside the assigned worktree.
- Git staging and commits belong to the harness.
- Verification side effects outside the intended source change are restored
  before delivery.

When the implementation and verification are complete, stop. The harness
inspects and commits the resulting file changes; no structured response is
required.
"""

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
