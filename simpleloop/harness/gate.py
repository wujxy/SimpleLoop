"""Gate: assembles the per-candidate gate-result dict from eval metrics.

The historical pre-commit path-diff check (``check_diff`` / PATH_GATE_REJECTED)
is gone: the executor's writable world is now constructed by the container
mount map (``MountMap``), so anything the executor should not touch is simply
absent from its container — no after-the-fact diff gate is needed. The PATHS
entry is retained in the result schema as an always-pass placeholder so the
gate dict's shape stays stable; the eval-based gates (FCN, etc.) remain the
sole merit authority."""
from __future__ import annotations


PATHS = "PATHS"
EVAL_COMMANDS = "EVAL_COMMANDS"


def _result(passed: bool | None, detail: str = "") -> dict:
    return {"passed": passed, "detail": detail}


def build_results(
    metrics_schema: dict | None,
    *,
    paths: bool | None,
    path_detail: str = "",
    eval_commands: bool | None = None,
    eval_detail: str = "",
    metrics: dict | None = None,
) -> dict[str, dict]:
    configured = (metrics_schema or {}).get("gates", [])
    objective_key = ((metrics_schema or {}).get("objective") or {}).get("key")
    if objective_key in {PATHS, EVAL_COMMANDS}:
        raise ValueError(f"objective key {objective_key} is reserved by the harness")
    seen = {PATHS, EVAL_COMMANDS}
    if objective_key:
        seen.add(objective_key)
    for item in configured:
        key = item["key"]
        if key in seen:
            raise ValueError(f"gate key {key} is reserved or duplicated")
        seen.add(key)

    results = {PATHS: _result(paths, path_detail)}
    if paths is not True:
        reason = "not run because PATHS failed" if paths is False else "not run"
        results[EVAL_COMMANDS] = _result(None, reason)
        results.update({item["key"]: _result(None, reason) for item in configured})
        return results

    results[EVAL_COMMANDS] = _result(eval_commands, eval_detail)
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
