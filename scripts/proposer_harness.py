"""Standalone execution and review artifacts for the complete Proposer."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from simpleloop import config as config_mod
from simpleloop.container.runtime import ApptainerRuntime, RuntimePreflightError
from simpleloop.harness import memory
from simpleloop.harness import views
from simpleloop.harness.workspace import Workspace, WorkspaceError
from simpleloop.memory import MemoryService
from simpleloop.memory.models import (
    ExistingFindingTarget,
    NewFindingTarget,
    ResearchProposal,
)
from simpleloop.roles import model as model_mod
from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles.orchestrator import ProposerOrchestrator


class ProposerHarnessError(RuntimeError):
    """The standalone Proposer attempt could not be prepared safely."""


def _target_to_dict(target) -> dict:
    if isinstance(target, ExistingFindingTarget):
        return {"mode": "existing", "finding_id": target.finding_id}
    if isinstance(target, NewFindingTarget):
        return {
            "mode": "new",
            "question": target.question,
            "mechanisms": list(target.mechanisms),
            "code_regions": list(target.code_regions),
        }
    raise TypeError(f"unsupported research target: {type(target).__name__}")


def _proposal_to_dict(proposal: ResearchProposal) -> dict:
    return {
        "instruction": proposal.instruction,
        "research_target": _target_to_dict(proposal.research_target),
        "model_claim_refs": list(proposal.model_claim_refs),
        "explanation_refs": list(proposal.explanation_refs),
        "hypothesis_id": proposal.hypothesis_id,
        "evidence_refs": list(proposal.evidence_refs),
        "mechanism": proposal.mechanism,
        "prediction": proposal.prediction,
        "affected_scope": proposal.affected_scope,
    }


def _history_state(history_dir: Path, baseline_sha: str) -> tuple[str, int]:
    rows = memory.read_history(Path(history_dir) / "history.jsonl")
    base_sha = baseline_sha
    for row in rows:
        if row.get("selected_sha"):
            base_sha = str(row["selected_sha"])
    current_round = max(
        (int(row["round"]) for row in rows),
        default=-1,
    ) + 1
    return base_sha, current_round


def _render_target(target: dict) -> str:
    if target["mode"] == "existing":
        return f"existing finding `{target['finding_id']}`"
    tags = [
        *(target.get("mechanisms") or []),
        *(target.get("code_regions") or []),
    ]
    suffix = f" ({', '.join(tags)})" if tags else ""
    return f"new question: {target['question']}{suffix}"


def _render_proposals_markdown(result: dict) -> str:
    inputs = result.get("input") or {}
    lines = [
        "# Proposer Test Result",
        "",
        f"- Status: `{result.get('status', 'unknown')}`",
        f"- Base SHA: `{inputs.get('base_sha', '')}`",
        f"- History source: `{inputs.get('history_source') or 'none'}`",
        f"- Seed: `{inputs.get('seed') if inputs.get('seed') is not None else 'none'}`",
    ]
    proposals = result.get("proposals") or []
    if not proposals:
        lines.extend(["", "No proposals were submitted."])
        return "\n".join(lines) + "\n"
    for index, proposal in enumerate(proposals, 1):
        lines.extend([
            "",
            f"## Proposal {index}",
            "",
            proposal["instruction"],
            "",
            f"Research target: {_render_target(proposal['research_target'])}",
            f"Hypothesis: `{proposal['hypothesis_id']}`",
            f"Model claims: {', '.join(proposal['model_claim_refs'])}",
            f"Explanations: {', '.join(proposal['explanation_refs'])}",
            f"Mechanism: {proposal['mechanism']}",
            f"Prediction: {proposal['prediction']}",
            f"Affected scope: {proposal['affected_scope']}",
        ])
        evidence = proposal.get("evidence_refs") or []
        if evidence:
            lines.extend([
                "",
                "Evidence:",
                *[f"- `{ref}`" for ref in evidence],
            ])
    return "\n".join(lines) + "\n"

@dataclass(frozen=True)
class ProposerHarnessSummary:
    result_path: Path
    report_path: Path
    proposal_count: int
    abstained: bool
    base_sha: str


def _json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump())
    return str(value)


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _simpleloop_revision() -> str | None:
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def _completed_result(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("status") == "completed"


def run_proposer(
    config_path: str | Path,
    output_dir: str | Path,
    *,
    from_run: str | Path | None = None,
    seed: int | None = None,
) -> ProposerHarnessSummary:
    """Run one complete Proposer attempt and persist review artifacts."""
    config_path = Path(config_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    result_path = output_dir / "result.json"
    report_path = output_dir / "proposals.md"
    if _completed_result(result_path):
        raise ProposerHarnessError(
            f"completed proposer result already exists: {result_path}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    history_dir = (
        Path(from_run).expanduser().resolve()
        if from_run is not None
        else output_dir
    )
    if from_run is not None and not history_dir.is_dir():
        raise ProposerHarnessError(
            f"history run does not exist or is not a directory: {history_dir}"
        )

    started = time.monotonic()
    input_record = {
        "config_path": str(config_path),
        "history_source": str(history_dir) if from_run is not None else None,
        "seed": seed,
    }
    workspace = None
    worktree_created = False
    proposal_result = None
    caught: Exception | None = None
    cleanup_error: Exception | None = None
    usage_events: list[object] = []

    try:
        cfg = config_mod.load(config_path)
        researcher = (cfg.get("roles") or {}).get("researcher")
        if researcher is None:
            raise ProposerHarnessError(
                "roles.researcher is required for standalone proposer"
            )
        runtime = ApptainerRuntime(
            image=cfg["runtime_image"],
            binds=cfg["runtime_binds"],
            run_dir=output_dir,
        )
        runtime.preflight()
        workspace_repo = (
            history_dir / "repo"
            if from_run is not None
            else Path(cfg["repo_path"])
        )
        if not workspace_repo.is_dir():
            raise ProposerHarnessError(
                f"proposer source repository does not exist: {workspace_repo}"
            )
        workspace = Workspace(
            run_dir=output_dir,
            repo_path=str(workspace_repo),
            baseline_ref=cfg["baseline_ref"],
            editable=cfg["editable_paths"],
        )
        workspace.setup()
        baseline_sha = workspace.baseline_sha()
        base_sha, current_round = _history_state(history_dir, baseline_sha)
        input_record.update({
            "repo_path": str(workspace_repo),
            "configured_repo_path": cfg["repo_path"],
            "base_sha": base_sha,
            "simpleloop_revision": _simpleloop_revision(),
            "goal": cfg["goal"],
            "editable_paths": list(cfg["editable_paths"]),
            "frozen_paths": list(cfg["frozen_paths"]),
            "gate_block": views.gate_block(cfg.get("metrics")),
            "candidates_per_round": cfg["candidates_per_round"],
            "scientist_steps": cfg["scientist_steps"],
        })
        source_path = workspace.add_worktree("proposer-test", base_sha)
        worktree_created = True
        memory_service = MemoryService(
            run_dir=history_dir,
            metrics_schema=cfg["metrics"],
        )
        orchestrator = ProposerOrchestrator(
            model=model_mod.build_chat_model(researcher),
            runtime=runtime,
            timeout_seconds=cfg["agent_timeout_seconds"],
            command_timeout_seconds=researcher["command_timeout_seconds"],
            command_output_cap_chars=researcher["command_output_cap_chars"],
            usage_observer=lambda usage: usage_events.append(
                _json_safe(usage)
            ),
        )
        proposal_result = orchestrator.run(
            goal=cfg["goal"],
            editable=cfg["editable_paths"],
            frozen=cfg["frozen_paths"],
            memory_service=memory_service,
            base_sha=base_sha,
            source_path=source_path,
            repo_path=workspace.repo,
            run_dir=history_dir,
            current_round=current_round,
            candidates_per_round=cfg["candidates_per_round"],
            gate_block=input_record["gate_block"],
            prompt_dir=None,
            hints=cfg.get("hints") or None,
            scientist_steps=cfg["scientist_steps"],
            random_seed=seed,
        )
    except Exception as exc:
        caught = exc
    finally:
        if workspace is not None and worktree_created:
            try:
                workspace.remove_worktree("proposer-test")
            except Exception as exc:
                cleanup_error = exc

    failure = caught or cleanup_error
    elapsed = time.monotonic() - started
    if failure is not None:
        failed_result = {
            "status": "failed",
            "input": input_record,
            "proposals": [],
            "error": {
                "type": type(failure).__name__,
                "message": str(failure),
            },
            "elapsed_seconds": elapsed,
        }
        try:
            _write_json(result_path, failed_result)
        except OSError:
            pass
        raise failure

    assert proposal_result is not None
    result = {
        "status": "completed",
        "input": input_record,
        "proposals": [
            _proposal_to_dict(proposal)
            for proposal in proposal_result.proposals
        ],
        "abstained": proposal_result.abstained,
        "abstain_reason": proposal_result.abstain_reason,
        "abstain_blocking_unknown": (
            proposal_result.abstain_blocking_unknown
        ),
        "deliberation_telemetry": _json_safe(
            proposal_result.deliberation_telemetry
        ),
        "trace": _json_safe(proposal_result.trace),
        "usage": usage_events or _json_safe(proposal_result.usage),
        "elapsed_seconds": elapsed,
    }
    _write_json(result_path, result)
    report_path.write_text(
        _render_proposals_markdown(result), encoding="utf-8"
    )
    return ProposerHarnessSummary(
        result_path=result_path,
        report_path=report_path,
        proposal_count=len(result["proposals"]),
        abstained=bool(proposal_result.abstained),
        base_sha=input_record["base_sha"],
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the complete SimpleLoop Proposer without Executor or "
            "evaluation."
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--from-run")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(argv)
    try:
        summary = run_proposer(
            args.config,
            args.output_dir,
            from_run=args.from_run,
            seed=args.seed,
        )
    except (
        config_mod.ConfigError,
        RuntimePreflightError,
        model_mod.ModelError,
        proposer_mod.ProposerError,
        WorkspaceError,
        ProposerHarnessError,
        ValueError,
    ) as exc:
        print(f"Proposer error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(
        f"Generated {summary.proposal_count} proposal(s) from "
        f"{summary.base_sha}."
    )
    print(f"  result: {summary.result_path}")
    print(f"  report: {summary.report_path}")


if __name__ == "__main__":
    main()
