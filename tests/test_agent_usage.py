from __future__ import annotations

import io
import json
from pathlib import Path

from simpleloop.roles import agent as agent_mod
from simpleloop.roles.agent import Agent, _decode_output


class RecordingRuntime:
    def __init__(self):
        self.calls = []
        self.overrides = None
        self.executor_home = Path("/home/tester")
        self.sandbox = None

    def exec_argv(
        self, payload, *, cwd, mounts=None, home=None,
    ):
        if mounts is not None:
            self.sandbox = Path(home)
        self.calls.append((list(payload), Path(cwd)))
        return ["apptainer", "exec", "image.sif", *payload]

    def subprocess_env(self, overrides=None):
        self.overrides = dict(overrides or {})
        return {
            "APPTAINERENV_CLAUDE_CODE_MAX_OUTPUT_TOKENS":
                self.overrides["CLAUDE_CODE_MAX_OUTPUT_TOKENS"],
        }


class CapturingStdin:
    def __init__(self):
        self.text = ""

    def write(self, value):
        self.text += value
        return len(value)

    def close(self):
        pass


class FinishedProcess:
    def __init__(self):
        self.stdin = CapturingStdin()
        self.stdout = io.StringIO('{"result":"ok","usage":{}}\n')
        self.stderr = io.StringIO("")
        self.returncode = 0
        self.pid = 1234

    def poll(self):
        return self.returncode


def test_decode_output_extracts_structured_result_and_usage():
    result = _decode_output(json.dumps({
        "result": "fallback",
        "structured_output": {"proposals": ["try A"]},
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_read_input_tokens": 3,
        },
    }))

    assert result.text == "fallback"
    assert result.data == {"proposals": ["try A"]}
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
    }


def test_agent_notifies_usage_observer():
    seen = []
    agent = Agent(runtime=RecordingRuntime(), usage_observer=seen.append)

    agent._notify_usage({"input_tokens": 1, "output_tokens": 2}, "proposer")

    assert seen == [{"input_tokens": 1, "output_tokens": 2}]


def test_usage_observer_failure_is_nonfatal(capsys):
    def fail(_usage):
        raise OSError("state unavailable")

    agent = Agent(runtime=RecordingRuntime(), usage_observer=fail)

    agent._notify_usage({"input_tokens": 1, "output_tokens": 2}, "researcher")

    assert "[telemetry] warning:" in capsys.readouterr().out


def test_agent_wraps_literal_claude_and_keeps_prompt_on_stdin(
    monkeypatch,
    tmp_path: Path,
):
    runtime = RecordingRuntime()
    process = FinishedProcess()
    popen_call = {}
    lifecycle = []

    def fake_popen(argv, **kwargs):
        popen_call["argv"] = argv
        popen_call["kwargs"] = kwargs
        return process

    monkeypatch.setattr(agent_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        agent_mod.CHILD_PROCESSES, "register",
        lambda pid: lifecycle.append(("register", pid)),
    )
    monkeypatch.setattr(
        agent_mod.CHILD_PROCESSES, "unregister",
        lambda pid: lifecycle.append(("unregister", pid)),
    )
    agent = Agent(runtime=runtime, timeout_seconds=60)

    assert agent.run_text(
        "large prompt",
        cwd=tmp_path,
        label="executor",
    ) == "ok"

    payload, cwd = runtime.calls[0]
    assert payload[0] == "claude"
    assert payload[1:5] == [
        "-p",
        "--input-format",
        "text",
        "--output-format",
    ]
    assert "large prompt" not in payload
    assert cwd == tmp_path
    assert process.stdin.text == "large prompt"
    assert popen_call["argv"][:3] == [
        "apptainer",
        "exec",
        "image.sif",
    ]
    assert "shell" not in popen_call["kwargs"]
    assert runtime.overrides == {
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"
    }
    assert lifecycle == [("register", 1234), ("unregister", 1234)]


def test_executor_agent_owns_private_work_and_home(monkeypatch, tmp_path: Path):
    runtime = RecordingRuntime()
    monkeypatch.setattr(
        agent_mod.subprocess, "Popen", lambda *args, **kwargs: FinishedProcess(),
    )
    agent = Agent(
        runtime=runtime,
        timeout_seconds=60,
        mounts=agent_mod.MountMap(rw=("src",)),
    )

    assert agent.run_text("edit", cwd=tmp_path, label="executor") == "ok"

    home = runtime.sandbox
    assert home.name == "home"
    assert not home.parent.exists()
    assert runtime.overrides["HOME"] == str(runtime.executor_home)
