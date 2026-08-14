"""Executor: turns the proposal into a code change and delivers a commit SHA
(or None when gate-rejected / no change). The agent only edits the worktree;
the harness gates the changed paths and commits them itself."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from ..prompts import load_semantic
from ..harness.workspace import Workspace
from ..stages.executor import parse_self_report


@dataclass
class ExecResult:
    sha: str | None          # None when gate-rejected or no change
    reason: str | None       # None on success; otherwise why there's no SHA
    changed_paths: list[str]
    path_gate_passed: bool
    path_gate_violations: list[str]
    output: str = ""         # the agent's raw text response (for handoff logs)
    # The executor's structured SELF_REPORT (parsed best-effort from output).
    # None when the agent emitted no usable block. Carries the executor's own
    # framing of whether it finished and WHY a change is absent/incomplete,
    # partitioned into objective (a fact about the code: target missing /
    # ambiguous / frozen / structurally unbuildable) vs effort (feasible but
    # too complex/risky this session). Recorded into history so the loop has
    # the executor's voice; NOT used to steer the Proposer (the Harness gates
    # remain the only merit oracle) — see prompts/executor.md.
    self_report: dict | None = None


def execute(agent: Agent, *, proposal: str, goal: str,
            workspace: Workspace, worktree: Path, round_id: int | str,
            gate_block: str = "", prompt_dir: str | Path | None = None) -> ExecResult:
    """Run the executor agent and produce (or fail to produce) a commit.

    The executor's file world (what it can read/write) is constructed entirely
    by the harness via the container mount map on the agent — it is not stated
    in the prompt and not gated after the fact. Anything the executor should
    not touch is simply absent from its container; edits land in the worktree
    via the binds, and the harness commits them."""
    semantic = load_semantic("executor", prompt_dir)
    prompt = f"""{semantic}

Task goal:
{goal}

Direction to implement:
{proposal}

Gates:
{gate_block}

Fixed execution boundaries:
- Edits stay inside the assigned worktree (your writable world).
- Git staging and commits belong to the harness.
- Verification side effects outside the intended source change are restored
  before delivery.

When the implementation and verification are complete, emit your SELF_REPORT
block (see the protocol in your role brief) and stop. The Harness inspects and
commits the resulting file changes; the SELF_REPORT block is the only required
structured response.
"""

    agent_output = agent.run_text(prompt, cwd=worktree, label=f"executor r{round_id}")
    self_report = parse_self_report(agent_output)

    changed = workspace.changed_paths(worktree)
    if not changed:
        return ExecResult(
            sha=None,
            reason="executor made no changes",
            changed_paths=[],
            path_gate_passed=True,
            path_gate_violations=[],
            output=agent_output,
            self_report=self_report,
        )

    sha = workspace.commit(worktree, round_id, changed)
    return ExecResult(
        sha=sha,
        reason=None,
        changed_paths=changed,
        path_gate_passed=True,
        path_gate_violations=[],
        output=agent_output,
        self_report=self_report,
    )
