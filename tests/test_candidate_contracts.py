from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from simpleloop.candidate import (
    CandidateBatchRequest,
    CandidatePlan,
    CandidateRequest,
    EvaluationResult,
)
from simpleloop.stages.gate import GateSpec, apply_gates
from simpleloop.stages.proposer import Proposal
from simpleloop.world import SourceWorkspace


def test_candidate_batch_allows_independent_parents(tmp_path: Path):
    plans = (
        CandidatePlan(0, "parent-a", Proposal("a")),
        CandidatePlan(1, "parent-b", Proposal("b")),
    )
    batch = CandidateBatchRequest(4, plans)
    workspace = SourceWorkspace("4-c1", tmp_path, "parent-b")
    request = CandidateRequest(4, 1, "parent-b", plans[1].proposal, workspace)

    assert batch.candidates[1].parent_sha == request.parent_sha
    assert request.workspace is workspace
    with pytest.raises(FrozenInstanceError):
        plans[0].parent_sha = "mutated"


def test_apply_gates_accepts_only_finite_objective():
    spec = GateSpec("SPEED_MS", ("CORRECTNESS",))

    good = apply_gates(
        EvaluationResult(
            "ok", {"SPEED_MS": 90.0, "CORRECTNESS": True}, (0,),
        ),
        spec,
    )
    bad = apply_gates(
        EvaluationResult(
            "nan", {"SPEED_MS": float("nan"), "CORRECTNESS": True},
            (0,),
        ),
        spec,
    )

    assert good.passed is True
    assert good.eligible is True
    assert bad.passed is True
    assert bad.eligible is False


def test_apply_gates_preserves_skip_details():
    decision = apply_gates(
        None,
        GateSpec("SPEED_MS", ("CORRECTNESS",)),
        skip_reason="not run because Executor produced no change",
    )

    assert decision.results["PATHS"].passed is True
    assert decision.results["EVAL_COMMANDS"].passed is None
    assert decision.results["EVAL_COMMANDS"].detail == (
        "not run because Executor produced no change"
    )


@pytest.mark.parametrize("reserved", ["PATHS", "EVAL_COMMANDS"])
def test_apply_gates_rejects_reserved_objective(reserved: str):
    with pytest.raises(ValueError, match="reserved"):
        apply_gates(
            EvaluationResult("", {}, (0,)),
            GateSpec(reserved, ()),
        )
