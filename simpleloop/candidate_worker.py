"""CandidateWorker: one candidate's factual execution — executor ->
path gate/commit -> harness eval/gates -> result dict.

Business only: the worktree lifecycle belongs to the execution backends
(they create it before, remove it after), and job management belongs to
HEPJobBackend. The worker is launchable standalone:

  python -m simpleloop.candidate_worker --manifest /path/manifest.json \
      --job-id 12345.0

Completion contract for remote execution: result.json (pure business
result) plus usage.json (telemetry/audit sidecar) are written atomically
and _FINISHED touched last in result_dir. ANY business-side failure
(executor/eval error, worker bug) must still produce all of them —
a missing _FINISHED means the process was killed by infrastructure
(condor_rm/OOM/node death), and only then is a job-level retry meaningful.

This module deliberately avoids importing simpleloop.loop (which pulls in
matplotlib); the worker's dependency chain is stdlib + pyyaml so a bare
compute-node python can run it.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import socket
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from .container.runtime import ApptainerRuntime, world_mount_map
from .harness import evals, gate, views
from .harness.handoff import write_handoff
from .harness.workspace import Workspace
from .roles import executor as executor_mod
from .roles.agent import Agent, AgentError


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class CandidateSpec:
    """Serializable input for one candidate; persisted as manifest.json."""
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
        # Tolerate unknown fields (e.g. legacy ``finding_id`` from manifests
        # written before S2c) so --continue across the version boundary does
        # not crash. Manifests are machine-generated, so strict checking buys
        # little.
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


@dataclass
class CandidateDeps:
    """The runtime fixtures run_candidate needs. Either mapped from the
    frontend's RunContext (local backend) or rebuilt from the resolved
    config (standalone worker CLI on a compute node)."""
    cfg: dict
    run_dir: Path
    runtime: ApptainerRuntime
    workspace: Workspace
    executor_agent: Agent
    prompt_dir: Path | None = None
    gate_lines: str = ""

    @property
    def metrics_schema(self) -> dict | None:
        return self.cfg.get("metrics")


def build_deps(
    cfg: dict,
    run_dir: str | Path,
    usage_observer=None,
    prompt_dir: str | Path | None = None,
) -> CandidateDeps:
    """Rebuild worker dependencies from a resolved config. Does NOT call
    workspace.setup() (no clone) — the frontend owns the shared repo; the
    worker only commits through its given worktree."""
    run_dir = Path(run_dir)
    runtime = ApptainerRuntime(
        image=cfg["runtime_image"],
        binds=cfg["runtime_binds"],
        run_dir=run_dir,
    )
    timeout = cfg.get("agent_timeout_seconds", 3600)
    max_output_tokens = cfg.get("agent_max_output_tokens", 64000)
    executor = (cfg.get("roles") or {}).get("executor") or {}
    executor_agent = Agent(runtime=runtime, command="claude",
                           timeout_seconds=timeout,
                           allowed_tools="Read,Edit,Write,Bash",
                           max_output_tokens=max_output_tokens,
                           model=executor.get("model"),
                           base_url=executor.get("base_url"),
                           usage_observer=usage_observer,
                           mounts=world_mount_map(cfg))
    workspace = Workspace(
        run_dir=run_dir,
        repo_path=cfg["repo_path"],
        baseline_ref=cfg["baseline_ref"],
        editable=cfg["editable_paths"],
    )
    return CandidateDeps(
        cfg=cfg, run_dir=run_dir, runtime=runtime, workspace=workspace,
        executor_agent=executor_agent,
        prompt_dir=Path(prompt_dir) if prompt_dir else None,
        gate_lines=views.gate_block(cfg.get("metrics")),
    )


def run_candidate(deps: CandidateDeps, spec: CandidateSpec) -> dict:
    """Execute and evaluate one candidate. The worktree at
    spec.worktree_path must already exist; the caller removes it."""
    cfg = deps.cfg
    metrics_schema = deps.metrics_schema
    worktree = Path(spec.worktree_path)
    worktree_id = f"{spec.round_id}-c{spec.candidate_id}"
    try:
        print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
              f"proposal: {spec.proposal[:120]}", flush=True)
        result = executor_mod.execute(
            deps.executor_agent, proposal=spec.proposal, goal=cfg["goal"],
            workspace=deps.workspace, worktree=worktree, round_id=worktree_id,
            gate_block=deps.gate_lines,
            prompt_dir=deps.prompt_dir,
        )
    except (AgentError, ValueError) as exc:
        print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
              f"executor failed: {exc}", flush=True)
        write_handoff(deps.run_dir, spec.round_id,
                      f"{worktree_id}.executor.json", {
            "candidate_id": spec.candidate_id,
            "proposal": spec.proposal,
            "parent_sha": spec.parent_sha,
            "status": "EXECUTOR_FAILED",
            "error": str(exc),
        })
        return candidate_failure(
            spec.candidate_id, spec, str(exc), spec.parent_sha,
            metrics_schema=metrics_schema, status="EXECUTOR_FAILED",
        )

    # Persist executor output immediately — the agent's response text is
    # discarded by the executor, so capture it here for traceability.
    write_handoff(deps.run_dir, spec.round_id,
                  f"{worktree_id}.executor.json", {
        "candidate_id": spec.candidate_id,
        "proposal": spec.proposal,
        "parent_sha": spec.parent_sha,
        "sha": result.sha,
        "status": ("COMMITTED" if result.sha else "NO_CHANGE"),
        "changed_paths": result.changed_paths,
        "reason": result.reason,
        "executor_response": result.output,
        "self_report": result.self_report,
    })

    if result.sha is None:
        detail = "not run because Executor produced no change"
        gate_results = gate.build_results(metrics_schema, paths=True)
        for name in gate_results:
            if name != gate.PATHS:
                gate_results[name] = {"passed": None, "detail": detail}
        return _candidate_result(
            spec, result, status="NO_CHANGE", gates=gate_results,
        )

    print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
          f"committed: {result.sha} ({len(result.changed_paths)} files)",
          flush=True)
    try:
        eval_result = evals.run_eval(
            cfg["eval_commands"],
            cwd=worktree,
            runtime=deps.runtime,
            metrics_schema=metrics_schema,
            timeout_seconds=cfg.get("eval_timeout_seconds", 600),
            output_cap=cfg.get("eval_output_cap_chars", 16000),
        )
    except Exception as exc:
        eval_block = f"(eval failed to run: {exc})"
        print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
              f"eval error: {exc}", flush=True)
        gate_results = gate.build_results(
            metrics_schema,
            paths=True,
            eval_commands=False,
            eval_detail=str(exc),
        )
        write_handoff(deps.run_dir, spec.round_id,
                      f"{worktree_id}.eval.json", {
            "candidate_id": spec.candidate_id,
            "sha": result.sha,
            "status": "EVAL_FAILED",
            "error": str(exc),
            "gates": gate_results,
        })
        return _candidate_result(
            spec,
            result,
            status="EVAL_FAILED",
            gates=gate_results,
            eval_block=eval_block,
        )

    eval_detail = "" if eval_result.commands_ok else (
        f"exit codes: {list(eval_result.returncodes)}"
    )
    gate_results = gate.build_results(
        metrics_schema,
        paths=True,
        eval_commands=eval_result.commands_ok,
        eval_detail=eval_detail,
        metrics=eval_result.metrics,
    )
    gate_passed = gate.all_passed(gate_results)
    eligible = _eligible(result.sha, gate_passed, eval_result.metrics,
                         metrics_schema)
    write_handoff(deps.run_dir, spec.round_id,
                  f"{worktree_id}.eval.json", {
        "candidate_id": spec.candidate_id,
        "sha": result.sha,
        "status": "COMPLETED" if gate_passed else "GATE_REJECTED",
        "metrics": eval_result.metrics,
        "gates": gate_results,
        "gate_passed": gate_passed,
        "eligible": eligible,
        "eval_block": eval_result.text,
        "returncodes": list(eval_result.returncodes),
        "commands_ok": eval_result.commands_ok,
    })
    return _candidate_result(
        spec,
        result,
        status="COMPLETED" if gate_passed else "GATE_REJECTED",
        gates=gate_results,
        eval_block=eval_result.text,
        metrics=eval_result.metrics,
        gate_passed=gate_passed,
        eligible=eligible,
    )


def _eligible(sha: str | None, gate_passed: bool, metrics: dict,
              metrics_schema: dict | None) -> bool:
    if not sha or not gate_passed or not metrics_schema:
        return False
    objective_key = metrics_schema["objective"]["key"]
    objective = metrics.get(objective_key)
    return (
        isinstance(objective, (int, float))
        and not isinstance(objective, bool)
        and math.isfinite(objective)
    )


def _candidate_result(
    spec: CandidateSpec,
    result: executor_mod.ExecResult,
    *,
    status: str,
    gates: dict,
    eval_block: str = "",
    metrics: dict | None = None,
    gate_passed: bool = False,
    eligible: bool = False,
) -> dict:
    return {
        "candidate": spec.candidate_id,
        "experiment_id": f"r{spec.round_id}c{spec.candidate_id}",
        "proposal": spec.proposal,
        "parent_sha": spec.parent_sha,
        "sha": result.sha,
        "status": status,
        "eval_block": eval_block,
        "metrics": metrics or {},
        "changed_paths": result.changed_paths,
        "gates": gates,
        "gate_passed": gate_passed,
        "eligible": eligible,
        "selected": False,
        "self_report": result.self_report,
    }


def _run_baseline_eval(deps: CandidateDeps, spec: CandidateSpec, cfg: dict) -> dict:
    """Run only the evaluation part for baseline assessment.

    Skips the Executor and just runs eval commands to get metrics.
    """
    print(f"[{stamp()}] running baseline eval on worktree {spec.worktree_path}", flush=True)

    # Run the eval commands
    result = evals.run_eval(
        cfg["eval_commands"],
        cwd=Path(spec.worktree_path),
        runtime=deps.runtime,
        metrics_schema=cfg.get("metrics"),
        timeout_seconds=cfg.get("eval_timeout_seconds", 600),
        output_cap=cfg.get("eval_output_cap_chars", 16000),
    )

    # Validate the result
    if not result.commands_ok:
        raise ValueError(f"baseline evaluation failed: {result.text[:1000]}")

    gate_results = gate.build_results(
        cfg.get("metrics"),
        paths=True,
        eval_commands=result.commands_ok,
        metrics=result.metrics,
    )
    gate_passed = gate.all_passed(gate_results)
    return {
        "candidate": spec.candidate_id,
        "experiment_id": f"r{spec.round_id}c{spec.candidate_id}",
        "proposal": spec.proposal,
        "parent_sha": spec.parent_sha,
        "sha": spec.parent_sha,
        "status": "BASELINE",
        "eval_block": result.text,
        "metrics": result.metrics,
        "changed_paths": [],
        "gates": gate_results,
        "gate_passed": gate_passed,
        "self_report": None,
        "eligible": _eligible(
            spec.parent_sha, gate_passed, result.metrics, cfg.get("metrics"),
        ),
        "selected": False,
    }


def candidate_failure(candidate_id: int, spec: CandidateSpec,
                      reason: str, parent_sha: str, sha: str | None = None,
                      eval_block: str = "", eval_metrics: dict | None = None,
                      changed_paths: list[str] | None = None,
                      gate_results: dict | None = None,
                      metrics_schema: dict | None = None,
                      status: str = "WORKER_FAILED") -> dict:
    return {
        "candidate": candidate_id,
        "experiment_id": f"r{spec.round_id}c{candidate_id}",
        "proposal": spec.proposal,
        "parent_sha": parent_sha,
        "sha": sha,
        "status": status,
        "eval_block": eval_block or f"[loop failure] {reason[:200]}",
        "metrics": eval_metrics or {},
        "changed_paths": changed_paths or [],
        "gates": gate_results or gate.build_results(
            metrics_schema, paths=None,
        ),
        "gate_passed": False,
        "eligible": False,
        "selected": False,
        "self_report": None,
    }


def write_result(result_dir: str | Path, result: dict,
                 *, sidecar: dict | None = None) -> None:
    """Atomic terminal output: result.json.tmp -> rename, then the sidecar
    (usage/audit meta for the frontend's telemetry), _FINISHED last.
    The backend only reads the result once _FINISHED exists."""
    result_dir = Path(result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    tmp = result_dir / "result.json.tmp"
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                              default=str) + "\n", encoding="utf-8")
    os.replace(tmp, result_dir / "result.json")
    if sidecar is not None:
        meta_tmp = result_dir / "usage.json.tmp"
        meta_tmp.write_text(json.dumps(sidecar, ensure_ascii=False, indent=2,
                                       default=str) + "\n", encoding="utf-8")
        os.replace(meta_tmp, result_dir / "usage.json")
    (result_dir / "_FINISHED").touch()


def main(argv: list[str] | None = None) -> int:
    """Standalone worker entry. Exit 0 whenever a terminal result could be
    written (business failures included); non-zero only when the manifest
    or result path is unusable — i.e. the failure is infrastructure-grade
    and the backend's lost-job retry applies."""
    parser = argparse.ArgumentParser(prog="candidate_worker")
    parser.add_argument("--manifest", required=True,
                        help="Path to the candidate manifest JSON.")
    parser.add_argument("--job-id", default=None,
                        help="Scheduler job id (recorded in result.execution).")
    parser.add_argument("--baseline-only", action="store_true",
                        help="Only run eval for baseline, skip the Executor.")
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
        spec = CandidateSpec.from_dict(spec_dict)
        deps = build_deps(
            cfg, run_dir, usage_observer=usage.append,
            prompt_dir=spec.prompt_dir or None,
        )
        deps.runtime.preflight()
        if args.baseline_only:
            result = _run_baseline_eval(deps, spec, cfg)
        else:
            result = run_candidate(deps, spec)
    except Exception as exc:
        # Catch-all invariant: a business-side failure still produces a
        # terminal result; _FINISHED absence must mean "killed by infra".
        print(f"[{stamp()}] worker failed before/at business execution: {exc}",
              flush=True)
        result = candidate_failure(
            int(spec_dict.get("candidate_id") or 0),
            CandidateSpec(
                round_id=int(spec_dict.get("round_id") or 0),
                candidate_id=int(spec_dict.get("candidate_id") or 0),
                parent_sha=str(spec_dict.get("parent_sha") or ""),
                proposal=str(spec_dict.get("proposal") or ""),
            ),
            f"worker failed: {exc}",
            str(spec_dict.get("parent_sha") or ""),
            status="WORKER_FAILED",
        )
    # result.json stays pure business; telemetry usage and execution audit
    # travel in the usage.json sidecar for the frontend to ingest.
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
