from __future__ import annotations

import io
import os
import subprocess

import pytest

from proposer.research_tools import (
    RESEARCH_TOOL_SPECS,
    ResearchCommandRunner,
    ResearchTools,
    render_research_tool_prompt,
)
from proposer.runtime import MountMap


class _Runtime:
    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.executor_home = "/home/test"
        self.argv_call = None

    def exec_argv(self, payload, *, cwd, mounts, home,
                  extra_binds=(), work_cwd="/work", network=True, **_kw):
        self.argv_call = (payload, {"cwd": cwd, "work_cwd": work_cwd,
                                    "extra_binds": extra_binds,
                                    "mounts": mounts})
        return ["apptainer", *payload]

    def research_subprocess_env(self, home=None):
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


class _FakeMemoryService:
    def __init__(self):
        self.calls = []

    def inspect_episode(self, ref):
        self.calls.append(("inspect_episode", ref))
        return {"ref": ref, "eval_block": f"body for {ref}"}

    def list_findings(self, *, state, limit, current_round):
        self.calls.append(("list_findings", state, limit, current_round))
        return [{"id": "F-001", "state": state}]

    def search_findings(self, *, query, limit):
        self.calls.append(("search_findings", query, limit))
        return [{"id": "F-001", "score": 1.0}]

    def inspect_finding(self, finding_id):
        self.calls.append(("inspect_finding", finding_id))
        if finding_id == "MISSING":
            raise ValueError(f"unknown finding: {finding_id}")
        return {"id": finding_id}

    def search_experiments(self, *, query, filters, limit, buckets):
        self.calls.append(
            ("search_experiments", query, filters, limit, buckets),
        )
        if buckets:
            return {"relevant": [], "contrasting": [], "diverse": []}
        return []


# --- tool prompt ---------------------------------------------------------

def test_research_tool_prompt_is_composed_from_tool_specs():
    prompt = render_research_tool_prompt()

    assert {spec.action for spec in RESEARCH_TOOL_SPECS} == {
        "run_research_command",
        "read_file",
        "grep_files",
        "glob_files",
        "write_scratch_file",
        "inspect_episode",
        "list_findings",
        "search_findings",
        "inspect_finding",
        "search_experiments",
    }
    for spec in RESEARCH_TOOL_SPECS:
        assert spec.schema in prompt
        assert spec.description in prompt


# --- ResearchCommandRunner (unchanged behavior) --------------------------

def _runner(tmp_path, *, cap=100, timeout=12):
    paths = {
        name: tmp_path / name
        for name in ("workspace", "repo", "history", "scratch")
    }
    for path in paths.values():
        path.mkdir()
    git_dir = paths["repo"] / ".git" / "worktrees" / "research"
    git_dir.mkdir(parents=True)
    (paths["workspace"] / ".git").write_text(
        f"gitdir: {git_dir}\n", encoding="utf-8",
    )
    runtime = _Runtime(paths["history"])
    home = tmp_path / "home"
    home.mkdir()
    runner = ResearchCommandRunner(
        runtime=runtime,
        workspace=paths["workspace"],
        repo=paths["repo"],
        history_dir=paths["history"],
        scratch=paths["scratch"],
        world_mount=MountMap(),
        home=home,
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
    lifecycle = []

    def fake_popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return process

    monkeypatch.setattr("proposer.research_tools.subprocess.Popen",
                        fake_popen)
    monkeypatch.setattr(
        "proposer.research_tools.os.killpg", lambda *_args: None,
    )
    monkeypatch.setattr(
        "proposer.research_tools.CHILD_PROCESSES.register",
        lambda pid: lifecycle.append(("register", pid)),
    )
    monkeypatch.setattr(
        "proposer.research_tools.CHILD_PROCESSES.unregister",
        lambda pid: lifecycle.append(("unregister", pid)),
    )

    result = runner.run("git show HEAD", cwd="work")

    payload, kwargs = runtime.argv_call
    assert payload == [
        "env", "GIT_DIR=/repo/.git/worktrees/research",
        "GIT_COMMON_DIR=/repo/.git", "GIT_WORK_TREE=/work",
        "bash", "-lc", "git show HEAD",
    ]
    assert kwargs["work_cwd"] == "/work"
    assert popen_calls[0][1]["start_new_session"] is True
    assert popen_calls[0][1]["shell"] is False
    assert result == {
        "ok": False,
        "returncode": 7,
        "timed_out": False,
        "truncated": False,
        "output": "stdout\n[stderr]\nstderr",
    }
    assert lifecycle == [("register", 123), ("unregister", 123)]


def test_research_command_uses_the_snapshot_worktree_head(tmp_path):
    repo = tmp_path / "repo"
    workspace = tmp_path / "workspace"
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
    git("-C", str(repo), "worktree", "add", "--detach", str(workspace), parent_sha)

    class HostRuntime:
        run_dir = tmp_path
        executor_home = str(tmp_path / "home")

        def exec_argv(self, payload, *, cwd, mounts, home,
                      extra_binds=(), work_cwd="/work", network=True, **_kw):
            replacements = {
                "/repo": str(repo),
                "/work": str(workspace),
            }
            return [
                next((item.replace(old, new) for old, new in replacements.items()
                      if old in item), item)
                for item in payload
            ]

        def research_subprocess_env(self, home=None):
            return {"PATH": os.environ["PATH"]}

    home = tmp_path / "home"
    home.mkdir()
    runner = ResearchCommandRunner(
        runtime=HostRuntime(), workspace=workspace, repo=repo,
        history_dir=history, scratch=scratch,
        world_mount=MountMap(), home=home,
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
        "proposer.research_tools.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        "proposer.research_tools.os.killpg",
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
        "proposer.research_tools.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(
        "proposer.research_tools.os.killpg", lambda *_args: None,
    )

    result = runner.run("true", cwd="work")

    assert result["truncated"] is True
    assert result["output"] == "x" * 12
    assert process.stdout.remaining == 0


@pytest.mark.parametrize(
    ("command", "cwd"), [("", "work"), ("true", "history")],
)
def test_research_command_rejects_invalid_input(tmp_path, command, cwd):
    runner, _runtime = _runner(tmp_path)
    with pytest.raises(ValueError):
        runner.run(command, cwd=cwd)


# --- ResearchTools memory-tool dispatch ----------------------------------

def _tools(tmp_path, *, memory=None, current_round=0):
    runner, runtime = _runner(tmp_path)
    return ResearchTools(
        runtime=runtime, workspace=runner.workspace, repo=runner.repo,
        history_dir=runner.history_dir, scratch=runner.scratch,
        world_mount=runner.world_mount,
        home=runner.home,
        memory_service=memory or _FakeMemoryService(),
        command_timeout_seconds=12,
        command_output_cap_chars=100,
        current_round=current_round,
    )


def test_research_tools_inspect_episode_goes_through_memory(tmp_path):
    memory = _FakeMemoryService()
    tools = _tools(tmp_path, memory=memory)

    result = tools.execute(
        {"action": "inspect_episode", "ref": "r2c0"}, deadline=1000,
    )

    assert result["ok"]
    assert result["result"]["ref"] == "r2c0"
    assert memory.calls == [("inspect_episode", "r2c0")]


def test_research_tools_list_findings_passes_current_round(tmp_path):
    memory = _FakeMemoryService()
    tools = _tools(tmp_path, memory=memory, current_round=7)

    result = tools.execute(
        {"action": "list_findings", "state": "active", "limit": 10},
        deadline=1000,
    )

    assert result["ok"]
    assert memory.calls == [("list_findings", "active", 10, 7)]


def test_research_tools_search_findings(tmp_path):
    memory = _FakeMemoryService()
    tools = _tools(tmp_path, memory=memory)

    result = tools.execute(
        {"action": "search_findings", "query": "cache", "limit": 3},
        deadline=1000,
    )

    assert result["ok"]
    assert memory.calls == [("search_findings", "cache", 3)]


def test_research_tools_inspect_finding_reports_missing(tmp_path):
    memory = _FakeMemoryService()
    tools = _tools(tmp_path, memory=memory)

    result = tools.execute(
        {"action": "inspect_finding", "finding_id": "MISSING"},
        deadline=1000,
    )

    assert result == {"ok": False, "error": "unknown finding: MISSING"}


def test_research_tools_search_experiments_default_buckets(tmp_path):
    memory = _FakeMemoryService()
    tools = _tools(tmp_path, memory=memory)

    result = tools.execute(
        {
            "action": "search_experiments", "query": "cache",
            "filters": {"gate_passed": True}, "limit": 5, "buckets": True,
        },
        deadline=1000,
    )

    assert result["ok"]
    assert memory.calls == [
        ("search_experiments", "cache", {"gate_passed": True}, 5, True),
    ]


def test_research_tools_command_uses_remaining_deadline(tmp_path, monkeypatch):
    tools = _tools(tmp_path)
    calls = []
    tools.command_runner.run = lambda command, **kwargs: (
        calls.append((command, kwargs)) or {"ok": True}
    )
    monkeypatch.setattr(
        "proposer.research_tools.time.monotonic", lambda: 90,
    )

    result = tools.execute({
        "action": "run_research_command", "command": "rg cache",
        "cwd": "work",
    }, deadline=100)

    assert result["ok"] is True
    assert calls == [(
        "rg cache",
        {"cwd": "work", "workdir": None, "timeout_seconds": 10},
    )]


# --- file-tool dispatch ---------------------------------------------------

def test_research_tools_read_file_dispatch(tmp_path):
    tools = _tools(tmp_path)
    (tools.files.boundary.roots["/work"] / "a.py").write_text(
        "alpha\nbeta\n", encoding="utf-8",
    )

    result = tools.execute(
        {"action": "read_file", "path": "/work/a.py"}, deadline=1000,
    )

    assert result["ok"] is True
    assert "     1\talpha" in result["content"]


def test_research_tools_grep_and_glob_dispatch(tmp_path):
    tools = _tools(tmp_path)
    work = tools.files.boundary.roots["/work"]
    (work / "a.cc").write_text("needle here\n", encoding="utf-8")

    grep = tools.execute(
        {"action": "grep_files", "pattern": "needle", "path": "/work"},
        deadline=1000,
    )
    glob = tools.execute(
        {"action": "glob_files", "pattern": "*.cc", "path": "/work"},
        deadline=1000,
    )

    assert "/work/a.cc:1:needle here" in grep["content"]
    assert glob["matches"] == ["/work/a.cc"]


def test_research_tools_write_scratch_file_dispatch(tmp_path):
    tools = _tools(tmp_path)

    result = tools.execute(
        {"action": "write_scratch_file",
         "path": "/scratch/toy.py", "content": "print(1)\n"},
        deadline=1000,
    )

    assert result["ok"] is True
    host = tools.files.boundary.roots["/scratch"] / "toy.py"
    assert host.read_text(encoding="utf-8") == "print(1)\n"


def test_research_tools_file_tool_boundary_failure_is_observation(tmp_path):
    tools = _tools(tmp_path)

    result = tools.execute(
        {"action": "read_file", "path": "/work/../repo/x"}, deadline=1000,
    )

    assert result["ok"] is False
    assert "escapes" in result["error"]


def test_research_tools_unknown_action_is_observation_not_raise(tmp_path):
    tools = _tools(tmp_path)

    result = tools.execute({"action": "nonsense"}, deadline=1000)

    assert result == {
        "ok": False, "error": "unsupported research action: nonsense",
    }


# --- cwd memory ------------------------------------------------------------

def test_runner_workdir_is_used_and_remembered(tmp_path, monkeypatch):
    runner, runtime = _runner(tmp_path)
    (runner.workspace / "build").mkdir()
    monkeypatch.setattr(
        "proposer.research_tools.subprocess.Popen",
        lambda *_args, **_kwargs: _Process(),
    )
    monkeypatch.setattr(
        "proposer.research_tools.os.killpg", lambda *_args: None,
    )

    runner.run("make", workdir="/work/build")
    assert runtime.argv_call[1]["work_cwd"] == "/work/build"

    runner.run("./bench")  # neither cwd nor workdir → memory
    assert runtime.argv_call[1]["work_cwd"] == "/work/build"


def test_runner_cwd_spelling_maps_and_remembers(tmp_path, monkeypatch):
    runner, runtime = _runner(tmp_path)
    monkeypatch.setattr(
        "proposer.research_tools.subprocess.Popen",
        lambda *_args, **_kwargs: _Process(),
    )
    monkeypatch.setattr(
        "proposer.research_tools.os.killpg", lambda *_args: None,
    )

    runner.run("ls", cwd="scratch")
    assert runtime.argv_call[1]["work_cwd"] == "/scratch"

    runner.run("ls")
    assert runtime.argv_call[1]["work_cwd"] == "/scratch"

    runner.run("ls", cwd="work", workdir="/work")  # workdir wins
    assert runtime.argv_call[1]["work_cwd"] == "/work"


def test_runner_rejects_invalid_workdir(tmp_path):
    runner, _runtime = _runner(tmp_path)
    for bad in ("/repo/x", "/etc", "/work/missing", "/work/../outside"):
        with pytest.raises(ValueError):
            runner.run("true", workdir=bad)
