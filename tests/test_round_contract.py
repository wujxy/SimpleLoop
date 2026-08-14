"""Round selection and terminal-result contract tests."""
from __future__ import annotations

from dataclasses import replace

import pytest

from simpleloop.candidate import (
    CandidateArtifact,
    CandidateResult,
    CandidateStatus,
    EvaluationResult,
    ExecutionResult,
    GateDecision,
)
from simpleloop.round import RoundResult
from simpleloop.stages.proposer import Proposal, ProposalBatch
from simpleloop.stages.selector import Selection, select_candidate


def candidate(
    candidate_id: int,
    value: float,
    *,
    eligible: bool = True,
) -> CandidateResult:
    sha = f"sha-{candidate_id}"
    return CandidateResult(
        candidate_id=candidate_id,
        experiment_id=f"r1c{candidate_id}",
        proposal=Proposal(f"proposal-{candidate_id}"),
        parent_sha="parent",
        status=CandidateStatus.COMPLETED,
        execution=ExecutionResult("COMMITTED"),
        artifact=CandidateArtifact("parent", sha),
        evaluation=EvaluationResult("eval", {"SPEED": value}),
        gate=GateDecision({}, eligible, eligible),
    )


def select(candidates, *, incumbent=100.0, improve=True):
    return select_candidate(
        candidates=tuple(candidates),
        objective_key="SPEED",
        lower_is_better=True,
        incumbent_value=incumbent,
        require_improvement=improve,
    )


def test_selector_chooses_best_eligible_candidate():
    result = select([candidate(0, 90), candidate(1, 80)])

    assert result == Selection(1, "sha-1", "selected")


def test_selector_tie_breaks_by_candidate_id():
    result = select([candidate(3, 80), candidate(1, 80)])

    assert result.candidate_id == 1


def test_selector_reports_no_eligible_candidate():
    result = select([candidate(0, 80, eligible=False)])

    assert result == Selection(None, None, "no_eligible_candidate")


def test_selector_keeps_incumbent_without_improvement():
    result = select([candidate(0, 110)])

    assert result == Selection(None, None, "no_improvement")


def test_static_policy_accepts_gate_valid_regression():
    result = select([candidate(0, 110)], improve=False)

    assert result == Selection(0, "sha-0", "selected")


def test_boolean_or_nonfinite_objective_is_not_eligible():
    for value in (True, float("nan"), float("inf")):
        invalid = replace(
            candidate(0, 80),
            evaluation=EvaluationResult("eval", {"SPEED": value}),
        )
        assert select([invalid]).reason == "no_eligible_candidate"


def test_round_result_owns_selection_and_next_sha():
    chosen = candidate(0, 80)
    result = RoundResult(
        round_id=1,
        parent_sha="parent",
        proposals=ProposalBatch((chosen.proposal,)),
        candidates=(chosen,),
        selection=Selection(0, "sha-0", "selected"),
    )

    assert result.next_sha == "sha-0"
    assert not hasattr(chosen, "selected")


def test_unselected_round_keeps_parent_sha():
    result = RoundResult(
        round_id=1,
        parent_sha="parent",
        proposals=ProposalBatch((), abstention=None),
        candidates=(),
        selection=Selection(None, None, "no_eligible_candidate"),
    )

    assert result.next_sha == "parent"
