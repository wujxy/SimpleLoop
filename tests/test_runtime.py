from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop import cli as cli_mod
from simpleloop.harness import evals as evals_mod
from simpleloop import loop as loop_mod
from simpleloop.container import runtime as runtime_mod
from simpleloop.harness.evals import EvalResult
from simpleloop.container.runtime import ApptainerRuntime, RuntimePreflightError


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
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [{"key": "CORRECTNESS"}],
            },
        },
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
    assert cfg["runtime_definition"] == str(
        (tmp_path / "runtime.def").resolve()
    )
    assert cfg["runtime_binds"] == []


def test_runtime_accepts_explicit_relative_definition(tmp_path: Path):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")

    cfg = config_mod.load(
        _write_task(
            tmp_path,
            {
                "image": "runtime.sif",
                "definition": "containers/base.def",
            },
        )
    )

    assert cfg["runtime_definition"] == str(
        (tmp_path / "containers/base.def").resolve()
    )


def test_prepare_load_allows_missing_git_metadata_and_image(tmp_path: Path):
    task = _write_task(
        tmp_path,
        {"image": "missing.sif", "definition": "runtime.def"},
    )
    repo = tmp_path / "repo"
    (repo / ".git").rmdir()

    cfg = config_mod.load(task, require_ready=False)

    assert cfg["repo_path"] == str(repo.resolve())
    assert cfg["runtime_image"] == str((tmp_path / "missing.sif").resolve())


def test_prepare_load_still_requires_existing_source_directory(
    tmp_path: Path,
):
    task = _write_task(
        tmp_path,
        {"image": "missing.sif", "definition": "runtime.def"},
    )
    repo = tmp_path / "repo"
    (repo / ".git").rmdir()
    repo.rmdir()

    with pytest.raises(
        config_mod.ConfigError,
        match="source.path.*directory",
    ):
        config_mod.load(task, require_ready=False)


def test_prepare_load_still_validates_unrelated_schema(tmp_path: Path):
    task = _write_task(
        tmp_path,
        {"image": "missing.sif", "definition": "runtime.def"},
    )
    raw = yaml.safe_load(task.read_text(encoding="utf-8"))
    raw["loop"]["max_rounds"] = 0
    task.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(config_mod.ConfigError, match="loop.max_rounds"):
        config_mod.load(task, require_ready=False)


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
        ["bash", "-c", "printf '%s\n' \"$PWD\""],
        cwd=run_dir,
    ) == [
        "/usr/bin/apptainer",
        "exec",
        "--cleanenv",
        "--no-eval",
        "--userns",
        "--bind",
        f"{resource}:{resource}",
        "--bind",
        f"{run_dir}:{run_dir}:rw",
        "--cwd",
        str(run_dir),
        str(image),
        "bash",
        "-c",
        "printf '%s\n' \"$PWD\"",
    ]


def test_exec_argv_drops_userns_when_disabled(monkeypatch, tmp_path: Path):
    """On a normal root-owned host, setuid is fine; the env var disables userns."""
    monkeypatch.setenv("SIMPLELOOP_APPTAINER_USERNS", "0")
    runtime = _make_runtime(tmp_path, executable="/usr/bin/apptainer")
    argv = runtime.exec_argv(["true"], cwd=runtime.run_dir)
    assert "--userns" not in argv
    assert argv[:5] == [
        "/usr/bin/apptainer",
        "exec",
        "--cleanenv",
        "--no-eval",
        "--bind",
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


def test_research_argv_is_contained_read_only_and_offline(tmp_path: Path):
    base = _make_runtime(tmp_path, executable="/usr/bin/apptainer")
    runtime = ApptainerRuntime(
        base.image, [tmp_path], base.run_dir,
        executable=base.executable,
    )
    source = tmp_path / "source"
    repo = tmp_path / "repo-view"
    scratch = tmp_path / "scratch"
    for path in (source, repo, scratch):
        path.mkdir()
    history = runtime.run_dir / "history.jsonl"
    history.write_text("{}\n", encoding="utf-8")
    rounds = runtime.run_dir / "rounds"
    rounds.mkdir()
    secret = runtime.run_dir / "job_env.sh"
    secret.write_text("export ANTHROPIC_API_KEY=secret\n", encoding="utf-8")

    argv = runtime.research_exec_argv(
        ["bash", "-lc", "git show --stat HEAD"],
        source=source,
        repo=repo,
        history=runtime.run_dir,
        scratch=scratch,
        cwd="source",
    )

    assert "--containall" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert f"{source.resolve()}:/source:ro" in argv
    assert f"{repo.resolve()}:/repo:ro" in argv
    assert f"{history.resolve()}:/history.jsonl:ro" in argv
    assert f"{rounds.resolve()}:/rounds:ro" in argv
    assert f"{scratch.resolve()}:/scratch:rw" in argv
    assert f"{tmp_path.resolve()}:{tmp_path.resolve()}:ro" not in argv
    assert not any(str(secret) in arg for arg in argv)
    assert argv[argv.index("--cwd") + 1] == "/source"


def test_research_argv_accepts_only_source_or_scratch_cwd(tmp_path: Path):
    runtime = _make_runtime(tmp_path)
    with pytest.raises(ValueError, match="source.*scratch"):
        runtime.research_exec_argv(
            ["true"], source=tmp_path, repo=tmp_path,
            history=tmp_path, scratch=tmp_path, cwd="history",
        )


def test_research_env_contains_no_credentials(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runtime_mod.os, "environ", {
        "PATH": "/host/bin",
        "HOME": "/home/user",
        "LANG": "C.UTF-8",
        "HEPAI_API_KEY": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "CONDOR_TOKEN": "secret",
        "HTTPS_PROXY": "https://proxy",
    })
    runtime = _make_runtime(tmp_path)

    assert runtime.research_subprocess_env() == {
        "PATH": "/host/bin",
        "HOME": "/home/user",
        "LANG": "C.UTF-8",
    }


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
            "BASH_FUNC_which%%": "() {  /usr/bin/which ...; }",
            "BASH_FUNC_module%%": "() {  local _mlredir=0; ... }",
            "which_declare": "declare -f",
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
    # Bash-exported shell functions poison /bin/sh children in the container;
    # strip them along with the apptainer injection prefixes.
    assert "BASH_FUNC_which%%" not in env
    assert "BASH_FUNC_module%%" not in env
    assert "which_declare" not in env
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
    assert seen["argv"][-4] == "-c"
    probe = seen["argv"][-3]
    assert "command -v" in probe
    for tool in ("bash", "git", "node", "claude"):
        assert tool in probe
    for task_specific_tool in ("gcc", "g++", "make", "cmake"):
        assert task_specific_tool not in probe
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

    monkeypatch.setattr(evals_mod.subprocess, "run", fake_run)
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }

    result = evals_mod.run_eval(
        ["bash scripts/eval.sh --evtmax 10"],
        tmp_path,
        runtime,
        schema,
    )

    assert seen["argv"][-3:] == [
        "bash",
        "-c",
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


def test_run_eval_records_nonzero_status_and_preserves_streams(
    monkeypatch, tmp_path: Path,
):
    runtime = _make_runtime(tmp_path, executable="/usr/bin/apptainer")
    results = iter(
        [
            subprocess.CompletedProcess([], 0, "first", ""),
            subprocess.CompletedProcess([], 9, "build progress",
                                        "compiler diagnostic"),
        ]
    )
    monkeypatch.setattr(
        evals_mod.subprocess,
        "run",
        lambda *args, **kwargs: next(results),
    )

    result = evals_mod.run_eval(["first", "second"], tmp_path, runtime)

    assert result.returncodes == (0, 9)
    assert result.commands_ok is False
    assert "[EXIT 9]" in result.text
    # The failing command's stdout and stderr are preserved in the report.
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
def test_local_backend_require_baseline_acceptance_rejects_bad_results(
    result: EvalResult,
    schema: dict | None,
    message: str,
):
    from simpleloop.execution.local import LocalBackend
    from simpleloop.loop import BaselineAcceptanceError

    class FakeRuntime:
        def summary_lines(self):
            return ()

        def preflight(self):
            pass

        def __getattr__(self, name):
            # Mock any other attributes that might be accessed
            return None

    class FakeWorkspace:
        def __init__(self):
            pass

        def add_worktree(self, name, sha):
            return Path("/fake/worktree")

        def remove_worktree(self, name):
            pass

    class FakeContext:
        def __init__(self):
            self.cfg = {
                "eval_commands": ["echo test"],
                "eval_timeout_seconds": 60,
                "eval_output_cap_chars": 16000,
                "metrics": schema,
            }
            self.runtime = FakeRuntime()
            self.workspace = FakeWorkspace()

    backend = LocalBackend(FakeContext())

    with pytest.raises(
        BaselineAcceptanceError,
        match=message,
    ):
        # We need to test the validation logic
        # Since we can't easily run the full eval_baseline, test the validation separately
        backend._require_baseline_acceptance(result, schema)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_local_backend_require_baseline_acceptance_rejects_unusable_objective(value):
    from simpleloop.execution.local import LocalBackend
    from simpleloop.loop import BaselineAcceptanceError

    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [],
    }
    result = EvalResult("bad objective", {"SPEED_MS": value}, (0,))

    class FakeContext:
        pass

    backend = LocalBackend(FakeContext())

    with pytest.raises(
        BaselineAcceptanceError,
        match="SPEED_MS",
    ):
        backend._require_baseline_acceptance(result, schema)


def test_local_backend_require_baseline_acceptance_accepts_objective_and_all_gates():
    from simpleloop.execution.local import LocalBackend

    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "A"}, {"key": "B"}],
    }

    class FakeContext:
        pass

    backend = LocalBackend(FakeContext())
    backend._require_baseline_acceptance(
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

    class FakeModel:
        @classmethod
        def from_config(cls, _config):
            return object()

    class FakeProposer:
        def __init__(self, **_kwargs):
            events.append("agent")

    class FakeWorkspace:
        def __init__(self, *, run_dir, **kwargs):
            self.repo = Path(run_dir) / "repo"

        def setup(self):
            events.append("workspace.setup")
            self.repo.mkdir()

        def baseline_sha(self):
            return "baseline-sha"

    class FakeBackend:
        def __init__(self, ctx):
            pass

        def eval_baseline(self, *, baseline_sha: str) -> tuple[str, dict]:
            return "", {"SPEED_MS": 100.0}

        def run_candidates(self, *, proposals: list[dict], round_id: int,
                           parent_sha: str, prior_metrics: dict,
                           baseline_metrics: dict, journal=None) -> list[dict]:
            return []

        def resume_round(self, jobs: list[dict], *, round_id: int,
                         parent_sha: str, journal=None) -> list[dict]:
            return []

    cfg = {
        "goal": "test",
        "editable_paths": ["src/**"],
        "frozen_paths": [],
        "max_rounds": 0,
        "candidates_per_round": 1,
        "max_workers": 1,
        "agent_timeout_seconds": 60,
        "roles": {
            "researcher": {
                "model": "gpt-5.5", "base_url": "https://example.invalid",
                "command_timeout_seconds": 2,
                "command_output_cap_chars": 1000,
            },
            "executor": {
                "model": "glm-5", "base_url": "https://example.invalid",
            },
        },
        "runtime_image": str(tmp_path / "runtime.sif"),
        "runtime_binds": [],
        "eval_commands": ["run-eval"],
        "metrics": {"objective": {"key": "SPEED_MS", "lower_is_better": True},
                    "gates": []},
        "repo_path": str(tmp_path / "source"),
        "baseline_ref": "HEAD",
    }
    monkeypatch.setattr(config_mod, "load", lambda _path: cfg)
    monkeypatch.setattr(loop_mod, "ApptainerRuntime", FakeRuntime)
    monkeypatch.setattr(loop_mod, "Agent", FakeAgent)
    monkeypatch.setattr(loop_mod.model_mod, "HepAIChatModel", FakeModel)
    monkeypatch.setattr(loop_mod.proposer_mod, "ProposerAgent", FakeProposer)
    monkeypatch.setattr(loop_mod, "Workspace", FakeWorkspace)
    monkeypatch.setattr(loop_mod, "build_backend", lambda ctx: FakeBackend(ctx))

    loop_mod.run("task.yaml", tmp_path / "run")

    assert events.count("preflight") == 1
    assert events.index("preflight") < events.index("agent")
    assert events.index("preflight") < events.index("workspace.setup")


def test_assert_executor_ready_requires_executor_base_url():
    # executor missing entirely -> the 12-round ConnectionRefused failure mode
    with pytest.raises(config_mod.ConfigError, match="roles.executor.base_url"):
        loop_mod._assert_executor_ready({"roles": {"executor": None}})
    # blank base_url is just as bad
    with pytest.raises(config_mod.ConfigError, match="roles.executor.base_url"):
        loop_mod._assert_executor_ready(
            {"roles": {"executor": {"model": "glm-5", "base_url": " "}}})


def test_assert_executor_ready_requires_auth_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = {"roles": {"executor": {"model": "glm-5",
                                  "base_url": "https://x.example/anthropic"}}}
    with pytest.raises(config_mod.ConfigError, match="ANTHROPIC_AUTH_TOKEN"):
        loop_mod._assert_executor_ready(cfg)


def test_assert_executor_ready_passes_when_configured():
    cfg = {"roles": {"executor": {"model": "glm-5",
                                  "base_url": "https://x.example/anthropic"}}}
    # autouse fixture seeds ANTHROPIC_AUTH_TOKEN; a configured executor passes
    loop_mod._assert_executor_ready(cfg)


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
        config_mod,
        "load",
        lambda _path: {"self_improvement": None},
    )
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
