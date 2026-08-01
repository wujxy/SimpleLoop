from __future__ import annotations

import json
import subprocess

import pytest

from simpleloop.roles.research_tools import (
    Insight,
    InsightStore,
    ResearchCommandRunner,
    render_insights,
    search_history,
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

    def communicate(self, timeout=None):
        self.timeouts.append(timeout)
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("apptainer", timeout)
        return self.output


def _candidate(round_id: int, *, proposal: str = "cache values") -> dict:
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
    }


def _history(count: int = 3) -> list[dict]:
    return [
        {"round": round_id, "parent_sha": f"parent-{round_id}",
         "candidates": [_candidate(round_id)]}
        for round_id in range(count)
    ]


@pytest.mark.parametrize(
    "query",
    ["r2c0", "cache", "COMPLETED", "src/r2.cc", "SPEED_MS", "physics_gate"],
)
def test_search_history_matches_factual_index_fields(query: str):
    matches = search_history(_history(), query)

    assert matches[0]["ref"] == "r2c0"
    assert "eval_block" not in matches[0]


def test_search_history_is_newest_first_and_capped():
    matches = search_history(_history(25), "cache", limit=20)

    assert len(matches) == 20
    assert matches[0]["ref"] == "r24c0"
    assert matches[-1]["ref"] == "r5c0"


def test_search_history_requires_nonblank_query():
    with pytest.raises(ValueError, match="non-empty"):
        search_history(_history(), "   ")


def test_insight_store_is_append_only_and_idempotent(tmp_path):
    store = InsightStore(tmp_path / "insights.jsonl")
    insight = Insight(text="Cache misses dominate.", refs=("r0c0",))

    assert store.append(round_id=0, insight=insight) is True
    assert store.append(round_id=0, insight=insight) is False
    assert store.load() == [{
        "id": "I0",
        "round": 0,
        "text": "Cache misses dominate.",
        "refs": ["r0c0"],
    }]


def test_insight_store_rejects_conflict(tmp_path):
    store = InsightStore(tmp_path / "insights.jsonl")
    store.append(0, Insight("A", ("r0c0",)))

    with pytest.raises(ValueError, match="conflicting"):
        store.append(0, Insight("B", ("r0c0",)))


@pytest.mark.parametrize(
    "value",
    [
        {"text": "", "refs": ["r0c0"]},
        {"text": "x" * 501, "refs": ["r0c0"]},
        {"text": "fact", "refs": []},
        {"text": "fact", "refs": [1]},
        {"text": "fact", "refs": ["r0c0"], "extra": True},
    ],
)
def test_insight_rejects_invalid_shape(value):
    with pytest.raises(ValueError):
        Insight.from_dict(value)


def test_insight_store_rejects_noncurrent_file_schema(tmp_path):
    path = tmp_path / "insights.jsonl"
    path.write_text(json.dumps({
        "id": "I0", "round": 0, "summary": "old schema",
    }) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="current insight schema"):
        InsightStore(path).load()


def test_render_insights_keeps_all_records_compact():
    records = [
        {"id": "I0", "round": 0, "text": "A", "refs": ["r0c0"]},
        {"id": "I1", "round": 1, "text": "B", "refs": ["r1c0"]},
    ]

    assert json.loads(render_insights(records)) == records


def _runner(tmp_path, *, cap=100, timeout=12):
    paths = {
        name: tmp_path / name
        for name in ("source", "repo", "history", "scratch")
    }
    for path in paths.values():
        path.mkdir()
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

    result = runner.run("git show HEAD", cwd="source")

    payload, paths = runtime.argv_call
    assert payload == [
        "env", "GIT_DIR=/repo/.git", "GIT_WORK_TREE=/source",
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
    process = _Process(output=("abcdefghij", "klmnop"))
    monkeypatch.setattr(
        "simpleloop.roles.research_tools.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )

    result = runner.run("true", cwd="source")

    assert result["truncated"] is True
    assert result["output"] == "abcdefghij\n["


@pytest.mark.parametrize(
    ("command", "cwd"), [("", "source"), ("true", "history")],
)
def test_research_command_rejects_invalid_input(tmp_path, command, cwd):
    runner, _runtime = _runner(tmp_path)
    with pytest.raises(ValueError):
        runner.run(command, cwd=cwd)
