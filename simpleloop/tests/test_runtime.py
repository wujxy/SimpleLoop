from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop import runtime as runtime_mod
from simpleloop.runtime import ApptainerRuntime, RuntimePreflightError


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


def _make_runtime(
    tmp_path: Path,
    *,
    executable: str = "apptainer",
) -> ApptainerRuntime:
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return ApptainerRuntime(
        image=image,
        binds=[],
        run_dir=run_dir,
        executable=executable,
    )


def test_exec_argv_keeps_paths_and_payload_arguments_atomic(tmp_path: Path):
    run_dir = tmp_path / "run dir"
    run_dir.mkdir()
    resource = tmp_path / "resource dir"
    resource.mkdir()
    image = tmp_path / "image file.sif"
    image.write_bytes(b"test")
    runtime = ApptainerRuntime(
        image=image,
        binds=[resource],
        run_dir=run_dir,
        executable="/usr/bin/apptainer",
    )

    assert runtime.exec_argv(
        ["bash", "-lc", "printf '%s\n' \"$PWD\""],
        cwd=run_dir,
    ) == [
        "/usr/bin/apptainer",
        "exec",
        "--cleanenv",
        "--bind",
        f"{resource}:{resource}",
        "--bind",
        f"{run_dir}:{run_dir}:rw",
        "--cwd",
        str(run_dir),
        str(image),
        "bash",
        "-lc",
        "printf '%s\n' \"$PWD\"",
    ]


def test_exec_argv_does_not_duplicate_run_directory_bind(tmp_path: Path):
    runtime = _make_runtime(tmp_path, executable="/usr/bin/apptainer")
    runtime = ApptainerRuntime(
        runtime.image,
        [runtime.run_dir],
        runtime.run_dir,
        executable=runtime.executable,
    )

    argv = runtime.exec_argv(["true"], cwd=runtime.run_dir)

    assert argv.count("--bind") == 1
    assert f"{runtime.run_dir}:{runtime.run_dir}:rw" in argv


def test_subprocess_env_strips_container_injection_and_forwards_allowlist(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setattr(
        runtime_mod.os,
        "environ",
        {
            "PATH": "/host/bin",
            "PYTHONPATH": "/bad/python",
            "LD_LIBRARY_PATH": "/bad/lib",
            "APPTAINERENV_PYTHONPATH": "/worse/python",
            "SINGULARITYENV_LD_LIBRARY_PATH": "/worse/lib",
            "APPTAINER_BIND": "/unexpected",
            "APPTAINER_BINDPATH": "/also-unexpected",
            "ANTHROPIC_BASE_URL": "https://endpoint.example",
            "UNRELATED_SECRET": "do-not-forward-as-payload",
        },
    )
    runtime = _make_runtime(tmp_path)

    env = runtime.subprocess_env(
        {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}
    )

    assert env["PATH"] == "/host/bin"
    assert env["PYTHONPATH"] == "/bad/python"
    assert env["LD_LIBRARY_PATH"] == "/bad/lib"
    assert "APPTAINER_BIND" not in env
    assert "APPTAINER_BINDPATH" not in env
    assert "APPTAINERENV_PYTHONPATH" not in env
    assert "SINGULARITYENV_LD_LIBRARY_PATH" not in env
    assert (
        env["APPTAINERENV_ANTHROPIC_BASE_URL"]
        == "https://endpoint.example"
    )
    assert (
        env["APPTAINERENV_CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"
    )
    assert "APPTAINERENV_UNRELATED_SECRET" not in env


@pytest.mark.parametrize(
    "name",
    [
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
    ],
)
def test_subprocess_env_forwards_each_approved_host_variable(
    monkeypatch,
    tmp_path: Path,
    name: str,
):
    monkeypatch.setattr(runtime_mod.os, "environ", {name: "sentinel"})

    env = _make_runtime(tmp_path).subprocess_env()

    assert env[f"APPTAINERENV_{name}"] == "sentinel"


def test_subprocess_env_ignores_unapproved_override(tmp_path: Path):
    env = _make_runtime(tmp_path).subprocess_env(
        {"PYTHONPATH": "/injected"}
    )
    assert "APPTAINERENV_PYTHONPATH" not in env


def test_preflight_reports_missing_host_apptainer(
    monkeypatch,
    tmp_path: Path,
):
    runtime = _make_runtime(tmp_path)
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda _name: None)

    with pytest.raises(
        RuntimePreflightError,
        match="apptainer executable not found on host",
    ):
        runtime.preflight()


def test_preflight_uses_one_container_probe(monkeypatch, tmp_path: Path):
    runtime = _make_runtime(tmp_path)
    monkeypatch.setattr(
        runtime_mod.shutil,
        "which",
        lambda _name: "/usr/bin/apptainer",
    )
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            "preflight: PASS\n",
            "",
        )

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)

    runtime.preflight()

    assert runtime.executable == "/usr/bin/apptainer"
    assert seen["argv"][-5] == "bash"
    assert seen["argv"][-4] == "-lc"
    assert "command -v" in seen["argv"][-3]
    assert seen["argv"][-1] == str(runtime.run_dir)
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["cwd"] == str(runtime.run_dir)
    assert seen["kwargs"]["timeout"] == 60


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("missing tool: cmake", "cmake"),
        ("run directory is not writable", "not writable"),
    ],
)
def test_preflight_surfaces_container_probe_failure(
    monkeypatch,
    tmp_path: Path,
    stderr: str,
    expected: str,
):
    runtime = _make_runtime(tmp_path)
    monkeypatch.setattr(
        runtime_mod.shutil,
        "which",
        lambda _name: "/usr/bin/apptainer",
    )
    monkeypatch.setattr(
        runtime_mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            127,
            "",
            stderr,
        ),
    )

    with pytest.raises(RuntimePreflightError, match=expected):
        runtime.preflight()


@pytest.mark.parametrize("missing", ["image", "bind", "run_dir"])
def test_preflight_rejects_missing_paths_before_starting_container(
    monkeypatch,
    tmp_path: Path,
    missing: str,
):
    runtime = _make_runtime(tmp_path)
    resource = tmp_path / "resource"
    resource.mkdir()
    runtime = ApptainerRuntime(
        runtime.image,
        [resource],
        runtime.run_dir,
    )
    target = {
        "image": runtime.image,
        "bind": resource,
        "run_dir": runtime.run_dir,
    }[missing]
    target.rmdir() if target.is_dir() else target.unlink()
    monkeypatch.setattr(
        runtime_mod.shutil,
        "which",
        lambda _name: "/usr/bin/apptainer",
    )
    started = []
    monkeypatch.setattr(
        runtime_mod.subprocess,
        "run",
        lambda *args, **kwargs: started.append(True),
    )

    with pytest.raises(RuntimePreflightError, match="does not exist"):
        runtime.preflight()

    assert started == []


def test_preflight_reports_timeout(monkeypatch, tmp_path: Path):
    runtime = _make_runtime(tmp_path)
    monkeypatch.setattr(
        runtime_mod.shutil,
        "which",
        lambda _name: "/usr/bin/apptainer",
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(runtime_mod.subprocess, "run", timeout)

    with pytest.raises(RuntimePreflightError, match="timed out after 60s"):
        runtime.preflight()


def test_runtime_summary_does_not_include_environment_values(
    monkeypatch,
    tmp_path: Path,
):
    runtime = _make_runtime(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "top-secret")

    summary = "\n".join(runtime.summary_lines())

    assert "runtime: apptainer" in summary
    assert str(runtime.image) in summary
    assert str(runtime.run_dir) in summary
    assert "top-secret" not in summary
