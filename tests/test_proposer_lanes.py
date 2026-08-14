"""Unit tests for the backend-agnostic proposer-lane helpers.

Pure file-I/O tests — no subprocess, no model. They lock the
manifest/result/collect contract that every execution backend relies on.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.execution import proposer_lanes as pl
from simpleloop.execution.base import InfraRoundError
from simpleloop.stages.proposer import ProposalBatch


def _write(result_dir: Path, name: str, payload: dict | str) -> None:
    result_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        (result_dir / name).write_text(payload, encoding="utf-8")
    else:
        (result_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


# --- read_lane_result --------------------------------------------------------

def test_read_lane_result_accepts_a_well_formed_result(tmp_path):
    _write(tmp_path, "result.json",
           {"status": "COMPLETED", "proposals": [], "outcome": "submit"})
    result = pl.read_lane_result(tmp_path)
    assert result["status"] == "COMPLETED"
    assert result["proposals"] == []


def test_read_lane_result_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError):
        pl.read_lane_result(tmp_path)


def test_read_lane_result_rejects_malformed_json(tmp_path):
    _write(tmp_path, "result.json", "{not json")
    with pytest.raises(ValueError):
        pl.read_lane_result(tmp_path)


def test_read_lane_result_rejects_wrong_shape(tmp_path):
    # proposals must be a list; status must be a string.
    _write(tmp_path, "result.json", {"status": "COMPLETED", "proposals": "nope"})
    with pytest.raises(ValueError):
        pl.read_lane_result(tmp_path)


# --- read_worker_meta --------------------------------------------------------

def test_read_worker_meta_degrades_to_empty_when_absent(tmp_path):
    assert pl.read_worker_meta(tmp_path) == {}


def test_read_worker_meta_reads_usage_sidecar(tmp_path):
    _write(tmp_path, "usage.json", {"usage": [{"model": "x", "input": 10}]})
    assert pl.read_worker_meta(tmp_path) == {"usage": [{"model": "x", "input": 10}]}


# --- collect_lane_results ----------------------------------------------------

class _FakeTelemetry:
    def __init__(self):
        self.records = []

    def record_usage(self, record):
        self.records.append(record)


def _lane(result_dir, *, state, result):
    return pl.LaneJob(lane_id=0, result_dir=Path(result_dir),
                      state=state, result=result)


def test_collect_lane_results_builds_proposals_and_ingests_usage(tmp_path):
    _write(tmp_path, "usage.json",
           {"usage": [{"model": "glm", "input": 1}, {"model": "glm", "input": 2}]})
    job = _lane(tmp_path, state="COMPLETED", result={
        "status": "COMPLETED", "outcome": "submit",
        "proposals": [{"instruction": "try cache",
                       "research_target": {"question": "cache?"},
                       "evidence_refs": [], "material_difference": None}],
        "reason_kind": None, "telemetry": {"tool_calls": 3},
    })
    telemetry = _FakeTelemetry()

    out = pl.collect_lane_results([job], round_id=1, telemetry=telemetry)

    assert isinstance(out, ProposalBatch)
    assert len(out.proposals) == 1
    assert out.proposals[0].instruction == "try cache"
    assert out.abstained is False
    # usage.json was ingested into telemetry (the only path proposer model
    # usage reaches the host).
    assert telemetry.records == [{"model": "glm", "input": 1},
                                 {"model": "glm", "input": 2}]
    # Per-lane trace/telemetry survive into the Host ProposalBatch.
    assert out.trace == {"lanes": [{"lane_id": 0, "outcome": "submit",
                                    "n_proposals": 1, "reason_kind": None}]}
    assert out.telemetry == {
        "lanes": [{"lane_id": 0, "telemetry": {"tool_calls": 3}}]}


def test_collect_lane_results_empty_proposals_is_an_abstention(tmp_path):
    # A LANE_FAILED result still has _FINISHED, so the backend marks the lane
    # COMPLETED; collect turns zero proposals into an abstention (the run
    # continues) rather than an infra failure.
    job = _lane(tmp_path, state="COMPLETED", result={
        "status": "LANE_FAILED", "outcome": "error", "proposals": [],
        "reason_kind": None, "telemetry": {},
    })
    out = pl.collect_lane_results([job], round_id=1)
    assert out.proposals == ()
    assert out.abstained is True
    assert out.abstention.reason == "all lanes abstained/blocked/errored"


def test_collect_lane_results_raises_when_no_lane_completed(tmp_path):
    job = _lane(tmp_path, state="INFRA_FAILED", result=None)
    with pytest.raises(InfraRoundError, match="failed on infrastructure"):
        pl.collect_lane_results([job], round_id=5)


# --- manifest + inflight round-trip -----------------------------------------

def test_write_lane_manifest_is_atomic_and_readable(tmp_path):
    from simpleloop.proposer_lane_worker import ProposerLaneSpec

    spec = ProposerLaneSpec(lane_id=0, round_id=3, base_sha="abc",
                            run_dir=str(tmp_path),
                            workspace_path=str(tmp_path / "ws"),
                            result_dir=str(tmp_path), proposal_slots=2)
    pl.write_lane_manifest(tmp_path, spec)
    # atomic write leaves only manifest.json, not the .tmp
    assert (tmp_path / "manifest.json").exists()
    assert not (tmp_path / "manifest.json.tmp").exists()
    on_disk = json.loads((tmp_path / "manifest.json").read_text())
    assert on_disk["proposal_slots"] == 2
    assert on_disk["base_sha"] == "abc"


def test_inflight_marker_write_and_clear(tmp_path):
    pl.write_inflight_proposer(tmp_path, 4, "sha", [{"lane_id": 0, "pid": 123}])
    path = pl.inflight_proposer_path(tmp_path)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["lanes"] == [{"lane_id": 0, "pid": 123}]
    pl.clear_inflight_proposer(tmp_path)
    assert not path.exists()
