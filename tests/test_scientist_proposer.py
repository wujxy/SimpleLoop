from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.memory.models import NewFindingTarget, ResearchProposal

from simpleloop.roles.inquiry import (
    Explanation,
    InquiryPhase,
    HypothesisSelection,
    InquiryState,
    ModelClaim,
    ResearchHypothesis,
    ScientistSessionState,
    Understanding,
    WorkingModel,
)
from simpleloop.roles.model import ModelReply
from simpleloop.roles.proposer import (
    ProposerAgent,
    _build_phase_system_prompt,
    _parse_scientist_action,
    _validate_scientist_guard,
    _apply_scientist_action,
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



def _explanation_action(eid):
    return _parse_scientist_action(json.dumps({
        "action": "submit_explanation", "id": eid,
        "phenomenon": f"gap {eid}", "account": f"account {eid}",
        "model_basis": ["M1"], "expected_if_true": [f"signal {eid}"],
        "evidence_needed": [f"evidence {eid}"],
    }))


def _hypothesis_action(index):
    return _parse_scientist_action(json.dumps({
        "action": "submit_hypothesis", "id": f"H{index}",
        "generative_op": f"G{index}", "model_basis": ["M1"],
        "explanation_basis": ["E1"], "mechanism": f"mechanism {index}",
        "intervention_family": f"family {index}", "scope": f"scope {index}",
        "why_plausible": "the account predicts this direction",
        "critical_unknown": f"premise {index}",
    }))


def test_fresh_cycle_preserves_one_session_and_artifact_lineage():
    session = ScientistSessionState.fresh()
    identity = id(session)
    actions = [
        _parse_scientist_action(json.dumps({
            "action": "commit_understanding", "problem": "slow",
            "target_outcome": "lower cost", "boundary": "whole flow",
            "current_account_of_the_whole": "inputs trigger repeated work",
            "key_unknowns": ["dominant term"],
        })),
        _parse_scientist_action(json.dumps({
            "action": "propose_working_model", "working_model": {
                "representation": "cost = frequency * unit cost",
                "explanatory_structure": "counterfactual cost model",
                "claims": [{"id": "M1", "claim": "calls repeat",
                            "evidence_refs": ["source:src/a.cc"]}],
                "important_unknowns": ["dominant term"],
            },
        })),
        _commit_model_action(version=1),
        _explanation_action("E1"), _explanation_action("E2"),
        _parse_scientist_action(json.dumps({
            "action": "commit_explanation_set",
            "explanation_ids": ["E1", "E2"],
            "explanation_sufficiency_justification": None,
        })),
        _parse_scientist_action(json.dumps({
            "action": "emit_lever_map", "levers": [{
                "id": "L1", "target_mechanism": "repetition",
                "why_leverage_exists": "frequency multiplies unit cost",
                "model_basis": ["M1"], "explanation_basis": ["E1"],
            }],
        })),
        *[_hypothesis_action(i) for i in range(1, 5)],
        _parse_scientist_action(json.dumps({
            "action": "commit_hypothesis_portfolio",
            "hypothesis_ids": ["H1", "H2", "H3", "H4"],
            "coverage_rationale": "four distinct mechanism families",
            "portfolio_sufficiency_justification": None,
            "unused_generative_ops": [],
        })),
    ]

    for step, action in enumerate(actions, 1):
        assert _validate_scientist_guard(
            session, action, Path("."), select_quota=2,
        ) is None
        _apply_scientist_action(session, action, step=step)

    assert id(session) == identity
    assert session.inquiry.understanding.current_account_of_the_whole
    assert session.inquiry.working_model.version == 1
    assert [item.id for item in session.inquiry.explanations] == ["E1", "E2"]
    assert [item.id for item in session.inquiry.hypotheses] == [
        "H1", "H2", "H3", "H4",
    ]
    assert session.inquiry.phase is InquiryPhase.NARROW
    assert session.inquiry.history_visible is True
    assert session.inquiry.history_injected_at_step == len(actions)
    assert [(item.source, item.target) for item in session.inquiry.phase_transitions] == [
        (InquiryPhase.UNDERSTAND, InquiryPhase.MODEL),
        (InquiryPhase.MODEL, InquiryPhase.EXPLAIN),
        (InquiryPhase.EXPLAIN, InquiryPhase.EXPLORE),
        (InquiryPhase.EXPLORE, InquiryPhase.NARROW),
    ]


def test_history_injection_policy_flips_atomically_at_portfolio_commit():
    session = ScientistSessionState.fresh()
    session.inquiry.phase = InquiryPhase.EXPLORE
    session.inquiry.hypotheses = [ResearchHypothesis(
        id="H1", generative_op="G1", model_basis=("M1",),
        explanation_basis=("E1",), mechanism="frequency",
        intervention_family="reuse", scope="whole flow",
        why_plausible="repeated state", critical_unknown="sharing",
    )]
    action = _parse_scientist_action(json.dumps({
        "action": "commit_hypothesis_portfolio", "hypothesis_ids": ["H1"],
        "coverage_rationale": "attempted mechanism and representation space",
        "portfolio_sufficiency_justification": "other ideas lack lineage",
        "unused_generative_ops": ["G2"],
    }))
    assert "search_experiments" not in _build_phase_system_prompt(
        None, session.inquiry.phase, session.inquiry.history_visible,
    )
    _apply_scientist_action(session, action, step=9)
    assert session.inquiry.phase is InquiryPhase.NARROW
    assert session.inquiry.history_visible is True
    assert '"action":"search_experiments"' in _build_phase_system_prompt(
        None, session.inquiry.phase, session.inquiry.history_visible,
    )


class _LaneModel:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return ModelReply(json.dumps(self.actions.pop(0)), usage={"total_tokens": 1})


class _LaneMemory:
    def __init__(self):
        self.history_calls = 0

    def build_fresh_inquiry_context(self, **kwargs):
        return "FRESH WORLD ONLY"

    def build_history_entry_pack(self, **kwargs):
        self.history_calls += 1
        return "THIN FACTUAL HISTORY"


def _fresh_lane_actions():
    actions = [
        {"action": "commit_understanding", "problem": "slow",
         "target_outcome": "lower cost", "boundary": "whole flow",
         "current_account_of_the_whole": "inputs trigger repeated work",
         "key_unknowns": ["dominant term"]},
        {"action": "propose_working_model", "working_model": {
            "representation": "cost = frequency * unit cost",
            "explanatory_structure": "counterfactual cost model",
            "claims": [{"id": "M1", "claim": "calls repeat",
                        "evidence_refs": ["source:src/a.cc"]}],
            "important_unknowns": ["dominant term"]}},
        {"action": "commit_working_model", "model_version": 1,
         "model_check": {
             "explains_target": "yes", "counterfactual": {
                 "change": "halve calls", "predicted_effect": "lower cost",
                 "model_claim_refs": ["M1"]},
             "important_unknowns": [{"question": "dominant term",
                                      "why_it_matters": "focus"}],
             "blocking_unknown": None,
             "why_model_is_sufficient_for_next_stage": "supports accounts"}},
        {"action": "submit_explanation", "id": "E1", "phenomenon": "gap",
         "account": "repetition", "model_basis": ["M1"],
         "expected_if_true": ["repeat"], "evidence_needed": ["trace"]},
        {"action": "submit_explanation", "id": "E2", "phenomenon": "gap",
         "account": "unit cost", "model_basis": ["M1"],
         "expected_if_true": ["expensive calls"], "evidence_needed": ["profile"]},
        {"action": "commit_explanation_set", "explanation_ids": ["E1", "E2"],
         "explanation_sufficiency_justification": None},
        {"action": "emit_lever_map", "levers": [{
            "id": "L1", "target_mechanism": "repetition",
            "why_leverage_exists": "frequency multiplies cost",
            "model_basis": ["M1"], "explanation_basis": ["E1"]}]},
    ]
    for index in range(1, 5):
        actions.append({
            "action": "submit_hypothesis", "id": f"H{index}",
            "generative_op": f"G{index}", "model_basis": ["M1"],
            "explanation_basis": ["E1"], "mechanism": f"mechanism {index}",
            "intervention_family": f"family {index}", "scope": f"scope {index}",
            "why_plausible": "lineage supports it",
            "critical_unknown": f"premise {index}"})
    actions.extend([
        {"action": "commit_hypothesis_portfolio",
         "hypothesis_ids": ["H1", "H2", "H3", "H4"],
         "coverage_rationale": "four mechanism families",
         "portfolio_sufficiency_justification": None,
         "unused_generative_ops": []},
        {"action": "select_for_deepen", "selected": [{
            "hypothesis_id": "H1", "evidence_refs": ["experiment:r0c0"],
            "rationale": "history tests the critical premise"}]},
    ])
    return actions


def test_run_lane_switches_prompt_tools_and_history_together(tmp_path, monkeypatch):
    model = _LaneModel(_fresh_lane_actions())
    memory = _LaneMemory()
    agent = ProposerAgent(
        model=model, runtime=object(), timeout_seconds=30, max_steps=20,
        command_timeout_seconds=5, command_output_cap_chars=1000,
    )
    tool_calls = []

    class Tools:
        def __init__(self, kwargs):
            self.history_enabled = kwargs["history_enabled"]
            self.memory = kwargs["memory_service"]

    def make_tools(**kwargs):
        tool_calls.append(kwargs)
        return Tools(kwargs)

    monkeypatch.setattr(agent, "_make_tools", make_tools)
    result = agent.run_lane(
        assigned_ops=("G1", "G2", "G3", "G4"), select_quota=2,
        scientist_steps=len(_fresh_lane_actions()), goal="lower cost",
        editable=["src/**"], frozen=["tests/**"], memory_service=memory,
        base_sha="abc", source_path=tmp_path, repo_path=tmp_path,
        run_dir=tmp_path, current_round=1, gate_block="must pass",
        prompt_dir=None,
    )

    assert [call["history_enabled"] for call in tool_calls] == [False, True]
    assert tool_calls[0]["history_dir"] is None
    assert tool_calls[0]["memory_service"] is None
    assert tool_calls[1]["history_dir"] == tmp_path
    assert tool_calls[1]["memory_service"] is memory
    assert memory.history_calls == 1
    assert '"action":"search_experiments"' not in model.calls[0]["system"]
    assert '"action":"search_experiments"' in model.calls[-1]["system"]
    assert sum(
        message["content"] == "THIN FACTUAL HISTORY"
        for message in model.calls[-1]["messages"]
    ) == 1
    assert result.trace["history_injected_at_step"] == 12
    assert result.trace["phase"] == "deepen"
    assert {
        "contexts", "phase_transitions", "actions", "understanding",
        "working_model", "model_revisions", "explanations", "lever_map",
        "fresh_hypotheses", "narrow_decisions", "selected_hypotheses",
        "deep_evidence", "proposals", "reopen_counts", "fresh_reframes",
        "usage_by_phase", "tool_calls_by_phase", "steps_to_working_model",
        "steps_to_portfolio", "steps_to_outcome", "outcome",
    } <= result.trace.keys()
    assert result.trace["model_revisions"][0]["version"] == 1
    assert result.trace["steps_to_working_model"] == 3
    assert result.trace["steps_to_portfolio"] == 12
    assert result.trace["steps_to_outcome"] == 13
    assert result.trace["narrow_decisions"] == [{
        "step": 13, "selected": ["H1"],
    }]
    assert result.trace["outcome"] == "research_incomplete"
    json.dumps(result.trace)



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


def test_research_proposal_requires_scientist_lineage_metadata():
    with pytest.raises(TypeError):
        ResearchProposal(
            instruction="change X",
            research_target=NewFindingTarget(question="why X"),
        )

    proposal = ResearchProposal(
        instruction="change X",
        research_target=NewFindingTarget(question="why X"),
        model_claim_refs=("M1",), explanation_refs=("E1",),
        hypothesis_id="H1", evidence_refs=("source:src/x.cc",),
        mechanism="remove repeated work",
        prediction="call count falls while gates remain satisfied",
        affected_scope="src/x.cc:X",
    )
    assert proposal.hypothesis_id == "H1"
    assert proposal.model_claim_refs == ("M1",)


def _history_session(phase=InquiryPhase.NARROW):
    session = _session_with_model()
    session.inquiry.phase = phase
    session.inquiry.set_history_visible(True, step=8)
    session.inquiry.explanations = [Explanation(
        id="E1", phenomenon="gap", account="repetition",
        model_basis=("M1",), expected_if_true=("repeat",),
        evidence_needed=("trace",),
    )]
    session.inquiry.hypotheses = [ResearchHypothesis(
        id="H1", generative_op="G2", model_basis=("M1",),
        explanation_basis=("E1",), mechanism="lifetime",
        intervention_family="reuse", scope="whole flow",
        why_plausible="state repeats", critical_unknown="sharing",
    )]
    return session


def test_select_for_deepen_transitions_and_proposal_requires_deep_lineage(tmp_path):
    session = _history_session()
    selected = _parse_scientist_action(json.dumps({
        "action": "select_for_deepen", "selected": [{
            "hypothesis_id": "H1", "evidence_refs": ["experiment:r0c0"],
            "rationale": "history bears on sharing",
        }],
    }))
    _apply_scientist_action(session, selected, step=9)
    assert session.inquiry.phase is InquiryPhase.DEEPEN
    assert session.inquiry.selections[0].hypothesis_id == "H1"

    raw = {
        "action": "submit_proposals", "proposals": [{
            "instruction": "Lift invariant state to the event lifetime.",
            "research_target": {"mode": "new", "question": "is state shared?"},
            "model_claim_refs": ["M1"], "explanation_refs": ["E1"],
            "hypothesis_id": "H1", "evidence_refs": ["source:src/a.cc"],
            "mechanism": "avoid repeated construction",
            "prediction": "construction count falls without gate changes",
            "affected_scope": "src/a.cc:Builder",
        }],
    }
    proposal_action = _parse_scientist_action(json.dumps(raw))
    assert _validate_scientist_guard(
        session, proposal_action, tmp_path, select_quota=2,
    ) == "proposal_requires_deep_evidence"
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.cc").write_text("// source")
    session.inquiry.deep_evidence_refs.add("__source_examined__")
    assert _validate_scientist_guard(
        session, proposal_action, tmp_path, select_quota=2,
    ) is None
    assert proposal_action["proposals"][0].hypothesis_id == "H1"


def test_rollbacks_preserve_history_and_clear_only_downstream_artifacts():
    session = _history_session(InquiryPhase.DEEPEN)
    model = session.inquiry.working_model
    explanations = list(session.inquiry.explanations)
    session.inquiry.selections = [HypothesisSelection(
        "H1", ("experiment:r0c0",), "evidence",
    )]
    action = _parse_scientist_action(json.dumps({
        "action": "continue_explore", "reason": "premise failed",
        "evidence_refs": ["experiment:r0c0"],
    }))
    _apply_scientist_action(session, action, step=12)
    assert session.inquiry.phase is InquiryPhase.EXPLORE
    assert session.inquiry.history_visible is True
    assert session.inquiry.working_model is model
    assert session.inquiry.explanations == explanations
    assert session.inquiry.selections == []

    reopen = _parse_scientist_action(json.dumps({
        "action": "reopen_model", "reason": "counterfactual failed",
        "evidence_refs": ["experiment:r0c0"],
    }))
    _apply_scientist_action(session, reopen, step=13)
    assert session.inquiry.phase is InquiryPhase.MODEL
    assert session.inquiry.history_visible is True
    assert session.inquiry.working_model is None
    assert session.inquiry.explanations == []


def test_fresh_reframe_archives_context_and_resets_all_epistemic_state():
    session = _history_session(InquiryPhase.NARROW)
    old_context = session.context
    action = _parse_scientist_action(json.dumps({
        "action": "fresh_reframe", "reason": "historical frame dominates",
        "evidence_refs": ["experiment:r0c0"],
    }))
    _apply_scientist_action(session, action, step=14)
    assert session.archived_contexts == [old_context]
    assert session.inquiry.context_id == 1
    assert session.inquiry.phase is InquiryPhase.UNDERSTAND
    assert session.inquiry.history_visible is False
    assert session.inquiry.working_model is None
    assert session.runtime.new_evidence == set()
    assert _validate_scientist_guard(
        session, action, Path("."), select_quota=2,
    ) == "action_not_allowed_in_understand"
