"""Strict candidate worker JSON codec tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.candidate import CandidateStatus
from simpleloop.persistence.artifacts import (
    ProtocolError,
    decode_candidate_result,
    encode_candidate_result,
)


FIXTURE = Path(__file__).parent / "fixtures" / "phase0" / "candidate-result.json"


def load_candidate() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_candidate_fixture_round_trips():
    raw = load_candidate()
    result = decode_candidate_result(raw)

    assert result.status is CandidateStatus.COMPLETED
    assert result.parent_sha == "parent"
    assert result.artifact.sha == "child"
    assert result.metrics["SPEED_MS"] == 90.0
    assert not hasattr(result, "selected")
    assert encode_candidate_result(result) == raw


@pytest.mark.parametrize("key", ["candidate", "proposal", "parent_sha", "status"])
def test_candidate_decoder_rejects_missing_required_key(key):
    raw = load_candidate()
    raw.pop(key)

    with pytest.raises(ProtocolError, match=key):
        decode_candidate_result(raw)


def test_candidate_decoder_rejects_unknown_status():
    raw = load_candidate()
    raw["status"] = "SURPRISING"

    with pytest.raises(ProtocolError, match="status"):
        decode_candidate_result(raw)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("changed_paths", "src/cache.cc"),
        ("metrics", []),
        ("gates", []),
        ("gate_passed", "yes"),
        ("eligible", 1),
    ],
)
def test_candidate_decoder_rejects_wrong_field_types(key, value):
    raw = load_candidate()
    raw[key] = value

    with pytest.raises(ProtocolError, match=key):
        decode_candidate_result(raw)


@pytest.mark.parametrize(
    "gate_row",
    ["pass", {"passed": "yes", "detail": ""}, {"passed": True, "detail": 1}],
)
def test_candidate_decoder_rejects_malformed_gate_rows(gate_row):
    raw = load_candidate()
    raw["gates"]["PATHS"] = gate_row

    with pytest.raises(ProtocolError, match="gates.PATHS"):
        decode_candidate_result(raw)


def test_old_payload_defaults_experiment_id():
    raw = load_candidate()
    raw.pop("experiment_id")

    assert decode_candidate_result(raw).experiment_id == "candidate-1"


def test_worker_failure_reason_round_trips_through_eval_block():
    raw = load_candidate()
    raw.update({
        "sha": None,
        "status": "WORKER_FAILED",
        "eval_block": "[loop failure] unexpected bug",
        "metrics": {},
        "changed_paths": [],
        "gates": {},
        "gate_passed": False,
        "eligible": False,
    })

    result = decode_candidate_result(raw)

    assert result.execution.reason == "[loop failure] unexpected bug"
    assert result.artifact is None
    assert result.evaluation is None
    assert encode_candidate_result(result) == raw


def test_implementation_incomplete_round_trips_with_post_mortem():
    """The incomplete-implementation candidate: post-mortem text in
    eval_block, committed sha retained, excluded from evaluation and
    selection."""
    from simpleloop.candidate import CandidateStatus

    raw = load_candidate()
    raw.update({
        "status": "IMPLEMENTATION_INCOMPLETE",
        "sha": "child",
        "eval_block": (
            "[harness post-mortem] the experimenter session ended "
            "without completing the intervention; "
            "stop_cause=session_ended_without_report; changed: src/a.cc; "
            "last words: 'Now I'll insert…'"),
        "metrics": {},
        "changed_paths": ["src/a.cc"],
        "gates": {"PATHS": {"passed": None, "detail": ""}},
        "gate_passed": False,
        "eligible": False,
    })

    result = decode_candidate_result(raw)

    assert result.status is CandidateStatus.IMPLEMENTATION_INCOMPLETE
    assert "[harness post-mortem]" in result.execution.reason
    assert result.artifact is not None and result.artifact.sha == "child"
    assert result.evaluation is None  # never evaluated
    assert result.eligible is False
    assert encode_candidate_result(result) == raw
