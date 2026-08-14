from pathlib import PurePosixPath
import subprocess

from simpleloop.world import (
    MountMode,
    MountSpec,
    ProcessRequest,
    SandboxSpec,
)
from simpleloop.world.apptainer import (
    ApptainerSandbox,
    evaluator_environment,
    executor_environment,
)
from proposer.runtime import ApptainerRuntime


def _bound(tmp_path, *, network=True, environment=None, userns=True):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"sif")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ApptainerSandbox(executable="apptainer", userns=userns).bind(
        SandboxSpec(image, environment or {}, network),
        (MountSpec(repo, PurePosixPath("/work"), MountMode.READ_ONLY),),
    )


def test_bound_sandbox_builds_shell_free_contained_argv(tmp_path):
    sandbox = _bound(tmp_path)

    argv = sandbox.argv(
        ProcessRequest(("python", "-V"), PurePosixPath("/work"), 10)
    )

    assert argv[-2:] == ["python", "-V"]
    assert "--containall" in argv
    assert "--no-mount" in argv
    assert any(value.endswith(":/work:ro") for value in argv)


def test_network_none_is_explicit_and_environment_is_clean(tmp_path):
    sandbox = _bound(
        tmp_path,
        network=False,
        environment={"TOKEN": "secret"},
    )

    assert "--network" in sandbox.argv(
        ProcessRequest(("true",), PurePosixPath("/work"), 10)
    )
    assert sandbox.launcher_env({"APPTAINER_BAD": "x"}) == {
        "APPTAINERENV_TOKEN": "secret"
    }
    assert "secret" not in "\n".join(sandbox.summary_lines())


def test_user_namespace_is_an_explicit_provider_choice(tmp_path):
    request = ProcessRequest(("true",), PurePosixPath("/work"), 10)

    assert "--userns" in _bound(tmp_path).argv(request)
    assert "--userns" not in _bound(tmp_path, userns=False).argv(request)


def test_proposer_uses_the_same_explicit_user_namespace_choice(tmp_path):
    runtime = ApptainerRuntime(
        tmp_path / "runtime.sif", (), tmp_path, userns=False,
    )

    assert "--userns" not in runtime.exec_argv(("true",), cwd=tmp_path)


def test_role_environments_keep_credentials_out_of_evaluator():
    ambient = {
        "ANTHROPIC_AUTH_TOKEN": "token",
        "ANTHROPIC_BASE_URL": "https://ambient.invalid",
        "HTTPS_PROXY": "http://proxy.invalid",
        "UNRELATED": "ignored",
    }

    executor = executor_environment(
        base_url="https://configured.invalid",
        max_output_tokens=12345,
        environ=ambient,
    )
    evaluator = evaluator_environment(ambient)

    assert executor["ANTHROPIC_AUTH_TOKEN"] == "token"
    assert executor["ANTHROPIC_BASE_URL"] == "https://configured.invalid"
    assert executor["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "12345"
    assert evaluator == {"HTTPS_PROXY": "http://proxy.invalid"}


def test_run_returns_timeout_result_and_kills_process_group(
    monkeypatch,
    tmp_path,
):
    class FakeProcess:
        pid = 123
        returncode = -9
        calls = 0

        def communicate(self, _stdin=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(["apptainer"], timeout)
            return "partial", "timed out"

    killed = []
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: FakeProcess())
    monkeypatch.setattr("simpleloop.world.apptainer.os.killpg", lambda pid, sig: killed.append((pid, sig)))
    sandbox = _bound(tmp_path)

    result = sandbox.run(
        ProcessRequest(("sleep", "9"), PurePosixPath("/work"), 1)
    )

    assert result.timed_out is True
    assert result.exit_code == -9
    assert killed and killed[0][0] == 123
