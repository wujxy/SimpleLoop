"""LocalBackend: candidates run in frontend threads (the historical behavior);
the proposer runs as a subprocess (``simpleloop.proposer_lane_worker``), the
same worker HEPJob submits via condor — so a proposer crash no longer takes
down the frontend and the two backends are symmetric.

Candidate runs never persist in-flight state, so the journal is accepted and
ignored. The backend owns candidate worktree and thread lifecycles; candidate
business semantics live in ``simpleloop.candidate.run_candidate_guarded``."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from . import proposer_lanes as pl
from .base import ExecutionBackend, InfraRoundError
from ..candidate import (
    CandidateBatchRequest,
    CandidateRequest,
    CandidateResult,
    EvaluationResult,
    candidate_failure_from_request,
    run_candidate_guarded,
)
from ..harness import evals
from ..persistence.candidate_trace import HandoffCandidateTrace
from ..stages.artifacts import GitArtifactWorkspace
from ..stages.evaluator import (
    BaselineAcceptanceError,
    EvaluationConfig,
    HarnessEvaluator,
    validate_baseline,
)
from ..stages.executor import AgentExecutor, ExecutorConfig
from ..stages.gate import GateSpec
from ..stages.proposer import ProposalBatch, ProposerRequest


def stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the worker's whole process group, then SIGKILL if it lingers.
    Mirrors roles/agent.py:_kill_group — the worker spawns claude + apptainer
    grandchildren, so killing only the worker PID would orphan them."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class LocalBackend(ExecutionBackend):
    def __init__(self, ctx, *, lane_runner=None, candidate_runner=None):
        self.ctx = ctx
        # DI seam: tests inject a fake ``(spec, result_dir) -> LaneRunner`` to
        # avoid spawning a real subprocess. Production leaves this None so
        # run_proposer_lanes uses _popen_lane (the real worker subprocess).
        self._lane_runner = lane_runner
        self._candidate_runner = candidate_runner or self._run_pipeline

    def run_candidates(
        self,
        request: CandidateBatchRequest,
        *,
        journal=None,
    ) -> tuple[CandidateResult, ...]:
        plans = request.candidates
        max_workers = min(
            self.ctx.cfg.get("max_workers", 1),
            max(1, len(plans)),
        )
        if max_workers <= 1 or len(plans) <= 1:
            return tuple(
                self._run_plan(request.round_id, plan)
                for plan in plans
            )
        results: list[CandidateResult | None] = [None] * len(plans)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._run_plan, request.round_id, plan): index
                for index, plan in enumerate(plans)
            }
            for future in as_completed(futures):
                index = futures[future]
                results[index] = future.result()
        return tuple(result for result in results if result is not None)

    def _run_plan(self, round_id, plan) -> CandidateResult:
        worktree_id = f"{round_id}-c{plan.candidate_id}"
        worktree = None
        request = None
        try:
            worktree = self.ctx.workspace.add_worktree(
                worktree_id, plan.parent_sha,
            )
            request = CandidateRequest(
                round_id,
                plan.candidate_id,
                plan.parent_sha,
                plan.proposal,
                Path(worktree),
            )
            return self._candidate_runner(request)
        except Exception as exc:
            if request is None:
                request = CandidateRequest(
                    round_id,
                    plan.candidate_id,
                    plan.parent_sha,
                    plan.proposal,
                    Path(worktree) if worktree else Path("."),
                )
            print(
                f"[{stamp()}] candidate r{round_id}-c{plan.candidate_id} "
                f"worker failed: {exc}",
                flush=True,
            )
            return candidate_failure_from_request(
                request,
                f"candidate worker failed: {exc}",
                gate_spec=self._candidate_gate_spec(),
            )
        finally:
            if worktree is not None:
                self.ctx.workspace.remove_worktree(worktree_id)

    def _candidate_gate_spec(self) -> GateSpec:
        schema = self.ctx.cfg.get("metrics") or {}
        objective = schema.get("objective") or {}
        return GateSpec(
            str(objective.get("key") or "OBJECTIVE"),
            tuple(
                str(item["key"])
                for item in (schema.get("gates") or ())
                if item.get("key")
            ),
        )

    def _run_pipeline(self, request: CandidateRequest) -> CandidateResult:
        cfg = self.ctx.cfg
        gate_spec = self._candidate_gate_spec()
        return run_candidate_guarded(
            request,
            executor=AgentExecutor(
                self.ctx.executor_agent,
                ExecutorConfig(
                    str(cfg.get("goal") or ""),
                    gate_block=str(getattr(self.ctx, "gate_lines", "")),
                    prompt_dir=getattr(self.ctx, "prompt_dir", None),
                ),
            ),
            artifacts=GitArtifactWorkspace(self.ctx.workspace),
            evaluator=HarnessEvaluator(
                self.ctx.runtime,
                EvaluationConfig(
                    tuple(str(command) for command in cfg.get("eval_commands", ())),
                    gate_spec.objective_key,
                    gate_spec.gate_keys,
                    int(cfg.get("eval_timeout_seconds", 600)),
                    int(cfg.get("eval_output_cap_chars", 16000)),
                ),
            ),
            gate_spec=gate_spec,
            trace=HandoffCandidateTrace(getattr(self.ctx, "run_dir", None)),
        )

    def run_proposer_lanes(self, request: ProposerRequest) -> ProposalBatch:
        """Run the single proposer lane as a subprocess — the same
        ``simpleloop.proposer_lane_worker`` HEPJob submits via condor — then
        collect its ``result.json``. The Host never imports or runs proposer
        code in-process; a proposer crash no longer takes down the frontend."""
        from ..proposer_lane_worker import ProposerLaneSpec

        ctx, cfg = self.ctx, self.ctx.cfg
        round_id = request.round_id
        base_sha = request.incumbent_sha
        run_dir = Path(ctx.run_dir)
        lane_id = 0
        result_dir = pl.lane_result_dir(run_dir, round_id, lane_id)
        result_dir.mkdir(parents=True, exist_ok=True)
        workspace = ctx.workspace.add_lane_workspace(lane_id, base_sha)
        spec = ProposerLaneSpec(
            lane_id=lane_id, round_id=round_id, base_sha=base_sha,
            run_dir=str(run_dir), workspace_path=str(workspace),
            result_dir=str(result_dir),
            prompt_dir=str(getattr(ctx, "prompt_dir", None) or ""),
            proposal_slots=cfg.get("candidates_per_round", 1),
            scientist_steps=cfg.get("scientist_steps", 200), attempt=1,
        )
        pl.write_lane_manifest(result_dir, spec)
        print(f"[{stamp()}] proposer round {round_id + 1}: 1 lane "
              f"workspace @ {base_sha[:10]} (local subprocess)", flush=True)
        runner = self._lane_runner or self._popen_lane
        try:
            job = runner(spec, result_dir)
            return pl.collect_lane_results(
                [job], round_id=round_id,
                telemetry=getattr(ctx, "telemetry", None))
        finally:
            pl.clear_inflight_proposer(run_dir)
            ctx.workspace.remove_lane_workspace(lane_id)

    def run_self_review(self, *, round_id: int) -> dict:
        """Run one RSI self-review round as the same proposer-lane worker
        subprocess, but in self mode (manifest carries mode='self'). Returns the
        worker's ``self_review`` payload. No lane workspace is created: the
        worker reads the incumbent self-repo itself via SelfRepo(deps.run_dir),
        so only the manifest + spawn + collect differ from run_proposer_lanes
        (the self-review reader/collector, not the lane ones)."""
        from ..proposer_lane_worker import ProposerLaneSpec

        ctx, cfg = self.ctx, self.ctx.cfg
        run_dir = Path(ctx.run_dir)
        lane_id = 0
        result_dir = pl.lane_result_dir(run_dir, round_id, lane_id)
        result_dir.mkdir(parents=True, exist_ok=True)
        spec = ProposerLaneSpec(
            lane_id=lane_id, round_id=round_id, base_sha="",
            run_dir=str(run_dir), workspace_path="",
            result_dir=str(result_dir),
            prompt_dir=str(getattr(ctx, "prompt_dir", None) or ""),
            scientist_steps=cfg.get("scientist_steps", 200), attempt=1,
            mode="self",
        )
        pl.write_lane_manifest(result_dir, spec)
        print(f"[{stamp()}] self-review round {round_id + 1}: spawning worker "
              f"in self mode (local subprocess)", flush=True)
        try:
            job = self._popen_lane(spec, result_dir,
                                   reader=pl.read_self_review_result)
            return pl.collect_self_review_result(
                job, telemetry=getattr(ctx, "telemetry", None))
        finally:
            pl.clear_inflight_proposer(run_dir)

    def _popen_lane(self, spec, result_dir, *, reader=pl.read_lane_result):
        """Spawn ``proposer_lane_worker`` on the host, poll to completion, and
        return a COMPLETED ``LaneJob`` (with ``result.json`` read) — or raise
        ``InfraRoundError``.

        The worker runs on the host (not inside Apptainer) because it needs
        network for the model HTTP API; only its research probes run inside the
        offline Apptainer container — exactly as the in-process path did. The
        worker inherits ``os.environ`` (the frontend already holds the model
        tokens + importable ``simpleloop``), so no job_env.sh is needed.

        ``start_new_session=True`` puts the worker in its own process group so
        ``os.killpg`` reaches its ``claude``/apptainer grandchildren on timeout.
        """
        ctx, cfg = self.ctx, self.ctx.cfg
        manifest = str(Path(result_dir) / "manifest.json")
        out_path, err_path = Path(result_dir) / "job.out", Path(result_dir) / "job.err"
        argv = [sys.executable, "-m", "simpleloop.proposer_lane_worker",
                "--manifest", manifest]
        out = open(out_path, "w")
        err = open(err_path, "w")
        try:
            proc = subprocess.Popen(
                argv, env=os.environ.copy(), stdout=out, stderr=err,
                start_new_session=True)
        finally:
            out.close()
            err.close()
        pl.write_inflight_proposer(
            ctx.run_dir, spec.round_id, spec.base_sha,
            [{"lane_id": spec.lane_id, "pid": proc.pid}])

        timeout = (cfg.get("hepjob") or {}).get("run_timeout_seconds", 21600)
        started = time.monotonic()
        deadline = started + timeout
        heartbeat = started + 30.0
        while proc.poll() is None:
            now = time.monotonic()
            if now >= deadline:
                _kill_group(proc)
                raise InfraRoundError(
                    f"proposer lane r{spec.round_id}-l{spec.lane_id} exceeded "
                    f"run_timeout ({timeout}s)")
            if now >= heartbeat:
                print(f"[local-proposer] still running "
                      f"({now - started:.0f}s in, pid={proc.pid})", flush=True)
                heartbeat = now + 30.0
            time.sleep(5)

        rc = proc.returncode
        if rc != 0 or not (Path(result_dir) / "_FINISHED").exists():
            raise InfraRoundError(
                f"proposer lane r{spec.round_id}-l{spec.lane_id} exited rc={rc} "
                f"without _FINISHED (killed by infrastructure)")
        print(f"[{stamp()}] proposer lane subprocess finished (rc={rc})",
              flush=True)
        return pl.LaneJob(
            lane_id=spec.lane_id, result_dir=Path(result_dir),
            state="COMPLETED", result=reader(result_dir))

    def cleanup_proposer_orphans(self) -> None:
        """Kill any proposer-lane subprocess a crashed frontend left running
        (recorded by PID in inflight_proposer.json), then clear the marker.
        Mirrors HEPJobBackend's condor_rm sweep, but with process groups."""
        path = pl.inflight_proposer_path(self.ctx.run_dir)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for entry in (data.get("lanes") or []):
            pid = entry.get("pid")
            if isinstance(pid, int):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        pl.clear_inflight_proposer(self.ctx.run_dir)

    def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
        """Run the baseline eval locally on the baseline worktree.

        Returns (eval_block, metrics). Raises BaselineAcceptanceError on failure.
        """
        cfg = self.ctx.cfg
        runtime = self.ctx.runtime
        workspace = self.ctx.workspace
        print(f"[{stamp()}] running baseline eval (on {baseline_sha[:10]})...",
              flush=True)
        wt = None
        try:
            wt = workspace.add_worktree("baseline", baseline_sha)
            bind_paths = [
                *(str(path) for path in runtime.binds
                  if path != runtime.run_dir),
                str(runtime.run_dir),
            ]
            context = (
                f"image: {runtime.image}\n"
                f"binds: {', '.join(bind_paths)}\n"
                f"cwd: {wt}"
            )
            try:
                result = evals.run_eval(
                    cfg["eval_commands"],
                    cwd=wt,
                    runtime=runtime,
                    metrics_schema=cfg.get("metrics"),
                    timeout_seconds=cfg.get("eval_timeout_seconds", 600),
                    output_cap=cfg.get("eval_output_cap_chars", 16000),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise BaselineAcceptanceError(
                    "baseline evaluation failed to run: "
                    f"{exc}\n{context}"
                ) from exc
            try:
                self._require_baseline_acceptance(result, cfg.get("metrics"))
            except BaselineAcceptanceError as exc:
                raise BaselineAcceptanceError(
                    f"{exc}\n{context}"
                ) from exc
            print(f"[{stamp()}] baseline eval done.", flush=True)
            return result.text, result.metrics
        finally:
            if wt is not None:
                workspace.remove_worktree("baseline")

    def _require_baseline_acceptance(
        self, result: evals.EvalResult,
        metrics_schema: dict,
    ) -> None:
        """Reject an unusable baseline before any optimization agent is called."""
        schema = metrics_schema or {}
        objective = schema.get("objective") or {}
        validate_baseline(
            EvaluationResult(
                result.text,
                result.metrics,
                tuple(result.returncodes),
            ),
            GateSpec(
                str(objective.get("key") or "OBJECTIVE"),
                tuple(
                    str(item["key"])
                    for item in schema.get("gates", ())
                    if item.get("key")
                ),
            ),
        )
