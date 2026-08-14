"""One-release adapter for legacy ``kind: task`` documents."""
from __future__ import annotations

from pathlib import Path

from ._config_runtime import ConfigError, resolve_manifest


_TOP_KEYS = {
    "kind", "task", "safety", "loop", "runtime", "eval", "source",
    "execution", "roles",
}


def resolve(raw: dict, path: Path, *, require_ready: bool) -> dict:
    """Translate the complete old vocabulary into the neutral manifest."""
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        hint = (
            " — 'researcher:' moved under 'roles:'; use 'roles.researcher:'"
            if "researcher" in unknown else ""
        )
        raise ConfigError(
            f"config: unknown top-level key(s): {sorted(unknown)}{hint}"
        )
    if raw.get("kind") != "task":
        raise ConfigError("config: kind must be 'task'")
    task = _block(raw, "task")
    safety = _block(raw, "safety")
    loop = _block(raw, "loop")
    runtime = _block(raw, "runtime")
    evaluation = _block(raw, "eval")
    source = _block(raw, "source")
    execution = raw.get("execution") or {}
    roles = raw.get("roles") or {}
    for value, allowed, field in (
        (task, {"goal", "hints"}, "task"),
        (safety, {"editable_paths"}, "safety"),
        (loop, {
            "max_rounds", "agent_timeout_seconds", "agent_max_output_tokens",
            "candidates_per_round", "max_workers", "scientist_steps",
            "cognitive_steps", "gen_steps", "context",
        }, "loop"),
        (runtime, {"image", "definition", "binds", "read_only_binds"}, "runtime"),
        (evaluation, {
            "commands", "metrics", "timeout_seconds", "output_cap_chars",
            "history_cap_chars",
        }, "eval"),
        (source, {"path", "baseline_ref"}, "source"),
    ):
        _unknown(value, allowed, field)
    if not isinstance(execution, dict):
        raise ConfigError("execution: must be an object")
    _unknown(execution, {"backend", "hepjob"}, "execution")
    hepjob = execution.get("hepjob") or {}
    if not isinstance(hepjob, dict):
        raise ConfigError("execution.hepjob: must be an object")
    if not isinstance(roles, dict):
        raise ConfigError("roles: must be an object")
    _unknown(roles, {"researcher", "executor"}, "roles")
    if "researcher" in roles and not isinstance(roles["researcher"], dict):
        raise ConfigError("researcher: must be an object")
    if "executor" in roles and not isinstance(roles["executor"], dict):
        raise ConfigError("roles.executor: must be an object")

    metrics = evaluation.get("metrics")
    if not isinstance(metrics, dict):
        raise ConfigError("eval.metrics: required and must be an object")
    _unknown(metrics, {"objective", "gates"}, "eval.metrics")
    objective = metrics.get("objective")
    if isinstance(objective, dict):
        _unknown(
            objective, {"key", "lower_is_better"}, "eval.metrics.objective",
        )

    steps = loop.get("scientist_steps")
    if steps is None:
        steps = loop.get("cognitive_steps", loop.get("gen_steps", 200))
    manifest = {
        "goal": task.get("goal"),
        "hints": task.get("hints", []),
        "loop": {
            "max_rounds": loop.get("max_rounds"),
            "agent_timeout_seconds": loop.get("agent_timeout_seconds", 3600),
            "agent_max_output_tokens": loop.get("agent_max_output_tokens", 64000),
            "candidates_per_round": loop.get("candidates_per_round", 1),
            "max_parallel_candidates": loop.get("max_workers", 1),
            "proposer_max_steps": steps,
            "context": loop.get("context"),
        },
        "source": {
            "repo": source.get("path"),
            "baseline": source.get("baseline_ref", "HEAD"),
        },
        "world": {
            "image": runtime.get("image"),
            "definition": runtime.get("definition"),
            "writable": safety.get("editable_paths"),
            "external_writable": runtime.get("binds", []),
            "external_readonly": runtime.get("read_only_binds", []),
        },
        "evaluation": {
            "commands": evaluation.get("commands"),
            "objective": objective,
            "gates": metrics.get("gates", []),
            "timeout_seconds": evaluation.get("timeout_seconds", 600),
            "output_cap_chars": evaluation.get("output_cap_chars", 16000),
            "history_cap_chars": evaluation.get("history_cap_chars", 6000),
        },
        "scheduler": {
            "kind": execution.get("backend", "local"),
            "options": hepjob,
        },
        "proposer": roles.get("researcher"),
        "executor": roles.get("executor"),
    }
    try:
        return resolve_manifest(manifest, path, require_ready=require_ready)
    except ConfigError as exc:
        raise ConfigError(_legacy_error(str(exc))) from exc


def _block(raw: dict, key: str) -> dict:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: required and must be an object")
    return value


def _unknown(raw: dict, allowed: set[str], field: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{field}: unknown key(s): {sorted(unknown)}")


def _legacy_error(message: str) -> str:
    for new, old in (
        ("world.external_readonly", "runtime.read_only_binds"),
        ("world.external_writable", "runtime.binds"),
        ("world.writable", "safety.editable_paths"),
        ("world.image", "runtime.image"),
        ("world.definition", "runtime.definition"),
        ("source.repo", "source.path"),
        ("source.baseline", "source.baseline_ref"),
        ("loop.max_parallel_candidates", "loop.max_workers"),
        ("proposer.max_steps", "loop.scientist_steps"),
        ("evaluation.objective", "eval.metrics.objective"),
        ("evaluation.gates", "eval.metrics.gates"),
        ("evaluation.", "eval."),
        ("providers.scheduler.kind", "execution.backend"),
        ("providers.scheduler.", "execution.hepjob."),
        ("providers.scheduler", "execution.hepjob"),
        ("proposer.", "researcher."),
        ("proposer:", "researcher:"),
        ("executor.", "roles.executor."),
        ("hints", "task.hints"),
        ("goal", "task.goal"),
    ):
        message = message.replace(new, old)
    return message
