"""Tests for simpleloop.explore.render — startup/state-header text."""
from __future__ import annotations

from simpleloop.explore.monitor import analyze_explore_health
from simpleloop.explore.models import PolicySignal, SEVERITY_CHALLENGE
from simpleloop.explore.render import (
    render_explore_for_state_header,
    render_explore_for_startup,
)
from simpleloop.memory.experiment_index import Experiment
from simpleloop.memory.models import Finding


OBJ = "SPEED_MS"


def _exp(candidate, round, parent_sha, sha, finding_id="F-001",
         objective=100.0, paths=("src/a.cc",)) -> Experiment:
    return Experiment(
        experiment_id=f"r{round}c{candidate}", round=round, candidate=candidate,
        proposal="p", parent_sha=parent_sha, candidate_sha=sha,
        status="COMPLETED", gate_passed=True, eligible=True, selected=False,
        metrics={OBJ: objective}, changed_paths=paths,
        finding_id=finding_id, eval_block="",
    )


def _stall_report():
    """A report with an active family_stall + global_stall → challenge."""
    exps = [
        _exp(0, 0, "root", "s0", finding_id=None),
        _exp(1, 1, "s0", "s1", finding_id="F-001", objective=100.0),
        _exp(2, 2, "s1", "s2", finding_id="F-002", objective=100.0),
        _exp(3, 3, "s2", "s3", finding_id="F-003", objective=100.0),
    ]
    findings = {
        f.id: f for f in [
            Finding(id="F-001", question="q1",
                    mechanisms=("hot-path-micro-optimization",),
                    code_regions=("src/a.cc:calc",), state="active",
                    created_round=1, last_touched_round=1),
            Finding(id="F-002", question="q2",
                    mechanisms=("hot path micro optimization",),
                    code_regions=("src/a.cc:calc",), state="active",
                    created_round=2, last_touched_round=2),
            Finding(id="F-003", question="q3",
                    mechanisms=("hot-path-micro-optimization",),
                    code_regions=("src/a.cc:calc",), state="active",
                    created_round=3, last_touched_round=3),
        ]
    }
    return analyze_explore_health(
        findings, exps, current_round=4, objective_key=OBJ,
        lower_is_better=True,
    )


def test_startup_render_contains_family_and_global_and_policy():
    r = _stall_report()
    text = render_explore_for_startup(r)
    assert "Explore health" in text
    assert "family" in text
    assert "consecutive_no_improve=" in text
    assert "consecutive_no_improve_rounds=" in text
    # POLICY paragraph is now informational (no submit gate).
    assert "informational only" in text
    assert "does not gate submit" in text


def test_startup_render_empty_on_first_round():
    from simpleloop.explore.models import ExploreReport
    r = ExploreReport(first_round=True, analysis_eligible=False)
    assert render_explore_for_startup(r) == ""
    assert render_explore_for_startup(None) == ""


def test_state_header_is_compact():
    r = _stall_report()
    text = render_explore_for_state_header(r)
    assert "family_stall" in text
    assert "global_stall" in text
    assert "challenge_required: true" in text
    # No long prose paragraph leaks into the header.
    assert "POLICY:" not in text


def test_state_header_empty_when_nothing_active():
    from simpleloop.explore.models import ExploreReport
    r = ExploreReport(first_round=False, analysis_eligible=True)
    assert render_explore_for_state_header(r) == ""
    assert render_explore_for_state_header(None) == ""


def test_render_handles_manual_report_with_signal_only():
    # A hand-built report (no experiments) still renders its signals.
    from simpleloop.explore.models import (
        ExploreReport, FamilyExploreHealth, GlobalExploreHealth,
    )
    fam = FamilyExploreHealth(
        family_id="src/x.cc::m", code_region="src/x.cc",
        mechanisms=("m",), finding_ids=("F-001",),
        attempts=4, evaluable_attempts=4, implementation_failures=0,
        improvements=0, neutral=4, regressions=0, selected=0,
        consecutive_no_improve=4, recent_rounds=(1, 2, 3, 4),
        policy_signals=(PolicySignal(
            name="family_stall", active=True,
            rule="consecutive_no_improve >= 3",
            severity=SEVERITY_CHALLENGE,
        ),),
    )
    gh = GlobalExploreHealth(
        attempts=4, evaluable_attempts=4, recent_window=5,
        recent_improvements=0, recent_neutral=4, recent_regressions=0,
        consecutive_no_improve_rounds=4, recent_mechanisms=("m",),
        policy_signals=(PolicySignal(
            name="global_stall", active=True,
            rule="consecutive_no_improve_rounds >= 3",
            severity=SEVERITY_CHALLENGE,
        ),),
    )
    r = ExploreReport(
        first_round=False, analysis_eligible=True,
        families=(fam,), global_health=gh,
        challenge_required=True,
        challenge_reasons=("family_stall:src/x.cc::m", "global_stall"),
    )
    text = render_explore_for_startup(r)
    assert "src/x.cc::m" in text
    assert "challenge_required" not in text  # startup uses POLICY, not the tag
    header = render_explore_for_state_header(r)
    assert "challenge_required: true" in header
