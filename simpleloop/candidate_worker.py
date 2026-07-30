"""CandidateWorker: one candidate's full business execution — executor ->
gate/commit -> eval -> judger -> result dict.

Business only: the worktree lifecycle belongs to the execution backends
(they create it before, remove it after), and job management belongs to
HEPJobBackend. The worker is launchable standalone:

  python -m simpleloop.candidate_worker --manifest /path/manifest.json \
      --job-id 12345.0

Completion contract for remote execution: result.json (pure business
result) plus usage.json (telemetry/audit sidecar) are written atomically
and _FINISHED touched last in result_dir. ANY business-side failure
(executor/eval/judger error, worker bug) must still produce all of them —
a missing _FINISHED means the process was killed by infrastructure
(condor_rm/OOM/node death), and only then is a job-level retry meaningful.

This module deliberately avoids importing simpleloop.loop (which pulls in
matplotlib); the worker's dependency chain is stdlib + pyyaml so a bare
compute-node python can run it.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config as config_mod
from .container.runtime import ApptainerRuntime
from .harness import evals, views
from .harness.workspace import Workspace
from .roles import executor as executor_mod
from .roles import judger as judger_mod
from .roles.agent import Agent, AgentError


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class CandidateSpec:
    """Serializable input for one candidate; persisted as manifest.json."""
    round_id: int
    candidate_id: int
    parent_sha: str
    family: str
    decision: str
    proposal: str
    run_dir: str = ""
    prior_metrics: dict = field(default_factory=dict)
    baseline_metrics: dict = field(default_factory=dict)
    worktree_path: str = ""
    result_dir: str = ""
    attempt: int = 1

    def to_dict(self) -> dict:
        return {
            "round_id": self.round_id,
            "candidate_id": self.candidate_id,
            "parent_sha": self.parent_sha,
            "family": self.family,
            "decision": self.decision,
            "proposal": self.proposal,
            "run_dir": self.run_dir,
            "prior_metrics": dict(self.prior_metrics or {}),
            "baseline_metrics": dict(self.baseline_metrics or {}),
            "worktree_path": self.worktree_path,
            "result_dir": self.result_dir,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CandidateSpec":
        return cls(
            round_id=int(data["round_id"]),
            candidate_id=int(data["candidate_id"]),
            parent_sha=str(data["parent_sha"]),
            family=str(data.get("family") or "single"),
            decision=str(data.get("decision") or ""),
            proposal=str(data["proposal"]),
            run_dir=str(data.get("run_dir") or ""),
            prior_metrics=dict(data.get("prior_metrics") or {}),
            baseline_metrics=dict(data.get("baseline_metrics") or {}),
            worktree_path=str(data.get("worktree_path") or ""),
            result_dir=str(data.get("result_dir") or ""),
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
    judger_agent: Agent
    gate_lines: str = ""

    @property
    def metrics_schema(self) -> dict | None:
        return self.cfg.get("metrics")


def build_deps(cfg: dict, run_dir: str | Path,
               usage_observer=None) -> CandidateDeps:
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
    executor_agent = Agent(runtime=runtime, command="claude",
                           timeout_seconds=timeout,
                           allowed_tools="Read,Edit,Write,Bash",
                           max_output_tokens=max_output_tokens,
                           usage_observer=usage_observer)
    judger_agent = Agent(runtime=runtime, command="claude",
                         timeout_seconds=timeout,
                         allowed_tools="Read,Bash",
                         max_output_tokens=max_output_tokens,
                         usage_observer=usage_observer)
    workspace = Workspace(
        run_dir=run_dir,
        repo_path=cfg["repo_path"],
        baseline_ref=cfg["baseline_ref"],
        editable=cfg["editable_paths"],
    )
    return CandidateDeps(
        cfg=cfg, run_dir=run_dir, runtime=runtime, workspace=workspace,
        executor_agent=executor_agent, judger_agent=judger_agent,
        gate_lines=views.gate_block(cfg.get("metrics")),
    )


def run_candidate(deps: CandidateDeps, spec: CandidateSpec) -> dict:
    """Executor + eval + judger for one candidate. The worktree at
    spec.worktree_path must already exist; the caller removes it."""
    cfg = deps.cfg
    metrics_schema = deps.metrics_schema
    worktree = Path(spec.worktree_path)
    worktree_id = f"{spec.round_id}-c{spec.candidate_id}"
    result = None
    eval_block = ""
    eval_metrics: dict = {}
    accepted = False
    stage = "executor"
    try:
        print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
              f"(family={spec.family}, decision={spec.decision}): "
              f"{spec.proposal[:120]}", flush=True)
        result = executor_mod.execute(
            deps.executor_agent, proposal=spec.proposal, goal=cfg["goal"],
            editable=cfg["editable_paths"], frozen=cfg["frozen_paths"],
            workspace=deps.workspace, worktree=worktree, round_id=worktree_id,
            gate_block=deps.gate_lines,
        )
        if result.sha:
            print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
                  f"committed: {result.sha} ({len(result.changed_paths)} files)",
                  flush=True)
        else:
            print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
                  f"no commit: {result.reason}", flush=True)
        if result.sha:
            stage = "eval"
            try:
                eval_result = evals.run_eval(
                    cfg["eval_commands"],
                    cwd=worktree,
                    runtime=deps.runtime,
                    metrics_schema=metrics_schema,
                    timeout_seconds=cfg.get("eval_timeout_seconds", 600),
                    output_cap=cfg.get("eval_output_cap_chars", 16000),
                )
                eval_block = eval_result.text
                eval_metrics = eval_result.metrics
            except Exception as exc:
                eval_block = f"(eval failed to run: {exc})"
                print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
                      f"eval error: {exc}", flush=True)
        accepted = candidate_accepted(result.sha, eval_metrics, metrics_schema)
        stage = "judger"
        judgment = judger_mod.judge(
            deps.judger_agent, goal=cfg["goal"], proposal=spec.proposal,
            sha=result.sha, reason=result.reason, parent_sha=spec.parent_sha,
            workspace=deps.workspace, eval_block=eval_block, cwd=worktree,
            metrics=eval_metrics, prior_metrics=spec.prior_metrics,
            baseline_metrics=spec.baseline_metrics, metrics_schema=metrics_schema,
            label=f"judger r{spec.round_id}-c{spec.candidate_id}",
        )
        print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
              f"score={judgment.score:.2f} risk={judgment.risk} "
              f"feedback: {judgment.feedback[:120]}", flush=True)
        print_objective(eval_metrics, spec.prior_metrics, spec.baseline_metrics,
                        metrics_schema)
        return {
            "candidate": spec.candidate_id,
            "family": spec.family,
            "decision": spec.decision,
            "proposal": spec.proposal,
            "sha": result.sha,
            "score": judgment.score,
            "risk": judgment.risk,
            "feedback": judgment.feedback,
            "feedback_for_proposer": judgment.feedback_for_proposer,
            "eval_block": eval_block,
            "metrics": eval_metrics,
            "changed_paths": result.changed_paths,
            "accepted": accepted,
            "selected": False,
            "candidate_status": _status_for(result.sha, result.reason),
        }
    except (AgentError, ValueError) as exc:
        print(f"[{stamp()}] candidate r{spec.round_id}-c{spec.candidate_id} "
              f"failed: {exc}", flush=True)
        changed_paths = result.changed_paths if result else []
        sha = result.sha if result else None
        return candidate_failure(
            spec.candidate_id, spec, str(exc), spec.parent_sha,
            sha=sha, eval_block=eval_block, eval_metrics=eval_metrics,
            changed_paths=changed_paths, accepted=accepted,
            status=f"{stage.upper()}_FAILED",
        )


def _status_for(sha: str | None, reason: str | None) -> str:
    """Business status of a candidate that reached the judger."""
    if sha:
        return "COMPLETED"
    if reason == "executor made no changes":
        return "NO_CHANGE"
    if reason and reason.startswith("gate rejected"):
        return "GATE_REJECTED"
    return "COMPLETED"


def _run_baseline_eval(deps: CandidateDeps, spec: CandidateSpec, cfg: dict) -> dict:
    """Run only the evaluation part for baseline assessment.

    Skips executor/judger and just runs eval commands to get metrics.
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

    # Return a result dict compatible with the worker contract
    return {
        "candidate": spec.candidate_id,
        "family": spec.family,
        "decision": spec.decision,
        "proposal": spec.proposal,
        "sha": spec.parent_sha,  # Baseline just reports the original commit
        "score": 0.0,
        "risk": "low",
        "feedback": "baseline evaluation completed",
        "feedback_for_proposer": "",
        "eval_block": result.text,
        "metrics": result.metrics,
        "changed_paths": [],
        "accepted": True,  # Baseline is always accepted
        "selected": False,
        "base_sha": spec.parent_sha,
        "candidate_status": "BASELINE",
    }


def candidate_failure(candidate_id: int, spec: CandidateSpec,
                      reason: str, parent_sha: str, sha: str | None = None,
                      eval_block: str = "", eval_metrics: dict | None = None,
                      changed_paths: list[str] | None = None,
                      accepted: bool = False,
                      status: str = "WORKER_FAILED") -> dict:
    failure_feedback = f"[loop failure] {reason[:200]}"
    proposer_failure = (
        "[loop failure] candidate failed before a usable result was produced"
    )
    return {
        "candidate": candidate_id,
        "family": spec.family,
        "decision": spec.decision,
        "proposal": spec.proposal,
        "sha": sha,
        "score": 0.0,
        "risk": "high",
        "feedback": failure_feedback,
        "feedback_for_proposer": proposer_failure,
        "eval_block": eval_block,
        "metrics": eval_metrics or {},
        "changed_paths": changed_paths or [],
        "accepted": accepted,
        "selected": False,
        "base_sha": parent_sha,
        "candidate_status": status,
    }


def candidate_accepted(candidate_sha: str | None, metrics: dict | None,
                       metrics_schema: dict | None) -> bool:
    """Whether a candidate becomes the next cumulative base: every declared
    gate must be explicitly True (missing/unknown is rejection)."""
    if not candidate_sha:
        return False
    gates = (metrics_schema or {}).get("gates", [])
    if not gates:
        return True
    values = metrics or {}
    return all(values.get(g["key"]) is True for g in gates)


def print_objective(metrics: dict | None, prior: dict | None,
                    baseline: dict | None, schema: dict | None) -> None:
    """Print the round's harness-parsed objective value with vs-prior and
    vs-baseline deltas; silent when there is no measurement this round."""
    if not schema or not metrics:
        return
    obj = schema.get("objective", {})
    key = obj.get("key")
    if not key:
        return
    val = metrics.get(key)
    if val is None:
        return
    lower_is_better = obj.get("lower_is_better", True)
    def _fmt_delta(this, other, label):
        delta = evals.objective_delta(this, other, lower_is_better)
        if delta is None:
            return None
        pct, improved = delta
        arrow = "↓ better" if improved else ("↑ worse" if pct != 0 else "= same")
        return f"{label} {other:g} ({pct:+.1f}%, {arrow})"
    parts = [f"{key}={val:g}"]
    for other, label in ((prior, "vs prior"), (baseline, "vs baseline")):
        if other:
            d = _fmt_delta(val, other.get(key), label)
            if d:
                parts.append(d)
    print(f"[{stamp()}] objective: " + "  |  ".join(parts), flush=True)


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
                        help="Only run eval for baseline, skip executor/judger.")
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
        deps = build_deps(cfg, run_dir, usage_observer=usage.append)
        deps.runtime.preflight()
        spec = CandidateSpec.from_dict(spec_dict)
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
            CandidateSpec.from_dict(spec_dict) if spec_dict else CandidateSpec(
                round_id=0, candidate_id=0, parent_sha="",
                family="single", decision="", proposal=""),
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
