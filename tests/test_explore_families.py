"""Tests for simpleloop.explore.families — family key & canonicalization."""
from __future__ import annotations

from simpleloop.explore.families import (
    UNKNOWN_MECHANISM,
    UNKNOWN_REGION,
    FamilyKey,
    assign_families,
    canonicalize_mechanisms,
    mechanism_bucket_for,
    normalize_region,
    region_bucket_for,
)
from simpleloop.memory.experiment_index import Experiment
from simpleloop.memory.models import Finding


def _finding(fid="F-001", mechanisms=(), code_regions=(), round=0) -> Finding:
    return Finding(
        id=fid, question="Q?", mechanisms=mechanisms, code_regions=code_regions,
        state="active", created_round=round, last_touched_round=round,
    )


def _exp(candidate, round, sha, fid, *, paths=("src/a.cc",)) -> Experiment:
    return Experiment(
        experiment_id=f"r{round}c{candidate}", round=round, candidate=candidate,
        proposal="p", parent_sha="root", candidate_sha=sha, status="COMPLETED",
        gate_passed=True, eligible=True, selected=False, metrics={},
        changed_paths=paths, finding_id=fid, eval_block="",
    )


# --- normalize_region ------------------------------------------------------

def test_normalize_region_keeps_function_drops_line_number():
    assert normalize_region("/OMILREC/src/x.cc:Calculate_EVL") == \
        "OMILREC/src/x.cc:Calculate_EVL"
    assert normalize_region("OMILREC/src/x.cc:42") == "OMILREC/src/x.cc"
    assert normalize_region("OMILREC/src/x.cc") == "OMILREC/src/x.cc"
    assert normalize_region("") == UNKNOWN_REGION
    assert normalize_region("   ") == UNKNOWN_REGION


# --- region_bucket_for -----------------------------------------------------

def test_region_bucket_uses_first_code_region():
    f = _finding(code_regions=("OMILREC/src/x.cc:calc",))
    assert region_bucket_for(f, []) == "OMILREC/src/x.cc:calc"


def test_region_bucket_falls_back_to_changed_paths():
    f = _finding(code_regions=())
    exps = [
        _exp(1, 0, "s1", "F-001", paths=("OMILREC/src/x.cc", "other.py")),
        _exp(2, 1, "s2", "F-001", paths=("OMILREC/src/x.cc",)),
        _exp(3, 2, "s3", "F-001", paths=("OMILREC/src/y.cc",)),
    ]
    # "OMILREC/src/x.cc" appears twice (most frequent).
    assert region_bucket_for(f, exps) == "OMILREC/src/x.cc"


def test_region_bucket_unknown_when_nothing_available():
    assert region_bucket_for(None, []) == UNKNOWN_REGION
    f = _finding(code_regions=())
    assert region_bucket_for(f, [_exp(1, 0, "s1", "F-001", paths=())]) == \
        UNKNOWN_REGION


# --- canonicalize_mechanisms ----------------------------------------------

def test_canonicalize_merges_synonym_wording():
    canon = canonicalize_mechanisms([
        "hot-path-micro-optimization",
        "hot path micro optimization",
        "algorithm-replacement",
    ])
    assert canon["hot-path-micro-optimization"] == \
        canon["hot path micro optimization"]
    assert canon["algorithm-replacement"] != \
        canon["hot-path-micro-optimization"]


def test_canonicalize_below_threshold_stays_separate():
    # Token sets {a, b} and {c, d}: Jaccard 0 → separate buckets.
    canon = canonicalize_mechanisms(["alpha-beta", "gamma-delta"])
    assert canon["alpha-beta"] != canon["gamma-delta"]


def test_canonicalize_transitive_chain():
    # a/b share enough, b/c share enough, but a/c may not directly — union-find
    # must collapse all three.
    canon = canonicalize_mechanisms([
        "loop-invariant-hoisting",
        "loop invariant hoisting refactor",
        "invariant hoisting refactor loop",
    ])
    labels = set(canon.values())
    assert len(labels) == 1


def test_canonicalize_empty():
    assert canonicalize_mechanisms([]) == {}
    assert canonicalize_mechanisms(["", "  "]) == {}


# --- mechanism_bucket_for -------------------------------------------------

def test_mechanism_bucket_unknown_when_empty():
    assert mechanism_bucket_for(_finding(mechanisms=()), {}) == UNKNOWN_MECHANISM


def test_mechanism_bucket_uses_canonical_label():
    canon = {"hot-path-micro-optimization": "hot path micro optimization"}
    f = _finding(mechanisms=("hot-path-micro-optimization",))
    assert mechanism_bucket_for(f, canon) == "hot path micro optimization"


# --- assign_families -------------------------------------------------------

def test_assign_families_same_region_synonym_mechanism_one_family():
    findings = {
        "F-001": _finding("F-001", mechanisms=("hot-path-micro-optimization",),
                          code_regions=("src/a.cc:calc",), round=1),
        "F-002": _finding("F-002", mechanisms=("hot path micro optimization",),
                          code_regions=("src/a.cc:calc",), round=2),
    }
    keys = assign_families(findings, [])
    assert keys["F-001"].family_id == keys["F-002"].family_id
    assert keys["F-001"].region_bucket == "src/a.cc:calc"


def test_assign_families_different_regions_never_merge():
    findings = {
        "F-001": _finding("F-001", mechanisms=("m",),
                          code_regions=("src/a.cc",), round=1),
        "F-002": _finding("F-002", mechanisms=("m",),
                          code_regions=("src/b.cc",), round=2),
    }
    keys = assign_families(findings, [])
    assert keys["F-001"].family_id != keys["F-002"].family_id


def test_assign_families_same_region_disjoint_mechanism_different_family():
    # Same region, mechanisms with token sets that do not overlap.
    findings = {
        "F-001": _finding("F-001", mechanisms=("alpha-beta",),
                          code_regions=("src/a.cc",), round=1),
        "F-002": _finding("F-002", mechanisms=("gamma-delta",),
                          code_regions=("src/a.cc",), round=2),
    }
    keys = assign_families(findings, [])
    assert keys["F-001"].family_id != keys["F-002"].family_id


def test_assign_families_family_key_is_frozen_hashable():
    k = FamilyKey(region_bucket="x", mechanism_bucket="y")
    assert k.family_id == "x::y"
    # Frozen dataclass must be hashable / usable as dict key.
    d = {k: 1}
    assert d[FamilyKey("x", "y")] == 1

# --- changed_paths is the primary region dimension (anti-bypass) ----------

def test_changed_paths_override_code_regions_tag():
    """The proposer controls the code_regions tag and can rename it; the actual
    changed_paths (deterministic file paths it cannot rename) must win.
    """
    finding = _finding("F-001", mechanisms=("m",),
                       code_regions=("src/renamed-tag.cc",), round=1)
    exps = [_exp(0, 1, "s1", "F-001", paths=("src/real.cc",))]
    assert region_bucket_for(finding, exps) == "src/real.cc"


def test_code_regions_tag_used_only_when_no_experiments():
    """Before any experiment runs, the tag is the only signal available."""
    finding = _finding("F-001", mechanisms=("m",),
                       code_regions=("src/tag.cc::fn",), round=1)
    assert region_bucket_for(finding, []) == "src/tag.cc::fn"


def test_renamed_code_regions_tag_does_not_escape_same_region():
    """Two findings with differently-worded code_regions tags but the same
    actual changed_paths file must land in the same family.
    """
    findings = {
        "F-001": _finding("F-001", mechanisms=("alpha",),
                          code_regions=("src/clever-name.cc",), round=1),
        "F-002": _finding("F-002", mechanisms=("alpha",),
                          code_regions=("src/different-name.cc",), round=2),
    }
    exps = [
        _exp(0, 1, "s1", "F-001", paths=("src/actual.cc",)),
        _exp(1, 2, "s2", "F-002", paths=("src/actual.cc",)),
    ]
    keys = assign_families(findings, exps)
    assert keys["F-001"].region_bucket == "src/actual.cc"
    assert keys["F-002"].region_bucket == "src/actual.cc"
    assert keys["F-001"].family_id == keys["F-002"].family_id
