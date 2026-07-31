"""Append-only factual experiment history and objective selection."""
from __future__ import annotations

import json
from pathlib import Path

from . import memory as memory_mod

def eligible(candidate: dict, metrics_schema: dict) -> bool:
    """Return whether a new or legacy candidate may enter selection."""
    if not candidate.get("sha"):
        return False
    metrics = candidate.get("metrics") or {}
    objective = metrics.get(metrics_schema["objective"]["key"])
    numeric = (
        isinstance(objective, (int, float))
        and not isinstance(objective, bool)
    )
    if "eligible" in candidate:
        return (
            candidate.get("eligible") is True
            and candidate.get("gate_passed") is True
            and numeric
        )
    if str(candidate.get("risk", "")).lower() == "high":
        return False
    gates_pass = all(
        metrics.get(item["key"]) is True
        for item in metrics_schema.get("gates", [])
    )
    return gates_pass and numeric


def best_candidate(rounds: list[dict], metrics_schema: dict) -> dict | None:
    """The eligible candidate with the best objective over a full history.

    Returns the first candidate at the best objective (with its round attached)
    or None. Used by both Store best tracking and ``simpleloop export``.
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
        self.best_sha: str | None = None        # harness-selected best commit
        self.best_round: int | None = None
        self.best_candidate: int | None = None

    def history(self) -> list[dict]:
        """Read all rounds back (for the proposer's prompt)."""
        return memory_mod.read_history(self.path)

    def append_generation(self, round_id: int, *, parent_sha: str,
                          selected_candidate: int | None,
                          selected_sha: str | None,
                          candidates: list[dict],
                          telemetry: dict | None = None) -> None:
        """Record a self-loop generation with multiple candidate attempts."""
        normalized = []
        for i, c in enumerate(candidates):
            normalized.append({
                "candidate": c.get("candidate", i),
                "proposal": c.get("proposal") or "",
                "parent_sha": c.get("parent_sha") or parent_sha,
                "sha": c.get("sha"),
                "status": c.get("status") or c.get("candidate_status"),
                "eval_block": (c.get("eval_block") or "")[:self.history_eval_cap],
                "metrics": c.get("metrics") or {},
                "changed_paths": c.get("changed_paths") or [],
                "gates": c.get("gates") or {},
                "gate_passed": c.get("gate_passed", c.get("accepted")),
                "eligible": eligible(c, self.metrics_schema),
                "selected": c.get("candidate", i) == selected_candidate,
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
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._recompute_best()

    def _recompute_best(self) -> None:
        """Recompute the metric-based best over the full history (from scratch
        each append, so a rule change never leaves stale best state)."""
        best = best_candidate(self.history(), self.metrics_schema)
        if best is None:
            return
        self.best_sha = best["sha"]
        self.best_round = best["round"]
        self.best_candidate = best.get("candidate")



def _iter_candidates(rounds: list[dict]):
    for r in rounds:
        for c in r.get("candidates") or []:
            row = dict(c)
            row["round"] = r.get("round")
            yield row
