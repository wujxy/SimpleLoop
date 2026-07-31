"""Gate: deterministic pre-commit diff safety check — reject a commit whose
diff touches a frozen path or a path outside editable_paths. Never delegated
to an LLM."""
from __future__ import annotations

from fnmatch import fnmatch


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


def check_diff(changed_paths: list[str], editable: list[str], frozen: list[str]) -> tuple[bool, list[str]]:
    """Return (ok, violations). A violation is a human-readable reason per bad path."""
    violations: list[str] = []
    for path in changed_paths:
        if _matches_any(path, frozen):
            violations.append(f"{path}: touches a frozen path")
        elif not _matches_any(path, editable):
            violations.append(f"{path}: outside editable_paths")
    return (len(violations) == 0, violations)


def _matches_any(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch(path, pat):
            return True
        # support ** globs minimally (fnmatch treats ** like *)
        if "/**/" in pat and fnmatch(path, pat.replace("/**/", "/")):
            return True
        if pat.startswith("**/") and fnmatch(path, pat[3:]):
            return True
        if pat.endswith("/**") and (path == pat[:-3].rstrip("/") or path.startswith(pat[:-3])):
            return True
    return False
