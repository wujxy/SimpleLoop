"""LocalBackend: candidates run in frontend threads (the historical behavior);
the proposer runs as a subprocess (``simpleloop.proposer_lane_worker``), the
same worker HEPJob submits via condor — so a proposer crash no longer takes
down the frontend and the two backends are symmetric.

Candidate dispatch itself stays in loop._run_candidates (its serial and
ThreadPool paths); this adapter only exists so loop.py can treat both backends
uniformly. Candidate runs never persist in-flight state, so the journal is
accepted and ignored; the proposer subprocess records its PID in
inflight_proposer.json so cleanup_proposer_orphans can reap it after a crash.
The deferred loop import avoids a module cycle (loop imports execution for the
backend factory)."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import proposer_lanes as pl
from .base import ExecutionBackend, InfraRoundError
from ..candidate import CandidateResult
from ..stages.proposer import ProposalBatch, ProposerRequest


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
    def __init__(self, ctx, *, lane_runner=None):
        self.ctx = ctx
        # DI seam: tests inject a fake ``(spec, result_dir) -> LaneRunner`` to
        # avoid spawning a real subprocess. Production leaves this None so
        # run_proposer_lanes uses _popen_lane (the real worker subprocess).
        self._lane_runner = lane_runner

    def run_candidates(self, *, proposals: list[str], round_id: int,
                       parent_sha: str, journal=None,
                       ) -> tuple[CandidateResult, ...]:
        from .. import loop as loop_mod
        return loop_mod._run_candidates(
            self.ctx, proposals, round_id, parent_sha,
        )

    def run_proposer_lanes(self, request: ProposerRequest) -> ProposalBatch:
        """Run the single proposer lane as a subprocess — the same
        ``simpleloop.proposer_lane_worker`` HEPJob submits via condor — then
        collect its ``result.json``. The Host never imports or runs proposer
        code in-process; a proposer crash no longer takes down the frontend."""
        from ..loop import stamp
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
        from ..loop import stamp
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
        from ..loop import stamp

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
        from ..harness import evals
        from ..loop import BaselineAcceptanceError

        cfg = self.ctx.cfg
        runtime = self.ctx.runtime
        workspace = self.ctx.workspace
        from ..loop import stamp
        import subprocess

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
        import math
        from ..loop import BaselineAcceptanceError

        failed_codes = [code for code in result.returncodes if code != 0]
        if failed_codes:
            raise BaselineAcceptanceError(
                "baseline evaluation command failed with exit "
                f"{failed_codes[0]}:\n{result.text[:8000]}"
            )

        objective = metrics_schema["objective"]["key"]
        value = result.metrics.get(objective)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise BaselineAcceptanceError(
                f"baseline objective {objective} is missing or not finite:\n"
                f"{result.text[:8000]}"
            )

        failed_gates = [
            gate["key"]
            for gate in metrics_schema.get("gates", [])
            if result.metrics.get(gate["key"]) is not True
        ]
        if failed_gates:
            raise BaselineAcceptanceError(
                "baseline gate(s) did not pass: "
                f"{', '.join(failed_gates)}:\n{result.text[:8000]}"
            )
