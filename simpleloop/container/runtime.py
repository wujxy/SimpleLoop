"""Mandatory Apptainer execution boundary for agents and evaluations."""
from __future__ import annotations

import os
import pwd
from pathlib import Path
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class RuntimePreflightError(RuntimeError):
    """Raised when the configured Apptainer runtime cannot safely start."""


def forwarded_payload_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The whitelisted env vars a payload (claude/eval) receives inside the
    container. Also materialized into job_env.sh for batch jobs so the
    worker's environment is run-scoped instead of depending on home-dir
    state (and tokens stay out of the condor job ad)."""
    env = os.environ if environ is None else environ
    return {key: env[key] for key in _FORWARDED_ENV if key in env}


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
_OVERRIDE_ENV = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS", "HOME"}
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

_EXECUTOR_PREFLIGHT_SCRIPT = r"""
set -eu
expected_home=$1
shift
[ "$PWD" = /work ] || { echo "executor cwd is not /work" >&2; exit 126; }
[ "$HOME" = "$expected_home" ] || { echo "executor HOME mismatch" >&2; exit 126; }
for tool in bash git node claude; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'missing executor tool: %s\n' "$tool" >&2
        exit 127
    }
done
mkdir -p "$HOME/.claude" || { echo "executor home is not writable" >&2; exit 126; }
: > "$HOME/.claude/simpleloop-preflight" || {
    echo "executor home is not writable" >&2
    exit 126
}
rm "$HOME/.claude/simpleloop-preflight"
test ! -e /work/.simpleloop-preflight-hidden || {
    echo "unlisted worktree path leaked into executor" >&2
    exit 126
}
for spec in "$@"; do
    mode=${spec%%:*}
    path=${spec#*:}
    case "$mode" in
        rw)
            test -e "/work/$path" || { echo "missing rw path: $path" >&2; exit 126; }
            if test -d "/work/$path"; then
                : > "/work/$path/.simpleloop-preflight"
                rm "/work/$path/.simpleloop-preflight"
            else
                test -w "/work/$path" || { echo "rw path is not writable: $path" >&2; exit 126; }
            fi
            ;;
        ro)
            test -e "/work/$path" || { echo "missing ro path: $path" >&2; exit 126; }
            ;;
        external)
            test -e "$path" || { echo "missing external path: $path" >&2; exit 126; }
            ;;
    esac
done
printf 'executor preflight: PASS\n'
""".strip()


@dataclass(frozen=True)
class MountMap:
    """The executor's file world: worktree-relative paths to mount read-write
    (the writable world — source + build-output dirs) and read-only (the build
    needs to read them but they are not optimization targets). Anything not
    listed is ABSENT from the executor container. Pure data — the harness fills
    it from config; no project-specific names live here."""

    rw: tuple[str, ...] = ()
    ro: tuple[str, ...] = ()
    external_ro: tuple[str, ...] = ()


def executor_mount_map(cfg: dict) -> MountMap:
    """Build the executor's complete, role-scoped filesystem contract."""
    return MountMap(
        rw=tuple(cfg["editable_paths"]),
        ro=tuple(cfg.get("read_only_paths") or ()),
        external_ro=tuple(cfg.get("executor_read_only_binds") or ()),
    )


def _account_home() -> Path:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute():
        raise RuntimePreflightError(f"account home is not absolute: {home}")
    return home


def _prepare_bind_source(
    worktree: Path, rel: str, *, writable: bool,
) -> Path | None:
    """Resolve a worktree-relative mount source. Build-output dirs (rw) may not
    exist yet — create them so the bind source exists. read-only paths that
    don't exist are warned and skipped (the build can't read what isn't there,
    but a stale config entry shouldn't abort the run)."""
    src = worktree / rel
    if src.exists():
        return src
    if writable:
        src.mkdir(parents=True, exist_ok=True)
        return src
    print(
        f"[runtime] warning: read-only mount '{rel}' not found in worktree; "
        f"skipping",
        flush=True,
    )
    return None


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
        self.executor_home = _account_home()
        self.executable = executable

    def exec_argv(
        self,
        payload: Sequence[str],
        *,
        cwd: str | Path,
        mounts: "MountMap | None" = None,
        scaffold: str | Path | None = None,
        home: str | Path | None = None,
    ) -> list[str]:
        """Return one shell-free Apptainer argv for a payload command.

        Without ``mounts`` the whole ``run_dir`` is mounted read-write (the
        baseline-eval / legacy path). With ``mounts`` the executor's file world
        is constructed instead: an empty ``scaffold`` dir is mounted at
        ``/work`` and only the declared rw/ro subpaths (relative to ``cwd``,
        the candidate worktree) appear under it — everything else is absent.
        ``--containall`` + ``--no-mount cwd,home,hostfs`` prevent the host
        worktree from leaking into the container."""
        argv = [self.executable, "exec", "--cleanenv", "--no-eval"]
        # --userns (default) avoids needing setuid on shared HPC nodes; set
        # SIMPLELOOP_APPTAINER_USERNS=0 to fall back to setuid.
        if os.environ.get("SIMPLELOOP_APPTAINER_USERNS", "1") != "0":
            argv.append("--userns")
        if mounts is None:
            for bind in self.binds:
                if bind != self.run_dir:
                    argv.extend(["--bind", f"{bind}:{bind}"])
            argv.extend(["--bind", f"{self.run_dir}:{self.run_dir}:rw"])
            cwd_arg = str(Path(cwd).expanduser().resolve())
        else:
            if scaffold is None:
                raise ValueError("executor scaffold is required")
            if home is None:
                raise ValueError("executor home is required")
            worktree = Path(cwd).expanduser().resolve()
            scaffold_dir = Path(scaffold).expanduser().resolve()
            home_dir = Path(home).expanduser().resolve()
            argv += ["--containall", "--no-mount", "cwd,home,hostfs"]
            argv.extend([
                "--bind", f"{home_dir}:{self.executor_home}:rw",
            ])
            # Empty scaffold -> /work: the container root of the executor's
            # world. Only the bound subpaths appear under it.
            argv.extend(["--bind", f"{scaffold_dir}:/work:rw"])
            for path in mounts.external_ro:
                external = Path(path).expanduser().resolve()
                argv.extend(["--bind", f"{external}:{external}:ro"])
            for rel in mounts.rw:
                src = _prepare_bind_source(worktree, rel, writable=True)
                if src is not None:
                    argv.extend(["--bind", f"{src}:/work/{rel}:rw"])
            for rel in mounts.ro:
                src = _prepare_bind_source(worktree, rel, writable=False)
                if src is not None:
                    argv.extend(["--bind", f"{src}:/work/{rel}:ro"])
            cwd_arg = "/work"
        argv.extend(["--cwd", cwd_arg, str(self.image)])
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
        payload_env = forwarded_payload_env()
        for key, value in (overrides or {}).items():
            if key in _OVERRIDE_ENV or key in _FORWARDED_ENV:
                payload_env[key] = str(value)
        for key, value in payload_env.items():
            env[f"APPTAINERENV_{key}"] = str(value)
        return env

    def research_exec_argv(
        self,
        payload: Sequence[str],
        *,
        workspace: str | Path,
        repo: str | Path,
        history: str | Path | None,
        scratch: str | Path,
        cwd: str,
    ) -> list[str]:
        """Build the offline Proposer research boundary.

        ``workspace`` is the lane's writable git worktree (base_sha tree
        materialized, history reachable read-only through the worktree's shared
        object store). It is bind-mounted read-write at ``/workspace`` so the
        proposer can write scratch code, compile, and run toy experiments.
        ``/repo`` stays read-only, which structurally prevents the proposer
        from committing (creating artifacts is the candidate's job, not the
        proposer's). ``history`` is the run directory whose ``history.jsonl``
        and ``rounds/`` are bind-mounted read-only for the Cognitive element;
        pass ``None`` for the history-blind Generator so no past-experiment
        files enter its world (the boundary is the mount, not a prompt)."""
        if cwd not in {"workspace", "scratch"}:
            raise ValueError("research cwd must be 'workspace' or 'scratch'")
        argv = [
            self.executable,
            "exec",
            "--cleanenv",
            "--no-eval",
            "--containall",
            "--net",
            "--network",
            "none",
        ]
        if os.environ.get("SIMPLELOOP_APPTAINER_USERNS", "1") != "0":
            argv.append("--userns")
        if history is not None:
            evidence = Path(history).resolve()
            history_file = evidence / "history.jsonl"
            rounds = evidence / "rounds"
            if history_file.is_file():
                argv.extend([
                    "--bind", f"{history_file}:/history.jsonl:ro",
                ])
            if rounds.is_dir():
                argv.extend(["--bind", f"{rounds}:/rounds:ro"])
        argv.extend([
            "--bind", f"{Path(workspace).resolve()}:/workspace:rw",
            "--bind", f"{Path(repo).resolve()}:/repo:ro",
            "--bind", f"{Path(scratch).resolve()}:/scratch:rw",
            "--cwd", f"/{cwd}", str(self.image),
        ])
        argv.extend(str(item) for item in payload)
        return argv

    def research_subprocess_env(self) -> dict[str, str]:
        """Allow only launcher basics; never expose credentials or proxies."""
        allowed = {
            "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "LD_LIBRARY_PATH",
        }
        return {
            key: value for key, value in os.environ.items()
            if key in allowed
        }

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

    def executor_preflight(
        self, *, worktree: str | Path, mounts: MountMap,
    ) -> None:
        """Exercise the exact Executor mount world before model work."""
        worktree_path = Path(worktree).expanduser().resolve()
        with tempfile.TemporaryDirectory(prefix="simpleloop-exec-preflight-") as root:
            root_path = Path(root)
            scaffold = root_path / "work"
            home = root_path / "home"
            scaffold.mkdir()
            home.mkdir(mode=0o700)
            specs = [
                *(f"rw:{path}" for path in mounts.rw),
                *(f"ro:{path}" for path in mounts.ro),
                *(f"external:{path}" for path in mounts.external_ro),
            ]
            argv = self.exec_argv(
                [
                    "bash", "-c", _EXECUTOR_PREFLIGHT_SCRIPT,
                    "simpleloop-executor-preflight",
                    str(self.executor_home), *specs,
                ],
                cwd=worktree_path,
                mounts=mounts,
                scaffold=scaffold,
                home=home,
            )
            sentinel = worktree_path / ".simpleloop-preflight-hidden"
            if sentinel.exists():
                raise RuntimePreflightError(
                    f"reserved preflight path already exists: {sentinel}"
                )
            sentinel.write_text("must stay hidden\n", encoding="utf-8")
            try:
                completed = subprocess.run(
                    argv,
                    cwd=str(worktree_path),
                    env=self.subprocess_env({"HOME": str(self.executor_home)}),
                    shell=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=60,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimePreflightError(
                    "Executor preflight timed out after 60s"
                ) from exc
            finally:
                sentinel.unlink(missing_ok=True)
            if completed.returncode:
                detail = (completed.stderr or completed.stdout).strip()[:4000]
                raise RuntimePreflightError(
                    "Executor preflight failed with exit "
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
