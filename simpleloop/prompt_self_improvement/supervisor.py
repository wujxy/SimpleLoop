"""Unattended outer loop for development-time prompt evolution."""
from __future__ import annotations

import json
from pathlib import Path

from .. import config as config_mod
from .. import loop as loop_mod
from ..harness import views
from .gate import OptimizerReport, PromptGate
from .history import PromptHistory
from .optimizer import MetaOptimizer


def run(
    config_path: str | Path,
    run_dir: str | Path,
    *,
    continue_run: bool = False,
    artifact_runner=loop_mod.run,
    optimizer_factory=MetaOptimizer,
) -> dict:
    cfg = config_mod.load(config_path)
    settings = cfg["prompt_self_improvement"]
    if not settings.get("enabled"):
        raise ValueError("prompt self-improvement is not enabled")

    run_dir = Path(run_dir).resolve()
    history = PromptHistory(settings["prompt_dir"], settings["history_dir"])
    history.initialize()
    history.recover_inflight()
    gate = PromptGate(settings["max_prompt_chars"])
    optimizer = optimizer_factory(
        command=settings["optimizer_command"],
        timeout_seconds=cfg.get("agent_timeout_seconds", 3600),
        max_output_tokens=cfg.get("agent_max_output_tokens", 64000),
    )
    run_id = str(run_dir)
    interval = settings["interval_rounds"]
    total = cfg["max_rounds"]
    completed = _count_rounds(run_dir)
    summary: dict = {"rounds": completed}

    if completed and completed % interval == 0:
        _trigger(
            history, gate, optimizer, cfg, run_dir, run_id, completed,
        )

    while completed < total:
        target = min(((completed // interval) + 1) * interval, total)
        summary = artifact_runner(
            config_path, run_dir, continue_run=continue_run or completed > 0,
            target_rounds=target,
        )
        new_completed = _count_rounds(run_dir)
        if new_completed <= completed:
            return summary
        completed = new_completed
        if completed % interval == 0:
            _trigger(
                history, gate, optimizer, cfg, run_dir, run_id, completed,
            )
    return summary


def _trigger(
    history: PromptHistory,
    gate: PromptGate,
    optimizer,
    cfg: dict,
    run_dir: Path,
    run_id: str,
    trigger_round: int,
) -> None:
    if not history.mark_inflight(run_id, trigger_round):
        return
    parent = history.state["active_version"]
    report_path = history.prompt_dir / "optimizer_report.yaml"
    report_path.unlink(missing_ok=True)
    settings = cfg["prompt_self_improvement"]
    try:
        optimizer.run(
            run_dir=run_dir, prompt_dir=history.prompt_dir,
            history_dir=history.history_dir,
            source_dir=Path(__file__).resolve().parents[2],
            goal=cfg["goal"], gate_block=views.gate_block(cfg["metrics"]),
        )
        report = OptimizerReport.load(report_path)
        changed = history.has_changes(parent)
        errors = gate.check(history.prompt_dir, report, changed=changed)
        if errors:
            raise ValueError("; ".join(errors))
        if changed:
            version = history.snapshot(trigger_round, report)
            history.finish_trigger("accepted", result_version=version)
        else:
            history.finish_trigger("no_change")
    except Exception as exc:
        history.restore(parent)
        history.finish_trigger("rejected", error=str(exc))


def _count_rounds(run_dir: Path) -> int:
    path = run_dir / "history.jsonl"
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            json.loads(line)
            count += 1
    return count
