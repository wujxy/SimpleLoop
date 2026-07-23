"""Thin synchronous wrapper for building an Apptainer SIF."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess


class ImageBuildError(RuntimeError):
    """Raised for user-facing image build validation or process failures."""


def default_output(definition: str | Path) -> Path:
    """Replace the definition suffix with `.sif`."""
    return Path(definition).expanduser().with_suffix(".sif")


def build_image(
    definition: str | Path,
    output: str | Path | None = None,
    *,
    force: bool = False,
) -> Path:
    """Build one SIF with `apptainer build --fakeroot`."""
    definition_path = Path(definition).expanduser().resolve()
    if not definition_path.is_file():
        raise ImageBuildError(
            f"definition file does not exist: {definition_path}"
        )

    output_path = (
        Path(output).expanduser().resolve()
        if output is not None
        else default_output(definition_path)
    )
    if not output_path.parent.is_dir():
        raise ImageBuildError(
            f"output directory does not exist: {output_path.parent}"
        )
    if output_path.exists() and not force:
        raise ImageBuildError(
            f"output already exists: {output_path}; "
            "pass --force to overwrite"
        )

    apptainer = shutil.which("apptainer")
    if not apptainer:
        raise ImageBuildError("apptainer executable not found on host")

    argv = [apptainer, "build", "--fakeroot"]
    if force:
        argv.append("--force")
    argv.extend([str(output_path), str(definition_path)])
    completed = subprocess.run(argv, check=False)
    if completed.returncode:
        raise ImageBuildError(
            f"apptainer build failed with exit {completed.returncode}"
        )
    return output_path
