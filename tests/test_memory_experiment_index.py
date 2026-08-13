"""Tests for the structured Experiment Index."""
from __future__ import annotations

from proposer.memory.experiment_index import (
    build_experiments,
    filter_experiments,
)


def _row(round_id: int, *, cid: int = 0, sha: str = "sha",
         gate: bool = True, sel: bool = False, elig: bool = True,
         fid: str | None = None, paths=("src/a.cc",),
         proposal: str = "proposal", status: str = "COMPLETED") -> dict:
    return {
        "round": round_id,
        "parent_sha": "parent",
        "candidates": [{
            "candidate": cid,
            "experiment_id": f"r{round_id}c{cid}",
            "finding_id": fid,
            "proposal": proposal,
            "parent_sha": "parent",
            "sha": sha,
            "status": status,
            "eval_block": "",
            "metrics": {"SPEED_MS": 100 - round_id},
            "changed_paths": list(paths),
            "gates": {},
            "gate_passed": gate,
            "eligible": elig,
            "selected": sel,
        }],
    }


def test_build_experiments_projects_history():
    history = [_row(0, fid="F-001"), _row(1, fid="F-002", sel=True)]
    experiments = build_experiments(history)
    assert [e.experiment_id for e in experiments] == ["r0c0", "r1c0"]
    assert experiments[0].finding_id == "F-001"
    assert experiments[1].selected is True


def test_filters_stack_as_and():
    experiments = build_experiments([
        _row(0, fid="F-001", gate=True, sel=True, paths=("src/a.cc",)),
        _row(1, fid="F-002", gate=False, sel=False, paths=("src/b.cc",)),
        _row(2, fid="F-001", gate=True, sel=False, paths=("src/a/nested.cc",)),
    ])
    passed = filter_experiments(experiments, gate_passed=True)
    assert {e.experiment_id for e in passed} == {"r0c0", "r2c0"}
    same_finding = filter_experiments(experiments, finding_id="F-001")
    assert {e.experiment_id for e in same_finding} == {"r0c0", "r2c0"}
    on_a = filter_experiments(experiments, changed_path="src/a")
    assert {e.experiment_id for e in on_a} == {"r0c0", "r2c0"}
    round_bound = filter_experiments(experiments, round_min=1, round_max=1)
    assert {e.experiment_id for e in round_bound} == {"r1c0"}


def test_build_falls_back_to_synthetic_id_when_missing():
    row = _row(0)
    del row["candidates"][0]["experiment_id"]
    exp = build_experiments([row])[0]
    assert exp.experiment_id == "r0c0"
