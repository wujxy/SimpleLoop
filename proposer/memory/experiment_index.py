"""Structured index over the immutable Experiment Ledger.

The ledger itself is ``history.jsonl``; this module only projects it into a
flat, filter-friendly list of experiments. It never mutates history.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Experiment:
    """One projected candidate row. All fields come straight from history."""

    experiment_id: str
    round: int
    candidate: int
    proposal: str
    parent_sha: str
    candidate_sha: str | None
    status: str
    gate_passed: bool
    eligible: bool
    selected: bool
    metrics: dict
    changed_paths: tuple[str, ...]
    finding_id: str | None
    eval_block: str
    self_report: dict | None = None

    def to_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "round": self.round,
            "candidate": self.candidate,
            "proposal": self.proposal,
            "parent_sha": self.parent_sha,
            "candidate_sha": self.candidate_sha,
            "status": self.status,
            "gate_passed": self.gate_passed,
            "eligible": self.eligible,
            "selected": self.selected,
            "metrics": dict(self.metrics),
            "changed_paths": list(self.changed_paths),
            "finding_id": self.finding_id,
            "eval_block": self.eval_block,
            "self_report": dict(self.self_report) if self.self_report else None,
        }


def build_experiments(history: list[dict]) -> list[Experiment]:
    """Project history rows into flat Experiment records."""
    out: list[Experiment] = []
    for record in history or []:
        if not isinstance(record, dict):
            continue
        round_id = int(record.get("round", 0))
        for cand in record.get("candidates") or []:
            if not isinstance(cand, dict):
                continue
            cid = int(cand.get("candidate", 0))
            experiment_id = str(
                cand.get("experiment_id") or f"r{round_id}c{cid}"
            )
            out.append(Experiment(
                experiment_id=experiment_id,
                round=round_id,
                candidate=cid,
                proposal=str(cand.get("proposal") or ""),
                parent_sha=str(
                    cand.get("parent_sha") or record.get("parent_sha") or ""
                ),
                candidate_sha=cand.get("sha"),
                status=str(cand.get("status") or ""),
                gate_passed=bool(cand.get("gate_passed")),
                eligible=bool(cand.get("eligible")),
                selected=bool(cand.get("selected")),
                metrics=dict(cand.get("metrics") or {}),
                changed_paths=tuple(cand.get("changed_paths") or ()),
                finding_id=cand.get("finding_id"),
                eval_block=str(cand.get("eval_block") or ""),
                self_report=(
                    dict(cand["self_report"])
                    if isinstance(cand.get("self_report"), dict)
                    else None
                ),
            ))
    return out


def filter_experiments(
    experiments: list[Experiment],
    *,
    gate_passed: bool | None = None,
    eligible: bool | None = None,
    selected: bool | None = None,
    finding_id: str | None = None,
    round_min: int | None = None,
    round_max: int | None = None,
    changed_path: str | None = None,
    status: str | None = None,
) -> list[Experiment]:
    """Structured filters that stack in retrieval calls. ``changed_path``
    matches on prefix (so ``OMILREC/`` catches every file below it)."""
    def keep(exp: Experiment) -> bool:
        if gate_passed is not None and exp.gate_passed != gate_passed:
            return False
        if eligible is not None and exp.eligible != eligible:
            return False
        if selected is not None and exp.selected != selected:
            return False
        if finding_id is not None and exp.finding_id != finding_id:
            return False
        if round_min is not None and exp.round < round_min:
            return False
        if round_max is not None and exp.round > round_max:
            return False
        if status is not None and exp.status != status:
            return False
        if changed_path is not None:
            if not any(
                path == changed_path or path.startswith(changed_path)
                for path in exp.changed_paths
            ):
                return False
        return True

    return [exp for exp in experiments if keep(exp)]
