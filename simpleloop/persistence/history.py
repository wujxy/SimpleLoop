"""Append-only factual history, selection, and episode queries."""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from ..persistence.artifacts import encode_candidate_result
from ..round import RoundResult


class HistoryConflictError(RuntimeError):
    """One round id has two different terminal facts."""


_EPISODE_REF_RE = re.compile(r"^r(0|[1-9]\d*)c(0|[1-9]\d*)$")


def read_history(path: Path) -> list[dict]:
    """Read append-only history JSONL, returning an empty list if absent."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read history memory {path}: {exc}") from exc
    required_candidate = {"status", "gate_passed", "eligible"}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"history memory {path} contains a non-object row")
        candidates = row.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError(
                f"history memory {path} row is missing 'candidates' list"
            )
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or not required_candidate <= candidate.keys()
            ):
                raise ValueError(
                    f"history memory {path} does not use the current "
                    "candidate schema"
                )
    return rows


def resolve_episode(history: list[dict], ref: str) -> dict:
    """Resolve one stable ``r<round>c<candidate>`` history reference."""
    match = _EPISODE_REF_RE.fullmatch(str(ref).strip())
    if match is None:
        raise ValueError(
            f"invalid memory reference {ref!r}; expected r<round>c<candidate>"
        )
    round_id, candidate_id = int(match.group(1)), int(match.group(2))
    record = next(
        (item for item in history if item.get("round") == round_id), None,
    )
    if record is None:
        raise ValueError(f"memory reference not found: {ref}")
    candidate = next((
        item for item in (record.get("candidates") or [])
        if item.get("candidate") == candidate_id
    ), None)
    if candidate is None:
        raise ValueError(f"memory reference not found: {ref}")
    experiment_id = str(
        candidate.get("experiment_id") or f"r{round_id}c{candidate_id}"
    )
    return {
        "ref": experiment_id,
        "experiment_id": experiment_id,
        "finding_id": candidate.get("finding_id"),
        "proposal": candidate.get("proposal") or "",
        "parent_sha": candidate.get("parent_sha") or record.get("parent_sha"),
        "candidate_sha": candidate.get("sha"),
        "status": candidate.get("status"),
        "selected": bool(candidate.get("selected")),
        "gate_passed": candidate.get("gate_passed"),
        "eligible": candidate.get("eligible"),
        "gates": candidate.get("gates") or {},
        "metrics": candidate.get("metrics") or {},
        "changed_paths": candidate.get("changed_paths") or [],
        "eval_block": candidate.get("eval_block") or "",
        # Parity with the proposer-side reader (proposer/memory/history.py):
        # the experimenter's account is part of the episode record.
        "self_report": candidate.get("self_report"),
    }


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
        return read_history(self.path)

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
            # The experimenter's report is part of the experiment record —
            # the Researcher reads it (objective claims, capped). The full
            # report lives in the candidate artifacts; history carries the
            # capped account, not silence.
            report = row.get("self_report")
            if isinstance(report, dict):
                row["self_report"] = {
                    "outcome": str(report.get("outcome"))[:40],
                    "summary": str(report.get("summary") or "")[:600],
                    "fidelity": str(report.get("fidelity") or "")[:600],
                }
            else:
                row["self_report"] = {
                    "outcome": "no_report", "summary": "", "fidelity": "",
                }
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
        existing = self.history()
        same_round = [row for row in existing if row.get("round") == result.round_id]
        if len(same_round) > 1:
            raise HistoryConflictError(
                f"history already contains duplicate round {result.round_id}"
            )
        if same_round:
            if same_round[0] == record:
                return
            raise HistoryConflictError(
                f"round {result.round_id} conflicts with persisted history"
            )
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def _iter_candidates(rounds: list[dict]):
    for r in rounds:
        for c in r.get("candidates") or []:
            row = dict(c)
            row["round"] = r.get("round")
            yield row
