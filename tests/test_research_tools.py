from __future__ import annotations

import json
import io
import os
import subprocess

import pytest

from simpleloop.roles.research_tools import (
    RESEARCH_TOOL_SPECS,
    ResearchCommandRunner,
    ResearchTools,
    render_history_directory,
    render_research_tool_prompt,
)


class _Runtime:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.argv_call = None

    def research_exec_argv(self, payload, **paths):
        self.argv_call = (payload, paths)
        return ["apptainer", *payload]

    def research_subprocess_env(self):
        return {"PATH": "/usr/bin"}


class _Process:
    pid = 123

    def __init__(self, *, output=("stdout", "stderr"), returncode=0,
                 timeout_once=False):
        self.output = output
        self.returncode = returncode
        self.timeout_once = timeout_once
        self.timeouts = []
        self.stdout = io.StringIO(output[0])
        self.stderr = io.StringIO(output[1])

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("apptainer", timeout)
        return self.returncode


class _RepeatingStream:
    def __init__(self, char: str, count: int):
        self.char = char
        self.remaining = count

    def read(self, size: int) -> str:
        count = min(size, self.remaining)
        self.remaining -= count
        return self.char * count


def _candidate(round_id: int, *, proposal: str = "cache values",
               note: str = "") -> dict:
    return {
        "candidate": 0,
        "proposal": proposal,
        "parent_sha": f"parent-{round_id}",
        "sha": f"sha-{round_id}",
        "status": "COMPLETED",
        "selected": True,
        "gate_passed": True,
        "eligible": True,
        "gates": {"physics_gate": {"passed": True}},
        "metrics": {"SPEED_MS": 100 - round_id},
        "changed_paths": [f"src/r{round_id}.cc"],
        "eval_block": f"SPEED_MS={100 - round_id}",
        "note": note,
    }


def _history(count: int = 3, *, note: str = "") -> list[dict]:
    return [
        {"round": round_id, "parent_sha": f"parent-{round_id}",
         "candidates": [_candidate(round_id, note=note)]}
        for round_id in range(count)
    ]


# --- render_history_directory --------------------------------------------

def test_render_history_directory_empty():
    assert render_history_directory([]) == "(no prior experiments)"


def test_render_history_directory_lists_every_round_with_note():
    history = _history(3, note="cache helped")
    out = render_history_directory(history)
    # Every prior candidate is listed as ref: note — no window, no collapse.
    assert "r0c0: cache helped" in out
    assert "r1c0: cache helped" in out
    assert "r2c0: cache helped" in out
    assert "Older experiments" not in out


def test_render_history_directory_blank_note_shows_bare_ref():
    history = _history(1, note="")
    out = render_history_directory(history)
    assert "r0c0:" in out
    assert "Older experiments" not in out


def test_render_history_directory_mixes_noted_and_blank_latest():
    # Older round carries a note; the latest round is still blank (written next
    # round) — both appear, with no collapsing to bare refs.
    history = _history(2, note="x")
    history[1]["candidates"][0]["note"] = ""
    out = render_history_directory(history)
    assert "r0c0: x" in out
    assert "r1c0:" in out
    assert "r1c0: x" not in out


# --- tool prompt ---------------------------------------------------------

def test_research_tool_prompt_is_composed_from_tool_specs():
    prompt = render_research_tool_prompt()

    assert {spec.action for spec in RESEARCH_TOOL_SPECS} == {
        "run_research_command",
        "inspect_episode",
    }
    for spec in RESEARCH_TOOL_SPECS:
        assert spec.schema in prompt
        assert spec.description in prompt


# --- ResearchCommandRunner (unchanged behavior) --------------------------

def _runner(tmp_path, *, cap=100, timeout=12):
    paths = {
        name: tmp_path / name
        for name in ("source", "repo", "history", "scratch")
    }
    for path in paths.values():
        path.mkdir()
    git_dir = paths["repo"] / ".git" / "worktrees" / "research"
    git_dir.mkdir(parents=True)
    (paths["source"] / ".git").write_text(
        f"gitdir: {git_dir}\n", encoding="utf-8",
    )
    runtime = _Runtime(paths["history"])
    runner = ResearchCommandRunner(
        runtime=runtime,
        source=paths["source"],
        repo=paths["repo"],
        history_dir=paths["history"],
        scratch=paths["scratch"],
        timeout_seconds=timeout,
        output_cap_chars=cap,
    )
    return runner, runtime


def test_research_command_uses_process_group_and_returns_observation(
    tmp_path, monkeypatch,
):
    runner, runtime = _runner(tmp_path)
    process = _Process(returncode=7)
    popen_calls = []

    def fake_popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return process

    monkeypatch.setattr("simpleloop.roles.research_tools.subprocess.Popen",
                        fake_popen)
    monkeypatch.setattr(
        "simpleloop.roles.research_tools.os.killpg", lambda *_args: None,
    )

    result = runner.run("git show HEAD", cwd="source")

    payload, paths = runtime.argv_call
    assert payload == [
        "env", "GIT_DIR=/repo/.git/worktrees/research",
        "GIT_COMMON_DIR=/repo/.git", "GIT_WORK_TREE=/source",
        "bash", "-lc", "git show HEAD",
    ]
    assert paths["cwd"] == "source"
    assert popen_calls[0][1]["start_new_session"] is True
    assert popen_calls[0][1]["shell"] is False
    assert result == {
        "ok": False,
        "returncode": 7,
        "timed_out": False,
        "truncated": False,
        "output": "stdout\n[stderr]\nstderr",
    }


def test_research_command_uses_the_snapshot_worktree_head(tmp_path):
    repo = tmp_path / "repo"
    source = tmp_path / "source"
    history = tmp_path / "history"
    scratch = tmp_path / "scratch"
    repo.mkdir()
    history.mkdir()
    scratch.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=True,
        ).stdout.strip()

    git("-C", str(repo), "init")
    git("-C", str(repo), "config", "user.name", "Test")
    git("-C", str(repo), "config", "user.email", "test@example.invalid")
    (repo / "value.txt").write_text("parent\n", encoding="utf-8")
    git("-C", str(repo), "add", "value.txt")
    git("-C", str(repo), "commit", "-m", "parent")
    parent_sha = git("-C", str(repo), "rev-parse", "HEAD")
    (repo / "value.txt").write_text("new head\n", encoding="utf-8")
    git("-C", str(repo), "commit", "-am", "new head")
    git("-C", str(repo), "worktree", "add", "--detach", str(source), parent_sha)

    class HostRuntime:
        run_dir = tmp_path

        def research_exec_argv(self, payload, **paths):
            replacements = {
                "/repo": str(paths["repo"]),
                "/source": str(paths["source"]),
            }
            return [
                next((item.replace(old, new) for old, new in replacements.items()
                      if old in item), item)
                for item in payload
            ]

        def research_subprocess_env(self):
            return {"PATH": os.environ["PATH"]}

    runner = ResearchCommandRunner(
        runtime=HostRuntime(), source=source, repo=repo,
        history_dir=history, scratch=scratch,
        timeout_seconds=10, output_cap_chars=1000,
    )

    result = runner.run(
        'git rev-parse HEAD && test -z "$(git status --porcelain)"',
    )

    assert result["ok"] is True
    assert result["output"].strip() == parent_sha


def test_research_command_timeout_kills_process_group(tmp_path, monkeypatch):
    runner, _runtime = _runner(tmp_path, timeout=9)
    process = _Process(output=("partial", ""), timeout_once=True)
    killed = []
    monkeypatch.setattr(
        "simpleloop.roles.research_tools.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        "simpleloop.roles.research_tools.os.killpg",
        lambda pid, sig: killed.append((pid, sig)),
    )

    result = runner.run("sleep 100", cwd="scratch")

    assert killed and killed[0][0] == process.pid
    assert result["timed_out"] is True
    assert result["returncode"] is None
    assert process.timeouts == [9, None]


def test_research_command_caps_combined_output(tmp_path, monkeypatch):
    runner, _runtime = _runner(tmp_path, cap=12)
    process = _Process(output=("", "tail"))
    process.stdout = _RepeatingStream("x", 1_000_000)
    monkeypatch.setattr(
        "simpleloop.roles.research_tools.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        "simpleloop.roles.research_tools.os.killpg", lambda *_args: None,
    )

    result = runner.run("true", cwd="source")

    assert result["truncated"] is True
    assert result["output"] == "x" * 12
    assert process.stdout.remaining == 0


@pytest.mark.parametrize(
    ("command", "cwd"), [("", "source"), ("true", "history")],
)
def test_research_command_rejects_invalid_input(tmp_path, command, cwd):
    runner, _runtime = _runner(tmp_path)
    with pytest.raises(ValueError):
        runner.run(command, cwd=cwd)


def test_research_tools_inspect_episode(tmp_path):
    runner, runtime = _runner(tmp_path)
    tools = ResearchTools(
        runtime=runtime, source=runner.source, repo=runner.repo,
        history_dir=runner.history_dir, scratch=runner.scratch,
        history=_history(), command_timeout_seconds=12,
        command_output_cap_chars=100,
    )

    episode = tools.execute(
        {"action": "inspect_episode", "ref": "r2c0"}, deadline=1000,
    )

    assert episode["ok"]
    assert episode["result"]["eval_block"] == "SPEED_MS=98"


def test_research_tools_command_uses_remaining_deadline(tmp_path, monkeypatch):
    runner, runtime = _runner(tmp_path)
    tools = ResearchTools(
        runtime=runtime, source=runner.source, repo=runner.repo,
        history_dir=runner.history_dir, scratch=runner.scratch,
        history=_history(), command_timeout_seconds=12,
        command_output_cap_chars=100,
    )
    calls = []
    tools.command_runner.run = lambda command, **kwargs: (
        calls.append((command, kwargs)) or {"ok": True}
    )
    monkeypatch.setattr(
        "simpleloop.roles.research_tools.time.monotonic", lambda: 90,
    )

    result = tools.execute({
        "action": "run_research_command", "command": "rg cache",
        "cwd": "source",
    }, deadline=100)

    assert result["ok"] is True
    assert calls == [("rg cache", {"cwd": "source", "timeout_seconds": 10})]