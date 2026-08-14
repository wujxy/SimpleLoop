"""Prepare a task's source repository and Apptainer image."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess

from . import config as config_mod
from .container.image import ImageBuildError, build_image
from .world import (
    ApptainerSandbox,
    SandboxPreflightError,
    SandboxSpec,
    evaluator_environment,
)


class InitError(RuntimeError):
    """User-facing failure while preparing a task."""


@dataclass(frozen=True)
class InitResult:
    repo_status: str
    image_status: str
    repo_path: Path
    image_path: Path


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess:
    executable = shutil.which("git")
    if not executable:
        raise InitError("git executable not found on host")
    completed = subprocess.run(
        [executable, "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise InitError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _is_repo_root(repo: Path) -> bool:
    completed = _git(repo, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode:
        return False
    return Path(completed.stdout.strip()).resolve() == repo.resolve()


def _has_head(repo: Path) -> bool:
    return (
        _git(
            repo,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            check=False,
        ).returncode
        == 0
    )


def _verify_ref(repo: Path, baseline_ref: str) -> None:
    completed = _git(
        repo,
        "rev-parse",
        "--verify",
        f"{baseline_ref}^{{commit}}",
        check=False,
    )
    if completed.returncode:
        raise InitError(
            "source.baseline_ref does not resolve to a commit: "
            f"{baseline_ref}"
        )


def prepare_git(repo: str | Path, baseline_ref: str) -> str:
    """Create a baseline commit when *repo* is not yet independently usable."""
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise InitError(
            f"source.path does not exist or is not a directory: {path}"
        )

    if _is_repo_root(path) and _has_head(path):
        _verify_ref(path, baseline_ref)
        return "ready"

    if not _is_repo_root(path):
        _git(path, "init")
    _git(path, "add", "-A")
    _git(
        path,
        "-c",
        "user.name=SimpleLoop",
        "-c",
        "user.email=simpleloop@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "simpleloop baseline",
    )
    _verify_ref(path, baseline_ref)
    return "initialized"


def _preflight_image(cfg: dict) -> None:
    for bind in cfg.get("runtime_binds", ()):
        if not Path(bind).expanduser().is_dir():
            raise SandboxPreflightError(
                f"runtime bind directory does not exist: {bind}"
            )
    ApptainerSandbox().preflight(SandboxSpec(
        Path(cfg["runtime_image"]), evaluator_environment(), True,
    ))


def initialize(
    config_path: str | Path,
    *,
    force: bool = False,
) -> InitResult:
    """Prepare Git and Apptainer prerequisites for one task config."""
    cfg = config_mod.load(config_path, require_ready=False)
    repo_status = prepare_git(cfg["repo_path"], cfg["baseline_ref"])
    image = Path(cfg["runtime_image"])
    definition = Path(cfg["runtime_definition"])
    image_existed = image.exists()

    if image_existed and not force:
        try:
            _preflight_image(cfg)
        except SandboxPreflightError as exc:
            raise InitError(
                f"configured Apptainer image is not usable: {exc}; "
                "pass --force to rebuild it"
            ) from exc
        image_status = "ready"
    else:
        if not definition.is_file():
            raise InitError(
                "runtime.definition does not exist or is not a file: "
                f"{definition}"
            )
        try:
            build_image(definition, image, force=force)
        except ImageBuildError as exc:
            raise InitError(str(exc)) from exc
        try:
            _preflight_image(cfg)
        except SandboxPreflightError as exc:
            raise InitError(
                f"built Apptainer image failed preflight: {exc}"
            ) from exc
        image_status = "rebuilt" if image_existed else "built"

    config_mod.load(config_path)
    return InitResult(
        repo_status=repo_status,
        image_status=image_status,
        repo_path=Path(cfg["repo_path"]),
        image_path=image,
    )
