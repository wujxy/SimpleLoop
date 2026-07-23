from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop import cli as cli_mod
from simpleloop import judger as judger_mod
from simpleloop import loop as loop_mod
from simpleloop import runtime as runtime_mod
from simpleloop.judger import EvalResult
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
        "--no-eval",
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
            "APPTAINER_NO_HOME": "1",
            "APPTAINER_CONTAINALL": "1",
            "SINGULARITY_BIND": "/legacy-unexpected",
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
    assert "APPTAINER_NO_HOME" not in env
    assert "APPTAINER_CONTAINALL" not in env
    assert "SINGULARITY_BIND" not in env
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


def test_subprocess_env_preserves_allowlisted_value_for_no_eval(
    monkeypatch,
    tmp_path: Path,
):
    value = '$(touch /tmp/should-not-run)`id`:$HOME:"quoted"'
    monkeypatch.setattr(
        runtime_mod.os,
        "environ",
        {"ANTHROPIC_AUTH_TOKEN": value},
    )

    env = _make_runtime(tmp_path).subprocess_env()

    assert env["APPTAINERENV_ANTHROPIC_AUTH_TOKEN"] == value


@pytest.mark.parametrize("separator", [":", ","])
def test_runtime_config_rejects_bind_separator(
    tmp_path: Path,
    separator: str,
):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    resource = tmp_path / f"resource{separator}name"
    resource.mkdir()

    with pytest.raises(config_mod.ConfigError, match="unsupported.*separator"):
        config_mod.load(
            _write_task(
                tmp_path,
                {"image": str(image), "binds": [str(resource)]},
            )
        )


@pytest.mark.parametrize("separator", [":", ","])
def test_preflight_rejects_run_directory_bind_separator(
    monkeypatch,
    tmp_path: Path,
    separator: str,
):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    run_dir = tmp_path / f"run{separator}name"
    run_dir.mkdir()
    runtime = ApptainerRuntime(image, [], run_dir)
    monkeypatch.setattr(
        runtime_mod.shutil,
        "which",
        lambda _name: "/usr/bin/apptainer",
    )

    with pytest.raises(
        RuntimePreflightError,
        match="unsupported.*separator",
    ):
        runtime.preflight()


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


def test_run_eval_wraps_bash_lc_and_parses_metrics(
    monkeypatch,
    tmp_path: Path,
):
    runtime = _make_runtime(tmp_path, executable="/usr/bin/apptainer")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            "SPEED_MS=12.5\nCORRECTNESS=PASS\n",
            "",
        )

    monkeypatch.setattr(judger_mod.subprocess, "run", fake_run)
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }

    result = judger_mod.run_eval(
        ["bash scripts/eval.sh --evtmax 10"],
        tmp_path,
        runtime,
        schema,
    )

    assert seen["argv"][-3:] == [
        "bash",
        "-lc",
        "bash scripts/eval.sh --evtmax 10",
    ]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["env"] == runtime.subprocess_env()
    assert result.returncodes == (0,)
    assert result.metrics == {
        "SPEED_MS": 12.5,
        "CORRECTNESS": True,
    }
    assert "[OK]" in result.text


def test_run_eval_records_each_nonzero_status(monkeypatch, tmp_path: Path):
    runtime = _make_runtime(tmp_path, executable="/usr/bin/apptainer")
    results = iter(
        [
            subprocess.CompletedProcess([], 0, "first", ""),
            subprocess.CompletedProcess([], 9, "", "second failed"),
        ]
    )
    monkeypatch.setattr(
        judger_mod.subprocess,
        "run",
        lambda *args, **kwargs: next(results),
    )

    result = judger_mod.run_eval(
        ["first", "second"],
        tmp_path,
        runtime,
    )

    assert result.returncodes == (0, 9)
    assert result.commands_ok is False
    assert "[EXIT 9]" in result.text


def test_run_eval_preserves_stdout_and_stderr_on_failure(
    monkeypatch,
    tmp_path: Path,
):
    runtime = _make_runtime(tmp_path, executable="/usr/bin/apptainer")
    monkeypatch.setattr(
        judger_mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            7,
            "build progress",
            "compiler diagnostic",
        ),
    )

    result = judger_mod.run_eval(["build"], tmp_path, runtime)

    assert "stdout:\nbuild progress" in result.text
    assert "stderr:\ncompiler diagnostic" in result.text


@pytest.mark.parametrize(
    ("result", "schema", "message"),
    [
        (EvalResult("$ eval [EXIT 3]", {}, (3,)), None, "exit 3"),
        (
            EvalResult(
                "missing objective",
                {"CORRECTNESS": True},
                (0,),
            ),
            {
                "objective": {
                    "key": "SPEED_MS",
                    "lower_is_better": True,
                },
                "gates": [{"key": "CORRECTNESS"}],
            },
            "SPEED_MS",
        ),
        (
            EvalResult(
                "failed gate",
                {"SPEED_MS": 10.0, "CORRECTNESS": False},
                (0,),
            ),
            {
                "objective": {
                    "key": "SPEED_MS",
                    "lower_is_better": True,
                },
                "gates": [{"key": "CORRECTNESS"}],
            },
            "CORRECTNESS",
        ),
    ],
)
def test_require_baseline_acceptance_rejects_bad_results(
    result: EvalResult,
    schema: dict | None,
    message: str,
):
    with pytest.raises(
        loop_mod.BaselineAcceptanceError,
        match=message,
    ):
        loop_mod._require_baseline_acceptance(result, schema)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_require_baseline_acceptance_rejects_unusable_objective(value):
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [],
    }
    result = EvalResult("bad objective", {"SPEED_MS": value}, (0,))

    with pytest.raises(
        loop_mod.BaselineAcceptanceError,
        match="SPEED_MS",
    ):
        loop_mod._require_baseline_acceptance(result, schema)


def test_require_baseline_acceptance_accepts_commands_without_schema():
    loop_mod._require_baseline_acceptance(
        EvalResult("ok", {}, (0, 0)),
        None,
    )


def test_require_baseline_acceptance_accepts_objective_and_all_gates():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "A"}, {"key": "B"}],
    }
    loop_mod._require_baseline_acceptance(
        EvalResult(
            "ok",
            {"SPEED_MS": 10.0, "A": True, "B": True},
            (0,),
        ),
        schema,
    )


def test_run_preflights_before_agent_or_workspace(
    monkeypatch,
    tmp_path: Path,
):
    events = []

    class FakeRuntime:
        def __init__(self, **kwargs):
            events.append("runtime.init")

        def summary_lines(self):
            return ()

        def preflight(self):
            events.append("preflight")

    class FakeAgent:
        def __init__(self, *, runtime, **kwargs):
            events.append("agent")
            self.runtime = runtime

    class FakeWorkspace:
        def __init__(self, *, run_dir, **kwargs):
            self.repo = Path(run_dir) / "repo"

        def setup(self):
            events.append("workspace.setup")
            self.repo.mkdir()

        def baseline_sha(self):
            return "baseline-sha"

    cfg = {
        "goal": "test",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "max_rounds": 0,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 60,
        "runtime_image": str(tmp_path / "runtime.sif"),
        "runtime_binds": [],
        "eval_commands": [],
        "metrics": None,
        "repo_path": str(tmp_path / "source"),
        "baseline_ref": "HEAD",
    }
    monkeypatch.setattr(config_mod, "load", lambda _path: cfg)
    monkeypatch.setattr(loop_mod, "ApptainerRuntime", FakeRuntime)
    monkeypatch.setattr(loop_mod, "Agent", FakeAgent)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)

    loop_mod.run("task.yaml", tmp_path / "run")

    assert events.count("preflight") == 1
    assert events.index("preflight") < events.index("agent")
    assert events.index("preflight") < events.index("workspace.setup")


def test_validate_prints_normalized_runtime(monkeypatch, capsys):
    monkeypatch.setattr(
        config_mod,
        "load",
        lambda _path: {
            "goal": "test",
            "max_rounds": 1,
            "candidates_per_round": 1,
            "max_workers": 1,
            "eval_commands": [],
            "repo_path": "/repo",
            "baseline_ref": "HEAD",
            "runtime_image": "/images/runtime.sif",
            "runtime_binds": ["/cvmfs", "/data/juno"],
        },
    )

    cli_mod.main(["validate", "--config", "task.yaml"])

    out = capsys.readouterr().out
    assert "runtime image: /images/runtime.sif" in out
    assert "runtime binds: /cvmfs, /data/juno" in out


@pytest.mark.parametrize(
    ("error", "prefix"),
    [
        (RuntimePreflightError("missing cmake"), "Runtime error:"),
        (
            loop_mod.BaselineAcceptanceError("CORRECTNESS failed"),
            "Baseline error:",
        ),
    ],
)
def test_run_cli_reports_runtime_failures_without_traceback(
    monkeypatch,
    capsys,
    error: Exception,
    prefix: str,
):
    monkeypatch.setattr(
        loop_mod,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(
            [
                "run",
                "--config",
                "task.yaml",
                "--run-dir",
                "run",
            ]
        )

    assert exc.value.code == 1
    assert prefix in capsys.readouterr().err
