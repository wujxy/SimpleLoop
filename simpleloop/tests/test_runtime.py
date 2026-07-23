from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod


_MISSING = object()


def _write_task(tmp_path: Path, runtime=_MISSING) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    raw = {
        "kind": "task",
        "task": {"goal": "test"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": {"max_rounds": 1},
        "source": {"path": str(repo)},
    }
    if runtime is not _MISSING:
        raw["runtime"] = runtime
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_runtime_is_required(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="runtime: required"):
        config_mod.load(_write_task(tmp_path))


def test_relative_image_resolves_and_binds_default_empty(tmp_path: Path):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")

    cfg = config_mod.load(
        _write_task(tmp_path, {"image": "runtime.sif"})
    )

    assert cfg["runtime_image"] == str(image.resolve())
    assert cfg["runtime_binds"] == []


def test_runtime_accepts_absolute_existing_bind_directories(tmp_path: Path):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    resource = tmp_path / "resources"
    resource.mkdir()

    cfg = config_mod.load(
        _write_task(
            tmp_path,
            {"image": str(image), "binds": [str(resource)]},
        )
    )

    assert cfg["runtime_binds"] == [str(resource.resolve())]


@pytest.mark.parametrize(
    ("runtime", "message"),
    [
        ({}, "runtime.image"),
        ({"image": "missing.sif"}, "does not exist"),
        (
            {"image": "runtime.sif", "binds": "not-a-list"},
            "runtime.binds",
        ),
        (
            {"image": "runtime.sif", "binds": ["relative"]},
            "must be absolute",
        ),
        (
            {"image": "runtime.sif", "unknown": True},
            "unknown key",
        ),
    ],
)
def test_runtime_rejects_invalid_shapes(
    tmp_path: Path,
    runtime: dict,
    message: str,
):
    (tmp_path / "runtime.sif").write_bytes(b"test")

    with pytest.raises(config_mod.ConfigError, match=message):
        config_mod.load(_write_task(tmp_path, runtime))


def test_runtime_rejects_unreadable_image(monkeypatch, tmp_path: Path):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    real_access = os.access
    monkeypatch.setattr(
        config_mod.os,
        "access",
        lambda path, mode: (
            False if Path(path) == image else real_access(path, mode)
        ),
    )

    with pytest.raises(config_mod.ConfigError, match="not readable"):
        config_mod.load(_write_task(tmp_path, {"image": "runtime.sif"}))


def test_runtime_rejects_bind_that_is_not_an_existing_directory(
    tmp_path: Path,
):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    missing = tmp_path / "missing-resource"

    with pytest.raises(config_mod.ConfigError, match="not an existing directory"):
        config_mod.load(
            _write_task(
                tmp_path,
                {"image": str(image), "binds": [str(missing)]},
            )
        )
