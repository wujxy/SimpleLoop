"""Candidate and baseline adapter for the unified worker."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from ... import config as config_mod
from ...candidate import candidate_failure_from_request, run_candidate_guarded
from ...candidate_worker import (
    CandidatePorts,
    CandidateSpec,
    _fallback_request,
    _run_baseline_eval,
    build_ports,
)
from ...persistence.artifacts import encode_candidate_result


def handle_candidate(
    payload: Mapping[str, object],
    observe_usage: Callable[[Mapping[str, object]], None],
) -> Mapping[str, object]:
    raw = dict(payload)
    ports: CandidatePorts | None = None
    try:
        run_dir = Path(str(raw["run_dir"]))
        cfg = config_mod.load_resolved(run_dir)
        spec = CandidateSpec.from_dict(raw)
        ports = build_ports(
            cfg,
            run_dir,
            spec.to_request().workspace,
            usage_observer=observe_usage,
            prompt_dir=spec.prompt_dir or None,
        )
        ports.preflight()
        if str(raw.get("mode") or "candidate") == "baseline":
            result = _run_baseline_eval(ports, spec)
        else:
            result = run_candidate_guarded(
                spec.to_request(),
                executor=ports.executor,
                artifacts=ports.artifacts,
                evaluator=ports.evaluator,
                gate_spec=ports.gate_spec,
                trace=ports.trace,
            )
    except Exception as exc:
        request = _fallback_request(raw)
        result = candidate_failure_from_request(
            request,
            f"worker failed: {exc}",
            gate_spec=ports.gate_spec if ports else None,
        )
    return encode_candidate_result(result)

