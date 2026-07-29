"""Mandatory Apptainer execution boundary for agents and evaluations."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from collections.abc import Mapping, Sequence


class RuntimePreflightError(RuntimeError):
    """Raised when the configured Apptainer runtime cannot safely start."""


_FORWARDED_ENV = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
}
_OVERRIDE_ENV = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS"}
_BLOCKED_PREFIXES = (
    "APPTAINER_",
    "APPTAINERENV_",
    "SINGULARITY_",
    "SINGULARITYENV_",
    # Exported shell functions (BASH_FUNC_*) from an outer environment poison
    # /bin/sh children inside the container; `which_declare` is the helper var.
    "BASH_FUNC_",
)
_BLOCKED_EXACT = frozenset({"which_declare"})
_PREFLIGHT_SCRIPT = """
for tool in bash git node claude; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'missing tool: %s\\n' "$tool" >&2
        exit 127
    }
done
test -w "$1" || {
    printf 'run directory is not writable: %s\\n' "$1" >&2
    exit 126
}
printf 'preflight: PASS\\n'
""".strip()


class ApptainerRuntime:
    """Build Apptainer argv/env consistently for every payload process."""

    def __init__(
        self,
        image: str | Path,
        binds: Sequence[str | Path],
        run_dir: str | Path,
        *,
        executable: str = "apptainer",
    ):
        self.image = Path(image).expanduser().resolve()
        self.binds = tuple(
            Path(path).expanduser().resolve() for path in binds
        )
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.executable = executable

    def exec_argv(
        self,
        payload: Sequence[str],
        *,
        cwd: str | Path,
    ) -> list[str]:
        """Return one shell-free Apptainer argv for a payload command."""
        argv = [self.executable, "exec", "--cleanenv", "--no-eval"]
        # --userns (default) avoids needing setuid on shared HPC nodes; set
        # SIMPLELOOP_APPTAINER_USERNS=0 to fall back to setuid.
        if os.environ.get("SIMPLELOOP_APPTAINER_USERNS", "1") != "0":
            argv.append("--userns")
        for bind in self.binds:
            if bind != self.run_dir:
                argv.extend(["--bind", f"{bind}:{bind}"])
        argv.extend(["--bind", f"{self.run_dir}:{self.run_dir}:rw"])
        argv.extend(
            [
                "--cwd",
                str(Path(cwd).expanduser().resolve()),
                str(self.image),
            ]
        )
        argv.extend(str(item) for item in payload)
        return argv

    def subprocess_env(
        self,
        overrides: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Return host launcher env with only approved container injections."""
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(_BLOCKED_PREFIXES)
            and key not in _BLOCKED_EXACT
        }
        payload_env = {
            key: os.environ[key]
            for key in _FORWARDED_ENV
            if key in os.environ
        }
        for key, value in (overrides or {}).items():
            if key in _OVERRIDE_ENV or key in _FORWARDED_ENV:
                payload_env[key] = str(value)
        for key, value in payload_env.items():
            env[f"APPTAINERENV_{key}"] = str(value)
        return env

    def preflight(self) -> None:
        """Verify host paths and required tools inside the configured image."""
        found = shutil.which(self.executable)
        if not found:
            raise RuntimePreflightError(
                "apptainer executable not found on host"
            )
        self.executable = found
        self._validate_paths()
        argv = self.exec_argv(
            [
                "bash",
                "-c",
                _PREFLIGHT_SCRIPT,
                "simpleloop-preflight",
                str(self.run_dir),
            ],
            cwd=self.run_dir,
        )
        try:
            completed = subprocess.run(
                argv,
                cwd=str(self.run_dir),
                env=self.subprocess_env(),
                shell=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimePreflightError(
                "Apptainer preflight timed out after 60s"
            ) from exc
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()[:4000]
            raise RuntimePreflightError(
                "Apptainer preflight failed with exit "
                f"{completed.returncode}: {detail}"
            )

    def summary_lines(self) -> tuple[str, str, str]:
        """Return a concise, secret-free startup summary."""
        bind_paths = [
            *(str(path) for path in self.binds if path != self.run_dir),
            str(self.run_dir),
        ]
        return (
            "runtime: apptainer",
            f"image: {self.image}",
            f"binds: {', '.join(bind_paths)}",
        )

    def _validate_paths(self) -> None:
        if not self.image.is_file():
            raise RuntimePreflightError(
                f"runtime image does not exist or is not a file: {self.image}"
            )
        if not os.access(self.image, os.R_OK):
            raise RuntimePreflightError(
                f"runtime image is not readable: {self.image}"
            )
        for bind in self.binds:
            if not bind.is_dir():
                raise RuntimePreflightError(
                    f"runtime bind directory does not exist: {bind}"
                )
            self._validate_bind_path(bind, "runtime bind directory")
        if not self.run_dir.is_dir():
            raise RuntimePreflightError(
                f"runtime run directory does not exist: {self.run_dir}"
            )
        self._validate_bind_path(self.run_dir, "runtime run directory")

    @staticmethod
    def _validate_bind_path(path: Path, label: str) -> None:
        if ":" in str(path) or "," in str(path):
            raise RuntimePreflightError(
                f"{label} contains an unsupported bind separator "
                f"(':' or ','): {path}"
            )
