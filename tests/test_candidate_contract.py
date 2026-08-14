"""Candidate business-result contract tests."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import PurePosixPath

import pytest

from simpleloop.candidate import (
    CandidateArtifact,
    CandidateResult,
    CandidateStatus,
    ExecutionResult,
    GateDecision,
)
from simpleloop.stages.proposer import Proposal


def _result(*, artifact_parent: str = "parent") -> CandidateResult:
    return CandidateResult(
        candidate_id=1,
        experiment_id="r2c1",
        proposal=Proposal("cache the transform"),
        parent_sha="parent",
        status=CandidateStatus.COMPLETED,
        execution=ExecutionResult("COMMITTED"),
        artifact=CandidateArtifact(
            artifact_parent, "child", (PurePosixPath("src/cache.cc"),)
        ),
        evaluation=None,
        gate=GateDecision({}, True, True),
    )


def test_candidate_result_is_frozen_and_selection_free():
    result = _result()

    assert result.sha == "child"
    assert not hasattr(result, "selected")
    with pytest.raises(FrozenInstanceError):
        result.status = CandidateStatus.NO_CHANGE


def test_candidate_rejects_artifact_with_different_parent():
    with pytest.raises(ValueError, match="parent_sha"):
        _result(artifact_parent="other")
