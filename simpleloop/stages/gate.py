"""Pure hard-gate decision for a candidate evaluation."""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..candidate import EvaluationResult, GateDecision, GateResult


PATHS = "PATHS"
EVAL_COMMANDS = "EVAL_COMMANDS"


@dataclass(frozen=True)
class GateSpec:
    objective_key: str
    gate_keys: tuple[str, ...]


def unavailable_gates(
    spec: GateSpec,
    reason: str = "not run",
) -> GateDecision:
    rows = {
        PATHS: GateResult(None, ""),
        EVAL_COMMANDS: GateResult(None, reason),
    }
    rows.update({key: GateResult(None, reason) for key in spec.gate_keys})
    return GateDecision(rows, False, False)


def apply_gates(
    evaluation: EvaluationResult | None,
    spec: GateSpec,
    *,
    skip_reason: str | None = None,
) -> GateDecision:
    if spec.objective_key in {PATHS, EVAL_COMMANDS}:
        raise ValueError(
            f"objective key {spec.objective_key} is reserved by the harness"
        )
    seen = {PATHS, EVAL_COMMANDS, spec.objective_key}
    for key in spec.gate_keys:
        if key in seen:
            raise ValueError(f"gate key {key} is reserved or duplicated")
        seen.add(key)

    if evaluation is None:
        reason = skip_reason or "not run"
        rows = {
            PATHS: GateResult(True),
            EVAL_COMMANDS: GateResult(None, reason),
        }
        rows.update({key: GateResult(None, reason) for key in spec.gate_keys})
        return GateDecision(rows, False, False)

    commands_ok = (
        evaluation.error is None
        and all(code == 0 for code in evaluation.returncodes)
    )
    command_detail = evaluation.error or (
        "" if commands_ok else f"exit codes: {list(evaluation.returncodes)}"
    )
    rows = {
        PATHS: GateResult(True),
        EVAL_COMMANDS: GateResult(commands_ok, command_detail),
    }
    for key in spec.gate_keys:
        value = evaluation.metrics.get(key)
        detail = (
            ""
            if value is True
            else "evaluator reported FAIL"
            if value is False
            else "metric missing or unknown"
        )
        rows[key] = GateResult(
            value if isinstance(value, bool) else None,
            detail,
        )
    passed = all(row.passed is True for row in rows.values())
    objective = evaluation.metrics.get(spec.objective_key)
    eligible = bool(
        passed
        and isinstance(objective, (int, float))
        and not isinstance(objective, bool)
        and math.isfinite(objective)
    )
    return GateDecision(rows, passed, eligible)
