"""Read and validate a task config (YAML or JSON).

Minimal schema:
  kind: task
  task.goal: str                      (required)
  safety.editable_paths: [glob]      (required)
  safety.frozen_paths: [glob]        (optional, default [])
  loop.max_rounds: int                (required)
  loop.agent_timeout_seconds: int    (optional, default 3600; per claude call budget)
  loop.agent_max_output_tokens: int  (optional, default 64000; per claude call output ceiling)
  loop.candidates_per_round: int     (optional, default 1; self-loop candidate fanout)
  loop.max_workers: int              (optional, default 1; candidate concurrency)
  loop.proposer_recent_rounds: int   (optional, default 6; rounds of history fed to proposer)
  runtime.image: path                (required; readable SIF image)
  runtime.definition: path           (optional; defaults beside image with .def suffix)
  runtime.binds: [absolute dir]      (optional, default [])
  eval.commands: [str]                (required non-empty; harness-run after each commit)
  eval.metrics: {objective, gates}    (required; the key=value lines the harness parses)
  eval.timeout_seconds: int           (optional, default 600; per eval command budget)
  eval.output_cap_chars: int          (optional, default 16000; retained output per command)
  eval.history_cap_chars: int         (optional, default 6000; eval text kept per round in history.jsonl)
  execution.backend: local|hepjob     (optional, default local; candidate execution backend)
  execution.hepjob.schedd_name: str   (required for hepjob; condor schedd, e.g. scheduler@host)
  execution.hepjob.accounting_group: str   (required for hepjob; e.g. JUNO.juno.default)
  execution.hepjob.accounting_group_user: str  (optional, default current user)
  execution.hepjob.ihep_group: str    (optional; +IHEP_RealGroup job attribute)
  execution.hepjob.request_os: str    (optional, default AlmaLinux9)
  execution.hepjob.cpu_model: str    (optional; target a CPU model — zen4/genoa or zen5/turin — emitted as a condor Requirements expression)
  execution.hepjob.memory_mb: int     (optional, default 6000)
  execution.hepjob.cpus: int          (optional, default 1)
  execution.hepjob.poll_seconds: int  (optional, default 30)
  execution.hepjob.max_attempts: int  (optional, default 2; job-level retries for Held/Lost)
  execution.hepjob.idle_warn_seconds: int   (optional, default 7200; warn only, never kills)
  execution.hepjob.run_timeout_seconds: int (optional, default 21600; running job is removed)
  execution.hepjob.disappearance_grace_seconds: int (optional, default 120)
  execution.hepjob.python_executable: str  (optional, default the frontend's sys.executable)
  execution.hepjob.submit_cmd/query_cmd/remove_cmd: str  (optional condor_* overrides)
  self_improvement.interval_rounds: int     (optional; block presence enables)
  source.path: path                   (required; the repo to optimize)
  source.baseline_ref: str            (optional, default HEAD)

Paths are relative to the config file; unknown keys are errors (strict).
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

TASK_TOP_KEYS = {
    "kind", "task", "safety", "loop", "runtime", "eval", "source", "execution",
    "self_improvement",
}


class ConfigError(ValueError):
    """User-facing config error with a field path."""


# Provenance snapshot written into every run_dir at loop start: the RESOLVED
# config dict (absolute paths, defaults filled in). `simpleloop plot` and
# `simpleloop export` read it back so a run stays self-describing after the
# original config file moves or changes.
RESOLVED_SNAPSHOT_NAME = "config.resolved.json"


def load_resolved(run_dir: str | Path) -> dict[str, Any]:
    """Read the resolved-config snapshot a run wrote into its run_dir."""
    path = Path(run_dir).expanduser().resolve() / RESOLVED_SNAPSHOT_NAME
    if not path.exists():
        raise ConfigError(
            f"no {RESOLVED_SNAPSHOT_NAME} in {path.parent} — the run predates "
            "config snapshots; pass --config explicitly")
    try:
        resolved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    if not isinstance(resolved, dict):
        raise ConfigError(f"{path}: top-level value must be an object")
    return resolved


def load(
    config_path: str | Path,
    *,
    require_ready: bool = True,
) -> dict[str, Any]:
    """Load and validate a task config.

    ``require_ready=False`` still validates the complete schema, but permits
    ``simpleloop init`` to create missing Git metadata and the runtime image.
    """
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
    return _resolve(raw, path, require_ready=require_ready)


def _resolve(
    raw: dict,
    path: Path,
    *,
    require_ready: bool,
) -> dict:
    unknown = set(raw) - TASK_TOP_KEYS
    if unknown:
        raise ConfigError(f"config: unknown top-level key(s): {sorted(unknown)}")
    if raw.get("kind") != "task":
        raise ConfigError("config: kind must be 'task'")

    task = _need(raw, "task", dict)
    safety = _need(raw, "safety", dict)
    loop = _need(raw, "loop", dict)
    source = _need(raw, "source", dict)
    runtime_image, runtime_definition, runtime_binds = _resolve_runtime(
        raw.get("runtime"),
        path,
        require_ready=require_ready,
    )
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
    agent_max_output_tokens = loop.get("agent_max_output_tokens", 64000)
    if not isinstance(agent_max_output_tokens, int) or agent_max_output_tokens < 8000:
        raise ConfigError("loop.agent_max_output_tokens: must be an integer >= 8000")
    candidates_per_round = loop.get("candidates_per_round", 1)
    if not isinstance(candidates_per_round, int) or candidates_per_round < 1:
        raise ConfigError("loop.candidates_per_round: must be a positive integer")
    max_workers = loop.get("max_workers", 1)
    if not isinstance(max_workers, int) or max_workers < 1:
        raise ConfigError("loop.max_workers: must be a positive integer")
    proposer_recent_rounds = loop.get("proposer_recent_rounds", 6)
    if not isinstance(proposer_recent_rounds, int) or proposer_recent_rounds < 1:
        raise ConfigError("loop.proposer_recent_rounds: must be a positive integer")

    src_path = source.get("path")
    if not src_path:
        raise ConfigError("source.path: required")
    repo = Path(_rel(src_path, path)).resolve()
    if not repo.is_dir():
        raise ConfigError(
            f"source.path: does not exist or is not a directory: {repo}"
        )
    if require_ready and not (repo / ".git").exists():
        raise ConfigError(f"source.path: not a git repo: {repo}")
    baseline_ref = str(source.get("baseline_ref") or "HEAD")

    eval_block = _need(raw, "eval", dict)
    eval_commands = eval_block.get("commands")
    if (not isinstance(eval_commands, list) or not eval_commands
            or not all(isinstance(c, str) and c.strip() for c in eval_commands)):
        raise ConfigError("eval.commands: required non-empty list of strings")
    eval_commands = [str(c) for c in eval_commands]

    eval_timeout = eval_block.get("timeout_seconds", 600)
    if not isinstance(eval_timeout, int) or eval_timeout < 1:
        raise ConfigError("eval.timeout_seconds: must be a positive integer (seconds)")
    eval_output_cap = eval_block.get("output_cap_chars", 16000)
    if not isinstance(eval_output_cap, int) or eval_output_cap < 1000:
        raise ConfigError("eval.output_cap_chars: must be an integer >= 1000")
    eval_history_cap = eval_block.get("history_cap_chars", 6000)
    if not isinstance(eval_history_cap, int) or eval_history_cap < 500:
        raise ConfigError("eval.history_cap_chars: must be an integer >= 500")

    # eval.metrics declares the key=value lines the harness parses (objective +
    # gates); required so best selection always has an objective measurement.
    if "metrics" not in eval_block:
        raise ConfigError(
            "eval.metrics: required — declare the objective (and gates) the "
            "harness parses out of eval output; score-based best selection "
            "without metrics is no longer supported")
    metrics = _resolve_metrics(eval_block["metrics"])

    execution_backend, hepjob = _resolve_execution(raw.get("execution"))
    self_improvement = (
        _resolve_self_improvement(raw["self_improvement"])
        if "self_improvement" in raw
        else None
    )

    return {
        "goal": str(goal),
        "editable_paths": [str(g) for g in editable],
        "frozen_paths": [str(g) for g in frozen],
        "max_rounds": int(max_rounds),
        "agent_timeout_seconds": int(agent_timeout),
        "agent_max_output_tokens": int(agent_max_output_tokens),
        "candidates_per_round": int(candidates_per_round),
        "max_workers": int(max_workers),
        "proposer_recent_rounds": int(proposer_recent_rounds),
        "runtime_image": runtime_image,
        "runtime_definition": runtime_definition,
        "runtime_binds": runtime_binds,
        "eval_commands": eval_commands,
        "eval_timeout_seconds": int(eval_timeout),
        "eval_output_cap_chars": int(eval_output_cap),
        "eval_history_cap_chars": int(eval_history_cap),
        "metrics": metrics,
        "execution_backend": execution_backend,
        "hepjob": hepjob,
        "repo_path": str(repo),
        "baseline_ref": baseline_ref,
        "config_dir": str(path.parent),
        "self_improvement": self_improvement,
    }


def _resolve_self_improvement(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("self_improvement: must be an object")
    unknown = set(raw) - {"interval_rounds"}
    if unknown:
        raise ConfigError(
            f"self_improvement: unknown key(s): {sorted(unknown)}"
        )
    interval = raw.get("interval_rounds", 10)
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
        raise ConfigError(
            "self_improvement.interval_rounds: must be a positive integer"
        )
    return {"interval_rounds": interval}


def _resolve_runtime(
    raw: object,
    config_path: Path,
    *,
    require_ready: bool,
) -> tuple[str, str, list[str]]:
    """Validate and resolve the mandatory Apptainer runtime block."""
    if not isinstance(raw, dict):
        raise ConfigError("runtime: required and must be an object")
    unknown = set(raw) - {"image", "definition", "binds"}
    if unknown:
        raise ConfigError(f"runtime: unknown key(s): {sorted(unknown)}")

    image_value = raw.get("image")
    if not isinstance(image_value, str) or not image_value.strip():
        raise ConfigError("runtime.image: required non-empty path")
    image = Path(_rel(image_value, config_path)).expanduser().resolve()
    definition_value = raw.get("definition")
    if definition_value is None:
        definition = image.with_suffix(".def")
    elif not isinstance(definition_value, str) or not definition_value.strip():
        raise ConfigError("runtime.definition: must be a non-empty path")
    else:
        definition = Path(
            _rel(definition_value, config_path)
        ).expanduser().resolve()
    if require_ready:
        if not image.is_file():
            raise ConfigError(
                f"runtime.image: does not exist or is not a file: {image}"
            )
        if not os.access(image, os.R_OK):
            raise ConfigError(f"runtime.image: not readable: {image}")

    raw_binds = raw.get("binds", [])
    if not isinstance(raw_binds, list):
        raise ConfigError(
            "runtime.binds: must be a list of absolute directories"
        )
    binds: list[str] = []
    for index, value in enumerate(raw_binds):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"runtime.binds[{index}]: must be a non-empty path"
            )
        bind = Path(value).expanduser()
        if not bind.is_absolute():
            raise ConfigError(
                f"runtime.binds[{index}]: must be absolute: {value}"
            )
        bind = bind.resolve()
        if not bind.is_dir():
            raise ConfigError(
                f"runtime.binds[{index}]: not an existing directory: {bind}"
            )
        if ":" in str(bind) or "," in str(bind):
            raise ConfigError(
                f"runtime.binds[{index}]: contains an unsupported bind "
                f"separator (':' or ','): {bind}"
            )
        binds.append(str(bind))
    return str(image), str(definition), binds


def _resolve_metrics(raw: object) -> dict:
    """Validate the eval.metrics block. Returns a normalized dict:
    {objective: {key, lower_is_better}, gates: [{key, description?}]}."""
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
    obj_key = obj_key.strip()
    reserved = {"PATHS", "EVAL_COMMANDS"}
    if obj_key in reserved:
        raise ConfigError(
            f"eval.metrics.objective.key: {obj_key} is reserved by the harness"
        )
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
    seen = {obj_key}
    for i, g in enumerate(raw_gates):
        if not isinstance(g, dict):
            raise ConfigError(f"eval.metrics.gates[{i}]: must be an object with a 'key' field")
        g_unknown = set(g) - {"key", "description"}
        if g_unknown:
            raise ConfigError(f"eval.metrics.gates[{i}]: unknown key(s): {sorted(g_unknown)}")
        gk = g.get("key")
        if not isinstance(gk, str) or not gk.strip():
            raise ConfigError(f"eval.metrics.gates[{i}].key: required non-empty string")
        gk = gk.strip()
        if gk in reserved:
            raise ConfigError(
                f"eval.metrics.gates[{i}].key: {gk} is reserved by the harness"
            )
        if gk in seen:
            raise ConfigError(
                "eval.metrics objective and gate keys must be unique: " + gk
            )
        seen.add(gk)
        description = g.get("description")
        if description is not None and not isinstance(description, str):
            raise ConfigError(f"eval.metrics.gates[{i}].description: must be a string")
        gate: dict = {"key": gk}
        if description and description.strip():
            gate["description"] = description.strip()
        gates.append(gate)

    return {"objective": {"key": obj_key, "lower_is_better": lower_is_better},
            "gates": gates}


_HEPJOB_DEFAULTS = {
    "accounting_group_user": None,   # filled with the current OS user
    "ihep_group": None,
    "request_os": "AlmaLinux9",
    "cpu_model": None,
    "memory_mb": 6000,
    "cpus": 1,
    "poll_seconds": 30,
    "max_attempts": 2,
    "idle_warn_seconds": 7200,
    "run_timeout_seconds": 21600,
    "disappearance_grace_seconds": 120,
    "python_executable": None,       # filled with sys.executable
    "submit_cmd": "condor_submit",
    "query_cmd": "condor_q",
    "remove_cmd": "condor_rm",
}

_HEPJOB_INT_RANGES = {
    "memory_mb": (1, None),
    "cpus": (1, None),
    "poll_seconds": (5, None),
    "max_attempts": (1, None),
    "idle_warn_seconds": (60, None),
    "run_timeout_seconds": (300, None),
    "disappearance_grace_seconds": (0, None),
}

# CPU model -> condor Requirements expression targeting the IHEP pool's
# machine ads (CpuFamily/CpuModelNumber). The pool advertises no CPU brand
# string, so targeting by model name requires this explicit map. Verified
# against scheduler@pvm069.ihep.ac.cn on 2026-07-30:
#   family 25 / model 17 -> AMD Zen 4 (Genoa), 125 slots (majority)
#   family 26 / model  2 -> AMD Zen 5 (Turin), 6 slots (asic001, lhws318)
_CPU_MODEL_REQUIREMENTS = {
    "zen4": "CpuFamily==25 && CpuModelNumber==17",
    "genoa": "CpuFamily==25 && CpuModelNumber==17",
    "zen5": "CpuFamily==26 && CpuModelNumber==2",
    "turin": "CpuFamily==26 && CpuModelNumber==2",
}


def _resolve_execution(raw: object) -> tuple[str, dict]:
    """Validate the optional execution block. Returns (backend, hepjob_cfg);
    hepjob_cfg carries defaults even for the local backend so a resolved
    snapshot stays self-describing."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("execution: must be an object")
    unknown = set(raw) - {"backend", "hepjob"}
    if unknown:
        raise ConfigError(f"execution: unknown key(s): {sorted(unknown)}")
    backend = raw.get("backend", "local")
    if backend not in ("local", "hepjob"):
        raise ConfigError(
            f"execution.backend: expected 'local' or 'hepjob', got {backend!r}")

    hepjob_raw = raw.get("hepjob", {})
    if not isinstance(hepjob_raw, dict):
        raise ConfigError("execution.hepjob: must be an object")
    allowed = {"schedd_name", "accounting_group"} | set(_HEPJOB_DEFAULTS)
    unknown = set(hepjob_raw) - allowed
    if unknown:
        raise ConfigError(
            f"execution.hepjob: unknown key(s): {sorted(unknown)}")

    hepjob = dict(_HEPJOB_DEFAULTS)
    hepjob["accounting_group_user"] = getpass.getuser()
    hepjob["python_executable"] = sys.executable
    for key, value in hepjob_raw.items():
        hepjob[key] = value

    for key in ("schedd_name", "accounting_group"):
        value = hepjob.get(key)
        if backend == "hepjob" and (not isinstance(value, str)
                                    or not value.strip()):
            raise ConfigError(
                f"execution.hepjob.{key}: required when backend is hepjob")
    for key in ("schedd_name", "accounting_group", "accounting_group_user",
                "ihep_group", "request_os", "cpu_model", "python_executable",
                "submit_cmd", "query_cmd", "remove_cmd"):
        value = hepjob.get(key)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"execution.hepjob.{key}: must be a string")
    if hepjob.get("cpu_model") is not None:
        model = hepjob["cpu_model"].strip().lower()
        if model not in _CPU_MODEL_REQUIREMENTS:
            raise ConfigError(
                f"execution.hepjob.cpu_model: unknown model "
                f"{hepjob['cpu_model']!r}; choose from "
                f"{sorted(_CPU_MODEL_REQUIREMENTS)}")
        hepjob["cpu_model"] = model
    for key, (low, _high) in _HEPJOB_INT_RANGES.items():
        value = hepjob[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < low:
            raise ConfigError(
                f"execution.hepjob.{key}: must be an integer >= {low}")
    return backend, hepjob


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
