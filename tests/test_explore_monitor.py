"""Tests for simpleloop.explore.monitor — the search-health report."""
from __future__ import annotations

from simpleloop.explore.monitor import analyze_explore_health
from simpleloop.memory.experiment_index import Experiment
from simpleloop.memory.models import Finding


OBJ = "SPEED_MS"


def _exp(candidate, *, round, parent_sha, sha, finding_id="F-001",
         gate=True, eligible=True, selected=False, objective=100.0,
         paths=("src/a.cc",)) -> Experiment:
    return Experiment(
        experiment_id=f"r{round}c{candidate}",
        round=round, candidate=candidate, proposal="p",
        parent_sha=parent_sha, candidate_sha=sha, status="COMPLETED",
        gate_passed=gate, eligible=eligible, selected=selected,
        metrics={OBJ: objective}, changed_paths=paths,
        finding_id=finding_id, eval_block="",
    )


def _finding(fid="F-001", *, mechanisms=(), code_regions=(), round=0,
             question="Q?") -> Finding:
    return Finding(
        id=fid, question=question, mechanisms=mechanisms,
        code_regions=code_regions, state="active", created_round=round,
        last_touched_round=round,
    )


def _report(experiments, *, findings=None, current_round=10,
            lower_is_better=True, objective_key=OBJ):
    return analyze_explore_health(
        {f.id: f for f in (findings or [_finding()])},
        experiments, current_round=current_round,
        objective_key=objective_key, lower_is_better=lower_is_better,
    )


def _signal(obj, name):
    for s in obj.policy_signals:
        if s.name == name:
            return s
    return None


def _family(report, family_id_contains=None):
    for f in report.families:
        if family_id_contains is None or family_id_contains in f.family_id:
            return f
    return None


# --- first_round / analysis_eligible --------------------------------------

def test_first_round_when_no_experiments():
    r = _report([])
    assert r.first_round is True
    assert r.analysis_eligible is False
    assert r.global_health is None
    assert r.challenge_required is False


def test_missing_objective_key_marks_not_eligible():
    # Has experiments but no objective key → analysis_eligible False, signals
    # inactive, no challenge.
    exps = [_exp(1, round=0, parent_sha="root", sha="s1")]
    r = analyze_explore_health(
        {"F-001": _finding()}, exps, current_round=2,
        objective_key=None, lower_is_better=True,
    )
    assert r.first_round is False
    assert r.analysis_eligible is False
    assert r.challenge_required is False
    # Fact counts are still present.
    assert r.findings and r.findings[0].attempts == 1
    assert all(not s.active for s in r.findings[0].policy_signals)


# --- per-finding parity ----------------------------------------------------

def test_two_neutrals_trigger_mechanism_challenge_but_not_challenge_required():
    # baseline + 2 neutral attempts in one finding.
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", objective=100.0),
        _exp(1, round=1, parent_sha="s0", sha="s1", objective=100.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", objective=100.0),
    ]
    r = _report(exps)
    fh = r.findings[0]
    assert _signal(fh, "mechanism_challenge").active is True
    assert r.challenge_required is False  # per-finding never escalates


def test_per_finding_feasibility_risk_and_contradictory():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", objective=100.0),
        _exp(1, round=1, parent_sha="s0", sha="s1", objective=100.0,
             gate=False, eligible=False),   # impl failure #1
        _exp(2, round=1, parent_sha="s0", sha="s1b", objective=100.0,
             gate=False, eligible=False),   # impl failure #2
        _exp(3, round=2, parent_sha="s0", sha="s3", objective=200.0),  # regress
    ]
    r = _report(exps, findings=[_finding(code_regions=("src/uniq.cc",))])
    fh = r.findings[0]
    assert _signal(fh, "feasibility_risk").active is True
    assert _signal(fh, "contradictory_result").active is True


# --- family stall (the core anti-bypass case) -----------------------------

def test_family_stall_across_new_findings_each_round():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", finding_id="F-001",
             objective=100.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", finding_id="F-002",
             objective=100.0),
        _exp(3, round=3, parent_sha="s2", sha="s3", finding_id="F-003",
             objective=100.0),
    ]
    findings = [
        _finding("F-001", mechanisms=("hot-path-micro-optimization",),
                 code_regions=("src/a.cc:calc",), round=1),
        _finding("F-002", mechanisms=("hot path micro optimization",),
                 code_regions=("src/a.cc:calc",), round=2),
        _finding("F-003", mechanisms=("hot-path-micro-optimization",),
                 code_regions=("src/a.cc:calc",), round=3),
    ]
    r = _report(exps, findings=findings)
    fam = _family(r, "calc")
    assert fam is not None
    assert fam.consecutive_no_improve == 3
    assert _signal(fam, "family_stall").active is True
    assert r.challenge_required is True
    assert any("family_stall" in reason for reason in r.challenge_reasons)


def test_alternating_families_do_not_trigger_single_family_stall():
    # Two distinct regions; each family only accumulates 2 no-improve attempts
    # (below the threshold of 3) — so neither family fires family_stall. The
    # *global* view may still fire global_stall (it sees the neutral chain
    # regardless of which family); that is tested separately.
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", finding_id="F-A",
             objective=100.0, paths=("src/a.cc",)),
        _exp(2, round=2, parent_sha="s1", sha="s2", finding_id="F-B",
             objective=100.0, paths=("src/b.cc",)),
        _exp(3, round=3, parent_sha="s2", sha="s3", finding_id="F-A",
             objective=100.0, paths=("src/a.cc",)),
        _exp(4, round=4, parent_sha="s3", sha="s4", finding_id="F-B",
             objective=100.0, paths=("src/b.cc",)),
    ]
    findings = [
        _finding("F-A", code_regions=("src/a.cc",), round=1),
        _finding("F-B", code_regions=("src/b.cc",), round=2),
    ]
    r = _report(exps, findings=findings)
    assert len(r.families) == 2
    for fam in r.families:
        assert fam.consecutive_no_improve <= 2
        assert _signal(fam, "family_stall").active is False
        assert _signal(fam, "family_regressing").active is False


def test_improvement_resets_family_consecutive():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", finding_id="F-001",
             objective=100.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", finding_id="F-001",
             objective=100.0),
        _exp(3, round=3, parent_sha="s2", sha="s3", finding_id="F-001",
             objective=90.0),  # improvement
    ]
    findings = [_finding("F-001", code_regions=("src/a.cc",))]
    r = _report(exps, findings=findings)
    fam = _family(r)
    assert fam.consecutive_no_improve == 0
    assert _signal(fam, "family_stall").active is False


def test_improvement_then_two_neutrals_counts_two():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", finding_id="F-001",
             objective=90.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", finding_id="F-001",
             objective=90.0),
        _exp(3, round=3, parent_sha="s2", sha="s3", finding_id="F-001",
             objective=90.0),
    ]
    findings = [_finding("F-001", code_regions=("src/a.cc",))]
    r = _report(exps, findings=findings)
    fam = _family(r)
    assert fam.consecutive_no_improve == 2
    assert _signal(fam, "family_stall").active is False


def test_unclassified_neither_resets_nor_increments_family():
    # neutral, unclassified (unresolvable parent), neutral → count 2, not 3.
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", finding_id="F-001",
             objective=100.0),  # neutral
        _exp(2, round=2, parent_sha="ghost", sha="s2", finding_id="F-001",
             objective=100.0),  # parent "ghost" unresolvable → unclassified
        _exp(3, round=3, parent_sha="s2", sha="s3", finding_id="F-001",
             objective=100.0),  # neutral
    ]
    findings = [_finding("F-001", code_regions=("src/a.cc",))]
    r = _report(exps, findings=findings)
    fam = _family(r)
    assert fam.consecutive_no_improve == 2


def test_family_regressing_active():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", finding_id="F-001",
             objective=120.0),  # regress
        _exp(2, round=2, parent_sha="s1", sha="s2", finding_id="F-001",
             objective=130.0),  # regress
    ]
    findings = [_finding("F-001", code_regions=("src/a.cc",))]
    r = _report(exps, findings=findings)
    fam = _family(r)
    assert _signal(fam, "family_regressing").active is True
    assert r.challenge_required is True


def test_family_feasibility_on_repeated_gate_failures():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", finding_id="F-001",
             gate=False, eligible=False),
        _exp(2, round=2, parent_sha="s0", sha="s2", finding_id="F-001",
             gate=False, eligible=False),
    ]
    findings = [_finding("F-001", code_regions=("src/a.cc",))]
    r = _report(exps, findings=findings)
    fam = _family(r)
    assert _signal(fam, "family_feasibility").active is True
    # feasibility is watch severity → does not alone trigger challenge.
    assert r.challenge_required is False


def test_family_overexploited_watch():
    # 5 attempts, none selected, but all improvements so no stall/regress.
    exps = [_exp(0, round=0, parent_sha="root", sha="s0", finding_id=None)]
    for i in range(1, 6):
        exps.append(_exp(i, round=i, parent_sha=f"s{i-1}", sha=f"s{i}",
                         finding_id="F-001", objective=100.0 - i, selected=False))
    findings = [_finding("F-001", code_regions=("src/a.cc",))]
    r = _report(exps, findings=findings)
    fam = _family(r)
    assert _signal(fam, "family_overexploited").active is True
    assert r.challenge_required is False


# --- global ----------------------------------------------------------------

def test_global_stall_run_length_and_reset():
    # 3 neutral rounds in a row → global_stall.
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", objective=100.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", objective=100.0),
        _exp(3, round=3, parent_sha="s2", sha="s3", objective=100.0),
    ]
    r = _report(exps)
    assert r.global_health.consecutive_no_improve_rounds == 3
    assert _signal(r.global_health, "global_stall").active is True
    assert r.challenge_required is True


def test_global_round_with_improvement_resets():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", objective=100.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", objective=90.0),  # improve
        _exp(3, round=3, parent_sha="s2", sha="s3", objective=90.0),  # neutral
    ]
    r = _report(exps)
    assert r.global_health.consecutive_no_improve_rounds == 1
    assert _signal(r.global_health, "global_stall").active is False


def test_global_regression_run_active():
    # Need >= 3 recent regressions and 0 recent improvements within window.
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", objective=110.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", objective=120.0),
        _exp(3, round=3, parent_sha="s2", sha="s3", objective=130.0),
    ]
    r = _report(exps)
    assert _signal(r.global_health, "global_regression_run").active is True


def test_challenge_reasons_include_family_and_global():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", finding_id="F-001",
             objective=100.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", finding_id="F-001",
             objective=100.0),
        _exp(3, round=3, parent_sha="s2", sha="s3", finding_id="F-001",
             objective=100.0),
    ]
    findings = [_finding("F-001", code_regions=("src/a.cc",))]
    r = _report(exps, findings=findings)
    assert any(s.startswith("family_stall:") for s in r.challenge_reasons)
    assert "global_stall" in r.challenge_reasons


def test_report_to_dict_round_trips():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", finding_id=None),
        _exp(1, round=1, parent_sha="s0", sha="s1", finding_id="F-001",
             objective=100.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", finding_id="F-001",
             objective=100.0),
        _exp(3, round=3, parent_sha="s2", sha="s3", finding_id="F-001",
             objective=100.0),
    ]
    findings = [_finding("F-001", code_regions=("src/a.cc",))]
    r = _report(exps, findings=findings)
    d = r.to_dict()
    assert d["first_round"] is False
    assert d["analysis_eligible"] is True
    assert d["challenge_required"] is True
    assert len(d["families"]) == 1
    assert d["global_health"] is not None
