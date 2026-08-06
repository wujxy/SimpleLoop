"""Executor: turns the proposal into a code change and delivers a commit SHA
(or None when gate-rejected / no change). The agent only edits the worktree;
the harness gates the changed paths and commits them itself."""
from __future__ import annotations

import json
import re
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

    ok, violations = gate.check_diff(changed, editable, frozen)
    if not ok:
        return ExecResult(
            sha=None,
            reason="gate rejected: " + "; ".join(violations),
            changed_paths=changed,
            path_gate_passed=False,
            path_gate_violations=violations,
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
