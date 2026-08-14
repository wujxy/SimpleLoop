"""Executor port and Agent adapter for one candidate attempt."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..candidate import ExecutionResult
from ..prompts import load_semantic
from ..roles.agent import Agent, AgentError
from ..world import SourceWorkspace
from .proposer import Proposal


@dataclass(frozen=True)
class ExecutorConfig:
    goal: str
    gate_block: str = ""
    prompt_dir: Path | None = None


@dataclass(frozen=True)
class ExecutionRequest:
    round_id: int
    candidate_id: int
    proposal: Proposal
    workspace: SourceWorkspace


class Executor(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        ...


_SELF_REPORT_OUTCOMES = frozenset({"completed", "partial", "blocked"})
_SELF_REPORT_KINDS = frozenset({"objective", "effort"})
_SUMMARY_MAX_CHARS = 600
_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n(\{.*?\})\s*\n```", re.DOTALL,
)


def parse_self_report(text: str) -> dict | None:
    """Best-effort extraction of the Executor's final SELF_REPORT."""
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
    if report is None or report.get("outcome") not in _SELF_REPORT_OUTCOMES:
        return None
    kind = report.get("blocked_reason_kind")
    if kind not in _SELF_REPORT_KINDS:
        kind = None
    summary = report.get("summary")
    if not isinstance(summary, str):
        summary = ""
    return {
        "outcome": report["outcome"],
        "blocked_reason_kind": kind,
        "summary": summary.strip()[:_SUMMARY_MAX_CHARS],
    }


class AgentExecutor:
    """Run the implementation agent without inspecting or committing Git."""

    def __init__(self, agent: Agent, config: ExecutorConfig):
        self.agent = agent
        self.config = config

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        semantic = load_semantic("executor", self.config.prompt_dir)
        prompt = f"""{semantic}

Task goal:
{self.config.goal}

Direction to implement:
{request.proposal.instruction}

Gates:
{self.config.gate_block}

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
        try:
            output = self.agent.run_text(
                prompt,
                cwd=request.workspace.path,
                label=f"executor r{request.round_id}-c{request.candidate_id}",
            )
        except (AgentError, ValueError) as exc:
            return ExecutionResult("EXECUTOR_FAILED", reason=str(exc))
        return ExecutionResult(
            "EXECUTED",
            output=output,
            self_report=parse_self_report(output),
        )
