"""Tests for simpleloop.explore.classify — objective classification."""
from __future__ import annotations

from simpleloop.explore.classify import (
    classify_experiments,
    classify_objective,
    jaccard_overlap,
    sha_objective_map,
    tokenize,
)
from simpleloop.memory.experiment_index import Experiment


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


def test_unresolvable_parent_is_unclassified_not_fake_improvement():
    # Parent "root" never produced a sha row → cannot resolve parent objective.
    exps = [_exp(1, round=0, parent_sha="root", sha="s1", objective=50.0)]
    cls = classify_experiments(exps, OBJ, lower_is_better=True)
    assert len(cls) == 1
    assert cls[0].kind == "unclassified"
    assert cls[0].parent_objective is None
    assert cls[0].eligible is True


def test_direction_awareness_flips_improvement_and_regression():
    # parent=100, candidate=50.
    parent = _exp(0, round=0, parent_sha="root", sha="p0", objective=100.0)
    child = _exp(1, round=1, parent_sha="p0", sha="c1", objective=50.0)
    lower = classify_experiments([parent, child], OBJ, lower_is_better=True)
    higher = classify_experiments([parent, child], OBJ, lower_is_better=False)
    lower_kind = [c for c in lower if c.candidate_sha == "c1"][0].kind
    higher_kind = [c for c in higher if c.candidate_sha == "c1"][0].kind
    assert lower_kind == "improvement"
    assert higher_kind == "regression"


def test_float_noise_within_eps_is_neutral():
    parent = _exp(0, round=0, parent_sha="root", sha="p0", objective=100.0)
    # delta well within eps (~1e-7 relative + 1e-9 absolute).
    child = _exp(1, round=1, parent_sha="p0", sha="c1", objective=100.0 + 1e-10)
    cls = classify_experiments([parent, child], OBJ, lower_is_better=True)
    assert [c for c in cls if c.candidate_sha == "c1"][0].kind == "neutral"


def test_gate_failed_not_classified_as_regression():
    parent = _exp(0, round=0, parent_sha="root", sha="p0", objective=100.0)
    # Ineligible (gate failed) even though objective regressed: feasibility,
    # not a mechanism refutation.
    fail = _exp(1, round=1, parent_sha="p0", sha="c1", objective=200.0,
                gate=False, eligible=False)
    cls = classify_experiments([parent, fail], OBJ, lower_is_better=True)
    fail_cls = [c for c in cls if c.candidate_sha == "c1"][0]
    assert fail_cls.kind == "unclassified"
    assert fail_cls.eligible is False


def test_one_record_per_experiment_in_order():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", objective=10.0),
        _exp(1, round=1, parent_sha="s0", sha="s1", objective=9.0),
        _exp(2, round=2, parent_sha="s1", sha="s2", objective=11.0),
    ]
    cls = classify_experiments(exps, OBJ, lower_is_better=True)
    assert [c.experiment_id for c in cls] == [e.experiment_id for e in exps]
    # s0's parent "root" is unresolvable → unclassified; the rest classify.
    assert cls[0].kind == "unclassified"
    assert cls[1].kind == "improvement"   # 9 < 10
    assert cls[2].kind == "regression"    # 11 > 9


def test_sha_objective_map_excludes_non_numeric():
    exps = [
        _exp(0, round=0, parent_sha="root", sha="s0", objective=10.0),
        Experiment(
            experiment_id="r0c9", round=0, candidate=9, proposal="p",
            parent_sha="root", candidate_sha="s9", status="COMPLETED",
            gate_passed=True, eligible=True, selected=False,
            metrics={OBJ: "not-a-number"}, changed_paths=("x",),
            finding_id="F-001", eval_block="",
        ),
    ]
    m = sha_objective_map(exps, OBJ)
    assert m == {"s0": 10.0}


def test_tokenize_and_jaccard_overlap():
    # The tokenizer keeps underscores, so "micro_optimization" is one token;
    # hyphens are separators. This is the blunt lexical signal used by the
    # near-duplicate check and mechanism canonicalization.
    assert tokenize("Hot-Path micro_optimization") == frozenset({
        "hot", "path", "micro_optimization",
    })
    assert jaccard_overlap("", "x") == 0.0
    assert jaccard_overlap("a b c", "a b c") == 1.0
    # Shared 3 of 5 unique tokens.
    assert jaccard_overlap("a b c", "a b c d e") == 0.6


def test_classify_objective_direct():
    assert classify_objective(50.0, 100.0, lower_is_better=True) == "improvement"
    assert classify_objective(150.0, 100.0, lower_is_better=True) == "regression"
    assert classify_objective(100.0, 100.0, lower_is_better=True) == "neutral"
    assert classify_objective(150.0, 100.0, lower_is_better=False) == "improvement"
