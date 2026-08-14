"""Strict durable transport shared by every SimpleLoop worker kind."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping


PROTOCOL = "simpleloop.worker.v1"


class ProtocolError(RuntimeError):
    """A durable request, result, or journal violates its wire contract."""


class WorkerStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkerRequest:
    kind: str
    request_id: str
    payload: Mapping[str, object]
    result_path: Path


@dataclass(frozen=True)
class WorkerResult:
    kind: str
    request_id: str
    status: WorkerStatus
    result: Mapping[str, object]
    usage: tuple[Mapping[str, object], ...] = ()
    error: str | None = None
    execution: Mapping[str, object] = field(default_factory=dict)


def _object(value: object, field_name: str) -> dict:
    if not isinstance(value, dict):
        raise ProtocolError(f"{field_name} must be an object")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{field_name} must be a non-empty string")
    return value


def _load(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"could not read {path}: {exc}") from exc
    return _object(raw, "document")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    try:
        directory = os.open(str(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_request(path: str | Path, request: WorkerRequest) -> None:
    _atomic_json(Path(path), {
        "protocol": PROTOCOL,
        "kind": request.kind,
        "request_id": request.request_id,
        "payload": dict(request.payload),
        "result_path": str(request.result_path),
    })


def read_request(path: str | Path) -> WorkerRequest:
    raw = _load(Path(path))
    if raw.get("protocol") != PROTOCOL:
        raise ProtocolError(f"unsupported worker protocol: {raw.get('protocol')!r}")
    return WorkerRequest(
        _text(raw.get("kind"), "kind"),
        _text(raw.get("request_id"), "request_id"),
        _object(raw.get("payload"), "payload"),
        Path(_text(raw.get("result_path"), "result_path")),
    )


def write_result(path: str | Path, result: WorkerResult) -> None:
    _atomic_json(Path(path), {
        "protocol": PROTOCOL,
        "kind": result.kind,
        "request_id": result.request_id,
        "status": result.status.value,
        "result": dict(result.result),
        "usage": [dict(item) for item in result.usage],
        "error": result.error,
        "execution": dict(result.execution),
    })


def read_result(
    path: str | Path,
    *,
    expected: WorkerRequest | None = None,
) -> WorkerResult:
    raw = _load(Path(path))
    if raw.get("protocol") != PROTOCOL:
        raise ProtocolError(f"unsupported worker protocol: {raw.get('protocol')!r}")
    kind = _text(raw.get("kind"), "kind")
    request_id = _text(raw.get("request_id"), "request_id")
    if expected is not None and kind != expected.kind:
        raise ProtocolError(
            f"result kind {kind!r} does not match request kind {expected.kind!r}"
        )
    if expected is not None and request_id != expected.request_id:
        raise ProtocolError(
            f"result request_id {request_id!r} does not match "
            f"request {expected.request_id!r}"
        )
    try:
        status = WorkerStatus(raw.get("status"))
    except ValueError as exc:
        raise ProtocolError(f"invalid worker status: {raw.get('status')!r}") from exc
    usage = raw.get("usage")
    if not isinstance(usage, list) or not all(isinstance(item, dict) for item in usage):
        raise ProtocolError("usage must be a list of objects")
    error = raw.get("error")
    if error is not None and not isinstance(error, str):
        raise ProtocolError("error must be null or a string")
    return WorkerResult(
        kind,
        request_id,
        status,
        _object(raw.get("result"), "result"),
        tuple(usage),
        error,
        _object(raw.get("execution"), "execution"),
    )

