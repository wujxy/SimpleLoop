# Vendored from simpleloop/container/runtime.py (S2b(ii)). The Host keeps its own copy for the
# executor; keep both in sync per the frozen Apptainer execution boundary.
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
# The whole worktree is mounted read-only at /work. The sentinel (written by
# the harness at the worktree root) must be VISIBLE but UNWRITABLE — proving
# the ro base is enforced by the filesystem itself, not a post-hoc gate.
test -e /work/.simpleloop-preflight-hidden || {
    echo "worktree root is not visible at /work" >&2
    exit 126
}
if : >> /work/.simpleloop-preflight-hidden 2>/dev/null; then
    echo "read-only base is writable at /work (ro enforcement failed)" >&2
    exit 126
fi
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
        external)
            test -e "$path" || { echo "missing external path: $path" >&2; exit 126; }
            ;;
        *)
            echo "unknown preflight spec mode: $mode" >&2
            exit 126
            ;;
    esac
done
printf 'executor preflight: PASS\n'
""".strip()


@dataclass(frozen=True)
class MountMap:
    """The writable subset of the file world both lanes mount. The whole
    worktree is mounted read-only at ``/work`` (everything visible + runnable);
    each path in ``rw`` is overlaid ``:rw`` on top (the writable world — source
    + build-output dirs the agent/eval may write). ``external_ro`` are absolute
    host dirs (e.g. /cvmfs) mounted ``:ro`` as-is. Pure data — the harness
    fills it from config; no project-specific names live here."""

    rw: tuple[str, ...] = ()
    external_ro: tuple[str, ...] = ()


def world_mount_map(cfg: dict) -> MountMap:
    """Build the shared filesystem contract both lanes derive from.

    The writable world is ``editable_paths``; everything else in the worktree
    is mounted read-only automatically (the whole tree is visible + runnable,
    only the editable subset is writable). Both lanes use the same map — so the
    writable set (the only thing that matters for proposal feasibility) is
    identical across lanes — and layer role extras (``/repo``, ``/scratch``,
    history) on top via ``extra_binds``.
    """
    return MountMap(
        rw=tuple(cfg["editable_paths"]),
        external_ro=tuple(cfg.get("read_only_binds") or ()),
    )


def _account_home() -> Path:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute():
        raise RuntimePreflightError(f"account home is not absolute: {home}")
    return home


def _prepare_rw_source(worktree: Path, rel: str) -> Path:
    """Resolve a worktree-relative WRITABLE overlay source. Build-output dirs
    (e.g. ``build``, ``TEMP``) may not exist yet in a fresh worktree — create
    them so the ``:rw`` overlay bind source exists."""
    src = worktree / rel
    if src.exists():
        return src
    src.mkdir(parents=True, exist_ok=True)
    return src


class ApptainerRuntime:
    """Build Apptainer argv/env consistently for every payload process."""

    def __init__(
        self,
        image: str | Path,
        binds: Sequence[str | Path],
        run_dir: str | Path,
        *,
        executable: str = "apptainer",
        userns: bool = True,
    ):
        self.image = Path(image).expanduser().resolve()
        self.binds = tuple(
            Path(path).expanduser().resolve() for path in binds
        )
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.executor_home = _account_home()
        self.executable = executable
        self.userns = userns

    def exec_argv(
        self,
        payload: Sequence[str],
        *,
        cwd: str | Path,
        mounts: "MountMap | None" = None,
        home: str | Path | None = None,
        extra_binds: Sequence[str] = (),
        work_cwd: str = "/work",
        network: bool = True,
    ) -> list[str]:
        """Return one shell-free Apptainer argv for a payload command.

        Without ``mounts`` the whole ``run_dir`` is mounted read-write (the
        baseline-eval / legacy path). With ``mounts`` the lane's file world is
        constructed instead: the whole candidate worktree (``cwd``) is mounted
        at ``/work`` READ-ONLY (everything visible + runnable), and each
        declared writable path (``mounts.rw``, relative to the worktree) is
        overlaid ``:rw`` on top. So the agent sees the entire repo and can
        build/test, but writes outside the editable set fail with EROFS at the
        filesystem — the ro/rw split is enforced by the mount, not a gate.
        ``mounts.external_ro`` (absolute host dirs like /cvmfs) are mounted
        ``:ro`` as-is. Both lanes use this path; they differ only in the
        ``MountMap`` (shared derivation) and the role-specific ``extra_binds``
        (raw ``src:dst:mode`` strings the caller assembles, e.g. the proposer's
        ``/repo``/``/scratch``/history). ``work_cwd`` overrides the in-container
        cwd (default ``/work``; the proposer passes ``/scratch`` for
        scratch-cwd commands). ``--containall`` + ``--no-mount cwd,home,hostfs``
        prevent the host worktree from leaking into the container."""
        argv = [self.executable, "exec", "--cleanenv", "--no-eval"]
        if self.userns:
            argv.append("--userns")
        if mounts is None:
            for bind in self.binds:
                if bind != self.run_dir:
                    argv.extend(["--bind", f"{bind}:{bind}"])
            argv.extend(["--bind", f"{self.run_dir}:{self.run_dir}:rw"])
            cwd_arg = str(Path(cwd).expanduser().resolve())
        else:
            if home is None:
                raise ValueError("agent home is required")
            worktree = Path(cwd).expanduser().resolve()
            home_dir = Path(home).expanduser().resolve()
            argv += ["--containall", "--no-mount", "cwd,home,hostfs"]
            if not network:
                # Fully offline. NOTE (verified on apptainer 1.3.3):
                # --containall does NOT isolate the network by itself — only
                # this explicit --network none does. The research path runs
                # online since 2026-08-16 (the offline boundary blocked
                # sanctioned measurement work, e.g. the calib DB over
                # frontier); this switch remains for callers that want it.
                argv += ["--net", "--network", "none"]
            argv.extend(["--bind", f"{home_dir}:{self.executor_home}:rw"])
            # The whole worktree is the agent's /work, read-only: everything is
            # visible and runnable, nothing writable yet. No project names here
            # — the writable subset is pure data from the MountMap.
            argv.extend(["--bind", f"{worktree}:/work:ro"])
            for path in mounts.external_ro:
                external = Path(path).expanduser().resolve()
                argv.extend(["--bind", f"{external}:{external}:ro"])
            # Overlay each declared writable subpath :rw on top of the ro base.
            # Apptainer resolves the more-specific mount for its subtree, so a
            # write under /work/<rw> succeeds while a write anywhere else under
            # /work hits EROFS. A subpath that doesn't exist yet (e.g. a
            # build-output dir) is created so the bind source exists.
            for rel in mounts.rw:
                src = _prepare_rw_source(worktree, rel)
                argv.extend(["--bind", f"{src}:/work/{rel}:rw"])
            cwd_arg = work_cwd
        for spec in extra_binds:
            argv.extend(["--bind", spec])
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

    def research_subprocess_env(self, home: str | Path | None = None) -> dict[str, str]:
        """Minimal launcher env for the offline research container.

        Allows only basics (never credentials/proxies). When ``home`` is given
        (the container home path bound for the agent), set both HOME and
        APPTAINERENV_HOME so in-container processes resolve the bound home."""
        allowed = {
            "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "LD_LIBRARY_PATH",
        }
        env = {
            key: value for key, value in os.environ.items()
            if key in allowed
        }
        if home is not None:
            env["HOME"] = str(home)
            env["APPTAINERENV_HOME"] = str(home)
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

    def executor_preflight(
        self, *, worktree: str | Path, mounts: MountMap,
    ) -> None:
        """Exercise the exact Executor mount world before model work."""
        worktree_path = Path(worktree).expanduser().resolve()
        with tempfile.TemporaryDirectory(prefix="simpleloop-exec-preflight-") as root:
            root_path = Path(root)
            home = root_path / "home"
            home.mkdir(mode=0o700)
            specs = [
                *(f"rw:{path}" for path in mounts.rw),
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
                home=home,
            )
            sentinel = worktree_path / ".simpleloop-preflight-hidden"
            if sentinel.exists():
                raise RuntimePreflightError(
                    f"reserved preflight path already exists: {sentinel}"
                )
            # The worktree root is mounted ro at /work, so this sentinel must be
            # VISIBLE at /work/... but unwritable (EROFS) — the script asserts
            # exactly that, proving the ro/rw split is mount-enforced.
            sentinel.write_text("must be visible but unwritable\n", encoding="utf-8")
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
