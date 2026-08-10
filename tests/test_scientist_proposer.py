from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.roles.inquiry import (
    Explanation,
    InquiryPhase,
    InquiryState,
    ModelClaim,
    ResearchHypothesis,
    ScientistSessionState,
    Understanding,
    WorkingModel,
)
from simpleloop.roles.proposer import (
    _build_phase_system_prompt,
    _parse_scientist_action,
    _validate_scientist_guard,
    phase_allowed_actions,
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


def test_phase_policy_is_the_single_history_visibility_source():
    fresh = phase_allowed_actions(InquiryPhase.UNDERSTAND, False)
    assert fresh == frozenset({
        "run_research_command",
        "commit_understanding",
        "block",
    })
    assert "search_experiments" not in fresh

    narrow = phase_allowed_actions(InquiryPhase.NARROW, True)
    assert {
        "search_experiments", "inspect_finding", "select_for_deepen",
        "continue_explore", "reopen_explain", "reopen_model",
        "fresh_reframe", "abandon_portfolio", "block",
    } <= narrow

    with pytest.raises(ValueError, match="requires history"):
        phase_allowed_actions(InquiryPhase.NARROW, False)


def test_scientist_parser_separates_model_revision_from_commit():
    proposed = _parse_scientist_action(json.dumps({
        "action": "propose_working_model",
        "working_model": {
            "representation": "cost = frequency * unit cost",
            "explanatory_structure": "counterfactual cost model",
            "claims": [{
                "id": "M1", "claim": "calls repeat",
                "evidence_refs": ["source:src/a.cc"],
            }],
            "important_unknowns": ["which factor dominates"],
        },
    }))
    assert proposed["working_model"]["claims"][0]["id"] == "M1"

    committed = _parse_scientist_action(json.dumps({
        "action": "commit_working_model",
        "model_version": 2,
        "model_check": {
            "explains_target": "yes",
            "counterfactual": {
                "change": "halve frequency", "predicted_effect": "lower cost",
                "model_claim_refs": ["M1"],
            },
            "important_unknowns": [{
                "question": "which factor dominates", "why_it_matters": "focus",
            }],
            "blocking_unknown": None,
            "why_model_is_sufficient_for_next_stage": "accounts can test terms",
        },
    }))
    assert committed["model_version"] == 2
    assert "working_model" not in committed


def test_scientist_parser_requires_whole_account_and_allows_conjecture():
    understanding = _parse_scientist_action(json.dumps({
        "action": "commit_understanding", "problem": "slow",
        "target_outcome": "lower total cost", "boundary": "whole flow",
        "current_account_of_the_whole": "input flows through repeated work",
        "key_unknowns": ["where repetition occurs"],
    }))["understanding"]
    assert understanding.current_account_of_the_whole.startswith("input flows")

    hypothesis = _parse_scientist_action(json.dumps({
        "action": "submit_hypothesis", "id": "H1", "generative_op": "G2",
        "model_basis": ["M1"], "explanation_basis": ["E1"],
        "mechanism": "lifetime", "intervention_family": "ownership",
        "scope": "system", "why_plausible": "state repeats",
        "critical_unknown": "whether state is invariant",
    }))["hypothesis"]
    assert hypothesis.evidence_refs == ()


def _session_with_model() -> ScientistSessionState:
    session = ScientistSessionState.fresh()
    session.inquiry.working_model = WorkingModel(
        version=2,
        representation="cost model",
        explanatory_structure="frequency times unit cost",
        claims=(ModelClaim("M1", "calls repeat", ("source:src/a.cc",)),),
        important_unknowns=("which factor dominates",),
    )
    return session


def _commit_model_action(*, blocking_unknown=None, version=2):
    return _parse_scientist_action(json.dumps({
        "action": "commit_working_model", "model_version": version,
        "model_check": {
            "explains_target": "yes",
            "counterfactual": {
                "change": "halve calls", "predicted_effect": "lower cost",
                "model_claim_refs": ["M1"],
            },
            "important_unknowns": [{
                "question": "which term dominates", "why_it_matters": "focus",
            }],
            "blocking_unknown": blocking_unknown,
            "why_model_is_sufficient_for_next_stage": "supports accounts",
        },
    }))


def test_model_guard_uses_blocking_unknown_not_important_unknown(tmp_path: Path):
    session = _session_with_model()
    session.inquiry.phase = InquiryPhase.MODEL

    assert _validate_scientist_guard(
        session, _commit_model_action(), tmp_path, select_quota=2,
    ) is None
    assert _validate_scientist_guard(
        session, _commit_model_action(blocking_unknown="call topology"),
        tmp_path, select_quota=2,
    ) == "model_blocking_unknown"
    assert _validate_scientist_guard(
        session, _commit_model_action(version=1), tmp_path, select_quota=2,
    ) == "stale_model_version"


def test_lineage_guards_reject_unknown_model_and_explanation(tmp_path: Path):
    session = _session_with_model()
    session.inquiry.phase = InquiryPhase.EXPLAIN
    explanation = _parse_scientist_action(json.dumps({
        "action": "submit_explanation", "id": "E9", "phenomenon": "gap",
        "account": "dependency blocks progress", "model_basis": ["M9"],
        "expected_if_true": ["repeat calls"], "evidence_needed": ["call sites"],
    }))
    assert _validate_scientist_guard(
        session, explanation, tmp_path, select_quota=2,
    ) == "unknown_model_claim"

    session.inquiry.explanations = [Explanation(
        id="E1", phenomenon="gap", account="repeated work",
        model_basis=("M1",), expected_if_true=("repeat calls",),
        evidence_needed=("call sites",),
    )]
    session.inquiry.phase = InquiryPhase.EXPLORE
    hypothesis = _parse_scientist_action(json.dumps({
        "action": "submit_hypothesis", "id": "H1", "generative_op": "G2",
        "model_basis": ["M1"], "explanation_basis": ["E9"],
        "mechanism": "lifetime", "intervention_family": "ownership",
        "scope": "system", "why_plausible": "state repeats",
        "critical_unknown": "whether state is invariant",
    }))
    assert _validate_scientist_guard(
        session, hypothesis, tmp_path, select_quota=2,
    ) == "unknown_explanation"


def test_breadth_is_a_target_with_justification_not_a_minimum(tmp_path: Path):
    session = _session_with_model()
    session.inquiry.explanations = [Explanation(
        id="E1", phenomenon="gap", account="repetition",
        model_basis=("M1",), expected_if_true=("repeat",),
        evidence_needed=("trace",),
    )]
    session.inquiry.hypotheses = [ResearchHypothesis(
        id="H1", generative_op="G2", model_basis=("M1",),
        explanation_basis=("E1",), mechanism="lifetime",
        intervention_family="ownership", scope="system",
        why_plausible="state repeats", critical_unknown="invariance",
    )]
    session.inquiry.phase = InquiryPhase.EXPLORE
    base = {
        "action": "commit_hypothesis_portfolio", "hypothesis_ids": ["H1"],
        "coverage_rationale": "tested mechanism and representation space",
        "portfolio_sufficiency_justification": None,
        "unused_generative_ops": ["G3"],
    }
    without = _parse_scientist_action(json.dumps(base))
    assert _validate_scientist_guard(
        session, without, tmp_path, select_quota=2,
    ) == "portfolio_below_breadth_target"

    base["portfolio_sufficiency_justification"] = (
        "additional directions would be unsupported cosmetic variants"
    )
    with_reason = _parse_scientist_action(json.dumps(base))
    assert _validate_scientist_guard(
        session, with_reason, tmp_path, select_quota=2,
    ) is None


def test_phase_guard_precedes_proposal_shape_checks(tmp_path: Path):
    session = _session_with_model()
    session.inquiry.phase = InquiryPhase.NARROW
    session.inquiry.set_history_visible(True, step=8)

    assert _validate_scientist_guard(
        session, {"action": "submit_proposals"}, tmp_path, select_quota=2,
    ) == "action_not_allowed_in_narrow"


def test_phase_prompt_exposes_only_current_actions():
    fresh = _build_phase_system_prompt(
        None, InquiryPhase.UNDERSTAND, False,
    )
    assert '"action":"commit_understanding"' in fresh
    assert '"action":"search_experiments"' not in fresh

    narrow = _build_phase_system_prompt(None, InquiryPhase.NARROW, True)
    assert '"action":"search_experiments"' in narrow
    assert '"action":"commit_understanding"' not in narrow
