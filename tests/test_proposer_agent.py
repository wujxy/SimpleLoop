from __future__ import annotations

import json
import traceback
from pathlib import Path

import pytest

from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles.model import ModelError, ModelReply
from simpleloop.roles.proposer import (
    ProposerAgent,
    ProposerError,
    ResearchPhase,
    _parse_action,
)
from simpleloop.roles.research_tools import Insight


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, **kwargs):
        # Snapshot messages so tests can inspect per-call state even though
        # the runtime mutates the same list in place.
        kwargs = {**kwargs, "messages": list(kwargs["messages"])}
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


def _frame() -> dict:
    return {
        "action": "frame_research",
        "observations": ["Recent cache attempts did not help."],
        "research_questions": ["Is lookup the real cost source?"],
    }


def _conclude() -> dict:
    return {
        "action": "conclude_research",
        "findings": ["Lookup hoist helped; cache widening did not."],
        "remaining_uncertainty": ["Magnitude uncertain."],
        "decision_basis": "Test the remaining lookup.",
    }


def _submit(proposals=("Replace the cache layout.",), memory=None) -> dict:
    return {
        "action": "submit_proposals",
        "proposals": list(proposals),
        "memory_update": memory or {
            "mode": "no_change", "reason": "nothing durable",
        },
    }


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


def test_state_machine_happy_path_submits_from_checkpoint(
    tmp_path, monkeypatch,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    usage = []
    model = FakeModel([
        _reply({"action": "search_history", "query": "cache"}, {"t": 1}),
        _reply(_frame(), {"t": 2}),
        _reply(_conclude(), {"t": 3}),
        _reply(_submit(memory={
            "mode": "save", "text": "Lookup is the cost source.",
            "refs": ["r0c0"],
        }), {"t": 4}),
    ])

    result = _agent(model, observer=usage.append).run(**_run_args(tmp_path))

    assert result.proposals == ["Replace the cache layout."]
    assert result.insight == Insight("Lookup is the cost source.", ("r0c0",))
    assert result.usage == [{"t": 1}, {"t": 2}, {"t": 3}, {"t": 4}]
    assert usage == result.usage
    assert [item[0]["action"] for item in FakeTools.instances[0].actions] == [
        "search_history",
    ]


def test_memory_update_no_change_yields_no_insight(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit(memory={
            "mode": "no_change", "reason": "nothing durable this round",
        })),
    ])

    result = _agent(model).run(**_run_args(tmp_path))

    assert result.insight is None


def test_memory_update_save_yields_insight(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit(memory={
            "mode": "save", "text": "X", "refs": ["r0c0"],
        })),
    ])

    result = _agent(model).run(**_run_args(tmp_path))

    assert result.insight == Insight("X", ("r0c0",))


def test_phase_illegal_submit_is_repaired(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    # Model tries submit from Observe, then follows the legal path.
    model = FakeModel([
        _reply(_submit()),
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit()),
    ])

    result = _agent(model, max_steps=10).run(**_run_args(tmp_path))

    assert result.proposals == ["Replace the cache layout."]
    output = capsys.readouterr().out
    assert "reason=phase_illegal" in output
    # The phase-repair message must name the current phase and legal actions.
    repair_msg = model.calls[1]["messages"][-1]["content"]
    assert "phase is observe" in repair_msg
    assert "frame_research" in repair_msg
    assert "submit_proposals is only legal from the checkpoint" in repair_msg


def test_continue_investigation_returns_to_investigate(
    tmp_path, monkeypatch,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply(_conclude()),
        _reply({
            "action": "continue_investigation",
            "gap": "not sure if old candidate tested this",
            "next_question": "which episodes touched this loop?",
        }),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply(_conclude()),
        _reply(_submit()),
    ])

    result = _agent(model, max_steps=10).run(**_run_args(tmp_path))

    assert result.proposals == ["Replace the cache layout."]
    assert [item[0]["action"] for item in FakeTools.instances[0].actions] == [
        "inspect_episode",
    ]


def test_reframe_research_returns_to_observe(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply(_conclude()),
        _reply({
            "action": "reframe_research",
            "reason": "QPDF is no longer the hotspot",
            "observation_scope": "re-compare current hotspots",
        }),
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit()),
    ])

    result = _agent(model, max_steps=10).run(**_run_args(tmp_path))

    assert result.proposals == ["Replace the cache layout."]


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
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit()),
    ])

    _agent(model, max_steps=10).run(**_run_args(tmp_path))

    output = capsys.readouterr().out
    assert "[proposer] started max_steps=10" in output
    assert "[proposer step 1/10] thinking" in output
    assert "phase=observe action=run_research_command cwd=source" in output
    assert f"command_chars={len(long_command)}" in output
    assert "result=ok exit_code=0" in output
    assert "output_chars=16 truncated=false" in output
    assert "phase=observe action=search_history query_chars=16" in output
    assert "matches=0" in output
    assert "phase=observe action=inspect_episode ref_chars=16" in output
    assert "phase=observe->investigate action=frame_research" in output
    assert "phase=investigate->checkpoint action=conclude_research" in output
    assert "phase=checkpoint->exit action=submit_proposals count=1" in output
    assert "[proposer] finished steps=6 elapsed=" in output
    assert "TOOL_RESULT_BODY" not in output
    assert "PRIVATE_COMMAND_BODY" not in output
    assert "PRIVATE_REF_BODY" not in output
    assert "HIDDEN_TAIL" not in output


def test_agent_injects_recent_facts_all_insights_and_immutable_protocol(
    tmp_path,
):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "proposer.md").write_text(
        "ACTIVE SCIENTIST", encoding="utf-8",
    )
    model = FakeModel([
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit()),
    ])
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

    _agent(model, max_steps=10).run(**args)

    call = model.calls[0]
    assert call["system"].startswith("ACTIVE SCIENTIST")
    assert "run_research_command" in call["system"]
    assert "frame_research" in call["system"]
    assert "conclude_research" in call["system"]
    assert "continue_investigation" in call["system"]
    assert "reframe_research" in call["system"]
    assert "Inspect the accepted source" in call["system"]
    assert "cannot call the Executor" in call["system"]
    context = call["messages"][0]["content"]
    assert '"round": 1' in context
    facts = context.split("Recent factual outcomes:", 1)[1].split(
        "Proposer Insights:", 1,
    )[0]
    assert '"round": 0' not in facts
    assert '"text": "old"' in context


def test_budget_reminder_injected_at_80pct(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    # max_steps=5 -> reminder at step 4. Loop never submits -> budget error,
    # but we only assert the reminder was injected before the error.
    model = FakeModel([
        _reply({"action": "search_history", "query": "cache"}),
        _reply({"action": "search_history", "query": "cache"}),
        _reply({"action": "search_history", "query": "cache"}),
        _reply({"action": "search_history", "query": "cache"}),
        _reply({"action": "search_history", "query": "cache"}),
    ])

    with pytest.raises(ProposerError, match="max_steps"):
        _agent(model, max_steps=5).run(**_run_args(tmp_path))

    # The 4th model call (step 4) should have the reminder in its messages.
    messages_at_step4 = model.calls[3]["messages"]
    reminder_present = any(
        item.get("content") == proposer_mod._BUDGET_REMINDER
        for item in messages_at_step4
    )
    assert reminder_present
    # And not present at step 3.
    messages_at_step3 = model.calls[2]["messages"]
    reminder_early = any(
        item.get("content") == proposer_mod._BUDGET_REMINDER
        for item in messages_at_step3
    )
    assert not reminder_early


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
        _reply(_frame(), {"t": 3}),
        _reply(_conclude(), {"t": 4}),
        _reply(_submit(), {"t": 5}),
    ])
    observed_usage = []

    result = _agent(
        model, max_steps=10, observer=observed_usage.append,
    ).run(**_run_args(tmp_path))

    assert result.proposals == ["Replace the cache layout."]
    assert result.usage == [{"t": 1}, {"t": 2}, {"t": 3}, {"t": 4}, {"t": 5}]
    assert observed_usage == result.usage
    assert model.calls[1]["timeout_seconds"] <= model.calls[0][
        "timeout_seconds"
    ]
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


def test_failed_protocol_repair_traceback_hides_rejected_content(
    tmp_path, monkeypatch,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    marker = "PRIVATE_ACTION_MARKER"
    model = FakeModel([
        _reply({"action": marker}) for _ in range(3)
    ])

    with pytest.raises(ProposerError) as exc_info:
        _agent(model).run(**_run_args(tmp_path))

    rendered = "".join(traceback.format_exception(exc_info.value))
    assert marker not in rendered


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
    # control actions missing required fields
    '{"action":"frame_research","observations":["a"]}',
    '{"action":"conclude_research","findings":["a"]}',
    '{"action":"continue_investigation","gap":"g"}',
    '{"action":"reframe_research","reason":"r"}',
    # submit without memory_update
    '{"action":"submit_proposals","proposals":["x"]}',
    # memory_update unknown mode
    '{"action":"submit_proposals","proposals":["x"],'
    '"memory_update":{"mode":"bogus"}}',
    # memory_update save missing refs
    '{"action":"submit_proposals","proposals":["x"],'
    '"memory_update":{"mode":"save","text":"t"}}',
    # memory_update no_change missing reason
    '{"action":"submit_proposals","proposals":["x"],'
    '"memory_update":{"mode":"no_change"}}',
])
def test_action_parser_rejects_malformed_contract(text):
    with pytest.raises(ProposerError):
        _parse_action(text, candidates_per_round=1)


def test_action_parser_normalizes_optional_cwd_and_proposals():
    command = _parse_action(
        '{"action":"run_research_command","command":"rg cache"}', 1,
    )
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":["  Try A  "],'
        '"memory_update":{"mode":"no_change","reason":"x"}}',
        1,
    )

    assert command["cwd"] == "source"
    assert submit["proposals"] == ["Try A"]


def test_phase_transition_table():
    from simpleloop.roles.proposer import _validate_phase_transition as v

    # Research tools never change phase.
    assert v(ResearchPhase.OBSERVE, "run_research_command") == ResearchPhase.OBSERVE
    assert v(ResearchPhase.INVESTIGATE, "search_history") == ResearchPhase.INVESTIGATE
    # Control transitions.
    assert v(ResearchPhase.OBSERVE, "frame_research") == ResearchPhase.INVESTIGATE
    assert v(ResearchPhase.INVESTIGATE, "conclude_research") == ResearchPhase.CHECKPOINT
    assert v(ResearchPhase.CHECKPOINT, "continue_investigation") == ResearchPhase.INVESTIGATE
    assert v(ResearchPhase.CHECKPOINT, "reframe_research") == ResearchPhase.OBSERVE
    assert v(ResearchPhase.CHECKPOINT, "submit_proposals") is None
    # Illegal.
    for phase, action in [
        (ResearchPhase.OBSERVE, "submit_proposals"),
        (ResearchPhase.OBSERVE, "conclude_research"),
        (ResearchPhase.INVESTIGATE, "submit_proposals"),
        (ResearchPhase.INVESTIGATE, "frame_research"),
        (ResearchPhase.CHECKPOINT, "frame_research"),
    ]:
        with pytest.raises(ProposerError, match="not legal in phase"):
            v(phase, action)