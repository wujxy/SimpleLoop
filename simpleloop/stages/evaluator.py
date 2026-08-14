"""Evaluator port for one committed candidate worktree."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..candidate import EvaluationResult


@dataclass(frozen=True)
class EvaluationConfig:
    commands: tuple[str, ...]
    objective_key: str
    gate_keys: tuple[str, ...]
    timeout_seconds: int = 600
    output_cap_chars: int = 16000


@dataclass(frozen=True)
class EvaluationRequest:
    worktree: Path


class Evaluator(Protocol):
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        ...
