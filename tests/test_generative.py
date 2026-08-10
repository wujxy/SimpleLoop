from pathlib import Path

from simpleloop.roles.generative import (
    GENERATIVE_OPS,
    g_definition,
    render_generative_basis,
)
from simpleloop.roles.inquiry import (
    Explanation,
    InquiryPhase,
    LeveragePoint,
    ModelClaim,
    ScientistSessionState,
    WorkingModel,
)
from simpleloop.roles.proposer import (
    _build_phase_system_prompt,
    _parse_scientist_action,
    _validate_scientist_guard,
)


def test_generative_basis_exposes_exact_g1_to_g9():
    assert GENERATIVE_OPS == tuple(f"G{i}" for i in range(1, 10))
    assert "Cross-domain" in g_definition("G1")
    assert "scale" in g_definition("G9").lower()
    assert "G2" in render_generative_basis(("G2",))
    assert "G1" not in render_generative_basis(("G2",))



def _explore_session(*, with_lever):
    session = ScientistSessionState.fresh()
    session.inquiry.phase = InquiryPhase.EXPLORE
    session.inquiry.working_model = WorkingModel(
        version=1, representation="cost", explanatory_structure="frequency",
        claims=(ModelClaim("M1", "calls repeat", ("source:a",)),),
        important_unknowns=(),
    )
    session.inquiry.explanations = [Explanation(
        id="E1", phenomenon="gap", account="repetition",
        model_basis=("M1",), expected_if_true=("repeat",),
        evidence_needed=("trace",),
    )]
    if with_lever:
        session.inquiry.lever_map = [LeveragePoint(
            id="L1", target_mechanism="repetition",
            why_leverage_exists="frequency multiplies cost",
            model_basis=("M1",), explanation_basis=("E1",),
        )]
    return session


def _hypothesis(op):
    return _parse_scientist_action(
        '{"action":"submit_hypothesis","id":"H1",'
        f'"generative_op":"{op}",'
        '"model_basis":["M1"],"explanation_basis":["E1"],'
        '"mechanism":"reuse","intervention_family":"lifetime",'
        '"scope":"whole flow","why_plausible":"state repeats",'
        '"critical_unknown":"whether state is invariant"}'
    )


def test_hypothesis_requires_lever_map_and_assigned_operator():
    assert _validate_scientist_guard(
        _explore_session(with_lever=False), _hypothesis("G2"), Path("."), 2,
        assigned_ops=("G2",),
    ) == "lever_map_required"
    assert _validate_scientist_guard(
        _explore_session(with_lever=True), _hypothesis("G9"), Path("."), 2,
        assigned_ops=("G2",),
    ) == "unassigned_generative_op"


def test_basis_is_absent_from_understand_and_present_in_explore():
    understand = _build_phase_system_prompt(
        None, InquiryPhase.UNDERSTAND, False, assigned_ops=("G2",),
    )
    explore = _build_phase_system_prompt(
        None, InquiryPhase.EXPLORE, False, assigned_ops=("G2",),
    )
    assert "G2 —" not in understand
    assert "G2 —" in explore
    assert "G1 —" not in explore
