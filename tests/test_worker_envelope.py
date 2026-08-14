from __future__ import annotations

import json

import pytest

from simpleloop.scheduling.envelope import (
    ProtocolError,
    WorkerRequest,
    WorkerResult,
    WorkerStatus,
    read_request,
    read_result,
    write_request,
    write_result,
)


def test_worker_result_round_trip_is_atomic(tmp_path):
    request = WorkerRequest(
        "candidate", "r2-c1", {"candidate_id": 1}, tmp_path / "result.json"
    )
    write_request(tmp_path / "manifest.json", request)
    assert read_request(tmp_path / "manifest.json") == request

    result = WorkerResult(
        "candidate",
        "r2-c1",
        WorkerStatus.COMPLETED,
        {"status": "NO_CHANGE"},
        ({"model": "m"},),
    )
    write_result(request.result_path, result)

    assert read_result(request.result_path, expected=request) == result
    assert not list(tmp_path.glob("*.tmp"))


def test_worker_result_rejects_request_identity_mismatch(tmp_path):
    request = WorkerRequest(
        "candidate", "r2-c1", {}, tmp_path / "result.json"
    )
    write_result(
        request.result_path,
        WorkerResult(
            "proposer",
            "r2-c1",
            WorkerStatus.COMPLETED,
            {},
        ),
    )

    with pytest.raises(ProtocolError, match="kind"):
        read_result(request.result_path, expected=request)


def test_worker_request_rejects_unknown_protocol(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({
        "protocol": "unknown",
        "kind": "candidate",
        "request_id": "x",
        "payload": {},
        "result_path": str(tmp_path / "result.json"),
    }))

    with pytest.raises(ProtocolError, match="protocol"):
        read_request(path)

