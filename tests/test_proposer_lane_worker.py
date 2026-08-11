"""Unit tests for the proposer-lane worker + HEPJobBackend lane plumbing.

These cover the pieces that don't need a live condor pool: the manifest
roundtrip, the proposal serialization inverse, the lane-result validator, and
the inflight orphan cleanup. The submit/supervise/collect loop itself mirrors
the candidate pipeline (covered by test_hepjob_backend's candidate path) and
needs a real scheduler to exercise end-to-end.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.execution.hepjob import HEPJobBackend, _Job
from simpleloop.memory.models import (
    ExistingFindingTarget, NewFindingTarget, ResearchProposal,
)
from simpleloop.proposer_lane_worker import (
    ProposerLaneSpec, _failure_result, _proposal_to_dict, proposal_from_dict,
)


# --- manifest roundtrip ---------------------------------------------------

def test_proposer_lane_spec_roundtrip():
    spec = ProposerLaneSpec(
        lane_id=2, round_id=3, base_sha="abc123",
        run_dir="/run", workspace_path="/run/lanes/lane-2/workspace",
        result_dir="/run/rounds/r3/lanes/l2",
        prompt_dir="/p", assigned_ops=["G1", "G4"], select_quota=2,
        gen_steps=50, cognitive_steps=30, attempt=1,
    )
    rebuilt = ProposerLaneSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert rebuilt == spec


def test_proposer_lane_spec_rejects_unknown_fields():
    with pytest.raises(ValueError, match="unknown proposer-lane manifest"):
        ProposerLaneSpec.from_dict({"lane_id": 0, "round_id": 0, "base_sha": "x",
                                    "surprise": 1})


# --- proposal serialization inverse ---------------------------------------

@pytest.mark.parametrize("target", [
    NewFindingTarget(question="is A faster?", mechanisms=("m1", "m2"),
                     code_regions=("src/a",)),
    ExistingFindingTarget(finding_id="F-007"),
    NewFindingTarget(question="q"),
])
def test_proposal_roundtrip(target):
    original = ResearchProposal(
        instruction="try hoisting the inner loop",
        research_target=target,
        evidence_refs=("experiment:r0c0", "source:src/a.cc:fn"),
        material_difference="new baseline",
    )
    # worker writes via asdict; backend rebuilds via proposal_from_dict
    as_json = json.loads(json.dumps(_proposal_to_dict(original)))
    rebuilt = proposal_from_dict(as_json)
    assert rebuilt.instruction == original.instruction
    assert rebuilt.evidence_refs == original.evidence_refs
    assert rebuilt.material_difference == original.material_difference
    assert rebuilt.research_target == target


def test_failure_result_shape():
    spec = ProposerLaneSpec(lane_id=1, round_id=0, base_sha="x")
    result = _failure_result(spec, "boom")
    assert result["status"] == "LANE_FAILED"
    assert result["lane_id"] == 1
    assert result["outcome"] == "error"
    assert result["proposals"] == []
    assert "boom" in result["explanation"]


# --- HEPJobBackend._read_lane_result --------------------------------------

def _make_backend(tmp_path: Path) -> HEPJobBackend:
    class _Ctx:
        run_dir = tmp_path
        cfg = {"metrics": None}
        telemetry = None
        workspace = None
        prompt_dir = None
    hep_cfg = {"remove_cmd": "true", "submit_cmd": "true",
               "query_cmd": "true", "collector": None, "schedd_name": None}
    return HEPJobBackend(_Ctx(), hep_cfg)


def test_read_lane_result_accepts_lane_shaped_result(tmp_path: Path):
    backend = _make_backend(tmp_path)
    job = _Job(candidate_id=0, worktree_id="0",
               result_dir=tmp_path / "l0")
    job.result_dir.mkdir()
    (job.result_dir / "result.json").write_text(json.dumps({
        "status": "COMPLETED", "lane_id": 0, "outcome": "submit",
        "proposals": [{"instruction": "x",
                       "research_target": {"question": "q"},
                       "evidence_refs": [], "material_difference": None}],
    }), encoding="utf-8")
    result = HEPJobBackend._read_lane_result(job)
    assert result["outcome"] == "submit"
    assert len(result["proposals"]) == 1


def test_read_lane_result_rejects_candidate_shaped_result(tmp_path: Path):
    backend = _make_backend(tmp_path)  # noqa: F841 (exercise construction)
    job = _Job(candidate_id=0, worktree_id="0",
               result_dir=tmp_path / "l1")
    job.result_dir.mkdir()
    # A CANDIDATE result (gates/gate_passed/eligible, no proposals) must be
    # rejected — it's the wrong shape for a proposer lane.
    (job.result_dir / "result.json").write_text(json.dumps({
        "status": "COMPLETED", "gates": {}, "gate_passed": True,
        "eligible": False,
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        HEPJobBackend._read_lane_result(job)


# --- cleanup_proposer_orphans ---------------------------------------------

def test_cleanup_proposer_orphans_kills_listed_jobs_and_clears_marker(
    monkeypatch, tmp_path: Path,
):
    backend = _make_backend(tmp_path)
    removed: list[list[str]] = []
    monkeypatch.setattr(
        "simpleloop.execution.hepjob.subprocess.run",
        lambda argv, **_k: removed.append(argv) or _completed(),
    )
    (tmp_path / "inflight_proposer.json").write_text(json.dumps({
        "round_id": 1, "base_sha": "x",
        "lane_job_ids": ["100.0", "101.0"],
    }), encoding="utf-8")

    backend.cleanup_proposer_orphans()

    # both job ids were passed to the remove command
    targeted = [argv[-1] for argv in removed]
    assert targeted == ["100.0", "101.0"]
    assert not (tmp_path / "inflight_proposer.json").exists()


def test_cleanup_proposer_orphans_noop_without_marker(tmp_path: Path):
    backend = _make_backend(tmp_path)
    assert not (tmp_path / "inflight_proposer.json").exists()
    backend.cleanup_proposer_orphans()  # must not raise
    assert not (tmp_path / "inflight_proposer.json").exists()


class _Completed:
    returncode = 0
    stdout = ""
    stderr = ""


def _completed():
    return _Completed()
