"""Thin HTCondor CLI implementation of the Scheduler protocol."""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .contracts import JobHandle, JobObservation, JobSpec, JobState


@dataclass(frozen=True)
class HEPJobConfig:
    schedd_name: str
    accounting_group: str
    accounting_group_user: str
    collector: str | None = None
    request_os: str = "AlmaLinux9"
    ihep_group: str | None = None
    submit_cmd: str = "condor_submit"
    query_cmd: str = "condor_q"
    remove_cmd: str = "condor_rm"
    environment_script: Path | None = None


class HEPJobScheduler:
    name = "hepjob"

    def __init__(
        self,
        config: HEPJobConfig,
        *,
        runner: Callable = subprocess.run,
    ):
        self.config = config
        self.runner = runner

    def _target_args(self) -> list[str]:
        argv: list[str] = []
        if self.config.collector:
            argv.extend(("-pool", self.config.collector))
        if self.config.schedd_name:
            argv.extend(("-name", self.config.schedd_name))
        return argv

    def submit(self, job: JobSpec) -> JobHandle:
        directory = job.manifest_path.parent
        directory.mkdir(parents=True, exist_ok=True)
        script = directory / "job.sh"
        source = ""
        if self.config.environment_script is not None:
            source = f"source {shlex.quote(str(self.config.environment_script))}\n"
        script.write_text(
            "#!/usr/bin/env bash\n"
            "set -uo pipefail\n"
            f"{source}"
            "export SIMPLELOOP_JOB_ID=\"${1:-}\"\n"
            "export SIMPLELOOP_SCHEDULER=hepjob\n"
            f"exec {shlex.join(job.argv)}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

        lines = [
            "universe = vanilla",
            f"executable = {script}",
            'arguments = "$(ClusterId).$(ProcId)"',
            f"output = {job.stdout_path}",
            f"error = {job.stderr_path}",
            f"log = {directory / 'job.log'}",
            "should_transfer_files = NO",
            f"request_memory = {job.resources.memory_mb}",
            f"request_cpus = {job.resources.cpus}",
            f"accounting_group = {self.config.accounting_group}",
            f"accounting_group_user = {self.config.accounting_group_user}",
            f'+HepJob_RequestOS = "{self.config.request_os}"',
        ]
        if job.resources.requirements:
            lines.append(f"Requirements = {job.resources.requirements}")
        real_group = self.config.ihep_group or _derived_group(
            self.config.accounting_group
        )
        if real_group:
            lines.append(f'+IHEP_RealGroup = "{real_group}"')
        for key, value in job.resources.attributes.items():
            lines.append(f"+{key} = {value}")
        lines.append("queue")
        submit_path = directory / "job.sub"
        submit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        completed = self._run([
            self.config.submit_cmd,
            *self._target_args(),
            str(submit_path),
        ])
        if completed.returncode:
            raise OSError(
                "condor submit failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        match = re.search(r"submitted to cluster\s+(\d+)", completed.stdout)
        if not match:
            raise OSError(
                "could not parse cluster id from condor_submit output: "
                + completed.stdout.strip()[:400]
            )
        return JobHandle(self.name, f"{match.group(1)}.0")

    def inspect(
        self,
        handles: tuple[JobHandle, ...],
    ) -> tuple[JobObservation, ...]:
        own = tuple(handle for handle in handles if handle.scheduler == self.name)
        if not own:
            return tuple(JobObservation(
                handle, JobState.UNKNOWN, "handle belongs to another scheduler"
            ) for handle in handles)
        completed = self._run([
            self.config.query_cmd,
            *self._target_args(),
            *(handle.value for handle in own),
            "-af", "ClusterId", "ProcId", "JobStatus",
        ])
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip()
            return tuple(JobObservation(
                handle, JobState.UNKNOWN, detail or "condor_q failed"
            ) for handle in handles)
        states: dict[str, JobState] = {}
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) != 3:
                continue
            cluster, process, status = fields
            states[f"{cluster}.{process}"] = {
                "1": JobState.PENDING,
                "2": JobState.RUNNING,
                "5": JobState.FAILED,
            }.get(status, JobState.UNKNOWN)
        return tuple(
            JobObservation(
                handle,
                (
                    states.get(handle.value, JobState.LOST)
                    if handle.scheduler == self.name else JobState.UNKNOWN
                ),
                (
                    "job is held" if states.get(handle.value) is JobState.FAILED
                    else ""
                ),
            )
            for handle in handles
        )

    def cancel(self, handle: JobHandle) -> None:
        if handle.scheduler != self.name:
            return
        self._run([
            self.config.remove_cmd,
            *self._target_args(),
            handle.value,
        ])

    def _run(self, argv: list[str]):
        return self.runner(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )


def _derived_group(accounting_group: str) -> str | None:
    parts = accounting_group.split(".")
    return parts[1] if len(parts) >= 2 and parts[0] and parts[1] else None
