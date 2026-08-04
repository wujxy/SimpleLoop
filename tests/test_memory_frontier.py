"""Tests for the Research Frontier."""
from __future__ import annotations

from simpleloop.memory.experiment_index import Experiment
from simpleloop.memory.frontier import compute_frontier
from simpleloop.memory.models import Finding


def _finding(fid: str, *, state: str = "active",
             mechanisms=(), code_regions=(),
             last_touched_round: int = 0,
             experiment_refs=()) -> Finding:
    return Finding(
        id=fid, question=f"q for {fid}",
        mechanisms=tuple(mechanisms), code_regions=tuple(code_regions),
        state=state, created_round=0,
        last_touched_round=last_touched_round,
        experiment_refs=tuple(experiment_refs),
        parent_finding_id=None, stats={},
    )


def _exp(eid: str, *, paths=("src/a.cc",)) -> Experiment:
    return Experiment(
        experiment_id=eid,
        round=int(eid[1:eid.index("c")]),
        candidate=int(eid[eid.index("c") + 1:]),
        proposal="", parent_sha="p", candidate_sha=None, status="COMPLETED",
        gate_passed=True, eligible=True, selected=False,
        metrics={}, changed_paths=tuple(paths), finding_id=None,
        eval_block="",
    )


def test_frontier_marks_stale_active_as_dormant():
    findings = {
        "F-001": _finding("F-001", state="active", last_touched_round=8),
        "F-002": _finding("F-002", state="active", last_touched_round=1),
        "F-003": _finding("F-003", state="dormant", last_touched_round=0),
    }
    frontier = compute_frontier(
        findings, [], current_round=10, dormancy_rounds=3,
    )
    active_ids = {entry["id"] for entry in frontier["active_findings"]}
    # F-001 last touched at round 8, current=10 (idle 2 <= dormancy 3) => active.
    # F-002 last touched at round 1, current=10 (idle 9) => dormant.
    # F-003 already stored dormant.
    assert active_ids == {"F-001"}
    assert frontier["dormant_count"] == 2


def test_coverage_uses_editable_prefixes_when_supplied():
    experiments = [
        _exp("r0c0", paths=("OMILREC/src/A.cc",)),
        _exp("r1c0", paths=("OMILREC/src/B.cc",)),
        _exp("r2c0", paths=("Config/src/JSON.cc",)),
    ]
    frontier = compute_frontier(
        {}, experiments, current_round=3, dormancy_rounds=3,
        editable_prefixes=("OMILREC/", "Config/", "Unused/"),
    )
    coverage = frontier["coverage"]["code_regions"]
    assert coverage["OMILREC/"] == 2
    assert coverage["Config/"] == 1
    assert coverage["Unused/"] == 0  # gap visible


def test_coverage_falls_back_to_bucket_prefix_without_editable():
    experiments = [
        _exp("r0c0", paths=("OMILREC/src/A.cc",)),
        _exp("r1c0", paths=("OMILREC/src/B.cc",)),
    ]
    frontier = compute_frontier(
        {}, experiments, current_round=1, dormancy_rounds=3,
    )
    assert frontier["coverage"]["code_regions"]["OMILREC/src"] == 2


def test_mechanism_coverage_derived_from_findings():
    findings = {
        "F-001": _finding("F-001", mechanisms=("hoist", "cache")),
        "F-002": _finding("F-002", mechanisms=("cache",)),
    }
    frontier = compute_frontier(
        findings, [], current_round=0, dormancy_rounds=3,
    )
    assert frontier["coverage"]["mechanisms"] == {"cache": 2, "hoist": 1}
