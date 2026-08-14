"""Build the stable, user-facing run summary from explicit inputs."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from ..harness.store import best_candidate


def write_summary(
    *, run_dir: str | Path, store, workspace,
    metrics_schema: Mapping[str, object],
    baseline_metrics: Mapping[str, object],
) -> dict[str, object]:
    run_dir = Path(run_dir)
    history = store.history()
    objective = metrics_schema.get("objective") or {}
    objective_key = objective.get("key")
    best = (
        best_candidate(history, dict(metrics_schema))
        if history and objective_key else None
    )
    baseline_sha = workspace.baseline_sha()
    summary = {
        "best_sha": best.get("sha") if best else None,
        "best_round": best.get("round") if best else None,
        "best_candidate": best.get("candidate") if best else None,
        "objective_key": objective_key,
        "best_objective": (
            (best.get("metrics") or {}).get(objective_key) if best else None
        ),
        "baseline_objective": baseline_metrics.get(objective_key),
        "baseline_sha": baseline_sha,
        "final_chain_sha": (
            history[-1].get("base_sha") if history else baseline_sha
        ),
        "rounds": len(history),
        "run_dir": str(run_dir),
        "repo": str(workspace.repo),
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary
