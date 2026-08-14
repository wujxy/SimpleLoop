from __future__ import annotations

import pytest

from simpleloop.rsi.models import (
    RsiResult,
    SelfChange,
    SelfDecision,
    SelfDecisionKind,
    SelfEvent,
    SelfEventKind,
)


def test_change_decision_requires_a_change():
    with pytest.raises(ValueError, match="CHANGE requires"):
        SelfDecision(SelfDecisionKind.CHANGE, "diagnosis")


def test_keep_decision_rejects_a_change():
    with pytest.raises(ValueError, match="KEEP cannot"):
        SelfDecision(
            SelfDecisionKind.KEEP,
            "diagnosis",
            change=SelfChange("prompt", "intent", "instruction"),
        )


def test_explicit_review_defer_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        SelfDecision(
            SelfDecisionKind.KEEP,
            "diagnosis",
            next_review_after_rounds=0,
        )


def test_adopted_event_requires_a_candidate_sha():
    with pytest.raises(ValueError, match="candidate_sha"):
        SelfEvent(
            "r3:CANDIDATE_ADOPTED",
            SelfEventKind.CANDIDATE_ADOPTED,
            3,
            "s0",
        )


def test_rsi_result_exposes_typed_decision():
    result = RsiResult(4, SelfDecisionKind.KEEP)

    assert result.decision.value == "KEEP"
