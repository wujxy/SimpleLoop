from __future__ import annotations

import pytest

from simpleloop.scheduling import worker
from simpleloop.scheduling.handlers import proposer as proposer_handler
from simpleloop.scheduling.envelope import (
    WorkerRequest,
    WorkerStatus,
    read_result,
    write_request,
)


def test_proposer_handler_owns_serializable_lane_spec():
    from simpleloop.scheduling.handlers.proposer import ProposerLaneSpec

    spec = ProposerLaneSpec(0, 4, "base", run_dir="/run", proposal_slots=2)

    assert ProposerLaneSpec.from_dict(spec.to_dict()) == spec


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


def test_candidate_dispatch_does_not_load_proposer_handler(monkeypatch):
    imported = []
    real_import = worker.importlib.import_module

    def track(name):
        imported.append(name)
        return real_import(name)

    monkeypatch.setattr(worker.importlib, "import_module", track)

    worker._load_handler("candidate")

    assert imported == ["simpleloop.scheduling.handlers.candidate"]


def test_viability_redirect_happens_before_proposer_composition(
    tmp_path, monkeypatch,
):
    selected = tmp_path / "candidate-self"
    (selected / "proposer").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.resolved.json").write_text("{}", encoding="utf-8")
    seen = []

    def build(*args, **kwargs):
        import sys
        seen.append(sys.path[0])
        raise RuntimeError("stop after redirect")

    monkeypatch.setattr(proposer_handler, "build_lane_deps", build)

    result = proposer_handler.handle_viability({
        "lane_id": 0, "round_id": 0, "base_sha": "base",
        "run_dir": str(run_dir), "workspace_path": str(tmp_path / "ws"),
        "self_repo": str(selected),
    }, lambda row: None)

    assert seen == [str(selected.resolve())]
    assert result["status"] == "LANE_FAILED"


def test_reflection_handler_registered():
    from simpleloop.scheduling.worker import _load_handler
    handler = _load_handler("reflection")
    assert callable(handler)
    assert handler.__name__ == "handle_reflection"


def test_reflection_handler_routes_and_lets_failures_escape(
        monkeypatch, tmp_path):
    """handle_reflection routes to run_reflection_lane and lets exceptions
    escape — a crashed reflection must become a FAILED envelope (retryable
    infrastructure), never a fabricated COMPLETED handoff."""
    calls = {}

    class _Deps:
        pass

    def _fake_build_lane_deps(cfg, run_dir, **kwargs):
        calls["deps_built"] = True
        return _Deps()

    def _fake_run_reflection_lane(deps, spec):
        calls["spec"] = spec
        return {
            "status": "COMPLETED", "mode": "reflection",
            "reflection": {
                "round_id": spec.round_id, "handoff": "audit warning",
                "self_limitation_suspected": True, "abstained": False,
                "note": None,
            },
            "trace": {}, "telemetry": {},
        }

    monkeypatch.setattr(proposer_handler, "build_lane_deps",
                        _fake_build_lane_deps)
    monkeypatch.setattr(proposer_handler, "run_reflection_lane",
                        _fake_run_reflection_lane)
    # _run imports config lazily inside the function; give it a real snapshot
    (tmp_path / "config.resolved.json").write_text("{}", encoding="utf-8")
    _Deps.runtime = type("R", (), {"preflight": lambda self: None})()
    payload = {
        "lane_id": 0, "round_id": 3, "base_sha": "abc",
        "run_dir": str(tmp_path), "workspace_path": str(tmp_path),
        "result_dir": str(tmp_path / "result"), "prompt_dir": "",
        "scientist_steps": 5, "mode": "reflection",
    }
    result = proposer_handler.handle_reflection(payload, lambda usage: None)
    assert result["status"] == "COMPLETED"
    assert result["mode"] == "reflection"
    assert result["reflection"]["handoff"] == "audit warning"
    # the spec carries the reflection mode through (not coerced to "task")
    assert calls["spec"].mode == "reflection"
    assert calls["spec"].round_id == 3

    def _boom(deps, spec):
        raise RuntimeError("reflection crashed")

    monkeypatch.setattr(proposer_handler, "run_reflection_lane", _boom)
    with pytest.raises(RuntimeError):
        proposer_handler.handle_reflection(payload, lambda usage: None)
