"""Executor port for one candidate implementation attempt."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..candidate import ExecutionResult
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
    worktree: Path


class Executor(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        ...
