"""Evaluator port, Apptainer adapter, and baseline acceptance policy."""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..candidate import EvaluationResult
from ..container.runtime import ApptainerRuntime
from ..harness import evals
from .gate import GateSpec


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


class BaselineAcceptanceError(RuntimeError):
    """The unmodified baseline cannot serve as an optimization parent."""


class HarnessEvaluator:
    def __init__(
        self,
        runtime: ApptainerRuntime,
        config: EvaluationConfig,
    ):
        self.runtime = runtime
        self.config = config

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        schema = {
            "objective": {"key": self.config.objective_key},
            "gates": [{"key": key} for key in self.config.gate_keys],
        }
        try:
            result = evals.run_eval(
                list(self.config.commands),
                cwd=request.worktree,
                runtime=self.runtime,
                metrics_schema=schema,
                timeout_seconds=self.config.timeout_seconds,
                output_cap=self.config.output_cap_chars,
            )
        except Exception as exc:
            return EvaluationResult(
                f"(eval failed to run: {exc})",
                error=str(exc),
            )
        return EvaluationResult(
            result.text,
            result.metrics,
            tuple(result.returncodes),
        )


def validate_baseline(
    evaluation: EvaluationResult,
    spec: GateSpec,
) -> None:
    failed_codes = [
        code for code in evaluation.returncodes if code != 0
    ]
    if failed_codes:
        raise BaselineAcceptanceError(
            "baseline evaluation command failed with exit "
            f"{failed_codes[0]}:\n{evaluation.text[:8000]}"
        )
    objective = evaluation.metrics.get(spec.objective_key)
    if (
        isinstance(objective, bool)
        or not isinstance(objective, (int, float))
        or not math.isfinite(objective)
    ):
        raise BaselineAcceptanceError(
            f"baseline objective {spec.objective_key} is missing or not "
            f"finite:\n{evaluation.text[:8000]}"
        )
    failed_gates = [
        key for key in spec.gate_keys
        if evaluation.metrics.get(key) is not True
    ]
    if failed_gates:
        raise BaselineAcceptanceError(
            "baseline gate(s) did not pass: "
            f"{', '.join(failed_gates)}:\n{evaluation.text[:8000]}"
        )
