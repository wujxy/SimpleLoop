"""Apptainer implementation of the execution sandbox boundary."""
from __future__ import annotations

import os
import pwd
import signal
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from .contracts import (
    MountSpec,
    ProcessRequest,
    ProcessResult,
    SandboxLaunchError,
    SandboxSpec,
)


BLOCKED_LAUNCHER_PREFIXES = (
    "APPTAINER_", "APPTAINERENV_", "SINGULARITY_", "SINGULARITYENV_",
    "BASH_FUNC_",
)
_BLOCKED_EXACT = frozenset({"which_declare"})
_FORWARDED_ENV = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
    "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
})


class SandboxPreflightError(RuntimeError):
    """The configured executable, image, mounts, or image tools are unusable."""


def forwarded_payload_env(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    return {key: source[key] for key in _FORWARDED_ENV if key in source}


def executor_environment(
    *, base_url: str | None, max_output_tokens: int,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    result = forwarded_payload_env(environ)
    if base_url:
        result["ANTHROPIC_BASE_URL"] = base_url
    result["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output_tokens)
    result["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    return result


def evaluator_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    return {
        key: value for key, value in forwarded_payload_env(environ).items()
        if not key.startswith("ANTHROPIC_")
    }


class ApptainerSandbox:
    def __init__(self, *, executable: str = "apptainer", userns: bool = True):
        self.executable = executable
        self.userns = userns

    def bind(
        self,
        spec: SandboxSpec,
        mounts: tuple[MountSpec, ...],
    ) -> "_BoundApptainerSandbox":
        return _BoundApptainerSandbox(
            self.executable, spec, mounts, userns=self.userns,
        )

    def preflight(self, spec: SandboxSpec) -> None:
        executable = shutil.which(self.executable)
        if executable is None:
            raise SandboxPreflightError("apptainer executable not found on host")
        image = Path(spec.image).expanduser().resolve()
        if not image.is_file() or not os.access(image, os.R_OK):
            raise SandboxPreflightError(
                f"runtime image is not a readable file: {image}"
            )
        sandbox = _BoundApptainerSandbox(
            executable, spec, (), userns=self.userns,
        )
        result = sandbox.run(ProcessRequest(
            ("bash", "-c", "for t in bash git node claude; do command -v \"$t\" >/dev/null || exit 127; done"),
            PurePosixPath("/"),
            60,
            label="preflight",
        ))
        if result.timed_out:
            raise SandboxPreflightError("Apptainer preflight timed out after 60s")
        if result.exit_code:
            detail = (result.stderr or result.stdout).strip()[:4000]
            raise SandboxPreflightError(
                f"Apptainer preflight failed with exit {result.exit_code}: {detail}"
            )


class _BoundApptainerSandbox:
    def __init__(
        self,
        executable: str,
        spec: SandboxSpec,
        mounts: tuple[MountSpec, ...],
        *,
        userns: bool,
    ):
        self.executable = executable
        self.spec = spec
        self.mounts = mounts
        self.userns = userns

    def argv(
        self,
        request: ProcessRequest,
        *,
        home: Path | None = None,
    ) -> list[str]:
        argv = [
            self.executable,
            "exec",
            "--cleanenv",
            "--no-eval",
        ]
        if self.userns:
            argv.append("--userns")
        argv.extend(["--containall", "--no-mount", "cwd,home,hostfs"])
        if not self.spec.network:
            argv.extend(["--net", "--network", "none"])
        if home is not None:
            account_home = pwd.getpwuid(os.getuid()).pw_dir
            argv.extend(["--bind", f"{home}:{account_home}:rw"])
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
            if not key.startswith(BLOCKED_LAUNCHER_PREFIXES)
            and key not in _BLOCKED_EXACT
        }
        result.update({
            f"APPTAINERENV_{key}": str(value)
            for key, value in self.spec.environment.items()
        })
        return result

    def run(self, request: ProcessRequest) -> ProcessResult:
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="simpleloop-home-") as home:
            argv = self.argv(request, home=Path(home))
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
