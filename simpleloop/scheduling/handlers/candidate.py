"""Candidate and baseline Host adapter for the unified worker."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from ... import config as config_mod
from ...candidate import (
    ArtifactWorkspace,
    CandidateArtifact,
    CandidateRequest,
    CandidateResult,
    CandidateStatus,
    CandidateTrace,
    ExecutionResult,
    candidate_failure_from_request,
    run_candidate_guarded,
)
from ...harness import views
from ...persistence.artifacts import encode_candidate_result
from ...persistence.candidate_trace import HandoffCandidateTrace
from ...roles.agent import Agent
from ...stages.artifacts import GitArtifactWorkspace
from ...stages.evaluator import (
    EvaluationConfig,
    EvaluationRequest,
    Evaluator,
    HarnessEvaluator,
)
from ...stages.executor import AgentExecutor, Executor, ExecutorConfig
from ...stages.gate import GateSpec, apply_gates
from ...stages.proposer import Proposal
from ...world import (
    ApptainerSandbox,
    SandboxSpec,
    SourceWorkspace,
    WorldBuilder,
    evaluator_environment,
    evaluator_world_spec,
    executor_environment,
    executor_world_spec,
)
from ...world.git import GitWorkspaceProvider


@dataclass(frozen=True)
class CandidateSpec:
    round_id: int
    candidate_id: int
    parent_sha: str
    proposal: str
    run_dir: str = ""
    worktree_path: str = ""
    result_dir: str = ""
    prompt_dir: str = ""
    attempt: int = 1

    def to_dict(self) -> dict[str, object]:
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
    def from_dict(cls, raw: Mapping[str, object]) -> "CandidateSpec":
        return cls(
            int(raw["round_id"]),
            int(raw["candidate_id"]),
            str(raw["parent_sha"]),
            str(raw["proposal"]),
            str(raw.get("run_dir") or ""),
            str(raw.get("worktree_path") or ""),
            str(raw.get("result_dir") or ""),
            str(raw.get("prompt_dir") or ""),
            int(raw.get("attempt") or 1),
        )

    def to_request(self) -> CandidateRequest:
        return CandidateRequest(
            self.round_id,
            self.candidate_id,
            self.parent_sha,
            Proposal(self.proposal),
            SourceWorkspace(
                f"{self.round_id}-c{self.candidate_id}",
                Path(self.worktree_path),
                self.parent_sha,
            ),
        )


@dataclass(frozen=True)
class CandidatePorts:
    workspace: SourceWorkspace
    executor: Executor
    artifacts: ArtifactWorkspace
    evaluator: Evaluator
    gate_spec: GateSpec
    trace: CandidateTrace
    preflight: Callable[[], None] = lambda: None


def _gate_spec(metrics_schema: Mapping[str, object] | None) -> GateSpec:
    schema = metrics_schema or {}
    objective = schema.get("objective") or {}
    return GateSpec(
        str(objective.get("key") or "OBJECTIVE"),
        tuple(
            str(item["key"])
            for item in schema.get("gates") or ()
            if item.get("key")
        ),
    )


def _evaluation_config(cfg: Mapping[str, object]) -> EvaluationConfig:
    spec = _gate_spec(cfg.get("metrics"))
    return EvaluationConfig(
        tuple(str(command) for command in cfg.get("eval_commands", ())),
        spec.objective_key,
        spec.gate_keys,
        int(cfg.get("eval_timeout_seconds", 600)),
        int(cfg.get("eval_output_cap_chars", 16000)),
    )


def build_ports(
    cfg: Mapping[str, object],
    run_dir: str | Path,
    workspace: SourceWorkspace,
    usage_observer=None,
    prompt_dir: str | Path | None = None,
) -> CandidatePorts:
    run_dir = Path(run_dir)
    sandbox = ApptainerSandbox()
    builder = WorldBuilder(sandbox)
    role = (cfg.get("roles") or {}).get("executor") or {}
    image = Path(cfg["runtime_image"])
    executor_spec = SandboxSpec(
        image,
        executor_environment(
            base_url=role.get("base_url"),
            max_output_tokens=int(cfg.get("agent_max_output_tokens", 64000)),
        ),
        True,
    )
    executor_world = builder.build(
        workspace,
        executor_spec,
        executor_world_spec(
            cfg.get("editable_paths", ()), cfg.get("read_only_binds", ()),
        ),
    )
    evaluator_world = builder.build(
        workspace,
        SandboxSpec(image, evaluator_environment(), True),
        evaluator_world_spec(cfg.get("runtime_binds", ())),
    )
    provider = GitWorkspaceProvider(
        run_dir, cfg["repo_path"], str(cfg["baseline_ref"]),
    )
    spec = _gate_spec(cfg.get("metrics"))
    return CandidatePorts(
        workspace,
        AgentExecutor(
            Agent(
                world=executor_world,
                command="claude",
                timeout_seconds=int(cfg.get("agent_timeout_seconds", 3600)),
                allowed_tools="Read,Edit,Write,Bash",
                model=role.get("model"),
                usage_observer=usage_observer,
            ),
            ExecutorConfig(
                str(cfg.get("goal") or ""),
                views.gate_block(cfg.get("metrics")),
                Path(prompt_dir) if prompt_dir else None,
            ),
        ),
        GitArtifactWorkspace(provider),
        HarnessEvaluator(evaluator_world, _evaluation_config(cfg)),
        spec,
        HandoffCandidateTrace(run_dir),
        lambda: sandbox.preflight(executor_spec),
    )


def _run_baseline_eval(ports: CandidatePorts, spec: CandidateSpec) -> CandidateResult:
    evaluation = ports.evaluator.evaluate(EvaluationRequest(spec.to_request().workspace))
    if evaluation.error:
        raise ValueError(f"baseline evaluation failed: {evaluation.error}")
    failed = [code for code in evaluation.returncodes if code != 0]
    if failed:
        raise ValueError(f"baseline evaluation failed: {evaluation.text[:1000]}")
    return CandidateResult(
        spec.candidate_id,
        f"r{spec.round_id}c{spec.candidate_id}",
        Proposal(spec.proposal),
        spec.parent_sha,
        CandidateStatus.BASELINE,
        ExecutionResult("BASELINE"),
        CandidateArtifact(spec.parent_sha, spec.parent_sha),
        evaluation,
        apply_gates(evaluation, ports.gate_spec),
    )


def _fallback_request(raw: Mapping[str, object]) -> CandidateRequest:
    round_id = int(raw.get("round_id") or 0)
    candidate_id = int(raw.get("candidate_id") or 0)
    parent = str(raw.get("parent_sha") or "")
    return CandidateRequest(
        round_id,
        candidate_id,
        parent,
        Proposal(str(raw.get("proposal") or "<missing proposal>")),
        SourceWorkspace(
            f"{round_id}-c{candidate_id}",
            Path(str(raw.get("worktree_path") or ".")),
            parent,
        ),
    )


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
