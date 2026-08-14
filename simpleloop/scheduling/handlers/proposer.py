"""Lazy Host adapter for the independently packaged proposer."""
from __future__ import annotations

import dataclasses
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


@dataclass(frozen=True)
class ProposerLaneSpec:
    lane_id: int
    round_id: int
    base_sha: str
    run_dir: str = ""
    workspace_path: str = ""
    result_dir: str = ""
    prompt_dir: str = ""
    proposal_slots: int = 1
    scientist_steps: int = 200
    attempt: int = 1
    mode: str = "task"
    self_repo: str = ""
    reviews_path: str = ""
    incumbent_self_sha: str = ""

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ProposerLaneSpec":
        return cls(
            int(raw["lane_id"]),
            int(raw["round_id"]),
            str(raw["base_sha"]),
            str(raw.get("run_dir") or ""),
            str(raw.get("workspace_path") or ""),
            str(raw.get("result_dir") or ""),
            str(raw.get("prompt_dir") or ""),
            int(raw.get("proposal_slots") or 1),
            int(raw.get("scientist_steps") or 200),
            int(raw.get("attempt") or 1),
            str(raw.get("mode") or "task"),
            str(raw.get("self_repo") or ""),
            str(raw.get("reviews_path") or ""),
            str(raw.get("incumbent_self_sha") or ""),
        )


@dataclass(frozen=True)
class ProposerLaneDeps:
    cfg: Mapping[str, object]
    run_dir: Path
    runtime: object
    repo_path: Path
    orchestrator: object
    memory_service: object
    prompt_dir: Path | None = None
    gate_lines: str = ""


def _redirect(payload: Mapping[str, object]) -> None:
    run_dir = Path(str(payload.get("run_dir") or ".")).resolve()
    selected = payload.get("self_repo")
    self_repo = (
        Path(str(selected)).resolve()
        if selected else run_dir / "self" / "repo"
    )
    if self_repo.is_dir() and str(self_repo) not in sys.path:
        sys.path.insert(0, str(self_repo))


def build_lane_deps(
    cfg: Mapping[str, object],
    run_dir: str | Path,
    *,
    usage_observer=None,
    prompt_dir: str | Path | None = None,
) -> ProposerLaneDeps:
    from proposer import model as model_mod
    from proposer.memory import MemoryService
    from proposer.orchestrator import ProposerOrchestrator
    from proposer.runtime import ApptainerRuntime
    from proposer.scientist import ContextPolicy
    from ...stages.gate import gate_block

    run_dir = Path(run_dir)
    runtime = ApptainerRuntime(
        image=cfg["runtime_image"],
        binds=cfg["runtime_binds"],
        run_dir=run_dir,
        userns=bool(cfg.get("sandbox_userns", True)),
    )
    researcher = (cfg.get("roles") or {}).get("researcher") or {}
    orchestrator = ProposerOrchestrator(
        model=model_mod.build_chat_model(researcher),
        runtime=runtime,
        timeout_seconds=int(cfg.get("agent_timeout_seconds", 3600)),
        command_timeout_seconds=int(researcher.get("command_timeout_seconds", 120)),
        command_output_cap_chars=int(researcher.get("command_output_cap_chars", 12000)),
        usage_observer=usage_observer,
        context_policy=ContextPolicy.from_config(cfg.get("context")),
    )
    return ProposerLaneDeps(
        cfg,
        run_dir,
        runtime,
        run_dir / "repo",
        orchestrator,
        MemoryService(run_dir=run_dir, metrics_schema=cfg.get("metrics") or {}),
        Path(prompt_dir) if prompt_dir else None,
        gate_block(cfg.get("metrics")),
    )


def proposal_from_dict(raw: Mapping[str, object]):
    from proposer.memory.models import (
        ExistingFindingTarget,
        NewFindingTarget,
        ResearchProposal,
    )

    target = raw.get("research_target") or {}
    if not isinstance(target, Mapping):
        research_target = NewFindingTarget(question=str(target))
    elif "finding_id" in target:
        research_target = ExistingFindingTarget(finding_id=str(target["finding_id"]))
    else:
        research_target = NewFindingTarget(
            question=str(target.get("question") or ""),
            mechanisms=tuple(target.get("mechanisms") or ()),
            code_regions=tuple(target.get("code_regions") or ()),
        )
    return ResearchProposal(
        instruction=str(raw.get("instruction") or ""),
        research_target=research_target,
        evidence_refs=tuple(raw.get("evidence_refs") or ()),
        material_difference=raw.get("material_difference"),
    )


def _lane_result_to_dict(result) -> dict[str, object]:
    return {
        "status": "COMPLETED",
        "lane_id": result.lane_id,
        "outcome": result.outcome,
        "proposals": [dataclasses.asdict(item) for item in result.proposals or ()],
        "reason_kind": result.reason_kind,
        "explanation": result.explanation,
        "abstain_reason": result.abstain_reason,
        "trace": result.trace or {},
        "telemetry": result.deliberation_telemetry or {},
    }


def run_lane(deps: ProposerLaneDeps, spec: ProposerLaneSpec) -> dict[str, object]:
    from proposer.runtime import world_mount_map

    result = deps.orchestrator.run_lane_episode(
        lane_id=spec.lane_id,
        workspace=Path(spec.workspace_path),
        base_sha=spec.base_sha,
        goal=deps.cfg["goal"],
        editable=deps.cfg["editable_paths"],
        frozen=[],
        world_mount=world_mount_map(deps.cfg),
        memory_service=deps.memory_service,
        repo_path=deps.repo_path,
        run_dir=deps.run_dir,
        current_round=spec.round_id,
        gate_block=deps.gate_lines,
        prompt_dir=deps.prompt_dir,
        hints=deps.cfg.get("hints") or None,
        proposal_slots=spec.proposal_slots,
        scientist_steps=spec.scientist_steps,
    )
    return _lane_result_to_dict(result)


def _contract_version() -> str:
    import proposer
    return proposer.CONTRACT_VERSION


def _self_review_result(spec: ProposerLaneSpec, result, sha: str) -> dict[str, object]:
    change = result.self_change or {}
    return {
        "status": "COMPLETED",
        "mode": "self",
        "lane_id": spec.lane_id,
        "round_id": spec.round_id,
        "self_review": {
            "contract_version": _contract_version(),
            "incumbent_self_sha": sha,
            "decision": result.decision,
            "diagnosis": result.diagnosis,
            "keep_reason": result.keep_reason,
            "next_review_after_rounds": result.next_review_after_rounds,
            "self_change": ({
                "target": change.get("target"),
                "intent": change.get("intent"),
                "instruction": change.get("instruction"),
                "evidence_refs": list(change.get("evidence_refs") or ()),
            } if change else None),
            "abstained": result.abstained,
        },
        "trace": result.trace or {},
        "telemetry": result.deliberation_telemetry or {},
    }


def run_self_review_lane(
    deps: ProposerLaneDeps,
    spec: ProposerLaneSpec,
) -> dict[str, object]:
    self_repo = Path(spec.self_repo)
    reviews_path = Path(spec.reviews_path)
    if not self_repo.is_dir() or not spec.incumbent_self_sha:
        raise ValueError("self-review manifest has no active self body")
    metrics = deps.cfg.get("metrics") or {}
    result = deps.orchestrator.run_self_review(
        self_repo=self_repo,
        run_dir=deps.run_dir,
        reviews_path=reviews_path,
        incumbent_self_sha=spec.incumbent_self_sha,
        goal=deps.cfg["goal"],
        objective_key=(metrics.get("objective") or {}).get("key"),
        current_round=spec.round_id,
        prompt_dir=deps.prompt_dir,
        memory_service=deps.memory_service,
        scientist_steps=spec.scientist_steps,
    )
    return _self_review_result(spec, result, spec.incumbent_self_sha)


def _failure_result(spec: ProposerLaneSpec, reason: str) -> dict[str, object]:
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


def _self_review_failure_result(
    spec: ProposerLaneSpec,
    reason: str,
) -> dict[str, object]:
    return {
        "status": "COMPLETED",
        "mode": "self",
        "lane_id": spec.lane_id,
        "round_id": spec.round_id,
        "self_review": {
            "contract_version": _contract_version(),
            "incumbent_self_sha": None,
            "decision": "KEEP",
            "diagnosis": reason,
            "keep_reason": "self-review did not complete; defaulting to KEEP "
                           "pending a successful review",
            "next_review_after_rounds": 3,
            "self_change": None,
            "abstained": True,
        },
        "trace": {},
        "telemetry": {"tool_calls": 0},
    }


def _run(
    payload: Mapping[str, object],
    observe_usage: Callable[[Mapping[str, object]], None],
    *,
    mode: str,
) -> Mapping[str, object]:
    _redirect(payload)
    from ... import config as config_mod

    raw = dict(payload)
    raw["mode"] = "self" if mode == "self" else "task"
    spec = ProposerLaneSpec.from_dict(raw)
    try:
        run_dir = Path(spec.run_dir)
        deps = build_lane_deps(
            config_mod.load_resolved(run_dir),
            run_dir,
            usage_observer=observe_usage,
            prompt_dir=spec.prompt_dir or None,
        )
        deps.runtime.preflight()
        if mode == "self":
            return run_self_review_lane(deps, spec)
        return run_lane(deps, spec)
    except Exception as exc:
        if mode == "self":
            return _self_review_failure_result(spec, f"worker failed: {exc}")
        return _failure_result(spec, f"worker failed: {exc}")


def handle_proposer(payload, observe_usage):
    return _run(payload, observe_usage, mode="task")


def handle_self_review(payload, observe_usage):
    return _run(payload, observe_usage, mode="self")


def handle_viability(payload, observe_usage):
    return _run(payload, observe_usage, mode="viability")
