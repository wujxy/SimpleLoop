"""Lazy Host adapter for independent proposer and RSI worker kinds."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Callable, Mapping


def _load(payload: Mapping[str, object]):
    run_dir = Path(str(payload.get("run_dir") or ".")).resolve()
    selected = payload.get("self_repo")
    self_repo = (
        Path(str(selected)).resolve()
        if selected else run_dir / "self" / "repo"
    )
    if self_repo.is_dir() and str(self_repo) not in sys.path:
        sys.path.insert(0, str(self_repo))
    return importlib.import_module("simpleloop.proposer_lane_worker")


def _run(
    payload: Mapping[str, object],
    observe_usage: Callable[[Mapping[str, object]], None],
    *,
    mode: str,
) -> Mapping[str, object]:
    legacy = _load(payload)
    from ... import config as config_mod

    raw = dict(payload)
    raw["mode"] = "self" if mode == "self" else "task"
    spec = legacy.ProposerLaneSpec.from_dict(raw)
    try:
        run_dir = Path(spec.run_dir)
        deps = legacy.build_lane_deps(
            config_mod.load_resolved(run_dir),
            run_dir,
            usage_observer=observe_usage,
            prompt_dir=spec.prompt_dir or None,
        )
        deps.runtime.preflight()
        if mode == "self":
            return legacy.run_self_review_lane(deps, spec)
        return legacy.run_lane(deps, spec)
    except Exception as exc:
        if mode == "self":
            return legacy._self_review_failure_result(spec, f"worker failed: {exc}")
        return legacy._failure_result(spec, f"worker failed: {exc}")


def handle_proposer(payload, observe_usage):
    return _run(payload, observe_usage, mode="task")


def handle_self_review(payload, observe_usage):
    return _run(payload, observe_usage, mode="self")


def handle_viability(payload, observe_usage):
    return _run(payload, observe_usage, mode="viability")

