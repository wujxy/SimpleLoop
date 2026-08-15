"""Validate one schema-neutral task manifest into the worker snapshot."""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path


class ConfigError(ValueError):
    """User-facing config error with a v1 field path."""


_PROPOSER_DEFAULTS = {
    "api": "hepai",
    "model": "gpt-5.5",
    "base_url": "https://aiapi.ihep.ac.cn/apiv2",
    "command_timeout_seconds": 120,
    "command_output_cap_chars": 12000,
}
_EXECUTOR_DEFAULTS = {"api": "anthropic"}

_HEPJOB_DEFAULTS = {
    "accounting_group_user": None,
    "collector": None,
    "ihep_group": None,
    "request_os": "AlmaLinux9",
    "cpu_model": None,
    "machine_constraint": None,
    "memory_mb": 6000,
    "cpus": 1,
    "poll_seconds": 30,
    "max_attempts": 2,
    "idle_warn_seconds": 7200,
    "run_timeout_seconds": 21600,
    "disappearance_grace_seconds": 120,
    "python_executable": None,
    "submit_cmd": "condor_submit",
    "query_cmd": "condor_q",
    "remove_cmd": "condor_rm",
}
_HEPJOB_INT_MINIMUMS = {
    "memory_mb": 1,
    "cpus": 1,
    "poll_seconds": 5,
    "max_attempts": 1,
    "idle_warn_seconds": 60,
    "run_timeout_seconds": 300,
    "disappearance_grace_seconds": 0,
}

# Condor pool CPU ads verified at IHEP on 2026-08-03.
_CPU_MODEL_REQUIREMENTS = {
    "zen4": "CpuFamily==25 && CpuModelNumber==17",
    "genoa": "CpuFamily==25 && CpuModelNumber==17",
    "zen5": "CpuFamily==26 && CpuModelNumber==2",
    "turin": "CpuFamily==26 && CpuModelNumber==2",
    "ivybridge": "CpuFamily==6 && CpuModelNumber==62",
    "haswell": "CpuFamily==6 && CpuModelNumber==63",
    "skylake-x": "CpuFamily==6 && CpuModelNumber==79",
    "skylake": "CpuFamily==6 && CpuModelNumber==85",
    "cascadelake": "CpuFamily==6 && CpuModelNumber==85",
    "icelake": "CpuFamily==6 && CpuModelNumber==106",
    "sapphirerapids": "CpuFamily==6 && CpuModelNumber==143",
}


def resolve_manifest(
    manifest: dict,
    config_path: Path,
    *,
    require_ready: bool,
) -> dict:
    """Return the one flat, resolved snapshot consumed by app/workers."""
    goal = manifest.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ConfigError("goal: required non-empty string")
    hints = manifest.get("hints", [])
    if not isinstance(hints, list):
        raise ConfigError("hints: must be a list of strings")
    for index, hint in enumerate(hints):
        if not isinstance(hint, str) or not hint.strip():
            raise ConfigError(f"hints[{index}]: must be a non-empty string")

    loop = _object(manifest, "loop")
    max_rounds = _integer(loop, "max_rounds", 1, required=True, field="loop")
    agent_timeout = _integer(
        loop, "agent_timeout_seconds", 60, default=3600, field="loop",
    )
    agent_tokens = _integer(
        loop, "agent_max_output_tokens", 8000, default=64000, field="loop",
    )
    candidates = _integer(
        loop, "candidates_per_round", 1, default=1, field="loop",
    )
    workers = _integer(
        loop, "max_parallel_candidates", 1, default=1, field="loop",
    )
    steps = _integer(
        loop, "proposer_max_steps", 4, default=200, field="proposer",
        public_key="max_steps",
    )

    world = _object(manifest, "world")
    image, definition, writable_binds, readonly_binds = _resolve_world(
        world, config_path, require_ready=require_ready,
    )
    editable = world.get("writable")
    if not isinstance(editable, list) or not editable:
        raise ConfigError("world.writable: required non-empty list of paths")
    editable = [_normalize_mount_path(str(value)) for value in editable]
    for value in editable:
        if any(char in value for char in ("*", "?", "[")):
            raise ConfigError(
                f"world.writable: glob pattern not supported ({value!r}); "
                "list real directory/file paths"
            )

    source = _object(manifest, "source")
    repo_value = source.get("repo")
    if not isinstance(repo_value, str) or not repo_value.strip():
        raise ConfigError("source.repo: required non-empty path")
    repo = Path(_relative(repo_value, config_path)).resolve()
    if not repo.is_dir():
        raise ConfigError(
            f"source.repo: does not exist or is not a directory: {repo}"
        )
    if require_ready and not (repo / ".git").exists():
        raise ConfigError(f"source.repo: not a git repo: {repo}")
    baseline = str(source.get("baseline") or "HEAD")

    evaluation = _object(manifest, "evaluation")
    commands = evaluation.get("commands")
    if (
        not isinstance(commands, list)
        or not commands
        or not all(isinstance(command, str) and command.strip() for command in commands)
    ):
        raise ConfigError(
            "evaluation.commands: required non-empty list of strings"
        )
    eval_timeout = _integer(
        evaluation, "timeout_seconds", 1, default=600, field="evaluation",
    )
    output_cap = _integer(
        evaluation, "output_cap_chars", 1000, default=16000,
        field="evaluation",
    )
    history_cap = _integer(
        evaluation, "history_cap_chars", 500, default=6000,
        field="evaluation",
    )
    metrics = _resolve_metrics(evaluation)
    backend, hepjob = _resolve_scheduler(_object(manifest, "scheduler"))
    proposer = _resolve_proposer(manifest.get("proposer"))
    executor = _resolve_executor(manifest.get("executor"))

    return {
        "goal": goal,
        "hints": list(hints),
        "editable_paths": editable,
        "max_rounds": max_rounds,
        "agent_timeout_seconds": agent_timeout,
        "agent_max_output_tokens": agent_tokens,
        "candidates_per_round": candidates,
        "max_workers": workers,
        "scientist_steps": steps,
        "context": loop.get("context"),
        "runtime_image": image,
        "runtime_definition": definition,
        "runtime_binds": writable_binds,
        "read_only_binds": readonly_binds,
        "eval_commands": list(commands),
        "eval_timeout_seconds": eval_timeout,
        "eval_output_cap_chars": output_cap,
        "eval_history_cap_chars": history_cap,
        "metrics": metrics,
        "execution_backend": backend,
        "hepjob": hepjob,
        "repo_path": str(repo),
        "baseline_ref": baseline,
        "config_dir": str(config_path.parent),
        "roles": {"researcher": proposer, "executor": executor},
    }


def _resolve_world(
    world: dict,
    config_path: Path,
    *,
    require_ready: bool,
) -> tuple[str, str, list[str], list[str]]:
    image_value = world.get("image")
    if not isinstance(image_value, str) or not image_value.strip():
        raise ConfigError("world.image: required non-empty path")
    image = Path(_relative(image_value, config_path)).expanduser().resolve()
    definition_value = world.get("definition")
    if definition_value is None:
        definition = image.with_suffix(".def")
    elif not isinstance(definition_value, str) or not definition_value.strip():
        raise ConfigError("world.definition: must be a non-empty path")
    else:
        definition = Path(
            _relative(definition_value, config_path)
        ).expanduser().resolve()
    if require_ready:
        if not image.is_file():
            raise ConfigError(
                f"world.image: does not exist or is not a file: {image}"
            )
        if not os.access(image, os.R_OK):
            raise ConfigError(f"world.image: not readable: {image}")
    writable = _bind_dirs(
        world.get("external_writable", []), "world.external_writable",
    )
    readonly = _bind_dirs(
        world.get("external_readonly", []), "world.external_readonly",
    )
    return str(image), str(definition), writable, readonly


def _bind_dirs(values: object, field: str) -> list[str]:
    if not isinstance(values, list):
        raise ConfigError(f"{field}: must be a list of absolute directories")
    result = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{field}[{index}]: must be a non-empty path")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ConfigError(f"{field}[{index}]: must be absolute: {value}")
        path = path.resolve()
        if not path.is_dir():
            raise ConfigError(f"{field}[{index}]: not an existing directory: {path}")
        if ":" in str(path) or "," in str(path):
            raise ConfigError(
                f"{field}[{index}]: contains unsupported ':' or ',': {path}"
            )
        result.append(str(path))
    return result


def _resolve_metrics(evaluation: dict) -> dict:
    objective = evaluation.get("objective")
    if not isinstance(objective, dict):
        raise ConfigError("evaluation.objective: required object")
    unknown = set(objective) - {"key", "lower_is_better"}
    if unknown:
        raise ConfigError(
            f"evaluation.objective: unknown key(s): {sorted(unknown)}"
        )
    key = objective.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ConfigError("evaluation.objective.key: required non-empty string")
    key = key.strip()
    reserved = {"PATHS", "EVAL_COMMANDS"}
    if key in reserved:
        raise ConfigError(f"evaluation.objective.key: {key} is reserved")
    lower = objective.get("lower_is_better")
    if not isinstance(lower, bool):
        raise ConfigError(
            "evaluation.objective.lower_is_better: required boolean"
        )
    raw_gates = evaluation.get("gates", [])
    if not isinstance(raw_gates, list):
        raise ConfigError("evaluation.gates: must be a list")
    gates, seen = [], {key}
    for index, gate in enumerate(raw_gates):
        if not isinstance(gate, dict):
            raise ConfigError(f"evaluation.gates[{index}]: must be an object")
        unknown = set(gate) - {"key", "description"}
        if unknown:
            raise ConfigError(
                f"evaluation.gates[{index}]: unknown key(s): {sorted(unknown)}"
            )
        gate_key = gate.get("key")
        if not isinstance(gate_key, str) or not gate_key.strip():
            raise ConfigError(
                f"evaluation.gates[{index}].key: required non-empty string"
            )
        gate_key = gate_key.strip()
        if gate_key in reserved:
            raise ConfigError(
                f"evaluation.gates[{index}].key: {gate_key} is reserved"
            )
        if gate_key in seen:
            raise ConfigError(
                f"evaluation objective and gate keys must be unique: {gate_key}"
            )
        seen.add(gate_key)
        description = gate.get("description")
        if description is not None and not isinstance(description, str):
            raise ConfigError(
                f"evaluation.gates[{index}].description: must be a string"
            )
        row = {"key": gate_key}
        if description and description.strip():
            row["description"] = description.strip()
        gates.append(row)
    return {"objective": {"key": key, "lower_is_better": lower}, "gates": gates}


def _resolve_proposer(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("proposer: must be an object")
    unknown = set(raw) - set(_PROPOSER_DEFAULTS)
    if unknown:
        raise ConfigError(f"proposer: unknown key(s): {sorted(unknown)}")
    result = {**_PROPOSER_DEFAULTS, **raw}
    if result["api"] not in ("hepai", "zhipu", "anthropic"):
        raise ConfigError(
            "proposer.api: supported values are hepai, zhipu and anthropic")
    for key in ("model", "base_url"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise ConfigError(f"proposer.{key}: must be a non-empty string")
    for key, minimum in (
        ("command_timeout_seconds", 1),
        ("command_output_cap_chars", 1000),
    ):
        value = result[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ConfigError(f"proposer.{key}: must be an integer >= {minimum}")
    return result


def _resolve_executor(raw: object) -> dict | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("executor: must be an object")
    unknown = set(raw) - set(_EXECUTOR_DEFAULTS) - {"model", "base_url"}
    if unknown:
        raise ConfigError(f"executor: unknown key(s): {sorted(unknown)}")
    result = {**_EXECUTOR_DEFAULTS, **raw}
    if result["api"] != "anthropic":
        raise ConfigError("executor.api: supports only 'anthropic'")
    for key in ("model", "base_url"):
        value = result.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"executor.{key}: must be a non-empty string")
    return result


def _resolve_scheduler(raw: dict) -> tuple[str, dict]:
    kind = raw.get("kind", "local")
    options = raw.get("options", {})
    if kind not in ("local", "hepjob"):
        raise ConfigError("providers.scheduler.kind: expected local or hepjob")
    if not isinstance(options, dict):
        raise ConfigError("providers.scheduler: settings must be an object")
    allowed = {"schedd_name", "accounting_group"} | set(_HEPJOB_DEFAULTS)
    unknown = set(options) - allowed
    if unknown:
        raise ConfigError(
            f"providers.scheduler: unknown key(s): {sorted(unknown)}"
        )
    if kind == "local" and options:
        raise ConfigError("providers.scheduler: local accepts no settings")
    hepjob = dict(_HEPJOB_DEFAULTS)
    hepjob["accounting_group_user"] = getpass.getuser()
    hepjob["python_executable"] = sys.executable
    hepjob.update(options)
    for key in ("schedd_name", "accounting_group"):
        value = hepjob.get(key)
        if kind == "hepjob" and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ConfigError(
                f"providers.scheduler.{key}: required for hepjob"
            )
    string_keys = (
        "schedd_name", "collector", "accounting_group",
        "accounting_group_user", "ihep_group", "request_os", "cpu_model",
        "machine_constraint", "python_executable", "submit_cmd", "query_cmd",
        "remove_cmd",
    )
    for key in string_keys:
        value = hepjob.get(key)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"providers.scheduler.{key}: must be a string")
    if hepjob.get("cpu_model") is not None:
        model = hepjob["cpu_model"].strip().lower()
        if model not in _CPU_MODEL_REQUIREMENTS:
            raise ConfigError(
                f"providers.scheduler.cpu_model: unknown model {model!r}; "
                f"choose from {sorted(_CPU_MODEL_REQUIREMENTS)}"
            )
        hepjob["cpu_model"] = model
    for key, minimum in _HEPJOB_INT_MINIMUMS.items():
        value = hepjob[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ConfigError(
                f"providers.scheduler.{key}: must be an integer >= {minimum}"
            )
    return kind, hepjob


def _object(raw: dict, key: str) -> dict:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: required and must be an object")
    return value


def _integer(
    raw: dict,
    key: str,
    minimum: int,
    *,
    field: str,
    required: bool = False,
    default: int | None = None,
    public_key: str | None = None,
) -> int:
    value = raw.get(key) if required else raw.get(key, default)
    label = f"{field}.{public_key or key}"
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        qualifier = "required " if required else ""
        raise ConfigError(f"{label}: {qualifier}integer >= {minimum}")
    return value


def _relative(value: str, config_path: Path) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = config_path.parent / path
    return str(path)


def _normalize_mount_path(value: str) -> str:
    path = value.strip().rstrip("/")
    return path[:-3] if path.endswith("/**") else path
