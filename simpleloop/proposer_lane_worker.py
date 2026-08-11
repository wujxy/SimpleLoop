"""ProposerLaneWorker: one proposer lane's research episode in its own
writable workspace -> proposals.

The lane workspace lifecycle belongs to the execution backends (they create
one workspace per lane before, remove it after); job management belongs to
HEPJobBackend. The worker is launchable standalone:

  python -m simpleloop.proposer_lane_worker --manifest /path/manifest.json \
      --job-id 12345.0

Completion contract (identical to CandidateWorker): result.json + usage.json
are written atomically and _FINISHED is touched last in result_dir. ANY
business-side failure still produces all of them — a missing _FINISHED means
the process was killed by infrastructure (condor_rm/OOM/node death), and only
then is a job-level retry meaningful.

Import-light: does not import simpleloop.loop (matplotlib). The orchestrator /
generator / proposer / memory chain has no matplotlib dependency.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from .candidate_worker import write_result
from .container.runtime import ApptainerRuntime
from .harness import views
from .harness.workspace import Workspace
from .memory import MemoryService
from .memory.models import (
    ExistingFindingTarget, NewFindingTarget, ResearchProposal,
)
from .roles import model as model_mod
from .roles.orchestrator import ProposerOrchestrator


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class ProposerLaneSpec:
    """Serializable input for one proposer lane; persisted as manifest.json."""
    lane_id: int
    round_id: int
    base_sha: str
    run_dir: str = ""
    workspace_path: str = ""
    result_dir: str = ""
    prompt_dir: str = ""
    assigned_ops: list[str] = field(default_factory=list)
    select_quota: int = 1
    gen_steps: int = 216
    cognitive_steps: int = 148
    attempt: int = 1

    def to_dict(self) -> dict:
        return {
            "lane_id": self.lane_id,
            "round_id": self.round_id,
            "base_sha": self.base_sha,
            "run_dir": self.run_dir,
            "workspace_path": self.workspace_path,
            "result_dir": self.result_dir,
            "prompt_dir": self.prompt_dir,
            "assigned_ops": list(self.assigned_ops),
            "select_quota": self.select_quota,
            "gen_steps": self.gen_steps,
            "cognitive_steps": self.cognitive_steps,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProposerLaneSpec":
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(
                f"unknown proposer-lane manifest fields: {sorted(unknown)}"
            )
        return cls(
            lane_id=int(data["lane_id"]),
            round_id=int(data["round_id"]),
            base_sha=str(data["base_sha"]),
            run_dir=str(data.get("run_dir") or ""),
            workspace_path=str(data.get("workspace_path") or ""),
            result_dir=str(data.get("result_dir") or ""),
            prompt_dir=str(data.get("prompt_dir") or ""),
            assigned_ops=list(data.get("assigned_ops") or []),
            select_quota=int(data.get("select_quota") or 1),
            gen_steps=int(data.get("gen_steps") or 216),
            cognitive_steps=int(data.get("cognitive_steps") or 148),
            attempt=int(data.get("attempt") or 1),
        )


@dataclass
class ProposerLaneDeps:
    """Runtime fixtures run_lane needs. Rebuilt from the resolved config on a
    compute node (the frontend owns the shared repo; the worker only reads the
    pre-created lane workspace)."""
    cfg: dict
    run_dir: Path
    runtime: ApptainerRuntime
    workspace: Workspace
    orchestrator: ProposerOrchestrator
    memory_service: MemoryService
    prompt_dir: Path | None = None
    gate_lines: str = ""


def build_lane_deps(
    cfg: dict,
    run_dir: str | Path,
    *,
    usage_observer=None,
    prompt_dir: str | Path | None = None,
) -> ProposerLaneDeps:
    """Rebuild proposer-lane worker dependencies from a resolved config. Does
    NOT call workspace.setup() (no clone) — the frontend owns the shared repo;
    the worker only reads the lane workspace the backend created for it."""
    run_dir = Path(run_dir)
    runtime = ApptainerRuntime(
        image=cfg["runtime_image"],
        binds=cfg["runtime_binds"],
        run_dir=run_dir,
    )
    researcher = (cfg.get("roles") or {}).get("researcher") or {}
    timeout = cfg.get("agent_timeout_seconds", 3600)
    orchestrator = ProposerOrchestrator(
        model=model_mod.build_chat_model(researcher),
        runtime=runtime,
        timeout_seconds=timeout,
        command_timeout_seconds=researcher.get("command_timeout_seconds", 120),
        command_output_cap_chars=researcher.get("command_output_cap_chars", 12000),
        usage_observer=usage_observer,
    )
    workspace = Workspace(
        run_dir=run_dir,
        repo_path=cfg["repo_path"],
        baseline_ref=cfg["baseline_ref"],
        editable=cfg["editable_paths"],
    )
    memory_service = MemoryService(
        run_dir=run_dir, metrics_schema=cfg.get("metrics") or {},
    )
    return ProposerLaneDeps(
        cfg=cfg, run_dir=run_dir, runtime=runtime, workspace=workspace,
        orchestrator=orchestrator, memory_service=memory_service,
        prompt_dir=Path(prompt_dir) if prompt_dir else None,
        gate_lines=views.gate_block(cfg.get("metrics")),
    )


def _proposal_to_dict(proposal) -> dict:
    """ResearchProposal -> plain dict (research_target recurses)."""
    return dataclasses.asdict(proposal)


def _target_from_dict(d: dict):
    """Inverse of asdict on ExistingFindingTarget/NewFindingTarget. asdict drops
    the type tag, so infer from the keys: finding_id -> existing, else new."""
    if not isinstance(d, dict):
        return NewFindingTarget(question=str(d))
    if "finding_id" in d:
        return ExistingFindingTarget(finding_id=str(d["finding_id"]))
    return NewFindingTarget(
        question=str(d.get("question") or ""),
        mechanisms=tuple(d.get("mechanisms") or ()),
        code_regions=tuple(d.get("code_regions") or ()),
    )


def proposal_from_dict(d: dict):
    """plain dict -> ResearchProposal (the inverse the backend uses to rebuild
    proposals from a lane worker's result.json)."""
    return ResearchProposal(
        instruction=str(d.get("instruction") or ""),
        research_target=_target_from_dict(d.get("research_target") or {}),
        evidence_refs=tuple(d.get("evidence_refs") or ()),
        material_difference=d.get("material_difference"),
    )


def _lane_result_to_dict(lr) -> dict:
    proposals = []
    if lr.proposals:
        proposals = [_proposal_to_dict(p) for p in lr.proposals]
    elif lr.proposal is not None:
        proposals = [_proposal_to_dict(lr.proposal)]
    return {
        "status": "COMPLETED",
        "lane_id": lr.lane_id,
        "outcome": lr.outcome,
        "proposals": proposals,
        "reason_kind": lr.reason_kind,
        "explanation": lr.explanation,
        "enrichment_partial": bool(lr.enrichment_partial),
        "abstain_reason": lr.abstain_reason,
        "trace": lr.trace or {},
        "telemetry": lr.deliberation_telemetry or {},
    }


def _failure_result(spec: ProposerLaneSpec, reason: str) -> dict:
    return {
        "status": "LANE_FAILED",
        "lane_id": spec.lane_id,
        "outcome": "error",
        "proposals": [],
        "reason_kind": None,
        "explanation": reason,
        "enrichment_partial": False,
        "abstain_reason": reason,
        "trace": {},
        "telemetry": {"tool_calls": 0},
    }


def run_lane(deps: ProposerLaneDeps, spec: ProposerLaneSpec) -> dict:
    """Run one lane episode in ``spec.workspace_path`` and return the result
    dict. The workspace must already exist (the backend created it)."""
    cfg = deps.cfg
    workspace = Path(spec.workspace_path)
    lane_result = deps.orchestrator.run_lane_episode(
        lane_id=spec.lane_id,
        assigned_ops=spec.assigned_ops,
        workspace=workspace,
        base_sha=spec.base_sha,
        goal=cfg["goal"],
        editable=cfg["editable_paths"],
        frozen=[],
        memory_service=deps.memory_service,
        repo_path=deps.workspace.repo,
        run_dir=deps.run_dir,
        current_round=spec.round_id,
        gate_block=deps.gate_lines,
        prompt_dir=deps.prompt_dir,
        hints=cfg.get("hints") or None,
        select_quota=spec.select_quota,
        gen_steps=spec.gen_steps,
        cognitive_steps=spec.cognitive_steps,
    )
    return _lane_result_to_dict(lane_result)


def main(argv: list[str] | None = None) -> int:
    """Standalone worker entry. Exit 0 whenever a terminal result could be
    written (business failures included); non-zero only when the manifest or
    result path is unusable — i.e. infrastructure-grade failure."""
    parser = argparse.ArgumentParser(prog="proposer_lane_worker")
    parser.add_argument("--manifest", required=True,
                        help="Path to the proposer-lane manifest JSON.")
    parser.add_argument("--job-id", default=None,
                        help="Scheduler job id (recorded in result.execution).")
    args = parser.parse_args(argv)

    usage: list = []
    spec_dict: dict = {}
    result: dict
    try:
        spec_dict = json.loads(
            Path(args.manifest).read_text(encoding="utf-8"))
        if not isinstance(spec_dict, dict):
            raise ValueError("manifest root must be an object")
        run_dir = Path(spec_dict["run_dir"])
        cfg = config_mod.load_resolved(run_dir)
        spec = ProposerLaneSpec.from_dict(spec_dict)
        deps = build_lane_deps(
            cfg, run_dir, usage_observer=usage.append,
            prompt_dir=spec.prompt_dir or None,
        )
        deps.runtime.preflight()
        result = run_lane(deps, spec)
    except Exception as exc:
        # Catch-all invariant: a business-side failure still produces a
        # terminal result; _FINISHED absence must mean "killed by infra".
        print(f"[{stamp()}] proposer lane worker failed: {exc}", flush=True)
        spec = ProposerLaneSpec(
            lane_id=int(spec_dict.get("lane_id") or 0),
            round_id=int(spec_dict.get("round_id") or 0),
            base_sha=str(spec_dict.get("base_sha") or ""),
        )
        result = _failure_result(spec, f"worker failed: {exc}")
    sidecar = {
        "usage": usage,
        "execution": {
            "backend": "hepjob",
            "job_id": args.job_id,
            "attempt": int(spec_dict.get("attempt") or 1),
            "host": socket.gethostname(),
        },
    }
    result_dir = spec_dict.get("result_dir")
    if not result_dir:
        print(f"[{stamp()}] FATAL: manifest has no result_dir; "
              "nowhere to write the terminal result", flush=True)
        return 2
    try:
        write_result(result_dir, result, sidecar=sidecar)
    except OSError as exc:
        print(f"[{stamp()}] FATAL: could not write result to {result_dir}: "
              f"{exc}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
