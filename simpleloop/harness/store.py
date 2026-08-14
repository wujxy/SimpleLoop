"""Append-only factual experiment history and objective selection."""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import memory as memory_mod
from ..persistence.artifacts import encode_candidate_result
from ..round import RoundResult

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

    def append_round(self, result: RoundResult) -> None:
        """Project one typed terminal round into the legacy JSONL format.

        Every candidate row carries a stable ``experiment_id`` (``r<N>c<M>``).
        finding↔experiment attribution is NOT stored here — the Kernel ledger
        carries no finding semantics; the proposer re-derives attribution at
        read time by joining its own findings.jsonl refs against these
        experiment_ids (join-from-history, contract §2.5).
        Proposer abstention and deliberation telemetry come from the same
        ``ProposalBatch`` that drove execution. The non-authoritative full
        trajectory lives in proposer_traces/, never here.
        """
        normalized = []
        for candidate in result.candidates:
            row = encode_candidate_result(
                candidate,
                selected=(
                    candidate.candidate_id == result.selection.candidate_id
                ),
            )
            row["eval_block"] = str(row["eval_block"])[:self.history_eval_cap]
            row.pop("self_report")
            row["telemetry"] = dict(candidate.telemetry)
            normalized.append(row)
        selected = next((c for c in normalized if c["selected"]), None)
        record = {
            "round": result.round_id,
            "parent_sha": result.parent_sha,
            "selected_candidate": result.selection.candidate_id,
            "selected_sha": result.selection.sha,
            "proposal": selected.get("proposal", "") if selected else "",
            "metrics": selected.get("metrics", {}) if selected else {},
            "changed_paths": selected.get("changed_paths", []) if selected else [],
            "base_sha": result.next_sha,
            "candidates": normalized,
            "telemetry": dict(result.telemetry),
        }
        if result.proposals.abstention is not None:
            record["abstention"] = {
                "reason": result.proposals.abstention.reason,
                "blocking_unknown": (
                    result.proposals.abstention.blocking_unknown
                ),
            }
        if result.proposals.telemetry:
            record["deliberation_telemetry"] = dict(
                result.proposals.telemetry
            )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _iter_candidates(rounds: list[dict]):
    for r in rounds:
        for c in r.get("candidates") or []:
            row = dict(c)
            row["round"] = r.get("round")
            yield row
