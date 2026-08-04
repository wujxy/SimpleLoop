"""Tests for deterministic deliberation signals (memory/signals.py)."""
from __future__ import annotations

from simpleloop.memory.experiment_index import Experiment
from simpleloop.memory.models import Finding
from simpleloop.memory.signals import (
    compute_deliberation_signals,
    jaccard_overlap,
    tokenize,
)


OBJ = "SPEED_MS"


def _exp(candidate, *, round, parent_sha, sha, finding_id="F-001",
         gate=True, eligible=True, selected=False, objective=100.0,
         status="COMPLETED") -> Experiment:
    return Experiment(
        experiment_id=f"r{round}c{candidate}",
        round=round,
        candidate=candidate,
        proposal="p",
        parent_sha=parent_sha,
        candidate_sha=sha,
        status=status,
        gate_passed=gate,
        eligible=eligible,
        selected=selected,
        metrics={OBJ: objective},
        changed_paths=("src/a.cc",),
        finding_id=finding_id,
        eval_block="",
    )


def _finding(fid="F-001", question="Q?", round=0) -> Finding:
    return Finding(
        id=fid, question=question, mechanisms=(), code_regions=(),
        state="active", created_round=round, last_touched_round=round,
    )


def _signals(experiments, *, findings=None, hints=False, current_round=5,
             lower_is_better=True):
    return compute_deliberation_signals(
        {f.id: f for f in (findings or [_finding()])},
        experiments,
        current_round=current_round,
        hints_present=hints,
        objective_key=OBJ,
        lower_is_better=lower_is_better,
    )


def test_first_round_returns_empty():
    sig = _signals([], hints=False)
    assert sig["first_round"] is True
    assert sig["findings"] == []
    assert sig["hints_present"] is False


def test_hints_present_propagated():
    sig = _signals([_exp(0, round=0, parent_sha="base", sha="s0")], hints=True)
    assert sig["hints_present"] is True
    assert sig["first_round"] is False


def test_facts_counts_and_policy_signal_split():
    # A chain where each parent is the prior selected candidate (resolvable):
    #   round0 parent 'root' (unclassified) -> s0 obj 100, selected
    #   round1 parent s0 (100) -> s1 obj 90   improvement
    #   round2 parent s1 (90)  -> s2 obj 90   neutral
    #   round3 parent s2 (90)  -> s3 obj 90   neutral
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", objective=100.0,
             selected=True),
        _exp(0, round=1, parent_sha="s0", sha="s1", objective=90.0),
        _exp(0, round=2, parent_sha="s1", sha="s2", objective=90.0),
        _exp(0, round=3, parent_sha="s2", sha="s3", objective=90.0),
    ]
    sig = _signals(exps)
    entry = sig["findings"][0]
    facts = entry["facts"]
    assert facts["attempts"] == 4
    assert facts["evaluable_attempts"] == 4
    assert facts["eligible_improvements"] == 1
    assert facts["eligible_neutral"] == 2
    assert facts["eligible_regressions"] == 0
    assert facts["implementation_failures"] == 0
    ps = entry["policy_signals"]
    assert ps["mechanism_challenge"]["active"] is True   # 2 neutral
    assert ps["feasibility_risk"]["active"] is False
    assert ps["contradictory_result"]["active"] is False
    # facts vs policy_signals are separate sections
    assert "rule" in ps["mechanism_challenge"]


def test_gate_failures_are_feasibility_not_mechanism():
    # Two experiments that never passed gate -> feasibility_risk, NOT mechanism.
    exps = [
        _exp(0, round=0, parent_sha="base", sha="s0", gate=False,
             eligible=False, objective=100.0),
        _exp(0, round=1, parent_sha="base", sha="s1", gate=False,
             eligible=False, objective=100.0),
    ]
    sig = _signals(exps)
    entry = sig["findings"][0]
    assert entry["facts"]["implementation_failures"] == 2
    assert entry["policy_signals"]["feasibility_risk"]["active"] is True
    assert entry["policy_signals"]["mechanism_challenge"]["active"] is False


def test_regression_is_contradictory_and_direction_aware():
    # lower_is_better=True: parent 90, child 100 -> regression
    exps = [
        _exp(0, round=0, parent_sha="base", sha="s0", objective=90.0,
             selected=True),
        _exp(0, round=1, parent_sha="s0", sha="s1", objective=100.0),
    ]
    sig = _signals(exps, lower_is_better=True)
    entry = sig["findings"][0]
    assert entry["facts"]["eligible_regressions"] == 1
    assert entry["policy_signals"]["contradictory_result"]["active"] is True

    # Flip direction: higher is better -> same numbers become improvement.
    sig_hi = _signals(exps, lower_is_better=False)
    entry_hi = sig_hi["findings"][0]
    assert entry_hi["facts"]["eligible_improvements"] == 1
    assert entry_hi["facts"]["eligible_regressions"] == 0


def test_baseline_parent_is_unclassified_not_force_fit():
    # parent_sha 'base' has no candidate row -> unclassified, no fake verdict.
    exps = [_exp(0, round=0, parent_sha="base", sha="s0", objective=50.0)]
    sig = _signals(exps)
    entry = sig["findings"][0]
    # The single eligible experiment's parent is the baseline (not in the sha
    # map), so it is not counted as improvement/neutral/regression.
    total = (entry["facts"]["eligible_improvements"]
             + entry["facts"]["eligible_neutral"]
             + entry["facts"]["eligible_regressions"])
    assert total == 0


def test_findings_ordered_signals_first_then_attempts():
    f_a = _finding("F-A", "a", round=0)
    f_b = _finding("F-B", "b", round=0)
    exps = [
        # F-B: mechanism_challenge (2 neutral)
        _exp(0, round=0, parent_sha="base", sha="s0", finding_id="F-B",
             objective=90.0, selected=True),
        _exp(0, round=1, parent_sha="s0", sha="s1", finding_id="F-B",
             objective=90.0),
        _exp(0, round=2, parent_sha="s1", sha="s2", finding_id="F-B",
             objective=90.0),
        # F-A: healthy improvement, no signal
        _exp(0, round=0, parent_sha="base", sha="t0", finding_id="F-A",
             objective=80.0, selected=True),
    ]
    sig = _signals(exps, findings=[f_a, f_b])
    order = [e["id"] for e in sig["findings"]]
    assert order[0] == "F-B"  # has an active policy signal


def test_tokenize_and_jaccard():
    assert tokenize("Hoist the Cache!") == frozenset({"hoist", "the", "cache"})
    assert jaccard_overlap("", "x") == 0.0
    assert jaccard_overlap("a b c", "a b c") == 1.0
    assert 0.0 < jaccard_overlap("a b c", "b c d") < 1.0
