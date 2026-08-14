"""HEPJobBackend: each candidate runs as an independent condor job executing
the standalone CandidateWorker (`python -m simpleloop.candidate_worker`).

The backend is the job LIFECYCLE supervisor, not a business actor:

  prepare worktree + manifest -> condor_submit -> poll condor_q ->
  reconcile (Held / running-timeout / disappeared) -> retry infra failures
  -> collect result.json -> return only business-terminal candidates.

Completion contract (shared with candidate_worker): the worker writes
result.json (pure business result) plus usage.json (telemetry/audit sidecar)
atomically and touches _FINISHED last; a job that left the queue WITHOUT
_FINISHED died of infrastructure causes and may be retried.
Infrastructure-failed candidates (INFRA_FAILED/TIMEOUT) are excluded from
the returned list — they must never enter the proposer's history as
proposal failures. If every candidate of a round dies of infrastructure,
InfraRoundError is raised and the round is not consumed.

In-flight state is persisted through the loop-supplied RoundJournal (save on
every transition) so a frontend crash can be resumed with --continue:
resume_round() rebuilds the job table from the journal's jobs payload and
re-enters the same poll loop without calling the proposer again. The
journal's round meta (proposer outputs, parent chain) belongs to the loop;
this backend only ever sees and produces the opaque jobs table.
"""
from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .. import candidate_worker
from .. import proposer_lane_worker
from ..candidate import CandidateResult
from ..candidate_worker import stamp
from ..container import runtime as runtime_mod
from . import proposer_lanes as pl
from .base import ExecutionBackend, InfraRoundError, RoundJournal
from ..config import _CPU_MODEL_REQUIREMENTS
from ..loop import BaselineAcceptanceError
from ..persistence.artifacts import ProtocolError, decode_candidate_result
from ..stages.proposer import ProposalBatch, ProposerRequest


def _requirements_expr(cfg: dict) -> str | None:
    """Build the condor Requirements expression from cpu_model and/or
    machine_constraint. Returns None if neither is set."""
    parts: list[str] = []
    if cfg.get("cpu_model"):
        parts.append(_CPU_MODEL_REQUIREMENTS[cfg["cpu_model"]])
    if cfg.get("machine_constraint"):
        parts.append(cfg["machine_constraint"])
    return " && ".join(parts) if parts else None

# condor JobStatus codes (from a successful `condor_q -af JobStatus` query)
_JOB_IDLE = 1
_JOB_RUNNING = 2
_JOB_HELD = 5

TERMINAL_STATES = ("COMPLETED", "INFRA_FAILED", "TIMEOUT")


@dataclass
class _Job:
    """One candidate's scheduler-side lifecycle state (distinct from the
    business status the worker writes into result.json)."""
    candidate_id: int
    worktree_id: str
    result_dir: Path
    job_id: str | None = None
    attempt: int = 1
    state: str = "SUBMITTED"        # SUBMITTED | COMPLETED | INFRA_FAILED | TIMEOUT
    note: str = ""
    submitted_at: float = 0.0       # monotonic
    running_since: float | None = None
    gone_since: float | None = None
    idle_warned: bool = False
    result: CandidateResult | dict | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class HEPJobBackend(ExecutionBackend):
    def __init__(self, ctx, hep_cfg: dict):
        self.ctx = ctx
        self.cfg = hep_cfg
        self.run_dir = Path(ctx.run_dir)
        self._round_id = -1
        self._parent_sha = ""
        self._journal: RoundJournal | None = None
        # Materialize job_env.sh once at construction (before the baseline job
        # is submitted) so EVERY condor job — baseline included — sources the
        # forwarded payload env. Previously only run_candidates() did this, so
        # the baseline job sourced a non-existent file (masked only because the
        # baseline runs no claude call).
        self._ensure_job_env()

    def _target_args(self) -> list[str]:
        """condor -pool/-name flags selecting the target schedd. -pool is
        required on login nodes whose default collector cannot see the JUNO
        schedds (cm01.ihep.ac.cn owns schedd06/07/10/11/12)."""
        args: list[str] = []
        if self.cfg.get("collector"):
            args += ["-pool", self.cfg["collector"]]
        if self.cfg.get("schedd_name"):
            args += ["-name", self.cfg["schedd_name"]]
        return args

    # ---- backend interface ----

    def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
        """Run the baseline evaluation as a single condor job.

        Returns (eval_block, metrics). Raises BaselineAcceptanceError on failure.
        """
        from ..loop import BaselineAcceptanceError, stamp
        from ..harness import evals

        baseline_job_id = "baseline"
        result_dir = self.run_dir / "baseline"
        result_dir.mkdir(parents=True, exist_ok=True)
        worktree = None
        try:
            worktree = self.ctx.workspace.add_worktree(baseline_job_id, baseline_sha)
            spec = candidate_worker.CandidateSpec(
                round_id=-1, candidate_id=-1,  # Special IDs for baseline
                parent_sha=baseline_sha,
                proposal="baseline evaluation",
                run_dir=str(self.run_dir),
                worktree_path=str(worktree),
                result_dir=str(result_dir),
                attempt=1,
            )
            # Write manifest for the baseline worker
            (result_dir / "manifest.json").write_text(
                json.dumps(spec.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")

            # Submit baseline as a special condor job
            job = self._prepare_baseline_job(result_dir)
            self._submit_baseline(job)

            # Wait for the baseline job to complete
            self._supervise_baseline(job)

            # Read and validate the result
            if not (result_dir / "_FINISHED").exists():
                raise BaselineAcceptanceError(
                    f"baseline job {job.job_id} completed without _FINISHED marker")

            try:
                result = self._read_result(job)
            except ProtocolError as exc:
                raise BaselineAcceptanceError(
                    f"baseline result.json is malformed: {exc}")

            eval_block = result.evaluation.text if result.evaluation else ""
            metrics = dict(result.metrics)

            # Validate baseline metrics
            import math
            metrics_schema = self.ctx.cfg.get("metrics")
            if metrics_schema:
                objective_key = metrics_schema["objective"]["key"]
                objective_value = metrics.get(objective_key)
                if (
                    isinstance(objective_value, bool)
                    or not isinstance(objective_value, (int, float))
                    or not math.isfinite(objective_value)
                ):
                    raise BaselineAcceptanceError(
                        f"baseline objective {objective_key} is missing or not finite:\n"
                        f"baseline metrics: {metrics}")

                failed_gates = [
                    gate["key"]
                    for gate in metrics_schema.get("gates", [])
                    if metrics.get(gate["key"]) is not True
                ]
                if failed_gates:
                    raise BaselineAcceptanceError(
                        f"baseline gate(s) did not pass: {', '.join(failed_gates)}:\n"
                        f"baseline metrics: {metrics}")

            # Remove the baseline worktree
            self.ctx.workspace.remove_worktree(baseline_job_id)

            print(f"[{stamp()}] baseline eval done on condor job {job.job_id}", flush=True)
            return eval_block, metrics
        finally:
            if worktree is not None and (result_dir / "_FINISHED").exists():
                # Worktree already removed above on success; clean up on failure
                try:
                    self.ctx.workspace.remove_worktree(baseline_job_id)
                except Exception:
                    pass

    def run_candidates(self, *, proposals: list[str], round_id: int,
                       parent_sha: str,
                       journal: RoundJournal | None = None,
                       ) -> tuple[CandidateResult, ...]:
        self._round_id = round_id
        self._parent_sha = parent_sha
        self._journal = journal
        self._ensure_job_env()
        jobs = []
        for i, proposal in enumerate(proposals):
            job = self._prepare(i, proposal, round_id, parent_sha)
            self._submit(job)
            jobs.append(job)
        self._save(jobs)
        return self._supervise(jobs)

    def resume_round(self, jobs_payload: list[dict], *, round_id: int,
                     parent_sha: str,
                     journal: RoundJournal | None = None,
                     ) -> tuple[CandidateResult, ...]:
        """Re-enter the poll loop for an in-flight round after a frontend
        restart. Job state is rebuilt from the journal's jobs table; the
        proposer is NOT called again."""
        self._round_id = round_id
        self._parent_sha = parent_sha
        self._journal = journal
        jobs = []
        for jd in jobs_payload:
            job = _Job(
                candidate_id=int(jd["candidate_id"]),
                worktree_id=str(jd["worktree_id"]),
                result_dir=Path(jd["result_dir"]),
                job_id=jd.get("job_id"),
                attempt=int(jd.get("attempt") or 1),
                state=str(jd.get("state") or "SUBMITTED"),
                note=str(jd.get("note") or ""),
            )
            job.submitted_at = time.monotonic()
            if job.state == "COMPLETED" and job.result is None:
                job.result = self._read_result(job)
            jobs.append(job)
        return self._supervise(jobs)

    # ---- prepare / submit ----

    def _prepare_baseline_job(self, result_dir: Path) -> _Job:
        """Prepare a minimal job object for baseline evaluation."""
        return _Job(candidate_id=-1, worktree_id="baseline", result_dir=result_dir)

    def _submit_baseline(self, job: _Job) -> None:
        """Submit baseline evaluation as a single condor job."""
        job_sh = job.result_dir / "job.sh"
        job_sh.write_text(
            "#!/usr/bin/env bash\n"
            "set -uo pipefail\n"
            f"source {shlex.quote(str(self._job_env_path()))}\n"
            f"exec {shlex.quote(self.cfg['python_executable'])}"
            " -m simpleloop.candidate_worker"
            f" --manifest {shlex.quote(str(job.result_dir / 'manifest.json'))}"
            f" --baseline-only\n",  # New flag to indicate baseline-only execution
            encoding="utf-8")
        job_sh.chmod(0o755)
        lines = [
            "universe = vanilla",
            f"executable = {job_sh}",
            'arguments = "$(ClusterId).$(ProcId)"',
            f"output = {job.result_dir / 'job.out'}",
            f"error = {job.result_dir / 'job.err'}",
            f"log = {job.result_dir / 'job.log'}",
            "should_transfer_files = NO",
            f"request_memory = {self.cfg['memory_mb']}",
            f"request_cpus = {self.cfg['cpus']}",
            f"accounting_group = {self.cfg['accounting_group']}",
            f"accounting_group_user = {self.cfg['accounting_group_user']}",
            f'+HepJob_RequestOS = "{self.cfg["request_os"]}"',
        ]
        _req = _requirements_expr(self.cfg)
        if _req:
            lines.append(f"Requirements = {_req}")
        if self.cfg.get("ihep_group"):
            lines.append(f'+IHEP_RealGroup = "{self.cfg["ihep_group"]}"')
        else:
            parts = self.cfg["accounting_group"].split(".")
            if len(parts) >= 2 and parts[0] and parts[1]:
                lines.append(f'+IHEP_RealGroup = "{parts[1]}"')
        lines.append("queue")
        submit_file = job.result_dir / "job.sub"
        submit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        argv = [self.cfg["submit_cmd"]] + self._target_args()
        argv.append(str(submit_file))
        completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            raise BaselineAcceptanceError(
                f"condor submit failed for baseline: "
                f"{completed.stderr.strip() or completed.stdout.strip()}")
        match = re.search(r"submitted to cluster (\d+)", completed.stdout)
        if not match:
            raise BaselineAcceptanceError(
                f"could not parse cluster id from submit output: "
                f"{completed.stdout.strip()[:400]}")
        job.job_id = f"{match.group(1)}.0"
        job.state = "SUBMITTED"
        job.submitted_at = time.monotonic()
        print(f"[{stamp()}] baseline submitted as job {job.job_id}", flush=True)

    def _supervise_baseline(self, job: _Job) -> dict:
        """Monitor a single baseline job until completion, return its metrics."""
        poll = self.cfg["poll_seconds"]
        while True:
            statuses = self._query_statuses([job.job_id])
            now = time.monotonic()
            if statuses is not None:
                status = statuses.get(job.job_id)
                if status == _JOB_HELD:
                    reason = self._hold_reason(job.job_id)
                    raise BaselineAcceptanceError(f"baseline job {job.job_id} HELD: {reason}")
                elif status == _JOB_RUNNING:
                    if job.running_since is None:
                        job.running_since = now
                        print(f"[{stamp()}] baseline job {job.job_id} running", flush=True)
                    elif now - job.running_since > self.cfg["run_timeout_seconds"]:
                        self._remove(job)
                        raise BaselineAcceptanceError(
                            f"baseline job {job.job_id} exceeded run_timeout "
                            f"({self.cfg['run_timeout_seconds']}s)")
                elif status == _JOB_IDLE:
                    if (now - job.submitted_at > self.cfg["idle_warn_seconds"]
                            and not job.idle_warned):
                        job.idle_warned = True
                        print(f"[{stamp()}] warning: baseline job {job.job_id} has been "
                              f"idle for over {self.cfg['idle_warn_seconds']}s", flush=True)
                elif status is None:
                    if (job.result_dir / "_FINISHED").exists():
                        print(f"[{stamp()}] baseline job {job.job_id} finished", flush=True)
                        break
                    if job.gone_since is None:
                        job.gone_since = now
                    elif now - job.gone_since > self.cfg["disappearance_grace_seconds"]:
                        raise BaselineAcceptanceError(
                            f"baseline job {job.job_id} left the queue without a result")
            else:
                print(f"[{stamp()}] warning: condor_q failed; retrying next poll", flush=True)
            time.sleep(poll)

        # Return success to indicate completion
        return {}

    def _prepare(self, candidate_id: int, proposal: str,
                 round_id: int, parent_sha: str) -> _Job:
        worktree_id = f"{round_id}-c{candidate_id}"
        result_dir = (self.run_dir / "rounds" / f"r{round_id}"
                      / "candidates" / f"c{candidate_id}")
        result_dir.mkdir(parents=True, exist_ok=True)
        worktree = self.ctx.workspace.add_worktree(worktree_id, parent_sha)
        job = _Job(candidate_id=candidate_id,
                   worktree_id=worktree_id, result_dir=result_dir)
        self._write_manifest(job, proposal, round_id, parent_sha, worktree)
        return job

    def _write_manifest(self, job: _Job, proposal: str, round_id: int,
                        parent_sha: str, worktree: Path) -> None:
        spec = candidate_worker.CandidateSpec(
            round_id=round_id, candidate_id=job.candidate_id,
            parent_sha=parent_sha, proposal=proposal,
            run_dir=str(self.run_dir),
            worktree_path=str(worktree), result_dir=str(job.result_dir),
            prompt_dir=str(getattr(self.ctx, "prompt_dir", None) or ""),
            attempt=job.attempt,
        )
        (job.result_dir / "manifest.json").write_text(
            json.dumps(spec.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    def _submit(self, job: _Job) -> None:
        job_sh = job.result_dir / "job.sh"
        job_sh.write_text(
            "#!/usr/bin/env bash\n"
            "set -uo pipefail\n"
            f"source {shlex.quote(str(self._job_env_path()))}\n"
            f"exec {shlex.quote(self.cfg['python_executable'])}"
            " -m simpleloop.candidate_worker"
            f" --manifest {shlex.quote(str(job.result_dir / 'manifest.json'))}"
            " --job-id \"${1:-}\"\n",
            encoding="utf-8")
        job_sh.chmod(0o755)
        lines = [
            "universe = vanilla",
            f"executable = {job_sh}",
            'arguments = "$(ClusterId).$(ProcId)"',
            f"output = {job.result_dir / 'job.out'}",
            f"error = {job.result_dir / 'job.err'}",
            f"log = {job.result_dir / 'job.log'}",
            "should_transfer_files = NO",
            f"request_memory = {self.cfg['memory_mb']}",
            f"request_cpus = {self.cfg['cpus']}",
            f"accounting_group = {self.cfg['accounting_group']}",
            f"accounting_group_user = {self.cfg['accounting_group_user']}",
            f'+HepJob_RequestOS = "{self.cfg["request_os"]}"',
        ]
        _req = _requirements_expr(self.cfg)
        if _req:
            lines.append(f"Requirements = {_req}")
        if self.cfg.get("ihep_group"):
            lines.append(f'+IHEP_RealGroup = "{self.cfg["ihep_group"]}"')
        else:
            # IHEP requires +IHEP_RealGroup even when accounting_group is set;
            # derive it from the accounting_group's "<ORG>.<group>.<...>" form
            # so a single config knob carries both.
            parts = self.cfg["accounting_group"].split(".")
            if len(parts) >= 2 and parts[0] and parts[1]:
                lines.append(f'+IHEP_RealGroup = "{parts[1]}"')
        lines.append("queue")
        submit_file = job.result_dir / "job.sub"
        submit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        argv = [self.cfg["submit_cmd"]] + self._target_args()
        argv.append(str(submit_file))
        completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            raise InfraRoundError(
                f"condor submit failed for candidate "
                f"r{self._round_id}-c{job.candidate_id}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}")
        match = re.search(r"submitted to cluster (\d+)", completed.stdout)
        if not match:
            raise InfraRoundError(
                f"could not parse cluster id from submit output: "
                f"{completed.stdout.strip()[:400]}")
        job.job_id = f"{match.group(1)}.0"
        job.state = "SUBMITTED"
        job.submitted_at = time.monotonic()
        job.running_since = None
        job.gone_since = None
        job.idle_warned = False
        print(f"[{stamp()}] candidate r{self._round_id}"
              f"-c{job.candidate_id} submitted as job {job.job_id} "
              f"(attempt {job.attempt})", flush=True)
        self._write_job_json(job)

    # ---- supervise / reconcile ----

    def _supervise(self, jobs: list[_Job]) -> tuple[CandidateResult, ...]:
        poll = self.cfg["poll_seconds"]
        while True:
            active = [j for j in jobs if not j.terminal]
            if not active:
                break
            statuses = self._query_statuses(
                [j.job_id for j in active if j.job_id])
            now = time.monotonic()
            if statuses is not None:
                # Only a SUCCESSFUL query may drive decisions; a transient
                # condor_q failure must never look like a disappeared job.
                for job in active:
                    self._reconcile(job, statuses.get(job.job_id), now)
                self._save(jobs)
            else:
                print(f"[{stamp()}] warning: condor_q failed; "
                      "retrying next poll", flush=True)
            if all(j.terminal for j in jobs):
                break
            time.sleep(poll)
        return self._collect(jobs)

    def _reconcile(self, job: _Job, status: int | None, now: float) -> None:
        label = f"r{self._round_id}-c{job.candidate_id}"
        if status == _JOB_HELD:
            reason = self._hold_reason(job.job_id)
            print(f"[{stamp()}] candidate {label} job {job.job_id} HELD: "
                  f"{reason}", flush=True)
            self._remove(job)
            self._retry_or_fail(job, f"HELD: {reason}")
        elif status == _JOB_RUNNING:
            job.gone_since = None
            if job.running_since is None:
                job.running_since = now
                print(f"[{stamp()}] candidate {label} job {job.job_id} "
                      "running", flush=True)
            elif now - job.running_since > self.cfg["run_timeout_seconds"]:
                print(f"[{stamp()}] candidate {label} job {job.job_id} "
                      f"exceeded run_timeout "
                      f"({self.cfg['run_timeout_seconds']}s); removing",
                      flush=True)
                self._remove(job)
                job.state = "TIMEOUT"
                job.note = "running timeout"
                self._write_job_json(job)
        elif status == _JOB_IDLE:
            job.gone_since = None
            if (not job.idle_warned
                    and now - job.submitted_at > self.cfg["idle_warn_seconds"]):
                job.idle_warned = True
                print(f"[{stamp()}] warning: candidate {label} job "
                      f"{job.job_id} has been idle for over "
                      f"{self.cfg['idle_warn_seconds']}s (cluster is likely "
                      "full; still waiting — no action taken)", flush=True)
        elif status is None:
            if (job.result_dir / "_FINISHED").exists():
                try:
                    job.result = self._read_result(job)
                    job.state = "COMPLETED"
                    job.note = ""
                except ProtocolError as exc:
                    job.state = "INFRA_FAILED"
                    job.note = f"malformed result: {exc}"
                print(f"[{stamp()}] candidate {label} job {job.job_id} "
                      f"finished -> {job.state}", flush=True)
                self._write_job_json(job)
            else:
                if job.gone_since is None:
                    job.gone_since = now
                elif (now - job.gone_since
                        > self.cfg["disappearance_grace_seconds"]):
                    self._retry_or_fail(
                        job, "LOST: left the queue without a result")
        else:
            job.gone_since = None

    def _retry_or_fail(self, job: _Job, note: str) -> None:
        label = f"r{self._round_id}-c{job.candidate_id}"
        if job.attempt < self.cfg["max_attempts"]:
            job.attempt += 1
            job.note = note
            print(f"[{stamp()}] candidate {label}: {note}; retrying as "
                  f"attempt {job.attempt}", flush=True)
            # Never reuse a possibly-dirty worktree: rebuild from parent_sha.
            self.ctx.workspace.remove_worktree(job.worktree_id)
            worktree = self.ctx.workspace.add_worktree(
                job.worktree_id, self._parent_sha)
            spec = candidate_worker.CandidateSpec.from_dict(
                json.loads((job.result_dir / "manifest.json").read_text(
                    encoding="utf-8")))
            spec.attempt = job.attempt
            spec.worktree_path = str(worktree)
            (job.result_dir / "manifest.json").write_text(
                json.dumps(spec.to_dict(), ensure_ascii=False, indent=2)
                + "\n", encoding="utf-8")
            self._submit(job)
        else:
            job.state = "INFRA_FAILED"
            job.note = note
            print(f"[{stamp()}] candidate {label}: {note}; attempts "
                  f"exhausted -> INFRA_FAILED", flush=True)
            self._write_job_json(job)

    # ---- collect ----

    def _collect(self, jobs: list[_Job]) -> tuple[CandidateResult, ...]:
        round_id = self._round_id
        candidates = []
        for job in jobs:
            try:
                self.ctx.workspace.remove_worktree(job.worktree_id)
            except Exception as exc:
                print(f"[{stamp()}] warning: could not remove worktree "
                      f"{job.worktree_id}: {exc}", flush=True)
            if job.state == "COMPLETED" and job.result is not None:
                result = job.result
                if not isinstance(result, CandidateResult):
                    raise TypeError("candidate job holds a non-candidate result")
                # The usage/audit sidecar travels up to the loop, which owns
                # telemetry accounting (record_usage + snapshot).
                meta = self._read_worker_meta(job)
                result = replace(result, usage=tuple(meta.get("usage") or ()))
                execution = meta.get("execution") or {}
                candidates.append(result)
                print(f"[{stamp()}] candidate r{round_id}"
                      f"-c{job.candidate_id} collected "
                      f"(status={result.status.value}, "
                      f"host={execution.get('host')})",
                      flush=True)
            else:
                # Infra failure: excluded from the round's candidates so it
                # never enters history as a proposal failure.
                print(f"[{stamp()}] candidate r{round_id}"
                      f"-c{job.candidate_id} excluded ({job.state}"
                      f"{': ' + job.note if job.note else ''})", flush=True)
        if not candidates:
            # The round is NOT consumed: the journal stays on disk so
            # --continue can re-enter; deleting it re-proposes the round.
            self._save(jobs)
            raise InfraRoundError(
                f"round {round_id}: all {len(jobs)} candidate job(s) failed "
                "on infrastructure (see rounds/r"
                f"{round_id}/candidates/*/job.{{out,err}}); the round was "
                "not recorded. Fix the infrastructure issue, then either "
                "re-run with --continue or delete the run's inflight file "
                "to re-propose the round.")
        return tuple(candidates)

    # ---- condor wrappers (the only places that touch condor_*) ----

    def _query_statuses(self, job_ids: list[str]) -> dict[str, int] | None:
        """{job_id: JobStatus} for the given jobs, or None when the query
        itself failed (callers must not treat that as 'job disappeared').

        Queries the current user's jobs in one shot and filters locally:
        passing "cluster.proc" strings as condor_q positional constraints is
        fragile (condor parses them as ad-constraints, not id matchers), so a
        single user-scoped query + local filter is both simpler and correct."""
        if not job_ids:
            return {}
        argv = [self.cfg["query_cmd"]] + self._target_args()
        argv += ["-af", "ClusterId", "ProcId", "JobStatus"]
        completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            return None
        wanted = set(job_ids)
        statuses = {}
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            job_id = f"{parts[0]}.{parts[1]}"
            if job_id not in wanted:
                continue
            try:
                statuses[job_id] = int(parts[2])
            except ValueError:
                continue
        return statuses

    def _hold_reason(self, job_id: str) -> str:
        cluster, _, proc = job_id.partition(".")
        argv = [self.cfg["query_cmd"]] + self._target_args()
        argv += ["-af", "HoldReason",
                 "-constraint", f"ClusterId=={cluster} && ProcId=={proc}"]
        completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
        reason = completed.stdout.strip()
        return reason if completed.returncode == 0 and reason else "unknown"

    def _remove(self, job: _Job) -> None:
        argv = [self.cfg["remove_cmd"]] + self._target_args()
        argv.append(job.job_id)
        subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, check=False)

    # ---- persistence ----

    def _job_env_path(self) -> Path:
        return self.run_dir / "job_env.sh"

    def _ensure_job_env(self) -> Path:
        """Materialize the worker environment into the run dir: the
        whitelisted payload env (API token, base URL, proxies) plus the
        simpleloop package location. Run-scoped and auditable; secrets stay
        out of the condor job ad."""
        path = self._job_env_path()
        pkg_parent = Path(candidate_worker.__file__).resolve().parent.parent
        lines = ["# generated by simpleloop HEPJobBackend; sourced by job.sh"]
        for key, value in sorted(runtime_mod.forwarded_payload_env().items()):
            lines.append(f"export {key}={shlex.quote(value)}")
        lines.append(
            f"export PYTHONPATH={shlex.quote(str(pkg_parent))}"
            '"${PYTHONPATH:+:$PYTHONPATH}"')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
        return path

    def _save(self, jobs: list[_Job]) -> None:
        """Hand the current job table to the loop's journal; the loop decides
        where and whether it persists (local runs pass no journal)."""
        if self._journal is None:
            return
        self._journal.save([
            {
                "candidate_id": job.candidate_id,
                "job_id": job.job_id,
                "attempt": job.attempt,
                "state": job.state,
                "note": job.note,
                "worktree_id": job.worktree_id,
                "result_dir": str(job.result_dir),
            }
            for job in jobs
        ])

    def _write_job_json(self, job: _Job) -> None:
        payload = {
            "candidate_id": job.candidate_id,
            "job_id": job.job_id,
            "attempt": job.attempt,
            "state": job.state,
            "note": job.note,
            "manifest": str(job.result_dir / "manifest.json"),
        }
        tmp = job.result_dir / "job.json.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2)
                       + "\n", encoding="utf-8")
        os.replace(tmp, job.result_dir / "job.json")

    @staticmethod
    def _read_result(job: _Job) -> CandidateResult:
        try:
            result = json.loads(
                (job.result_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"{exc}") from exc
        return decode_candidate_result(result)

    @staticmethod
    def _read_worker_meta(job: _Job) -> dict:
        """The worker's usage.json sidecar; delegates to the shared helper.
        Kept (not deleted) because the candidate collect path still calls it."""
        return pl.read_worker_meta(job.result_dir)

    # ==================================================================
    # Proposer-lane pipeline: each lane = one condor job running
    # simpleloop.proposer_lane_worker in its own writable lane workspace.
    # Mirrors the candidate pipeline, but: (a) the workspace is a lane
    # workspace (add_lane_workspace/remove_lane_workspace); (b) the worker
    # module is proposer_lane_worker; (c) PARTIAL lane failure is NOT a round
    # failure — successful lanes' proposals are collected and only an
    # all-infra-failure raises InfraRoundError; completed-but-empty (all lanes
    # abstained/blocked) returns an abstain ProposalBatch. Crash recovery is
    # lightweight: inflight_proposer.json records lane job ids so a crashed
    # frontend can kill orphans and re-propose on --continue (no mid-flight
    # resume — the proposer is cheap relative to candidates).
    # ==================================================================

    def run_proposer_lanes(self, request: ProposerRequest) -> ProposalBatch:
        """Submit the single proposer-lane job, collect proposals, return a
        ProposalBatch. Reuses _Job + the condor wrappers; lane-specific
        prepare/submit/read/collect own the workspace + manifest shape."""
        round_id = request.round_id
        base_sha = request.incumbent_sha
        self._round_id = round_id
        self._parent_sha = base_sha
        self._journal = None
        self._ensure_job_env()
        job = self._prepare_lane(
            0, round_id, base_sha,
            proposal_slots=self.ctx.cfg.get("candidates_per_round", 1),
        )
        self._submit_lane(job)
        jobs = [job]
        self._write_inflight_proposer(round_id, base_sha, jobs)
        try:
            return self._supervise_lanes(jobs, round_id)
        finally:
            self._clear_inflight_proposer()

    def _prepare_lane(self, lane_id: int, round_id: int, base_sha: str, *,
                      proposal_slots: int) -> _Job:
        result_dir = pl.lane_result_dir(self.run_dir, round_id, lane_id)
        result_dir.mkdir(parents=True, exist_ok=True)
        workspace = self.ctx.workspace.add_lane_workspace(lane_id, base_sha)
        job = _Job(candidate_id=lane_id, worktree_id=str(lane_id),
                   result_dir=result_dir)
        spec = proposer_lane_worker.ProposerLaneSpec(
            lane_id=lane_id, round_id=round_id, base_sha=base_sha,
            run_dir=str(self.run_dir), workspace_path=str(workspace),
            result_dir=str(result_dir),
            prompt_dir=str(getattr(self.ctx, "prompt_dir", None) or ""),
            proposal_slots=proposal_slots,
            scientist_steps=self.ctx.cfg.get("scientist_steps", 200),
            attempt=job.attempt,
        )
        pl.write_lane_manifest(result_dir, spec)
        return job

    def _submit_lane(self, job: _Job) -> None:
        job_sh = job.result_dir / "job.sh"
        job_sh.write_text(
            "#!/usr/bin/env bash\n"
            "set -uo pipefail\n"
            f"source {shlex.quote(str(self._job_env_path()))}\n"
            f"exec {shlex.quote(self.cfg['python_executable'])}"
            " -m simpleloop.proposer_lane_worker"
            f" --manifest {shlex.quote(str(job.result_dir / 'manifest.json'))}"
            " --job-id \"${1:-}\"\n",
            encoding="utf-8")
        job_sh.chmod(0o755)
        lines = [
            "universe = vanilla",
            f"executable = {job_sh}",
            'arguments = "$(ClusterId).$(ProcId)"',
            f"output = {job.result_dir / 'job.out'}",
            f"error = {job.result_dir / 'job.err'}",
            f"log = {job.result_dir / 'job.log'}",
            "should_transfer_files = NO",
            f"request_memory = {self.cfg['memory_mb']}",
            f"request_cpus = {self.cfg['cpus']}",
            f"accounting_group = {self.cfg['accounting_group']}",
            f"accounting_group_user = {self.cfg['accounting_group_user']}",
            f'+HepJob_RequestOS = "{self.cfg["request_os"]}"',
        ]
        _req = _requirements_expr(self.cfg)
        if _req:
            lines.append(f"Requirements = {_req}")
        if self.cfg.get("ihep_group"):
            lines.append(f'+IHEP_RealGroup = "{self.cfg["ihep_group"]}"')
        else:
            parts = self.cfg["accounting_group"].split(".")
            if len(parts) >= 2 and parts[0] and parts[1]:
                lines.append(f'+IHEP_RealGroup = "{parts[1]}"')
        lines.append("queue")
        submit_file = job.result_dir / "job.sub"
        submit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        argv = [self.cfg["submit_cmd"]] + self._target_args()
        argv.append(str(submit_file))
        completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            raise InfraRoundError(
                f"condor submit failed for proposer lane "
                f"r{self._round_id}-l{job.candidate_id}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}")
        match = re.search(r"submitted to cluster (\d+)", completed.stdout)
        if not match:
            raise InfraRoundError(
                f"could not parse cluster id from submit output: "
                f"{completed.stdout.strip()[:400]}")
        job.job_id = f"{match.group(1)}.0"
        job.state = "SUBMITTED"
        job.submitted_at = time.monotonic()
        job.running_since = None
        job.gone_since = None
        job.idle_warned = False
        print(f"[{stamp()}] proposer lane r{self._round_id}"
              f"-l{job.candidate_id} submitted as job {job.job_id} "
              f"(attempt {job.attempt})", flush=True)
        self._write_job_json(job)

    def _supervise_lanes(self, jobs: list[_Job], round_id: int):
        poll = self.cfg["poll_seconds"]
        while True:
            active = [j for j in jobs if not j.terminal]
            if not active:
                break
            statuses = self._query_statuses(
                [j.job_id for j in active if j.job_id])
            now = time.monotonic()
            if statuses is not None:
                for job in active:
                    self._reconcile_lane(job, statuses.get(job.job_id), now)
            else:
                print(f"[{stamp()}] warning: condor_q failed; "
                      "retrying next poll", flush=True)
            if all(j.terminal for j in jobs):
                break
            time.sleep(poll)
        return self._collect_lanes(jobs, round_id)

    def _reconcile_lane(self, job: _Job, status: int | None, now: float) -> None:
        label = f"r{self._round_id}-l{job.candidate_id}"
        if status == _JOB_HELD:
            reason = self._hold_reason(job.job_id)
            print(f"[{stamp()}] proposer lane {label} job {job.job_id} "
                  f"HELD: {reason}", flush=True)
            self._remove(job)
            self._retry_or_fail_lane(job, f"HELD: {reason}")
        elif status == _JOB_RUNNING:
            job.gone_since = None
            if job.running_since is None:
                job.running_since = now
            elif now - job.running_since > self.cfg["run_timeout_seconds"]:
                print(f"[{stamp()}] proposer lane {label} job {job.job_id} "
                      f"exceeded run_timeout; removing", flush=True)
                self._remove(job)
                job.state = "TIMEOUT"
                job.note = "running timeout"
                self._write_job_json(job)
        elif status == _JOB_IDLE:
            job.gone_since = None
            if (not job.idle_warned
                    and now - job.submitted_at > self.cfg["idle_warn_seconds"]):
                job.idle_warned = True
                print(f"[{stamp()}] warning: proposer lane {label} idle > "
                      f"{self.cfg['idle_warn_seconds']}s", flush=True)
        elif status is None:
            if (job.result_dir / "_FINISHED").exists():
                try:
                    job.result = pl.read_lane_result(job.result_dir)
                    job.state = "COMPLETED"
                    job.note = ""
                except ValueError as exc:
                    job.state = "INFRA_FAILED"
                    job.note = f"malformed result: {exc}"
                print(f"[{stamp()}] proposer lane {label} job {job.job_id} "
                      f"finished -> {job.state}", flush=True)
                self._write_job_json(job)
            else:
                if job.gone_since is None:
                    job.gone_since = now
                elif (now - job.gone_since
                        > self.cfg["disappearance_grace_seconds"]):
                    self._retry_or_fail_lane(
                        job, "LOST: left the queue without a result")
        else:
            job.gone_since = None

    def _retry_or_fail_lane(self, job: _Job, note: str) -> None:
        label = f"r{self._round_id}-l{job.candidate_id}"
        if job.attempt < self.cfg["max_attempts"]:
            job.attempt += 1
            job.note = note
            print(f"[{stamp()}] proposer lane {label}: {note}; retrying as "
                  f"attempt {job.attempt}", flush=True)
            self.ctx.workspace.remove_lane_workspace(job.candidate_id)
            workspace = self.ctx.workspace.add_lane_workspace(
                job.candidate_id, self._parent_sha)
            spec = proposer_lane_worker.ProposerLaneSpec.from_dict(
                json.loads((job.result_dir / "manifest.json").read_text(
                    encoding="utf-8")))
            spec.attempt = job.attempt
            spec.workspace_path = str(workspace)
            pl.write_lane_manifest(job.result_dir, spec)
            self._submit_lane(job)
        else:
            job.state = "INFRA_FAILED"
            job.note = note
            print(f"[{stamp()}] proposer lane {label}: {note}; attempts "
                  f"exhausted -> INFRA_FAILED", flush=True)
            self._write_job_json(job)

    def _collect_lanes(self, jobs: list[_Job], round_id: int):
        """Remove each lane's workspace, then delegate result collection
        (usage ingest, proposal rebuild, ProposalBatch assembly) to the shared
        helper. The shared helper duck-types the lane id so HEPJob's ``_Job``
        (``.candidate_id``) and ``pl.LaneJob`` (``.lane_id``) both work."""
        for job in jobs:
            try:
                self.ctx.workspace.remove_lane_workspace(job.candidate_id)
            except Exception as exc:
                print(f"[{stamp()}] warning: could not remove lane workspace "
                      f"l{job.candidate_id}: {exc}", flush=True)
        return pl.collect_lane_results(
            jobs, round_id=round_id,
            telemetry=getattr(self.ctx, "telemetry", None))

    # ---- proposer-lane inflight orphan marker (lightweight) ----

    def _inflight_proposer_path(self) -> Path:
        return self.run_dir / "inflight_proposer.json"

    def _write_inflight_proposer(self, round_id: int, base_sha: str,
                                 jobs: list[_Job]) -> None:
        path = self._inflight_proposer_path()
        payload = {
            "round_id": round_id,
            "base_sha": base_sha,
            "lane_job_ids": [j.job_id for j in jobs if j.job_id],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2)
                       + "\n", encoding="utf-8")
        os.replace(tmp, path)

    def _clear_inflight_proposer(self) -> None:
        self._inflight_proposer_path().unlink(missing_ok=True)

    def run_self_review(self, *, round_id: int) -> dict:
        """RSI self-review on HEPJob is deferred to S3c.3. The condor submit/
        supervise machinery is reusable; only the lane-specific glue
        (workspace, result reader, collect) needs self-review variants. RSI
        self-review is validated on the Local backend in v0 — a HEPJob run that
        enables it fails loudly here rather than silently misbehaving."""
        raise NotImplementedError(
            "self-review requires the Local backend in v0 (HEPJob support: S3c.3)")

    def cleanup_proposer_orphans(self) -> None:
        """On --continue after a crash during the proposer stage: kill any lane
        jobs still in the queue, then clear the marker. The interrupted round
        is then re-proposed from scratch (the proposer is cheap relative to
        candidates, so re-running beats mid-flight resume)."""
        path = self._inflight_proposer_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        job_ids = [j for j in (data.get("lane_job_ids") or [])
                   if isinstance(j, str) and j]
        for jid in job_ids:
            argv = [self.cfg["remove_cmd"]] + self._target_args()
            argv.append(jid)
            subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, check=False)
            print(f"[{stamp()}] --continue: removed orphan proposer job {jid}",
                  flush=True)
        path.unlink(missing_ok=True)
