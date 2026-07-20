"""Read and validate a task config (YAML or JSON).

Minimal schema:
  kind: task
  task.goal: str                      (required)
  safety.editable_paths: [glob]      (required)
  safety.frozen_paths: [glob]        (optional, default [])
  loop.max_rounds: int                (required)
  loop.agent_timeout_seconds: int    (optional, default 3600; per claude call budget)
  loop.candidates_per_round: int     (optional, default 1; self-loop candidate fanout)
  loop.max_workers: int              (optional, default 1; candidate concurrency)
  eval.commands: [str]                (optional; omit -> judger is diff-only)
  eval.metrics: {objective, gates}    (optional; omit -> judger reads prose, best by score)
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

    agent_timeout = loop.get("agent_timeout_seconds", 3600)
    if not isinstance(agent_timeout, int) or agent_timeout < 60:
        raise ConfigError("loop.agent_timeout_seconds: must be an integer >= 60 (seconds)")
    candidates_per_round = loop.get("candidates_per_round", 1)
    if not isinstance(candidates_per_round, int) or candidates_per_round < 1:
        raise ConfigError("loop.candidates_per_round: must be a positive integer")
    max_workers = loop.get("max_workers", 1)
    if not isinstance(max_workers, int) or max_workers < 1:
        raise ConfigError("loop.max_workers: must be a positive integer")

    src_path = source.get("path")
    if not src_path:
        raise ConfigError("source.path: required")
    repo = Path(_rel(src_path, path)).resolve()
    if not (repo / ".git").exists():
        raise ConfigError(f"source.path: not a git repo: {repo}")
    baseline_ref = str(source.get("baseline_ref") or "HEAD")

    eval_commands: list[str] = []
    metrics: dict | None = None
    if "eval" in raw:
        eval_block = raw["eval"]
        if not isinstance(eval_block, dict):
            raise ConfigError("eval: must be an object")
        eval_commands = eval_block.get("commands", [])
        if not isinstance(eval_commands, list):
            raise ConfigError("eval.commands: must be a list of strings")
        eval_commands = [str(c) for c in eval_commands]

        # metrics: declares the structured key=value lines the harness parses out
        # of eval output (NOT the judger — the judger only interprets). Two roles
        # only, both project-agnostic: `objective` (the thing being optimized +
        # its direction) and `gates` (pass/fail keys that veto a round). The harness
        # owns these numbers and selects `best` by the objective among gate-pass,
        # risk-not-high rounds. Without this block the judger falls back to reading
        # prose and is known to hallucinate numbers (see memory
        # simpleloop-judger-prior-round-compare). No noise_floor / reps — deferred
        # until a run proves they're needed.
        if "metrics" in eval_block:
            metrics = _resolve_metrics(eval_block["metrics"])

    return {
        "goal": str(goal),
        "editable_paths": [str(g) for g in editable],
        "frozen_paths": [str(g) for g in frozen],
        "max_rounds": int(max_rounds),
        "agent_timeout_seconds": int(agent_timeout),
        "candidates_per_round": int(candidates_per_round),
        "max_workers": int(max_workers),
        "eval_commands": eval_commands,
        "metrics": metrics,
        "repo_path": str(repo),
        "baseline_ref": baseline_ref,
        "config_dir": str(path.parent),
    }


def _resolve_metrics(raw: object) -> dict:
    """Validate the eval.metrics block. Returns a normalized dict.

    Schema (two roles only — anything else is an error, matching the strict
    unknown-top-level-key policy so a misspelled knob fails at load):
      metrics:
        objective:
          key: SPEED_MS          # the key=value line to parse
          lower_is_better: true  # required bool — direction is not guessable
        gates:                   # optional; list of {key: str}
          - key: CORRECTNESS

    The harness never hardcodes SPEED_MS / CORRECTNESS — these are config-declared,
    so the next user's objective can be binary_size or coverage_p99.
    """
    if not isinstance(raw, dict):
        raise ConfigError("eval.metrics: must be an object")
    unknown = set(raw) - {"objective", "gates"}
    if unknown:
        raise ConfigError(f"eval.metrics: unknown key(s): {sorted(unknown)} "
                          "(only 'objective' and 'gates' are declared; noise_floor/"
                          "reps are deferred until a run proves they're needed)")

    obj = raw.get("objective")
    if not isinstance(obj, dict):
        raise ConfigError("eval.metrics.objective: required object (the metric being optimized)")
    obj_key = obj.get("key")
    if not isinstance(obj_key, str) or not obj_key.strip():
        raise ConfigError("eval.metrics.objective.key: required non-empty string "
                          "(the key=value line the harness parses, e.g. SPEED_MS)")
    obj_unknown = set(obj) - {"key", "lower_is_better"}
    if obj_unknown:
        raise ConfigError(f"eval.metrics.objective: unknown key(s): {sorted(obj_unknown)}")
    lower_is_better = obj.get("lower_is_better")
    if not isinstance(lower_is_better, bool):
        raise ConfigError("eval.metrics.objective.lower_is_better: required bool "
                          "(direction is not guessable — declare it)")

    gates: list[dict] = []
    raw_gates = raw.get("gates", [])
    if not isinstance(raw_gates, list):
        raise ConfigError("eval.metrics.gates: must be a list of {key: str} objects")
    for i, g in enumerate(raw_gates):
        if not isinstance(g, dict):
            raise ConfigError(f"eval.metrics.gates[{i}]: must be an object with a 'key' field")
        g_unknown = set(g) - {"key"}
        if g_unknown:
            raise ConfigError(f"eval.metrics.gates[{i}]: unknown key(s): {sorted(g_unknown)}")
        gk = g.get("key")
        if not isinstance(gk, str) or not gk.strip():
            raise ConfigError(f"eval.metrics.gates[{i}].key: required non-empty string")
        gates.append({"key": gk})

    return {"objective": {"key": obj_key, "lower_is_better": lower_is_better},
            "gates": gates}


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
