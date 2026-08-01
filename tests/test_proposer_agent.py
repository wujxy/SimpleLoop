from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles.model import ModelError, ModelReply
from simpleloop.roles.proposer import ProposerAgent, ProposerError, _parse_action
from simpleloop.roles.research_tools import Insight


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeTools:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.actions = []
        self.pending_insight = None
        self.__class__.instances.append(self)

    def execute(self, action, *, deadline):
        self.actions.append((action, deadline))
        if action["action"] == "write_insight":
            self.pending_insight = Insight.from_dict({
                "text": action["text"], "refs": action["refs"],
            })
            return {"ok": True, "result": "insight pending"}
        if action["action"] == "run_research_command":
            return {
                "ok": True,
                "returncode": 0,
                "timed_out": False,
                "truncated": False,
                "output": "TOOL_RESULT_BODY",
            }
        if action["action"] == "search_history":
            return {"ok": True, "result": []}
        if action["action"] == "inspect_episode":
            return {"ok": True, "result": {"ref": action["ref"]}}
        raise AssertionError(f"unexpected fake action: {action['action']}")


def _reply(action: dict, usage=None) -> ModelReply:
    return ModelReply(json.dumps(action), usage=usage)


def _run_args(tmp_path: Path) -> dict:
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    run_dir = tmp_path / "run"
    for path in (source, repo, run_dir):
        path.mkdir(exist_ok=True)
    return {
        "goal": "make reconstruction faster",
        "editable": ["src/**"],
        "frozen": ["tests/**"],
        "history": [],
        "insights": [],
        "base_sha": "abc123",
        "source_path": source,
        "repo_path": repo,
        "run_dir": run_dir,
        "candidates_per_round": 1,
        "recent_rounds": 2,
        "gate_block": "- physics: must pass",
        "prompt_dir": None,
    }


def _agent(model, *, max_steps=5, observer=None):
    return ProposerAgent(
        model=model,
        runtime=object(),
        timeout_seconds=30,
        max_steps=max_steps,
        command_timeout_seconds=5,
        command_output_cap_chars=1000,
        usage_observer=observer,
    )


def test_agent_investigates_in_any_order_then_submits(
    tmp_path, monkeypatch,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    usage = []
    model = FakeModel([
        _reply({"action": "search_history", "query": "cache"}, {"t": 1}),
        _reply({
            "action": "write_insight",
            "text": "Cache misses dominate.",
            "refs": ["r0c0"],
        }, {"t": 2}),
        _reply({
            "action": "submit_proposals",
            "proposals": ["Replace the cache layout."],
        }, {"t": 3}),
    ])

    result = _agent(model, observer=usage.append).run(**_run_args(tmp_path))

    assert result.proposals == ["Replace the cache layout."]
    assert result.insight == Insight("Cache misses dominate.", ("r0c0",))
    assert result.usage == [{"t": 1}, {"t": 2}, {"t": 3}]
    assert usage == result.usage
    assert [item[0]["action"] for item in FakeTools.instances[0].actions] == [
        "search_history", "write_insight",
    ]
    assert json.loads(model.calls[1]["messages"][-1]["content"])[
        "tool_result"
    ]["ok"]


def test_agent_prints_safe_action_summaries(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    long_command = (
        "printf PRIVATE_COMMAND_BODY\n" + ("x" * 180) + "HIDDEN_TAIL"
    )
    model = FakeModel([
        _reply({
            "action": "run_research_command",
            "command": long_command,
            "cwd": "source",
        }),
        _reply({"action": "search_history", "query": "TOOL_RESULT_BODY"}),
        _reply({"action": "inspect_episode", "ref": "PRIVATE_REF_BODY"}),
        _reply({
            "action": "write_insight",
            "text": "PRIVATE INSIGHT BODY",
            "refs": ["r0c0"],
        }),
        _reply({
            "action": "submit_proposals",
            "proposals": ["Replace the cache layout."],
        }),
    ])

    _agent(model, max_steps=5).run(**_run_args(tmp_path))

    output = capsys.readouterr().out
    assert "[proposer] started max_steps=5" in output
    assert "[proposer step 1/5] thinking" in output
    assert "action=run_research_command cwd=source" in output
    assert f"command_chars={len(long_command)}" in output
    assert "result=ok exit_code=0" in output
    assert "output_chars=16 truncated=false" in output
    assert "action=search_history query_chars=16" in output
    assert "matches=0" in output
    assert "action=inspect_episode ref_chars=16" in output
    assert "action=write_insight refs=1" in output
    assert "action=submit_proposals count=1" in output
    assert "[proposer] finished steps=5 elapsed=" in output
    assert "TOOL_RESULT_BODY" not in output
    assert "PRIVATE_COMMAND_BODY" not in output
    assert "PRIVATE_REF_BODY" not in output
    assert "PRIVATE INSIGHT BODY" not in output
    assert "HIDDEN_TAIL" not in output


def test_later_insight_replaces_earlier_pending_insight(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply({"action": "write_insight", "text": "A", "refs": ["r0c0"]}),
        _reply({"action": "write_insight", "text": "B", "refs": ["r0c0"]}),
        _reply({"action": "submit_proposals", "proposals": ["Try A"]}),
    ])

    result = _agent(model).run(**_run_args(tmp_path))

    assert result.insight == Insight("B", ("r0c0",))


def test_agent_injects_recent_facts_all_insights_and_immutable_protocol(
    tmp_path,
):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "proposer.md").write_text(
        "ACTIVE SCIENTIST", encoding="utf-8",
    )
    model = FakeModel([_reply({
        "action": "submit_proposals", "proposals": ["Try A"],
    })])
    args = _run_args(tmp_path)
    args.update({
        "prompt_dir": prompt_dir,
        "recent_rounds": 1,
        "history": [
            {"round": 0, "candidates": []},
            {"round": 1, "candidates": []},
        ],
        "insights": [
            {"id": "I0", "round": 0, "text": "old", "refs": ["r0c0"]},
        ],
    })

    _agent(model).run(**args)

    call = model.calls[0]
    assert call["system"].startswith("ACTIVE SCIENTIST")
    assert "run_research_command" in call["system"]
    assert "cannot call the Executor" in call["system"]
    context = call["messages"][0]["content"]
    assert '"round": 1' in context
    facts = context.split("Recent factual outcomes:", 1)[1].split(
        "Proposer Insights:", 1,
    )[0]
    assert '"round": 0' not in facts
    assert '"text": "old"' in context


def test_agent_stops_at_step_budget(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply({"action": "search_history", "query": "cache"}),
    ])

    with pytest.raises(ProposerError, match="max_steps"):
        _agent(model, max_steps=1).run(**_run_args(tmp_path))


def test_agent_repairs_protocol_without_consuming_a_step(
    tmp_path, monkeypatch, capsys,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    rejected = (
        '{"action":"search_history","query":"cache"} PRIVATE_TAIL'
    )
    model = FakeModel([
        ModelReply(rejected, usage={"t": 1}),
        _reply(
            {"action": "search_history", "query": "cache"},
            {"t": 2},
        ),
        _reply({
            "action": "submit_proposals",
            "proposals": ["Try A"],
        }, {"t": 3}),
    ])

    result = _agent(model, max_steps=2).run(**_run_args(tmp_path))

    assert result.proposals == ["Try A"]
    assert result.usage == [{"t": 1}, {"t": 2}, {"t": 3}]
    assert [item[0]["action"] for item in FakeTools.instances[0].actions] == [
        "search_history",
    ]
    output = capsys.readouterr().out
    assert "protocol repair 1/2 reason=invalid_json" in output
    assert "PRIVATE_TAIL" not in output


def test_agent_fails_closed_after_two_protocol_repairs(
    tmp_path, monkeypatch,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([ModelReply("{} trailing") for _ in range(3)])

    with pytest.raises(ProposerError, match="after 2 repairs"):
        _agent(model).run(**_run_args(tmp_path))

    assert len(model.calls) == 3
    assert FakeTools.instances[0].actions == []


def test_agent_does_not_repair_model_transport_errors(tmp_path):
    class RaisingModel:
        def __init__(self):
            self.calls = 0

        def complete(self, **_kwargs):
            self.calls += 1
            raise ModelError("transport failed")

    model = RaisingModel()

    with pytest.raises(ModelError, match="transport failed"):
        _agent(model).run(**_run_args(tmp_path))

    assert model.calls == 1


@pytest.mark.parametrize("text", [
    "not json",
    "[]",
    '{"action":"unknown"}',
    '{"action":"search_history","query":"","extra":1}',
    '{"action":"run_research_command","command":"","cwd":"source"}',
    '{"action":"run_research_command","command":"true","cwd":"host"}',
    '{"action":"write_insight","text":"x","refs":[]}',
    '{"action":"submit_proposals","proposals":[]}',
    '{"action":"submit_proposals","proposals":[" "]}',
])
def test_action_parser_rejects_malformed_contract(text):
    with pytest.raises(ProposerError):
        _parse_action(text, candidates_per_round=1)


def test_action_parser_normalizes_optional_cwd_and_proposals():
    command = _parse_action(
        '{"action":"run_research_command","command":"rg cache"}', 1,
    )
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":["  Try A  "]}', 1,
    )

    assert command["cwd"] == "source"
    assert submit["proposals"] == ["Try A"]
