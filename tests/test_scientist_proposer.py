from __future__ import annotations

import pytest

from simpleloop.roles.inquiry import (
    InquiryPhase,
    InquiryState,
    ModelClaim,
    ResearchHypothesis,
    ScientistSessionState,
    Understanding,
    WorkingModel,
)


def test_inquiry_starts_fresh_with_a_whole_problem_account():
    session = ScientistSessionState.fresh()

    assert session.inquiry.phase is InquiryPhase.UNDERSTAND
    assert session.inquiry.context_id == 0
    assert session.inquiry.history_visible is False

    understanding = Understanding(
        problem="runtime is high",
        target_outcome="lower end-to-end latency",
        boundary="the complete reconstruction path",
        current_account_of_the_whole=(
            "events flow through reconstruction and repeated likelihood "
            "evaluation before producing a result"
        ),
        key_unknowns=("which cost component dominates",),
    )
    assert "repeated likelihood" in understanding.current_account_of_the_whole


def test_working_model_versions_are_explicit():
    model = WorkingModel(
        version=2,
        representation="total cost is repeated work plus outside work",
        explanatory_structure="frequency multiplied by per-call cost",
        claims=(ModelClaim("M1", "the target is end-to-end cost", ("source:a",)),),
        important_unknowns=("which term dominates",),
    )

    assert model.version == 2
    assert model.important_unknowns


def test_research_hypothesis_can_be_a_lineage_grounded_conjecture():
    hypothesis = ResearchHypothesis(
        id="H1",
        generative_op="G2",
        model_basis=("M1",),
        explanation_basis=("E1",),
        mechanism="State Lifetime",
        intervention_family="Ownership Lift",
        scope="whole-system",
        why_plausible="shared state appears to be rebuilt",
        critical_unknown="whether consumers share the same invariant state",
    )

    assert hypothesis.evidence_refs == ()
    assert hypothesis.signature() == (
        "state lifetime",
        "ownership lift",
        "whole-system",
    )


def test_history_visibility_is_monotonic_per_epistemic_context():
    state = InquiryState(context_id=0)

    state.set_history_visible(True, step=12)
    state.set_history_visible(True, step=13)

    assert state.history_visible is True
    assert state.history_injected_at_step == 12
    with pytest.raises(ValueError, match="cannot be hidden"):
        state.set_history_visible(False, step=14)


def test_fresh_reframe_preserves_only_cumulative_telemetry():
    session = ScientistSessionState.fresh()
    old_context = session.context
    session.cumulative_usage.append({"total_tokens": 20})
    session.cumulative_action_log.append({"action": "commit_understanding"})
    session.runtime.counts["tool"] = 7
    session.runtime.session_evidence.add("source:a")
    session.runtime.new_evidence.add("source:a")
    session.runtime.action_log.append({"action": "run_research_command"})
    session.runtime.protocol_repairs = 1
    session.runtime.last_tool_fingerprint = "run_research_command:source:rg"

    session.start_fresh_context()

    assert session.archived_contexts == [old_context]
    assert session.inquiry.context_id == 1
    assert session.inquiry.phase is InquiryPhase.UNDERSTAND
    assert session.runtime.counts == {}
    assert session.runtime.session_evidence == set()
    assert session.runtime.new_evidence == set()
    assert session.runtime.action_log == []
    assert session.runtime.protocol_repairs == 0
    assert session.runtime.last_tool_fingerprint is None
    assert session.cumulative_usage == [{"total_tokens": 20}]
    assert session.cumulative_action_log == [{"action": "commit_understanding"}]


def test_only_one_fresh_reframe_is_allowed():
    session = ScientistSessionState.fresh()
    session.start_fresh_context()

    with pytest.raises(ValueError, match="fresh reframe limit"):
        session.start_fresh_context()
