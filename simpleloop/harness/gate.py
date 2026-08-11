"""Gate: normalize external evaluator status and configured correctness metrics."""
from __future__ import annotations

EVAL_COMMANDS = "EVAL_COMMANDS"


def _result(passed: bool | None, detail: str = "") -> dict:
    return {"passed": passed, "detail": detail}


def build_results(
    metrics_schema: dict | None,
    *,
    eval_commands: bool | None = None,
    eval_detail: str = "",
    metrics: dict | None = None,
) -> dict[str, dict]:
    configured = (metrics_schema or {}).get("gates", [])
    objective_key = ((metrics_schema or {}).get("objective") or {}).get("key")
    if objective_key in {EVAL_COMMANDS}:
        raise ValueError(f"objective key {objective_key} is reserved by the harness")
    seen = {EVAL_COMMANDS}
    if objective_key:
        seen.add(objective_key)
    for item in configured:
        key = item["key"]
        if key in seen:
            raise ValueError(f"gate key {key} is reserved or duplicated")
        seen.add(key)

    results = {EVAL_COMMANDS: _result(eval_commands, eval_detail)}
    values = metrics or {}
    for item in configured:
        key = item["key"]
        value = values.get(key)
        detail = (
            ""
            if value is True
            else "evaluator reported FAIL"
            if value is False
            else "metric missing or unknown"
        )
        results[key] = _result(value if isinstance(value, bool) else None, detail)
    return results


def all_passed(results: dict[str, dict]) -> bool:
    return bool(results) and all(
        item.get("passed") is True for item in results.values()
    )
