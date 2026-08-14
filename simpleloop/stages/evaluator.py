"""Evaluator port, World adapter, and baseline acceptance policy."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from ..candidate import EvaluationResult
from ..world import ExecutionSandbox, ProcessRequest, SourceWorkspace
from .gate import GateSpec


@dataclass(frozen=True)
class EvalResult:
    text: str
    metrics: dict
    returncodes: tuple[int, ...]

    @property
    def commands_ok(self) -> bool:
        return all(code == 0 for code in self.returncodes)


def objective_delta(this, other, lower_is_better: bool) -> tuple[float, bool] | None:
    """Return percentage change and whether it improves on ``other``."""
    if (
        not isinstance(this, (int, float))
        or not isinstance(other, (int, float))
        or other == 0
    ):
        return None
    change = (this - other) / other * 100.0
    return change, (change < 0 if lower_is_better else change > 0)


def run_eval(
    commands: list[str],
    world: ExecutionSandbox,
    metrics_schema: dict | None = None,
    timeout_seconds: int = 600,
    output_cap: int = 16000,
) -> EvalResult:
    """Run evaluation commands and parse their declared metric lines."""
    blocks, output, returncodes = [], [], []
    for command in commands:
        completed = world.run(ProcessRequest(
            ("bash", "-c", command), PurePosixPath("/work"), timeout_seconds,
            label="evaluation",
        ))
        stdout, stderr = completed.stdout.strip(), completed.stderr.strip()
        status = "OK" if completed.exit_code == 0 else f"EXIT {completed.exit_code}"
        body = (
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
            if stdout and stderr else stdout or stderr
        )
        metric_source = f"{stdout}\n{stderr}" if stdout and stderr else body
        blocks.append(f"$ {command}  [{status}]\n{body[:output_cap]}")
        output.append(metric_source)
        returncodes.append(completed.exit_code)
    combined = "\n".join(output)
    metrics = _parse_metrics(combined, metrics_schema) if metrics_schema else {}
    return EvalResult("\n\n".join(blocks), metrics, tuple(returncodes))


def _parse_metrics(text: str, schema: dict | None) -> dict:
    if not schema:
        return {}
    keys = []
    objective = schema.get("objective", {})
    if objective.get("key"):
        keys.append((objective["key"], "objective"))
    keys.extend(
        (gate["key"], "gate")
        for gate in schema.get("gates", []) if gate.get("key")
    )
    parsed = {}
    for key, role in keys:
        match = re.search(
            rf"(?m)^\s*{re.escape(key)}\s*=\s*(\S+)", text,
        )
        if not match:
            continue
        token = match.group(1)
        if role == "objective":
            try:
                parsed[key] = float(token)
            except ValueError:
                pass
        else:
            parsed[key] = _gate_to_bool(token)
    return parsed


def _gate_to_bool(token: str):
    value = token.strip().lower()
    if value in ("", "na", "n/a", "none", "null"):
        return None
    if value in ("pass", "passed", "ok", "true", "1", "yes", "success"):
        return True
    if "fail" in value or value in (
        "false", "0", "no", "error", "err", "broken",
    ):
        return False
    return None


@dataclass(frozen=True)
class EvaluationConfig:
    commands: tuple[str, ...]
    objective_key: str
    gate_keys: tuple[str, ...]
    timeout_seconds: int = 600
    output_cap_chars: int = 16000


@dataclass(frozen=True)
class EvaluationRequest:
    workspace: SourceWorkspace


class Evaluator(Protocol):
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        ...


class BaselineAcceptanceError(RuntimeError):
    """The unmodified baseline cannot serve as an optimization parent."""


class WorldEvaluator:
    def __init__(
        self,
        world: ExecutionSandbox,
        config: EvaluationConfig,
    ):
        self.world = world
        self.config = config

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        schema = {
            "objective": {"key": self.config.objective_key},
            "gates": [{"key": key} for key in self.config.gate_keys],
        }
        try:
            result = run_eval(
                list(self.config.commands),
                world=self.world,
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
