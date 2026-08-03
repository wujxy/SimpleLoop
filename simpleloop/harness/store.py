"""Append-only factual experiment history and objective selection."""
from __future__ import annotations

import json
import math
import os
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
                          telemetry: dict | None = None) -> None:
        """Record a self-loop generation with multiple candidate attempts."""
        normalized = []
        for i, c in enumerate(candidates):
            normalized.append({
                "candidate": c.get("candidate", i),
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
                "selected": c.get("candidate", i) == selected_candidate,
                "note": c.get("note") or "",
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

    def backfill_notes(self, round_id: int, annotations: list[dict]) -> None:
        """Attach the next round's proposer-written notes to a prior round's
        candidates. Only the target round's ``note`` fields are set (once each);
        the file is then re-serialized and swapped atomically so a crash never
        leaves a half-written history. ``annotations`` is a list of
        ``{ref, text}``; refs encode ``r{round}c{candidate}``."""
        if not annotations:
            return
        rows = self.history()
        if not rows:
            raise ValueError("cannot backfill notes into empty history")
        target = next(
            (r for r in rows if r.get("round") == round_id), None,
        )
        if target is None:
            raise ValueError(
                f"cannot backfill notes: round {round_id} not in history"
            )
        by_ref: dict[str, str] = {a["ref"]: a["text"] for a in annotations}
        changed = False
        for candidate in target.get("candidates") or []:
            ref = f"r{round_id}c{candidate.get('candidate', 0)}"
            if ref in by_ref:
                candidate["note"] = by_ref[ref]
                changed = True
        if not changed:
            return
        # Re-serialize every row (only the target round's notes changed) and
        # swap atomically so a crash never leaves a half-written history.
        out_lines = [json.dumps(r, ensure_ascii=False) for r in rows]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def _iter_candidates(rounds: list[dict]):
    for r in rounds:
        for c in r.get("candidates") or []:
            row = dict(c)
            row["round"] = r.get("round")
            yield row
