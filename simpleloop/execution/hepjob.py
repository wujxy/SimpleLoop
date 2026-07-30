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
from dataclasses import dataclass
from pathlib import Path

from .. import candidate_worker
from ..candidate_worker import stamp
from ..container import runtime as runtime_mod
from .base import ExecutionBackend, InfraRoundError, RoundJournal
from ..config import _CPU_MODEL_REQUIREMENTS
from ..loop import BaselineAcceptanceError

# condor JobStatus codes (from a successful `condor_q -af JobStatus` query)
_JOB_IDLE = 1
_JOB_RUNNING = 2
_JOB_HELD = 5

TERMINAL_STATES = ("COMPLETED", "INFRA_FAILED", "TIMEOUT")

# Sidecar the worker writes next to result.json: telemetry usage records plus
# execution audit info (backend, job id, attempt, host). Read by _collect and
# handed to the loop, which owns telemetry accounting.
WORKER_META_NAME = "usage.json"


@dataclass
class _Job:
    """One candidate's scheduler-side lifecycle state (distinct from the
    business candidate_status the worker writes into result.json)."""
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
    result: dict | None = None      # business result after COMPLETED

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
                family="baseline",
                decision="baseline",
                proposal="baseline evaluation",
                run_dir=str(self.run_dir),
                prior_metrics={},
                baseline_metrics={},
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
            except ValueError as exc:
                raise BaselineAcceptanceError(
                    f"baseline result.json is malformed: {exc}")

            eval_block = result.get("eval_block", "")
            metrics = result.get("metrics", {})

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

    def run_candidates(self, *, proposals: list[dict], round_id: int,
                       parent_sha: str, prior_metrics: dict,
                       baseline_metrics: dict,
                       journal: RoundJournal | None = None) -> list[dict]:
        self._round_id = round_id
        self._parent_sha = parent_sha
        self._journal = journal
        self._ensure_job_env()
        jobs = []
        for i, proposal in enumerate(proposals):
            job = self._prepare(i, proposal, round_id, parent_sha,
                                prior_metrics, baseline_metrics)
            self._submit(job)
            jobs.append(job)
        self._save(jobs)
        return self._supervise(jobs)

    def resume_round(self, jobs_payload: list[dict], *, round_id: int,
                     parent_sha: str,
                     journal: RoundJournal | None = None) -> list[dict]:
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
        if self.cfg.get("cpu_model"):
            lines.append(
                f"Requirements = {_CPU_MODEL_REQUIREMENTS[self.cfg['cpu_model']]}")
        if self.cfg.get("ihep_group"):
            lines.append(f'+IHEP_RealGroup = "{self.cfg["ihep_group"]}"')
        else:
            parts = self.cfg["accounting_group"].split(".")
            if len(parts) >= 2 and parts[0] and parts[1]:
                lines.append(f'+IHEP_RealGroup = "{parts[1]}"')
        lines.append("queue")
        submit_file = job.result_dir / "job.sub"
        submit_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        argv = [self.cfg["submit_cmd"]]
        if self.cfg.get("schedd_name"):
            argv += ["-name", self.cfg["schedd_name"]]
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

    def _prepare(self, candidate_id: int, proposal: dict,
                 round_id: int, parent_sha: str, prior_metrics: dict,
                 baseline_metrics: dict) -> _Job:
        worktree_id = f"{round_id}-c{candidate_id}"
        result_dir = (self.run_dir / "rounds" / f"r{round_id}"
                      / "candidates" / f"c{candidate_id}")
        result_dir.mkdir(parents=True, exist_ok=True)
        worktree = self.ctx.workspace.add_worktree(worktree_id, parent_sha)
        job = _Job(candidate_id=candidate_id,
                   worktree_id=worktree_id, result_dir=result_dir)
        self._write_manifest(job, proposal, round_id, parent_sha,
                             prior_metrics, baseline_metrics, worktree)
        return job

    def _write_manifest(self, job: _Job, proposal: dict, round_id: int,
                        parent_sha: str, prior_metrics: dict,
                        baseline_metrics: dict, worktree: Path) -> None:
        spec = candidate_worker.CandidateSpec(
            round_id=round_id, candidate_id=job.candidate_id,
            parent_sha=parent_sha, family=proposal["family"],
            decision=proposal["decision"], proposal=proposal["proposal"],
            run_dir=str(self.run_dir), prior_metrics=prior_metrics,
            baseline_metrics=baseline_metrics,
            worktree_path=str(worktree), result_dir=str(job.result_dir),
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
        if self.cfg.get("cpu_model"):
            lines.append(
                f"Requirements = {_CPU_MODEL_REQUIREMENTS[self.cfg['cpu_model']]}")
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
        argv = [self.cfg["submit_cmd"]]
        if self.cfg.get("schedd_name"):
            argv += ["-name", self.cfg["schedd_name"]]
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

    def _supervise(self, jobs: list[_Job]) -> list[dict]:
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
                except ValueError as exc:
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

    def _collect(self, jobs: list[_Job]) -> list[dict]:
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
                # The usage/audit sidecar travels up to the loop, which owns
                # telemetry accounting (record_usage + snapshot).
                meta = self._read_worker_meta(job)
                result["usage"] = meta.get("usage") or []
                execution = meta.get("execution") or {}
                candidates.append(result)
                print(f"[{stamp()}] candidate r{round_id}"
                      f"-c{job.candidate_id} collected "
                      f"(status={result.get('candidate_status')}, "
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
        if self._journal is not None:
            self._journal.clear()
        return candidates

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
        argv = [self.cfg["query_cmd"]]
        if self.cfg.get("schedd_name"):
            argv += ["-name", self.cfg["schedd_name"]]
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
        argv = [self.cfg["query_cmd"]]
        if self.cfg.get("schedd_name"):
            argv += ["-name", self.cfg["schedd_name"]]
        argv += ["-af", "HoldReason",
                 "-constraint", f"ClusterId=={cluster} && ProcId=={proc}"]
        completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, check=False)
        reason = completed.stdout.strip()
        return reason if completed.returncode == 0 and reason else "unknown"

    def _remove(self, job: _Job) -> None:
        argv = [self.cfg["remove_cmd"]]
        if self.cfg.get("schedd_name"):
            argv += ["-name", self.cfg["schedd_name"]]
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
    def _read_result(job: _Job) -> dict:
        try:
            result = json.loads(
                (job.result_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{exc}") from exc
        if not isinstance(result, dict) or "candidate_status" not in result:
            raise ValueError("result.json is not a candidate result object")
        return result

    @staticmethod
    def _read_worker_meta(job: _Job) -> dict:
        """The worker's usage.json sidecar; missing/unreadable degrades to
        empty (telemetry is accounting, never worth failing a candidate)."""
        try:
            meta = json.loads((job.result_dir / WORKER_META_NAME).read_text(
                encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return meta if isinstance(meta, dict) else {}
