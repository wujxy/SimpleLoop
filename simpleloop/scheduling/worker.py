"""The only SimpleLoop worker executable."""
from __future__ import annotations

import argparse
import importlib
import os
import socket
from typing import Callable, Mapping

from .envelope import (
    WorkerResult,
    WorkerStatus,
    read_request,
    write_result,
)


_HANDLERS = {
    "candidate": "simpleloop.scheduling.handlers.candidate:handle_candidate",
    "proposer": "simpleloop.scheduling.handlers.proposer:handle_proposer",
    "self_review": "simpleloop.scheduling.handlers.proposer:handle_self_review",
    "self_edit": "simpleloop.scheduling.handlers.rsi:handle_self_edit",
    "viability": "simpleloop.scheduling.handlers.proposer:handle_viability",
}


def _load_handler(kind: str) -> Callable:
    target = _HANDLERS.get(kind)
    if target is None:
        raise ValueError(f"unsupported worker kind: {kind}")
    module_name, function_name = target.split(":", 1)
    return getattr(importlib.import_module(module_name), function_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simpleloop-worker")
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args(argv)
    try:
        request = read_request(args.manifest)
    except Exception as exc:
        print(f"worker could not read manifest: {exc}", flush=True)
        return 2

    usage: list[Mapping[str, object]] = []
    status = WorkerStatus.COMPLETED
    result: Mapping[str, object] = {}
    error = None
    try:
        handler = _load_handler(request.kind)
        result = handler(request.payload, usage.append)
        if not isinstance(result, Mapping):
            raise TypeError("worker handler result must be an object")
    except Exception as exc:
        status = WorkerStatus.FAILED
        result = {}
        error = str(exc)

    try:
        write_result(request.result_path, WorkerResult(
            request.kind,
            request.request_id,
            status,
            result,
            tuple(usage),
            error,
            {
                "scheduler": os.environ.get("SIMPLELOOP_SCHEDULER", "local"),
                "job_id": os.environ.get("SIMPLELOOP_JOB_ID"),
                "attempt": request.payload.get("attempt", 1),
                "host": socket.gethostname(),
            },
        ))
    except OSError as exc:
        print(f"worker could not write result: {exc}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
