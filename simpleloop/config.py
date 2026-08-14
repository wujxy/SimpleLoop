"""The single external configuration boundary for SimpleLoop."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from . import _config_runtime, legacy_config


ConfigError = _config_runtime.ConfigError
RESOLVED_SNAPSHOT_NAME = "config.resolved.json"

_TOP_KEYS = {
    "schema", "goal", "hints", "loop", "source", "world", "evaluation",
    "providers", "proposer", "executor", "rsi",
}


def load(config_path: str | Path, *, require_ready: bool = True) -> dict[str, Any]:
    """Load either ``simpleloop.v1`` or one transitional legacy task file."""
    path = Path(config_path).expanduser().resolve()
    raw = _read_document(path)
    if raw.get("schema") == "simpleloop.v1":
        return _resolve_v1(raw, path, require_ready=require_ready)
    if "schema" in raw:
        raise ConfigError(
            f"config.schema: expected 'simpleloop.v1', got {raw['schema']!r}"
        )
    resolved = legacy_config.resolve(raw, path, require_ready=require_ready)
    resolved["sandbox_userns"] = (
        os.environ.get("SIMPLELOOP_APPTAINER_USERNS", "1") != "0"
    )
    resolved["rsi"] = {"enabled": False}
    return resolved


def cpu_model_requirement(name: str) -> str:
    """Return the validated HTCondor requirement for a configured CPU name."""
    return _config_runtime._CPU_MODEL_REQUIREMENTS[name]


def load_resolved(run_dir: str | Path) -> dict[str, Any]:
    """Read a run's normalized snapshot, including pre-v1 snapshots."""
    path = Path(run_dir).expanduser().resolve() / RESOLVED_SNAPSHOT_NAME
    if not path.exists():
        raise ConfigError(
            f"no {RESOLVED_SNAPSHOT_NAME} in {path.parent} — the run predates "
            "config snapshots; pass --config explicitly"
        )
    try:
        resolved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(resolved, dict):
        raise ConfigError(f"{path}: top-level value must be an object")
    if "researcher" in resolved and "roles" not in resolved:
        resolved["roles"] = {
            "researcher": resolved.pop("researcher"), "executor": None,
        }
    if "scientist_steps" not in resolved:
        resolved["scientist_steps"] = resolved.get(
            "cognitive_steps", resolved.get("gen_steps", 200)
        )
    resolved.setdefault("sandbox_userns", True)
    resolved.setdefault("rsi", {"enabled": False})
    return resolved


def _read_document(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            raw = json.loads(text)
        elif path.suffix.lower() in (".yaml", ".yml"):
            raw = yaml.safe_load(text)
        else:
            raise ConfigError(
                f"config: unsupported extension {path.suffix!r}; "
                "use .json/.yaml/.yml"
            )
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"could not read config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config: top-level value must be an object")
    return raw


def _resolve_v1(raw: dict, path: Path, *, require_ready: bool) -> dict:
    _reject_unknown(raw, _TOP_KEYS, "config")
    goal = raw.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ConfigError("goal: required non-empty string")

    loop = _block(raw, "loop")
    source = _block(raw, "source")
    world = _block(raw, "world")
    evaluation = _block(raw, "evaluation")
    providers = _block(raw, "providers")
    proposer = _optional_block(raw, "proposer")
    executor = _optional_block(raw, "executor")
    rsi = _optional_block(raw, "rsi")

    _reject_unknown(loop, {
        "max_rounds", "candidates_per_round", "max_parallel_candidates",
        "agent_timeout_seconds", "agent_max_output_tokens", "context",
    }, "loop")
    _reject_unknown(source, {"repo", "baseline"}, "source")
    _reject_unknown(world, {
        "image", "definition", "writable", "external_writable",
        "external_readonly",
    }, "world")
    _reject_unknown(evaluation, {
        "commands", "objective", "gates", "timeout_seconds",
        "output_cap_chars", "history_cap_chars",
    }, "evaluation")
    _reject_unknown(providers, {"sandbox", "scheduler"}, "providers")
    _reject_unknown(proposer, {
        "api", "model", "base_url", "max_steps",
        "command_timeout_seconds", "command_output_cap_chars",
    }, "proposer")
    _reject_unknown(executor, {"api", "model", "base_url"}, "executor")
    _reject_unknown(rsi, {"enabled", "first_review_round"}, "rsi")

    sandbox = _block(providers, "sandbox")
    scheduler = _block(providers, "scheduler")
    _reject_unknown(sandbox, {"kind", "userns"}, "providers.sandbox")
    if sandbox.get("kind") != "apptainer":
        raise ConfigError("providers.sandbox.kind: only 'apptainer' is supported")
    userns = sandbox.get("userns", True)
    if not isinstance(userns, bool):
        raise ConfigError("providers.sandbox.userns: must be a boolean")

    scheduler_kind = scheduler.get("kind", "local")
    scheduler_values = {
        key: value for key, value in scheduler.items() if key != "kind"
    }
    if scheduler_kind == "local" and scheduler_values:
        raise ConfigError("providers.scheduler: local accepts only 'kind'")
    if scheduler_kind not in ("local", "hepjob"):
        raise ConfigError("providers.scheduler.kind: expected local or hepjob")

    objective = _block(evaluation, "objective")
    _reject_unknown(objective, {"key", "direction"}, "evaluation.objective")
    direction = objective.get("direction")
    if direction not in ("minimize", "maximize"):
        raise ConfigError(
            "evaluation.objective.direction: expected minimize or maximize"
        )

    manifest = {
        "goal": goal,
        "hints": raw.get("hints", []),
        "loop": {
            "max_rounds": loop.get("max_rounds"),
            "candidates_per_round": loop.get("candidates_per_round", 1),
            "max_parallel_candidates": loop.get("max_parallel_candidates", 1),
            "proposer_max_steps": proposer.get("max_steps", 200),
            **{
                key: loop[key] for key in (
                    "agent_timeout_seconds", "agent_max_output_tokens", "context",
                ) if key in loop
            },
        },
        "world": {
            "image": world.get("image"),
            "writable": world.get("writable"),
            "external_writable": world.get("external_writable", []),
            "external_readonly": world.get("external_readonly", []),
            **({"definition": world["definition"]} if "definition" in world else {}),
        },
        "evaluation": {
            "commands": evaluation.get("commands"),
            "objective": {
                "key": objective.get("key"),
                "lower_is_better": direction == "minimize",
            },
            "gates": evaluation.get("gates", []),
            **{
                key: evaluation[key] for key in (
                    "timeout_seconds", "output_cap_chars", "history_cap_chars",
                ) if key in evaluation
            },
        },
        "source": {
            "repo": source.get("repo"),
            "baseline": source.get("baseline", "HEAD"),
        },
        "scheduler": {
            "kind": scheduler_kind,
            "options": scheduler_values,
        },
        "proposer": (
            {key: value for key, value in proposer.items() if key != "max_steps"}
            if any(key != "max_steps" for key in proposer) else None
        ),
        "executor": executor or None,
    }
    resolved = _config_runtime.resolve_manifest(
        manifest, path, require_ready=require_ready,
    )
    enabled = rsi.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("rsi.enabled: must be a boolean")
    first = rsi.get("first_review_round")
    if enabled and (
        not isinstance(first, int) or isinstance(first, bool) or first < 0
    ):
        raise ConfigError(
            "rsi.first_review_round: required non-negative integer when enabled"
        )
    if not enabled and first is not None:
        raise ConfigError("rsi.first_review_round requires rsi.enabled: true")
    resolved["sandbox_userns"] = userns
    resolved["rsi"] = {
        "enabled": enabled,
        **({"first_self_review_round": first} if enabled else {}),
    }
    return resolved


def _block(raw: dict, key: str) -> dict:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: required and must be an object")
    return value


def _optional_block(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: must be an object")
    return value


def _reject_unknown(raw: dict, allowed: set[str], field: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"{field}: unknown key(s): {sorted(unknown)}")
