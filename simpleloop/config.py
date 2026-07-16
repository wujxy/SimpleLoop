"""Read and validate a task config (YAML or JSON).

Minimal schema:
  kind: task
  task.goal: str                      (required)
  safety.editable_paths: [glob]      (required)
  safety.frozen_paths: [glob]        (optional, default [])
  loop.max_rounds: int                (required)
  eval.commands: [str]                (optional; omit -> judger is diff-only)
  source.path: path                   (required; the repo to optimize)
  source.baseline_ref: str            (optional, default HEAD)

Paths in the config are relative to the config file. Strict: unknown top-level
keys are errors so a misspelled knob fails at validate, not silently mid-run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

TASK_TOP_KEYS = {"kind", "task", "safety", "loop", "eval", "source"}


class ConfigError(ValueError):
    """User-facing config error with a field path."""


def load(config_path: str | Path) -> dict[str, Any]:
    """Load and validate a task config. Returns the resolved config dict."""
    path = Path(config_path).expanduser().resolve()
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        raw = json.loads(text)
    elif path.suffix.lower() in (".yaml", ".yml"):
        raw = yaml.safe_load(text)
    else:
        raise ConfigError(f"config: unsupported extension {path.suffix!r}; use .json/.yaml/.yml")
    if not isinstance(raw, dict):
        raise ConfigError("config: top-level value must be an object")
    return _resolve(raw, path)


def _resolve(raw: dict, path: Path) -> dict:
    unknown = set(raw) - TASK_TOP_KEYS
    if unknown:
        raise ConfigError(f"config: unknown top-level key(s): {sorted(unknown)}")
    if raw.get("kind") != "task":
        raise ConfigError("config: kind must be 'task'")

    task = _need(raw, "task", dict)
    safety = _need(raw, "safety", dict)
    loop = _need(raw, "loop", dict)
    source = _need(raw, "source", dict)
    goal = task.get("goal")
    if not goal:
        raise ConfigError("task.goal: required and must be non-empty")

    editable = safety.get("editable_paths")
    if not isinstance(editable, list) or not editable:
        raise ConfigError("safety.editable_paths: required non-empty list of globs")
    frozen = safety.get("frozen_paths", [])
    if not isinstance(frozen, list):
        raise ConfigError("safety.frozen_paths: must be a list")

    max_rounds = loop.get("max_rounds")
    if not isinstance(max_rounds, int) or max_rounds < 1:
        raise ConfigError("loop.max_rounds: required positive integer")

    src_path = source.get("path")
    if not src_path:
        raise ConfigError("source.path: required")
    repo = Path(_rel(src_path, path)).resolve()
    if not (repo / ".git").exists():
        raise ConfigError(f"source.path: not a git repo: {repo}")
    baseline_ref = str(source.get("baseline_ref") or "HEAD")

    eval_commands: list[str] = []
    if "eval" in raw:
        eval_block = raw["eval"]
        if not isinstance(eval_block, dict):
            raise ConfigError("eval: must be an object")
        eval_commands = eval_block.get("commands", [])
        if not isinstance(eval_commands, list):
            raise ConfigError("eval.commands: must be a list of strings")
        eval_commands = [str(c) for c in eval_commands]

    return {
        "goal": str(goal),
        "editable_paths": [str(g) for g in editable],
        "frozen_paths": [str(g) for g in frozen],
        "max_rounds": int(max_rounds),
        "eval_commands": eval_commands,
        "repo_path": str(repo),
        "baseline_ref": baseline_ref,
        "config_dir": str(path.parent),
    }


def _need(raw: dict, key: str, kind: type) -> dict:
    val = raw.get(key)
    if not isinstance(val, dict):
        raise ConfigError(f"{key}: required and must be an object")
    return val


def _rel(value: str, config_path: Path) -> str:
    p = Path(value)
    if not p.is_absolute():
        p = (config_path.parent / p)
    return str(p)
