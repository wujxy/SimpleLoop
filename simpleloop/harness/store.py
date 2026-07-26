"""History store: append-only JSONL, one generation record per round.

The best commit is selected by the HARNESS from the real objective metric —
never by the judger's subjective score: among candidates where every declared
gate is True, risk != "high" and the objective is numeric, take the best
objective value (min if lower_is_better, else max; judger score breaks ties).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import memory as memory_mod

def eligible(candidate: dict, metrics_schema: dict) -> bool:
    """Shared winner/best rule: committed, risk not high, all declared gates
    True, and a numeric objective value."""
    if not candidate.get("sha"):
        return False
    if str(candidate.get("risk", "high")).lower() == "high":
        return False
    metrics = candidate.get("metrics") or {}
    if not all(metrics.get(g["key"]) is True
               for g in metrics_schema.get("gates", [])):
        return False
    return isinstance(metrics.get(metrics_schema["objective"]["key"]),
                      (int, float))


def best_candidate(rounds: list[dict], metrics_schema: dict) -> dict | None:
    """The eligible candidate with the best objective over a full history.

    Returns the candidate row (with its "round" attached) or None. Judger
    score breaks objective ties; used by both Store best tracking and
    `simpleloop export`.
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
        if ov == bv:
            better = (r.get("score") or -1.0) > (best.get("score") or -1.0)
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
        # Cap the eval_block stored per round so history.jsonl doesn't balloon;
        # the live eval_block fed to the judger is capped separately in run_eval.
        self.history_eval_cap = history_eval_cap
        self.best_score: float = -1.0           # judger quality number (report only)
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
                          reflection: str = "",
                          telemetry: dict | None = None) -> None:
        """Record a self-loop generation with multiple candidate attempts."""
        normalized = []
        for i, c in enumerate(candidates):
            normalized.append({
                "candidate": c.get("candidate", i),
                "family": c.get("family") or "single",
                "proposal": c.get("proposal") or "",
                "sha": c.get("sha"),
                "score": c.get("score"),
                "risk": c.get("risk", "high"),
                "decision": c.get("decision", ""),
                "feedback": c.get("feedback", ""),
                "feedback_for_proposer": c.get("feedback_for_proposer", ""),
                "eval_block": (c.get("eval_block") or "")[:self.history_eval_cap],
                "metrics": c.get("metrics") or {},
                "changed_paths": c.get("changed_paths") or [],
                "accepted": bool(c.get("accepted")),
                "selected": c.get("candidate", i) == selected_candidate,
                "telemetry": dict(c.get("telemetry") or {}),
            })
        selected = next((c for c in normalized if c["selected"]), None)
        record = {
            "round": round_id,
            "parent_sha": parent_sha,
            "selected_candidate": selected_candidate,
            "selected_sha": selected_sha,
            "sha": selected_sha,
            "proposal": selected.get("proposal", "") if selected else "",
            "score": selected.get("score") if selected else 0.0,
            "risk": selected.get("risk", "high") if selected else "high",
            "feedback": selected.get("feedback", "") if selected else "[no selected candidate]",
            "feedback_for_proposer": (
                selected.get("feedback_for_proposer", "")
                if selected else "[no selected candidate]"
            ),
            "metrics": selected.get("metrics", {}) if selected else {},
            "changed_paths": selected.get("changed_paths", []) if selected else [],
            "accepted": bool(selected_sha),
            "base_sha": selected_sha or parent_sha,
            "reflection": reflection,
            "decision": selected.get("decision", "") if selected else "",
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
        # keep best_score as the judger score exposed by the run summary
        if isinstance(best.get("score"), (int, float)):
            self.best_score = best["score"]



def _iter_candidates(rounds: list[dict]):
    for r in rounds:
        for c in r.get("candidates") or []:
            row = dict(c)
            row["round"] = r.get("round")
            yield row
