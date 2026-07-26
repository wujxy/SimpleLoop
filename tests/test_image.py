from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from simpleloop import cli as cli_mod
from simpleloop.container import image as image_mod
from simpleloop.container.image import ImageBuildError, build_image, default_output


def _write_definition(tmp_path: Path) -> Path:
    definition = tmp_path / "juno.def"
    definition.write_text(
        "Bootstrap: docker\nFrom: almalinux:9\n",
        encoding="utf-8",
    )
    return definition


def test_default_output_replaces_definition_suffix(tmp_path: Path):
    assert default_output(tmp_path / "juno.def") == tmp_path / "juno.sif"


def test_build_image_uses_fakeroot_and_explicit_output(
    monkeypatch,
    tmp_path: Path,
):
    definition = _write_definition(tmp_path)
    output = tmp_path / "images" / "custom.sif"
    output.parent.mkdir()
    seen = {}
    monkeypatch.setattr(
        image_mod.shutil,
        "which",
        lambda _name: "/usr/bin/apptainer",
    )

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(image_mod.subprocess, "run", fake_run)

    assert build_image(definition, output) == output.resolve()
    assert seen["argv"] == [
        "/usr/bin/apptainer",
        "build",
        "--fakeroot",
        str(output.resolve()),
        str(definition.resolve()),
    ]
    assert seen["kwargs"]["check"] is False


def test_build_image_strips_outer_container_env(monkeypatch, tmp_path: Path):
    """A leaked APPTAINER_BIND from an outer container session is applied to
    the build sandbox (no underlay during build) and kills it; the build env
    must drop APPTAINER_*/SINGULARITY_* like the exec runtime does."""
    definition = _write_definition(tmp_path)
    seen = {}
    monkeypatch.setattr(
        image_mod.shutil,
        "which",
        lambda _name: "/usr/bin/apptainer",
    )
    monkeypatch.setenv("APPTAINER_BIND", "/data,/cvmfs")
    monkeypatch.setenv("SINGULARITY_BIND", "/data")
    monkeypatch.setenv("APPTAINERENV_FOO", "x")
    monkeypatch.setenv("KEEP_ME", "1")

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(image_mod.subprocess, "run", fake_run)

    build_image(definition, tmp_path / "juno.sif")

    assert seen["env"]["KEEP_ME"] == "1"
    assert not any(
        key.startswith(("APPTAINER_", "APPTAINERENV_", "SINGULARITY_", "SINGULARITYENV_"))
        for key in seen["env"]
    )


def test_existing_output_requires_force(tmp_path: Path):
    definition = _write_definition(tmp_path)
    output = tmp_path / "juno.sif"
    output.write_bytes(b"existing")

    with pytest.raises(
        ImageBuildError,
        match="already exists.*--force",
    ):
        build_image(definition, output)


def test_force_is_forwarded_to_apptainer(
    monkeypatch,
    tmp_path: Path,
):
    definition = _write_definition(tmp_path)
    output = tmp_path / "juno.sif"
    output.write_bytes(b"existing")
    seen = []
    monkeypatch.setattr(
        image_mod.shutil,
        "which",
        lambda _name: "/usr/bin/apptainer",
    )
    monkeypatch.setattr(
        image_mod.subprocess,
        "run",
        lambda argv, **kwargs: (
            seen.append(argv)
            or subprocess.CompletedProcess(argv, 0)
        ),
    )

    build_image(definition, output, force=True)

    assert seen[0] == [
        "/usr/bin/apptainer",
        "build",
        "--fakeroot",
        "--force",
        str(output.resolve()),
        str(definition.resolve()),
    ]


def test_build_image_rejects_missing_definition(tmp_path: Path):
    with pytest.raises(ImageBuildError, match="definition file"):
        build_image(tmp_path / "missing.def")


def test_build_image_rejects_missing_output_directory(tmp_path: Path):
    definition = _write_definition(tmp_path)
    with pytest.raises(ImageBuildError, match="output directory"):
        build_image(definition, tmp_path / "missing" / "image.sif")


def test_build_image_reports_missing_apptainer(monkeypatch, tmp_path: Path):
    definition = _write_definition(tmp_path)
    monkeypatch.setattr(image_mod.shutil, "which", lambda _name: None)

    with pytest.raises(ImageBuildError, match="not found on host"):
        build_image(definition)


def test_build_image_propagates_nonzero_exit(monkeypatch, tmp_path: Path):
    definition = _write_definition(tmp_path)
    monkeypatch.setattr(
        image_mod.shutil,
        "which",
        lambda _name: "/usr/bin/apptainer",
    )
    monkeypatch.setattr(
        image_mod.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 17),
    )

    with pytest.raises(ImageBuildError, match="exit 17"):
        build_image(definition)


def test_image_build_cli_forwards_options_and_prints_output(
    monkeypatch,
    tmp_path: Path,
    capsys,
):
    definition = tmp_path / "custom.def"
    output = tmp_path / "custom.sif"
    seen = {}

    def fake_build(definition_arg, output_arg, *, force):
        seen["args"] = (definition_arg, output_arg, force)
        return output

    monkeypatch.setattr(cli_mod, "build_image", fake_build)

    cli_mod.main(
        [
            "image",
            "build",
            str(definition),
            "--output",
            str(output),
            "--force",
        ]
    )

    assert seen["args"] == (str(definition), str(output), True)
    assert f"Built image: {output}" in capsys.readouterr().out


def test_image_build_cli_reports_builder_error(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        cli_mod,
        "build_image",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ImageBuildError("build failed")
        ),
    )

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["image", "build", "missing.def"])

    assert exc.value.code == 1
    assert "Image build error: build failed" in capsys.readouterr().err
