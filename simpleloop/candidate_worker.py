"""Standalone transport adapter for the shared Candidate Pipeline.

The worker owns manifest decoding, adapter composition, telemetry sidecars,
and atomic terminal writes. Candidate business semantics live in
``simpleloop.candidate.run_candidate_guarded``.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable

from . import config as config_mod
from .candidate import (
    ArtifactWorkspace,
    CandidateArtifact,
    CandidateRequest,
    CandidateResult,
    CandidateStatus,
    CandidateTrace,
    EvaluationResult,
    ExecutionResult,
    GateDecision,
    GateResult,
    candidate_failure_from_request,
    run_candidate as run_candidate_pipeline,
    run_candidate_guarded,
)
from .container.runtime import ApptainerRuntime, world_mount_map
from .harness import views
from .harness.workspace import Workspace
from .persistence.artifacts import encode_candidate_result
from .persistence.candidate_trace import HandoffCandidateTrace
from .roles.agent import Agent
from .stages.artifacts import GitArtifactWorkspace
from .stages.evaluator import (
    EvaluationConfig,
    EvaluationRequest,
    Evaluator,
    HarnessEvaluator,
)
from .stages.executor import AgentExecutor, Executor, ExecutorConfig
from .stages.gate import GateSpec, apply_gates
from .stages.proposer import Proposal


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class CandidateSpec:
    """Serializable v0 transport input persisted as ``manifest.json``."""

    round_id: int
    candidate_id: int
    parent_sha: str
    proposal: str
    run_dir: str = ""
    worktree_path: str = ""
    result_dir: str = ""
    prompt_dir: str = ""
    attempt: int = 1

    def to_dict(self) -> dict:
        return {
            "round_id": self.round_id,
            "candidate_id": self.candidate_id,
            "parent_sha": self.parent_sha,
            "proposal": self.proposal,
            "run_dir": self.run_dir,
            "worktree_path": self.worktree_path,
            "result_dir": self.result_dir,
            "prompt_dir": self.prompt_dir,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CandidateSpec":
        return cls(
            round_id=int(data["round_id"]),
            candidate_id=int(data["candidate_id"]),
            parent_sha=str(data["parent_sha"]),
            proposal=str(data["proposal"]),
            run_dir=str(data.get("run_dir") or ""),
            worktree_path=str(data.get("worktree_path") or ""),
            result_dir=str(data.get("result_dir") or ""),
            prompt_dir=str(data.get("prompt_dir") or ""),
            attempt=int(data.get("attempt") or 1),
        )

    def to_request(self) -> CandidateRequest:
        return CandidateRequest(
            round_id=self.round_id,
            candidate_id=self.candidate_id,
            parent_sha=self.parent_sha,
            proposal=Proposal(self.proposal),
            worktree=Path(self.worktree_path),
        )


@dataclass(frozen=True)
class CandidatePorts:
    executor: Executor
    artifacts: ArtifactWorkspace
    evaluator: Evaluator
    gate_spec: GateSpec
    trace: CandidateTrace
    preflight: Callable[[], None] = lambda: None


def _gate_spec(metrics_schema: dict | None) -> GateSpec:
    schema = metrics_schema or {}
    objective = schema.get("objective") or {}
    objective_key = str(objective.get("key") or "OBJECTIVE")
    gate_keys = tuple(
        str(item["key"])
        for item in (schema.get("gates") or ())
        if item.get("key")
    )
    return GateSpec(objective_key, gate_keys)


def _evaluation_config(cfg: dict) -> EvaluationConfig:
    spec = _gate_spec(cfg.get("metrics"))
    return EvaluationConfig(
        commands=tuple(str(command) for command in cfg.get("eval_commands", ())),
        objective_key=spec.objective_key,
        gate_keys=spec.gate_keys,
        timeout_seconds=int(cfg.get("eval_timeout_seconds", 600)),
        output_cap_chars=int(cfg.get("eval_output_cap_chars", 16000)),
    )


def build_ports(
    cfg: dict,
    run_dir: str | Path,
    usage_observer=None,
    prompt_dir: str | Path | None = None,
) -> CandidatePorts:
    """Compose concrete ports from the resolved worker configuration."""
    run_dir = Path(run_dir)
    runtime = ApptainerRuntime(
        image=cfg["runtime_image"],
        binds=cfg["runtime_binds"],
        run_dir=run_dir,
    )
    role = (cfg.get("roles") or {}).get("executor") or {}
    agent = Agent(
        runtime=runtime,
        command="claude",
        timeout_seconds=cfg.get("agent_timeout_seconds", 3600),
        allowed_tools="Read,Edit,Write,Bash",
        max_output_tokens=cfg.get("agent_max_output_tokens", 64000),
        model=role.get("model"),
        base_url=role.get("base_url"),
        usage_observer=usage_observer,
        mounts=world_mount_map(cfg),
    )
    workspace = Workspace(
        run_dir=run_dir,
        repo_path=cfg["repo_path"],
        baseline_ref=cfg["baseline_ref"],
        editable=cfg["editable_paths"],
    )
    spec = _gate_spec(cfg.get("metrics"))
    return CandidatePorts(
        executor=AgentExecutor(
            agent,
            ExecutorConfig(
                goal=str(cfg.get("goal") or ""),
                gate_block=views.gate_block(cfg.get("metrics")),
                prompt_dir=Path(prompt_dir) if prompt_dir else None,
            ),
        ),
        artifacts=GitArtifactWorkspace(workspace),
        evaluator=HarnessEvaluator(runtime, _evaluation_config(cfg)),
        gate_spec=spec,
        trace=HandoffCandidateTrace(run_dir),
        preflight=runtime.preflight,
    )


# Temporary Phase 2 adapter for LocalBackend until Task 5 gives the backend
# explicit ports. It delegates to the same shared pipeline and owns no
# candidate status logic.
@dataclass
class CandidateDeps:
    cfg: dict
    run_dir: Path
    runtime: ApptainerRuntime
    workspace: Workspace
    executor_agent: Agent
    prompt_dir: Path | None = None
    gate_lines: str = ""


def build_deps(
    cfg: dict,
    run_dir: str | Path,
    usage_observer=None,
    prompt_dir: str | Path | None = None,
) -> CandidateDeps:
    ports = build_ports(cfg, run_dir, usage_observer, prompt_dir)
    assert isinstance(ports.executor, AgentExecutor)
    assert isinstance(ports.artifacts, GitArtifactWorkspace)
    assert isinstance(ports.evaluator, HarnessEvaluator)
    return CandidateDeps(
        cfg=cfg,
        run_dir=Path(run_dir),
        runtime=ports.evaluator.runtime,
        workspace=ports.artifacts.workspace,
        executor_agent=ports.executor.agent,
        prompt_dir=Path(prompt_dir) if prompt_dir else None,
        gate_lines=ports.executor.config.gate_block,
    )


def _ports_from_deps(deps: CandidateDeps) -> CandidatePorts:
    spec = _gate_spec(deps.cfg.get("metrics"))
    return CandidatePorts(
        executor=AgentExecutor(
            deps.executor_agent,
            ExecutorConfig(
                goal=str(deps.cfg["goal"]),
                gate_block=deps.gate_lines,
                prompt_dir=deps.prompt_dir,
            ),
        ),
        artifacts=GitArtifactWorkspace(deps.workspace),
        evaluator=HarnessEvaluator(
            deps.runtime,
            _evaluation_config(deps.cfg),
        ),
        gate_spec=spec,
        trace=HandoffCandidateTrace(deps.run_dir),
    )


def run_candidate(deps: CandidateDeps, spec: CandidateSpec) -> CandidateResult:
    ports = _ports_from_deps(deps)
    return run_candidate_pipeline(
        spec.to_request(),
        executor=ports.executor,
        artifacts=ports.artifacts,
        evaluator=ports.evaluator,
        gate_spec=ports.gate_spec,
        trace=ports.trace,
    )


def _eligible(
    sha: str | None,
    gate_passed: bool,
    metrics: dict,
    metrics_schema: dict | None,
) -> bool:
    if not sha or not gate_passed or not metrics_schema:
        return False
    objective = metrics.get(metrics_schema["objective"]["key"])
    return bool(
        isinstance(objective, (int, float))
        and not isinstance(objective, bool)
        and math.isfinite(objective)
    )


def _run_baseline_eval(
    ports: CandidatePorts,
    spec: CandidateSpec,
) -> CandidateResult:
    evaluation = ports.evaluator.evaluate(
        EvaluationRequest(Path(spec.worktree_path))
    )
    if evaluation.error:
        raise ValueError(f"baseline evaluation failed: {evaluation.error}")
    failed = [code for code in evaluation.returncodes if code != 0]
    if failed:
        raise ValueError(
            f"baseline evaluation failed: {evaluation.text[:1000]}"
        )
    gate = apply_gates(evaluation, ports.gate_spec)
    return CandidateResult(
        candidate_id=spec.candidate_id,
        experiment_id=f"r{spec.round_id}c{spec.candidate_id}",
        proposal=Proposal(spec.proposal),
        parent_sha=spec.parent_sha,
        status=CandidateStatus.BASELINE,
        execution=ExecutionResult("BASELINE"),
        artifact=CandidateArtifact(spec.parent_sha, spec.parent_sha),
        evaluation=evaluation,
        gate=gate,
    )


def candidate_failure(
    candidate_id: int,
    spec: CandidateSpec,
    reason: str,
    parent_sha: str,
    sha: str | None = None,
    eval_block: str = "",
    eval_metrics: dict | None = None,
    changed_paths: list[str] | None = None,
    gate_results: dict | None = None,
    metrics_schema: dict | None = None,
    status: str = "WORKER_FAILED",
) -> CandidateResult:
    request = CandidateRequest(
        spec.round_id,
        candidate_id,
        parent_sha,
        Proposal(spec.proposal),
        Path(spec.worktree_path),
    )
    result = candidate_failure_from_request(
        request,
        reason,
        gate_spec=_gate_spec(metrics_schema),
        status=CandidateStatus(status),
    )
    artifact = (
        CandidateArtifact(
            parent_sha,
            sha,
            tuple(PurePosixPath(path) for path in (changed_paths or ())),
        )
        if sha else None
    )
    evaluation = (
        EvaluationResult(
            eval_block,
            eval_metrics or {},
            error=eval_block or reason,
        )
        if sha and (eval_block or eval_metrics) else None
    )
    gate = result.gate
    if gate_results is not None:
        gate = GateDecision(
            {
                name: GateResult(
                    row.get("passed"), str(row.get("detail") or ""),
                )
                for name, row in gate_results.items()
            },
            False,
            False,
        )
    return replace(
        result,
        artifact=artifact,
        evaluation=evaluation,
        gate=gate,
    )


def write_result(
    result_dir: str | Path,
    result: CandidateResult,
    *,
    sidecar: dict | None = None,
) -> None:
    """Atomically write the business result, sidecar, then marker."""
    result_dir = Path(result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    tmp = result_dir / "result.json.tmp"
    tmp.write_text(
        json.dumps(
            encode_candidate_result(result),
            ensure_ascii=False,
            indent=2,
            default=str,
        ) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, result_dir / "result.json")
    if sidecar is not None:
        meta_tmp = result_dir / "usage.json.tmp"
        meta_tmp.write_text(
            json.dumps(
                sidecar, ensure_ascii=False, indent=2, default=str,
            ) + "\n",
            encoding="utf-8",
        )
        os.replace(meta_tmp, result_dir / "usage.json")
    (result_dir / "_FINISHED").touch()


def _fallback_request(spec_dict: dict) -> CandidateRequest:
    return CandidateRequest(
        round_id=int(spec_dict.get("round_id") or 0),
        candidate_id=int(spec_dict.get("candidate_id") or 0),
        parent_sha=str(spec_dict.get("parent_sha") or ""),
        proposal=Proposal(str(spec_dict.get("proposal") or "<missing proposal>")),
        worktree=Path(str(spec_dict.get("worktree_path") or ".")),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="candidate_worker")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args(argv)

    usage: list = []
    spec_dict: dict = {}
    ports: CandidatePorts | None = None
    result: CandidateResult
    try:
        spec_dict = json.loads(
            Path(args.manifest).read_text(encoding="utf-8")
        )
        if not isinstance(spec_dict, dict):
            raise ValueError("manifest root must be an object")
        run_dir = Path(spec_dict["run_dir"])
        cfg = config_mod.load_resolved(run_dir)
        spec = CandidateSpec.from_dict(spec_dict)
        ports = build_ports(
            cfg,
            run_dir,
            usage_observer=usage.append,
            prompt_dir=spec.prompt_dir or None,
        )
        ports.preflight()
        if args.baseline_only:
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
        print(
            f"[{stamp()}] worker failed before/at business execution: {exc}",
            flush=True,
        )
        if not spec_dict.get("result_dir"):
            return 2
        request = _fallback_request(spec_dict)
        result = candidate_failure_from_request(
            request,
            f"worker failed: {exc}",
            gate_spec=ports.gate_spec if ports else None,
        )

    result_dir = spec_dict.get("result_dir")
    if not result_dir:
        print(
            f"[{stamp()}] FATAL: manifest has no result_dir; "
            "nowhere to write the terminal result",
            flush=True,
        )
        return 2
    sidecar = {
        "usage": usage,
        "execution": {
            "backend": "hepjob",
            "job_id": args.job_id,
            "attempt": int(spec_dict.get("attempt") or 1),
            "host": socket.gethostname(),
        },
    }
    try:
        write_result(result_dir, result, sidecar=sidecar)
    except OSError as exc:
        print(
            f"[{stamp()}] FATAL: could not write result to {result_dir}: {exc}",
            flush=True,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
