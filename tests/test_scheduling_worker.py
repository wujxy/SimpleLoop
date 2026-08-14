from __future__ import annotations

from simpleloop.scheduling import worker
from simpleloop.scheduling.envelope import (
    WorkerRequest,
    WorkerStatus,
    read_result,
    write_request,
)


def _run(tmp_path, monkeypatch, kind, handler):
    result_path = tmp_path / "result.json"
    request = WorkerRequest(kind, "request-1", {"value": 3}, result_path)
    manifest = tmp_path / "manifest.json"
    write_request(manifest, request)
    monkeypatch.setattr(worker, "_load_handler", lambda selected: handler)
    assert worker.main(["--manifest", str(manifest)]) == 0
    return request, read_result(result_path, expected=request)


def test_worker_dispatches_payload_and_wraps_usage(tmp_path, monkeypatch):
    seen = []

    def handler(payload, observe_usage):
        seen.append(payload)
        observe_usage({"model": "m", "tokens": 4})
        return {"answer": payload["value"] + 1}

    _, result = _run(tmp_path, monkeypatch, "candidate", handler)

    assert seen == [{"value": 3}]
    assert result.status is WorkerStatus.COMPLETED
    assert result.result == {"answer": 4}
    assert result.usage == ({"model": "m", "tokens": 4},)


def test_worker_handler_failure_writes_terminal_envelope(tmp_path, monkeypatch):
    def handler(payload, observe_usage):
        raise RuntimeError("broken handler")

    _, result = _run(tmp_path, monkeypatch, "proposer", handler)

    assert result.status is WorkerStatus.FAILED
    assert result.result == {}
    assert result.error == "broken handler"


def test_worker_unknown_kind_writes_failed_envelope(tmp_path):
    result_path = tmp_path / "result.json"
    request = WorkerRequest("unknown", "request-1", {}, result_path)
    manifest = tmp_path / "manifest.json"
    write_request(manifest, request)

    assert worker.main(["--manifest", str(manifest)]) == 0

    result = read_result(result_path, expected=request)
    assert result.status is WorkerStatus.FAILED
    assert "unsupported worker kind" in result.error
