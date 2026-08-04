"""Append-only factual experiment history and objective selection."""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import memory as memory_mod

def eligible(candidate: dict, metrics_schema: dict) -> bool:
    """Return whether a candidate may enter objective selection."""
    if not candidate.get("sha"):
        return False
    metrics = candidate.get("metrics") or {}
    objective = metrics.get(metrics_schema["objective"]["key"])
    numeric = (
        isinstance(objective, (int, float))
        and not isinstance(objective, bool)
        and math.isfinite(objective)
    )
    return (
        candidate.get("eligible") is True
        and candidate.get("gate_passed") is True
        and numeric
    )


def best_candidate(rounds: list[dict], metrics_schema: dict) -> dict | None:
    """The eligible candidate with the best objective over a full history.

    Returns the first candidate at the best objective (with its round attached)
    or None.
    """
    obj = metrics_schema["objective"]
    obj_key = obj["key"]
    lower = obj["lower_is_better"]
    best: dict | None = None
    for r in _iter_candidates(rounds):
        if not eligible(r, metrics_schema):
            continue
        if best is None:
            best = r
            continue
        ov, bv = r["metrics"][obj_key], best["metrics"][obj_key]
        better = (ov < bv) if lower else (ov > bv)
        if better:
            best = r
    return best


class Store:
    def __init__(self, run_dir: Path, metrics_schema: dict,
                 history_eval_cap: int = 6000):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "history.jsonl"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.metrics_schema = metrics_schema
        # Cap evaluator output stored per candidate so history stays bounded.
        self.history_eval_cap = history_eval_cap

    def history(self) -> list[dict]:
        """Read all rounds back (for the proposer's prompt)."""
        return memory_mod.read_history(self.path)

    def append_generation(self, round_id: int, *, parent_sha: str,
                          selected_candidate: int | None,
                          selected_sha: str | None,
                          candidates: list[dict],
                          abstention: dict | None = None,
                          deliberation_telemetry: dict | None = None,
                          telemetry: dict | None = None) -> None:
        """Record a self-loop generation with multiple candidate attempts.

        Every candidate row carries a stable ``experiment_id`` (``r<N>c<M>``)
        and, when the proposal declared a research target, its ``finding_id``.
        ``abstention`` (``reason`` + optional ``blocking_unknown``) marks a
        zero-candidate round the Proposer deliberately abstained from.
        ``deliberation_telemetry`` records behavioral facts only (steps, action
        counts, verification outcome); the non-authoritative full trajectory
        lives in proposer_traces/, never here.
        """
        normalized = []
        for i, c in enumerate(candidates):
            candidate_id = c.get("candidate", i)
            experiment_id = c.get("experiment_id") or f"r{round_id}c{candidate_id}"
            normalized.append({
                "candidate": candidate_id,
                "experiment_id": experiment_id,
                "finding_id": c.get("finding_id"),
                "proposal": c.get("proposal") or "",
                "parent_sha": c.get("parent_sha") or parent_sha,
                "sha": c.get("sha"),
                "status": c.get("status"),
                "eval_block": (c.get("eval_block") or "")[:self.history_eval_cap],
                "metrics": c.get("metrics") or {},
                "changed_paths": c.get("changed_paths") or [],
                "gates": c.get("gates") or {},
                "gate_passed": c.get("gate_passed"),
                "eligible": eligible(c, self.metrics_schema),
                "selected": candidate_id == selected_candidate,
                "telemetry": dict(c.get("telemetry") or {}),
            })
        selected = next((c for c in normalized if c["selected"]), None)
        record = {
            "round": round_id,
            "parent_sha": parent_sha,
            "selected_candidate": selected_candidate,
            "selected_sha": selected_sha,
            "proposal": selected.get("proposal", "") if selected else "",
            "metrics": selected.get("metrics", {}) if selected else {},
            "changed_paths": selected.get("changed_paths", []) if selected else [],
            "base_sha": selected_sha or parent_sha,
            "candidates": normalized,
            "telemetry": dict(telemetry or {}),
        }
        if abstention is not None:
            record["abstention"] = {
                "reason": str(abstention.get("reason") or ""),
                "blocking_unknown": abstention.get("blocking_unknown"),
            }
        if deliberation_telemetry:
            record["deliberation_telemetry"] = dict(deliberation_telemetry)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _iter_candidates(rounds: list[dict]):
    for r in rounds:
        for c in r.get("candidates") or []:
            row = dict(c)
            row["round"] = r.get("round")
            yield row
