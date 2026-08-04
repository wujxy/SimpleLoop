from __future__ import annotations

import json
import traceback
from pathlib import Path

import pytest

from simpleloop.memory import MemoryService
from simpleloop.memory.models import (
    ExistingFindingTarget,
    NewFindingTarget,
)
from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles.model import ModelError, ModelReply
from simpleloop.roles.proposer import (
    ProposerAgent,
    ProposerError,
    ResearchPhase,
    _parse_action,
)


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
        self.__class__.instances.append(self)

    def execute(self, action, *, deadline):
        self.actions.append((action, deadline))
        if action["action"] == "run_research_command":
            return {
                "ok": True,
                "returncode": 0,
                "timed_out": False,
                "truncated": False,
                "output": "TOOL_RESULT_BODY",
            }
        if action["action"] == "inspect_episode":
            return {"ok": True, "result": {"ref": action["ref"]}}
        if action["action"] == "list_findings":
            return {"ok": True, "result": []}
        if action["action"] == "search_findings":
            return {"ok": True, "result": []}
        if action["action"] == "inspect_finding":
            return {"ok": True, "result": {"id": action["finding_id"]}}
        if action["action"] == "search_experiments":
            return {"ok": True, "result": {
                "relevant": [], "contrasting": [], "diverse": [],
            }}
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


def _new_target(question="Replace the cache layout."):
    return {"mode": "new", "question": question}


def _submit(proposals=None) -> dict:
    if proposals is None:
        proposals = [{
            "instruction": "Replace the cache layout.",
            "research_target": _new_target(),
        }]
    return {"action": "submit_proposals", "proposals": proposals}


_METRICS_SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [],
}


def _memory_service(run_dir: Path) -> MemoryService:
    return MemoryService(run_dir=run_dir, metrics_schema=_METRICS_SCHEMA)


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
        "memory_service": _memory_service(run_dir),
        "base_sha": "abc123",
        "source_path": source,
        "repo_path": repo,
        "run_dir": run_dir,
        "current_round": 0,
        "candidates_per_round": 1,
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


def test_state_machine_happy_path_submits_structured_proposals(
    tmp_path, monkeypatch,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    usage = []
    model = FakeModel([
        _reply({"action": "search_experiments", "query": "cache"}, {"t": 1}),
        _reply(_frame(), {"t": 2}),
        _reply(_conclude(), {"t": 3}),
        _reply(_submit(), {"t": 4}),
    ])

    result = _agent(model, observer=usage.append).run(**_run_args(tmp_path))

    assert len(result.proposals) == 1
    assert result.proposals[0].instruction == "Replace the cache layout."
    assert isinstance(result.proposals[0].research_target, NewFindingTarget)
    assert result.usage == [{"t": 1}, {"t": 2}, {"t": 3}, {"t": 4}]
    assert usage == result.usage
    assert [item[0]["action"] for item in FakeTools.instances[0].actions] == [
        "search_experiments",
    ]


def test_submit_accepts_existing_target(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit(proposals=[{
            "instruction": "Rerun the QPDF hoist experiment on the new baseline.",
            "research_target": {"mode": "existing", "finding_id": "F-008"},
        }])),
    ])

    result = _agent(model).run(**_run_args(tmp_path))

    assert isinstance(
        result.proposals[0].research_target, ExistingFindingTarget,
    )
    assert result.proposals[0].research_target.finding_id == "F-008"


def test_phase_illegal_submit_is_repaired(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_submit()),
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit()),
    ])

    result = _agent(model, max_steps=10).run(**_run_args(tmp_path))

    assert len(result.proposals) == 1
    output = capsys.readouterr().out
    assert "reason=phase_illegal" in output
    repair_msg = model.calls[1]["messages"][-1]["content"]
    assert "frame_research" in repair_msg


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

    assert len(result.proposals) == 1
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

    assert len(result.proposals) == 1


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
        _reply({"action": "inspect_episode", "ref": "PRIVATE_REF_BODY"}),
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit()),
    ])

    _agent(model, max_steps=10).run(**_run_args(tmp_path))

    output = capsys.readouterr().out
    assert "[proposer] started" in output
    assert "[proposer] finished" in output
    assert "run_research_command" in output
    assert "frame_research" in output
    assert "submit_proposals" in output
    assert "TOOL_RESULT_BODY" not in output
    assert "PRIVATE_COMMAND_BODY" not in output
    assert "PRIVATE_REF_BODY" not in output
    assert "HIDDEN_TAIL" not in output


def test_startup_pack_omits_full_history_directory(tmp_path):
    """Regression guard: the startup pack must NOT dump every prior candidate
    as `ref: note`. Continuity is supplied via Frontier + memory tools."""
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
    args["prompt_dir"] = prompt_dir

    _agent(model, max_steps=10).run(**args)

    call = model.calls[0]
    assert call["system"].startswith("ACTIVE SCIENTIST")
    for action in (
        "run_research_command", "frame_research", "conclude_research",
        "continue_investigation", "reframe_research", "list_findings",
        "search_findings", "inspect_finding", "search_experiments",
    ):
        assert action in call["system"]
    context = call["messages"][0]["content"]
    # No lab-notebook directory legacy anywhere in the startup pack.
    assert "notebook" not in context.lower()
    assert "annotations" not in context.lower()
    assert "ref: note" not in context.lower()


def test_budget_reminder_injected_at_80pct(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
    ])

    with pytest.raises(ProposerError, match="max_steps"):
        _agent(model, max_steps=5).run(**_run_args(tmp_path))

    messages_at_step4 = model.calls[3]["messages"]
    reminder_present = any(
        item.get("content") == proposer_mod._BUDGET_REMINDER
        for item in messages_at_step4
    )
    assert reminder_present
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
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
    ])

    with pytest.raises(ProposerError, match="max_steps"):
        _agent(model, max_steps=1).run(**_run_args(tmp_path))


def test_agent_repairs_protocol_without_consuming_a_step(
    tmp_path, monkeypatch, capsys,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    rejected = '{"action":"inspect_episode","ref":"r0c0"} PRIVATE_TAIL'
    model = FakeModel([
        ModelReply(rejected, usage={"t": 1}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}, {"t": 2}),
        _reply(_frame(), {"t": 3}),
        _reply(_conclude(), {"t": 4}),
        _reply(_submit(), {"t": 5}),
    ])
    observed_usage = []

    result = _agent(
        model, max_steps=10, observer=observed_usage.append,
    ).run(**_run_args(tmp_path))

    assert len(result.proposals) == 1
    assert result.usage == [{"t": 1}, {"t": 2}, {"t": 3}, {"t": 4}, {"t": 5}]
    assert observed_usage == result.usage
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


# --- action parser -------------------------------------------------------

@pytest.mark.parametrize("text", [
    "not json",
    "[]",
    '{"action":"unknown"}',
    '{"action":"search_history","query":"x"}',
    '{"action":"write_insight","text":"x","refs":["r0c0"]}',
    '{"action":"run_research_command","command":"","cwd":"source"}',
    '{"action":"run_research_command","command":"true","cwd":"host"}',
    '{"action":"submit_proposals","proposals":[]}',
    '{"action":"submit_proposals","proposals":[" "]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x"}]}',
    '{"action":"submit_proposals","proposals":[{'
    '"instruction":"x","research_target":{"mode":"existing"}}]}',
    '{"action":"submit_proposals","proposals":[{'
    '"instruction":"x","research_target":{"mode":"new"}}]}',
    '{"action":"submit_proposals","proposals":[{'
    '"instruction":"x","research_target":{"mode":"bogus"}}]}',
    # control actions missing required fields
    '{"action":"frame_research","observations":["a"]}',
    '{"action":"conclude_research","findings":["a"]}',
    '{"action":"continue_investigation","gap":"g"}',
    '{"action":"reframe_research","reason":"r"}',
    # new tool actions missing required fields
    '{"action":"search_findings"}',
    '{"action":"inspect_finding"}',
    '{"action":"search_experiments","filters":{}}',
    '{"action":"list_findings","state":"bogus"}',
])
def test_action_parser_rejects_malformed_contract(text):
    with pytest.raises(ProposerError):
        _parse_action(text, candidates_per_round=1)


def test_action_parser_normalizes_optional_cwd_and_proposals():
    command = _parse_action(
        '{"action":"run_research_command","command":"rg cache"}', 1,
    )
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":[{'
        '"instruction":"  Try A  ",'
        '"research_target":{"mode":"new","question":"is A the fix?"}}]}',
        1,
    )

    assert command["cwd"] == "source"
    assert submit["proposals"][0].instruction == "Try A"
    assert isinstance(submit["proposals"][0].research_target, NewFindingTarget)


def test_action_parser_accepts_existing_and_new_targets():
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":['
        '{"instruction":"A","research_target":{"mode":"existing",'
        '"finding_id":"F-001"}},'
        '{"instruction":"B","research_target":{"mode":"new",'
        '"question":"is B the fix?","mechanisms":["cache"],'
        '"code_regions":["src/foo"]}}'
        ']}',
        2,
    )
    assert len(submit["proposals"]) == 2
    assert isinstance(
        submit["proposals"][0].research_target, ExistingFindingTarget,
    )
    assert submit["proposals"][0].research_target.finding_id == "F-001"
    new_t = submit["proposals"][1].research_target
    assert isinstance(new_t, NewFindingTarget)
    assert new_t.question == "is B the fix?"
    assert new_t.mechanisms == ("cache",)
    assert new_t.code_regions == ("src/foo",)


def test_action_parser_accepts_new_tools():
    for text in [
        '{"action":"list_findings"}',
        '{"action":"list_findings","state":"all","limit":5}',
        '{"action":"search_findings","query":"cache"}',
        '{"action":"search_findings","query":"cache","limit":3}',
        '{"action":"inspect_finding","finding_id":"F-001"}',
        '{"action":"search_experiments","query":"cache"}',
        '{"action":"search_experiments","query":"cache","filters":{"gate_passed":true},'
        '"limit":15,"buckets":false}',
    ]:
        parsed = _parse_action(text, 1)
        assert parsed["action"] in {
            "list_findings", "search_findings", "inspect_finding",
            "search_experiments",
        }


def test_phase_transition_table():
    from simpleloop.roles.proposer import _validate_phase_transition as v

    # Research tools never change phase.
    assert v(ResearchPhase.OBSERVE, "run_research_command") == ResearchPhase.OBSERVE
    assert v(ResearchPhase.INVESTIGATE, "inspect_episode") == ResearchPhase.INVESTIGATE
    assert v(ResearchPhase.OBSERVE, "list_findings") == ResearchPhase.OBSERVE
    assert v(ResearchPhase.INVESTIGATE, "search_experiments") == ResearchPhase.INVESTIGATE
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
