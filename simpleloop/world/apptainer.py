"""Apptainer implementation of the execution sandbox boundary."""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Mapping

from .contracts import (
    MountSpec,
    ProcessRequest,
    ProcessResult,
    SandboxLaunchError,
    SandboxSpec,
)


_BLOCKED_PREFIXES = ("APPTAINER_", "APPTAINERENV_", "SINGULARITY_", "SINGULARITYENV_", "BASH_FUNC_")
_BLOCKED_EXACT = frozenset({"which_declare"})


class ApptainerSandbox:
    def __init__(self, *, executable: str = "apptainer"):
        self.executable = executable

    def bind(
        self,
        spec: SandboxSpec,
        mounts: tuple[MountSpec, ...],
    ) -> "_BoundApptainerSandbox":
        return _BoundApptainerSandbox(self.executable, spec, mounts)


class _BoundApptainerSandbox:
    def __init__(
        self,
        executable: str,
        spec: SandboxSpec,
        mounts: tuple[MountSpec, ...],
    ):
        self.executable = executable
        self.spec = spec
        self.mounts = mounts

    def argv(self, request: ProcessRequest) -> list[str]:
        argv = [
            self.executable,
            "exec",
            "--cleanenv",
            "--no-eval",
        ]
        if os.environ.get("SIMPLELOOP_APPTAINER_USERNS", "1") != "0":
            argv.append("--userns")
        argv.extend(["--containall", "--no-mount", "cwd,home,hostfs"])
        if not self.spec.network:
            argv.extend(["--net", "--network", "none"])
        for mount in self.mounts:
            argv.extend([
                "--bind",
                f"{mount.source}:{mount.target}:{mount.mode.value}",
            ])
        argv.extend(["--cwd", str(request.cwd), str(self.spec.image)])
        argv.extend(request.argv)
        return argv

    def launcher_env(
        self,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        source = os.environ if environ is None else environ
        result = {
            key: value
            for key, value in source.items()
            if not key.startswith(_BLOCKED_PREFIXES)
            and key not in _BLOCKED_EXACT
        }
        result.update({
            f"APPTAINERENV_{key}": str(value)
            for key, value in self.spec.environment.items()
        })
        return result

    def run(self, request: ProcessRequest) -> ProcessResult:
        argv = self.argv(request)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if request.stdin is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                start_new_session=True,
                env=self.launcher_env(),
            )
        except OSError as exc:
            raise SandboxLaunchError(
                f"could not launch {self.executable}: {exc}"
            ) from exc
        timed_out = False
        try:
            stdout, stderr = process.communicate(
                request.stdin,
                timeout=request.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return ProcessResult(
            request.argv,
            int(process.returncode),
            stdout or "",
            stderr or "",
            time.monotonic() - started,
            timed_out,
        )

    def summary_lines(self) -> tuple[str, str, str]:
        return (
            "sandbox: apptainer",
            f"image: {Path(self.spec.image)}",
            "mounts: " + ", ".join(
                f"{mount.source}:{mount.target}:{mount.mode.value}"
                for mount in self.mounts
            ),
        )
