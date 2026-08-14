from __future__ import annotations

from dataclasses import replace

import pytest

from simpleloop.candidate import (
    CandidateArtifact, CandidateResult, CandidateStatus, EvaluationResult,
    ExecutionResult, GateDecision,
)
from simpleloop.persistence.history import HistoryConflictError, Store
from simpleloop.round import RoundResult
from simpleloop.stages.proposer import Proposal, ProposalBatch
from simpleloop.stages.selector import Selection


SCHEMA = {
    "objective": {"key": "OBJ", "lower_is_better": True},
    "gates": [],
}


def _round(round_id: int = 0) -> RoundResult:
    candidate = CandidateResult(
        0, f"r{round_id}c0", Proposal("try it"), "parent",
        CandidateStatus.COMPLETED, ExecutionResult("COMMITTED"),
        CandidateArtifact("parent", "winner"),
        EvaluationResult("OBJ=1", {"OBJ": 1.0}),
        GateDecision({}, True, True),
    )
    return RoundResult(
        round_id, "parent", ProposalBatch((candidate.proposal,)), (candidate,),
        Selection(0, "winner", "selected"),
    )


def test_append_round_is_idempotent(tmp_path):
    store = Store(tmp_path, SCHEMA)
    result = _round()

    store.append_round(result)
    store.append_round(result)

    assert len(store.history()) == 1


def test_append_round_rejects_conflicting_round(tmp_path):
    store = Store(tmp_path, SCHEMA)
    result = _round()
    store.append_round(result)

    with pytest.raises(HistoryConflictError, match="round 0"):
        store.append_round(replace(result, parent_sha="different"))
