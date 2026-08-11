"""Executor: turn a proposal into an unrestricted workspace change and commit."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .agent import Agent
from ..prompts import load_semantic
from ..harness.workspace import Workspace


@dataclass
class ExecResult:
    sha: str | None          # None when the executor produced no change
    reason: str | None       # None on success; otherwise why there's no SHA
    changed_paths: list[str]
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


# Accepted SELF_REPORT values. Best-effort parse: anything outside these is
# treated as "no usable block" (None), so a malformed report never breaks a
# candidate — the full text is still captured in output / the handoff.
_SELF_REPORT_OUTCOMES = frozenset({"completed", "partial", "blocked"})
_SELF_REPORT_KINDS = frozenset({"objective", "effort"})
_SUMMARY_MAX_CHARS = 600
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", re.DOTALL)


def parse_self_report(text: str) -> dict | None:
    """Best-effort parse of the executor's SELF_REPORT block.

    Scans fenced JSON objects (last-first) and keeps the first that declares
    an ``outcome`` key — the executor otherwise emits prose/code, so a fenced
    object with ``outcome`` is the report. Returns a normalized dict, or None
    if no trustworthy block is present. Never raises.
    """
    if not text:
        return None
    report = None
    for raw in reversed(_JSON_FENCE_RE.findall(text)):
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "outcome" in obj:
            report = obj
            break
    if report is None:
        return None
    outcome = report.get("outcome")
    if outcome not in _SELF_REPORT_OUTCOMES:
        return None
    kind = report.get("blocked_reason_kind")
    if kind not in _SELF_REPORT_KINDS:
        kind = None
    summary = report.get("summary")
    if not isinstance(summary, str):
        summary = ""
    return {
        "outcome": outcome,
        "blocked_reason_kind": kind,
        "summary": summary.strip()[:_SUMMARY_MAX_CHARS],
    }


def execute(agent: Agent, *, proposal: str, goal: str,
            workspace: Workspace, worktree: Path, round_id: int | str,
            gate_block: str = "", prompt_dir: str | Path | None = None) -> ExecResult:
    """Run the executor agent and commit any change inside its workspace."""
    semantic = load_semantic("executor", prompt_dir)
    prompt = f"""{semantic}

Task goal:
{goal}

Direction to implement:
{proposal}

Gates:
{gate_block}

Workspace:
The provided workspace contains the complete mutable production artifact.
You may inspect, create, delete, move, replace, or reorganize anything inside it.
Its current structure is only the starting implementation, not part of the specification.
All task-specific prior knowledge available to you has been placed in this workspace.
Git staging and commits belong to the harness.

Evaluation:
Success is determined only by the stated goal and gates. Evaluation is external
to the workspace; do not infer structural requirements beyond those criteria.

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
            output=agent_output,
            self_report=self_report,
        )

    sha = workspace.commit(worktree, round_id, changed)
    return ExecResult(
        sha=sha,
        reason=None,
        changed_paths=changed,
        output=agent_output,
        self_report=self_report,
    )
