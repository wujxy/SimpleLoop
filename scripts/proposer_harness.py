"""Standalone runner for the Scientist proposer — no Executor, no evaluation.

Runs exactly one Scientist research round against a task config and persists
review artifacts (result.json + proposals.md) plus the Scientist's own
continuity store (scientists/lane-0/session.jsonl + notebook.md). This is the
fast way to observe how the Scientist reasons about a problem without paying
the cost of running executors or gates.

Mirrors ``LocalBackend.run_proposer_lanes`` exactly — same world-mount
construction, same single-lane workspace, same orchestrator call — so what you
see here is what a real round-0 (or resumed) proposer phase produces, up to the
point where the Loop would hand proposals to the Executor.

Usage:
    python scripts/proposer_harness.py \\
        --config examples/omilrec-post-v107-opt/task.yaml \\
        --output-dir examples/runs/omilrec-proposer-smoke

    # resume the resident Scientist from an existing run-dir's history:
    python scripts/proposer_harness.py \\
        --config examples/omilrec-post-v107-opt/task.yaml \\
        --output-dir examples/runs/omilrec-proposer-r1 \\
        --from-run examples/runs/omilrec-proposer-smoke
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from simpleloop import config as config_mod
from proposer.runtime import (
    ApptainerRuntime,
    RuntimePreflightError,
    world_mount_map,
)
from simpleloop.persistence import history
from simpleloop.stages.gate import gate_block
from simpleloop.world import WorkspaceError
from simpleloop.world.git import GitWorkspaceProvider
from proposer.memory import MemoryService
from proposer.memory.models import (
    ExistingFindingTarget,
    NewFindingTarget,
    ResearchProposal,
)
from proposer import model as model_mod
from proposer import scientist as proposer_mod
from proposer.orchestrator import ProposerOrchestrator


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
    """Serialize one proposal. ``ResearchProposal`` carries only the
    instruction, its research target, and round-local justification
    (evidence_refs / material_difference) — the hypothesis-era fields are gone.
    """
    return {
        "instruction": proposal.instruction,
        "research_target": _target_to_dict(proposal.research_target),
        "evidence_refs": list(proposal.evidence_refs),
        "material_difference": proposal.material_difference,
    }


def _history_state(history_dir: Path, baseline_sha: str) -> tuple[str, int]:
    """Reconstruct (base_sha, current_round) from a run-dir's history.jsonl.

    base_sha advances to the last selected candidate's sha; round is one past
    the last recorded round. With no history, falls back to baseline_sha / 0."""
    rows = history.read_history(Path(history_dir) / "history.jsonl")
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
        "# Scientist Proposer Result",
        "",
        f"- Status: `{result.get('status', 'unknown')}`",
        f"- Base SHA: `{inputs.get('base_sha', '')}`",
        f"- History source: `{inputs.get('history_source') or 'none'}`",
        f"- Round: `{inputs.get('current_round', 0)}`",
        f"- Scientist steps budget: `{inputs.get('scientist_steps', '')}`",
    ]
    proposals = result.get("proposals") or []
    if not proposals:
        lines.extend([
            "",
            f"No proposals were submitted (abstained: {result.get('abstained')}).",
            f"Reason: {result.get('abstain_reason') or '(none)'}",
        ])
        return "\n".join(lines) + "\n"
    for index, proposal in enumerate(proposals, 1):
        lines.extend([
            "",
            f"## Proposal {index}",
            "",
            proposal["instruction"],
            "",
            f"Research target: {_render_target(proposal['research_target'])}",
        ])
        if proposal.get("material_difference"):
            lines.append(f"Material difference: {proposal['material_difference']}")
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
    current_round: int


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
) -> ProposerHarnessSummary:
    """Run one Scientist research round and persist review artifacts."""
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
    }
    workspace = None
    lane = None
    proposal_result = None
    caught: Exception | None = None
    cleanup_error: Exception | None = None
    usage_events: list[object] = []

    try:
        cfg = config_mod.load(config_path)
        researcher = (cfg.get("roles") or {}).get("researcher")
        if researcher is None:
            raise ProposerHarnessError(
                "proposer is required for standalone proposer"
            )
        runtime = ApptainerRuntime(
            image=cfg["runtime_image"],
            binds=cfg["runtime_binds"],
            run_dir=output_dir,
            userns=bool(cfg.get("sandbox_userns", True)),
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
        workspace = GitWorkspaceProvider(
            run_dir=output_dir,
            repo_path=str(workspace_repo),
            baseline_ref=cfg["baseline_ref"],
        )
        if from_run is not None:
            # Reuse the history run's already-cloned repo; just stage a fresh
            # proposer worktree under this output dir.
            workspace.repo = workspace_repo
            workspace.wt_root = output_dir / "worktrees"
        else:
            workspace.initialize()
        baseline_sha = workspace.baseline_sha()
        base_sha, current_round = _history_state(history_dir, baseline_sha)
        input_record.update({
            "workspace_path": str(workspace_repo),
            "base_sha": base_sha,
            "current_round": current_round,
            "simpleloop_revision": _simpleloop_revision(),
            "goal": cfg["goal"],
            "editable_paths": list(cfg["editable_paths"]),
            "gate_block": gate_block(cfg.get("metrics")),
            "candidates_per_round": cfg["candidates_per_round"],
            "scientist_steps": cfg["scientist_steps"],
        })
        lane = workspace.create_lane("proposer-test", base_sha)
        source_path = lane.path
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
        # Same call shape as LocalBackend.run_proposer_lanes: the writable
        # world is mount-enforced via world_mount_map; the Scientist runs in a
        # single disposable lane worktree.
        proposal_result = orchestrator.run(
            goal=cfg["goal"],
            editable=list(cfg["editable_paths"]),
            frozen=[],
            world_mount=world_mount_map(cfg),
            memory_service=memory_service,
            base_sha=base_sha,
            workspaces=[source_path],
            repo_path=workspace.repo,
            run_dir=history_dir,
            current_round=current_round,
            candidates_per_round=cfg["candidates_per_round"],
            gate_block=input_record["gate_block"],
            prompt_dir=None,
            hints=cfg.get("hints") or None,
            scientist_steps=cfg["scientist_steps"],
        )
    except Exception as exc:
        caught = exc
    finally:
        if workspace is not None and lane is not None:
            try:
                workspace.remove_lane(lane)
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
        current_round=input_record["current_round"],
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the Scientist proposer for one round without Executor or "
            "evaluation — observe how it reasons about a task."
        )
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--from-run",
        help="Resume the resident Scientist from this run-dir's history "
        "(reads its history.jsonl + scientists/ session).",
    )
    args = parser.parse_args(argv)
    try:
        summary = run_proposer(
            args.config,
            args.output_dir,
            from_run=args.from_run,
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
    abstain = " (abstained)" if summary.abstained else ""
    print(
        f"[harness] round {summary.current_round}: "
        f"{summary.proposal_count} proposal(s){abstain} from "
        f"{summary.base_sha[:10]} ({'resumed' if args.from_run else 'cold start'})."
    )
    print(f"  result:   {summary.result_path}")
    print(f"  report:   {summary.report_path}")
    print(f"  session:  {Path(args.output_dir).resolve() / 'scientists' / 'lane-0'}")


if __name__ == "__main__":
    main()
