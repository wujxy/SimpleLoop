"""Local subprocess implementation of the Scheduler protocol."""
from __future__ import annotations

import os
import signal
import subprocess
import time

from ..processes import CHILD_PROCESSES
from .contracts import JobHandle, JobObservation, JobSpec, JobState


class LocalScheduler:
    name = "local"

    def __init__(self, *, terminate_grace_seconds: float = 2.0):
        self.terminate_grace_seconds = terminate_grace_seconds
        self._processes: dict[str, subprocess.Popen] = {}

    def submit(self, job: JobSpec) -> JobHandle:
        job.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        job.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            job.stdout_path.open("w", encoding="utf-8") as stdout,
            job.stderr_path.open("w", encoding="utf-8") as stderr,
        ):
            process = subprocess.Popen(
                list(job.argv),
                stdout=stdout,
                stderr=stderr,
                env=os.environ.copy(),
                start_new_session=True,
                shell=False,
            )
        start = _process_start(process.pid)
        if start is None:
            process.kill()
            raise OSError(f"could not identify local worker pid {process.pid}")
        value = f"{process.pid}:{start}"
        self._processes[value] = process
        # Own the detached group so a SIGTERM to the frontend reaps the
        # worker instead of orphaning it.
        CHILD_PROCESSES.register(process.pid)
        return JobHandle(self.name, value)

    def inspect(
        self,
        handles: tuple[JobHandle, ...],
    ) -> tuple[JobObservation, ...]:
        observations = []
        for handle in handles:
            if handle.scheduler != self.name:
                observations.append(JobObservation(
                    handle, JobState.UNKNOWN, "handle belongs to another scheduler"
                ))
                continue
            process = self._processes.get(handle.value)
            if process is None:
                state, detail = self._restored_state(handle.value)
                observations.append(JobObservation(handle, state, detail))
                continue
            returncode = process.poll()
            if returncode is None:
                state, detail = JobState.RUNNING, ""
            else:
                CHILD_PROCESSES.unregister(process.pid)
                if returncode == 0:
                    state, detail = JobState.SUCCEEDED, ""
                else:
                    state, detail = JobState.FAILED, (
                        f"worker exited with rc={returncode}"
                    )
            observations.append(JobObservation(handle, state, detail))
        return tuple(observations)

    @staticmethod
    def _restored_state(value: str) -> tuple[JobState, str]:
        """Probe a handle restored after a frontend crash.

        The handle encodes pid and process start time, so a worker that
        survived the crash is reported RUNNING (design: wait live jobs on
        resume); only a group that is verifiably gone is LOST.
        """
        identity = _parse_handle(value)
        if identity is not None and _process_start(identity[0]) == identity[1]:
            return JobState.RUNNING, "restored local worker is still running"
        return JobState.LOST, "restored local process is not running"

    def cancel(self, handle: JobHandle) -> None:
        if handle.scheduler != self.name:
            return
        identity = _parse_handle(handle.value)
        if identity is None:
            return
        pid, expected_start = identity
        try:
            if _process_start(pid) != expected_start:
                return
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + self.terminate_grace_seconds
            process = self._processes.get(handle.value)
            while time.monotonic() < deadline:
                if process is not None:
                    if process.poll() is not None:
                        return
                elif _process_start(pid) != expected_start:
                    return
                time.sleep(0.01)
            if _process_start(pid) == expected_start:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            CHILD_PROCESSES.unregister(pid)
        if process is not None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass


def _parse_handle(value: str) -> tuple[int, str] | None:
    try:
        pid_text, start = value.split(":", 1)
        pid = int(pid_text)
    except (ValueError, TypeError):
        return None
    if pid <= 1 or not start:
        return None
    return pid, start


def _process_start(pid: int) -> str | None:
    try:
        text = open(f"/proc/{pid}/stat", encoding="utf-8").read()
        closing = text.rfind(")")
        fields = text[closing + 2:].split()
        return fields[19]
    except (OSError, IndexError):
        return None
