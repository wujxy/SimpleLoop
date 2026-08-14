"""Compact typed-round builders for legacy history/reporting tests."""
from __future__ import annotations

from dataclasses import replace

from simpleloop.harness.store import eligible
from simpleloop.persistence.artifacts import decode_candidate_result
from simpleloop.round import RoundResult
from simpleloop.stages.proposer import Abstention, ProposalBatch
from simpleloop.stages.selector import Selection


def append_round(
    store,
    round_id: int,
    *,
    parent_sha: str,
    selected_candidate: int | None,
    selected_sha: str | None,
    candidates: list[dict],
    abstention: dict | None = None,
    deliberation_telemetry: dict | None = None,
    telemetry: dict | None = None,
) -> None:
    typed = []
    for index, candidate in enumerate(candidates):
        candidate_id = candidate.get("candidate", index)
        raw = {
            "candidate": candidate_id,
            "experiment_id": candidate.get("experiment_id")
            or f"r{round_id}c{candidate_id}",
            "proposal": candidate.get("proposal") or "test proposal",
            "parent_sha": candidate.get("parent_sha") or parent_sha,
            "sha": candidate.get("sha"),
            "status": candidate.get("status") or (
                "COMPLETED" if candidate.get("sha") else "NO_CHANGE"
            ),
            "eval_block": candidate.get("eval_block") or "",
            "metrics": candidate.get("metrics") or {},
            "changed_paths": candidate.get("changed_paths") or [],
            "gates": candidate.get("gates") or {},
            "gate_passed": candidate.get("gate_passed") is True,
            "eligible": eligible(candidate, store.metrics_schema),
            "selected": False,
            "self_report": candidate.get("self_report"),
        }
        typed.append(replace(
            decode_candidate_result(raw),
            telemetry=dict(candidate.get("telemetry") or {}),
        ))
    batch = ProposalBatch(
        tuple(candidate.proposal for candidate in typed),
        abstention=(
            Abstention(
                str(abstention.get("reason") or ""),
                abstention.get("blocking_unknown"),
            )
            if abstention is not None else None
        ),
        telemetry=dict(deliberation_telemetry or {}),
    )
    store.append_round(RoundResult(
        round_id=round_id,
        parent_sha=parent_sha,
        proposals=batch,
        candidates=tuple(typed),
        selection=Selection(
            selected_candidate,
            selected_sha,
            "selected" if selected_candidate is not None else "not_selected",
        ),
        telemetry=dict(telemetry or {}),
    ))
