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


def _submit(proposals=("Replace the cache layout.",), annotations=None) -> dict:
    return {
        "action": "submit_proposals",
        "proposals": list(proposals),
        "annotations": annotations if annotations is not None else [],
    }


def _run_args(tmp_path: Path, *, history=None) -> dict:
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    run_dir = tmp_path / "run"
    for path in (source, repo, run_dir):
        path.mkdir(exist_ok=True)
    return {
        "goal": "make reconstruction faster",
        "editable": ["src/**"],
        "frozen": ["tests/**"],
        "history": history if history is not None else [],
        "base_sha": "abc123",
        "source_path": source,
        "repo_path": repo,
        "run_dir": run_dir,
        "candidates_per_round": 1,
        "gate_block": "- physics: must pass",
        "prompt_dir": None,
    }


def _prior_round(candidates=1, note="") -> list[dict]:
    return [{
        "round": 0,
        "parent_sha": "parent-0",
        "candidates": [
            {"candidate": cid, "note": note}
            for cid in range(candidates)
        ],
    }]


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
        _reply({"action": "inspect_episode", "ref": "r0c0"}, {"t": 1}),
        _reply(_frame(), {"t": 2}),
        _reply(_conclude(), {"t": 3}),
        _reply(_submit(annotations=[
            {"ref": "r0c0", "text": "Lookup is the cost source."},
        ]), {"t": 4}),
    ])

    result = _agent(model, observer=usage.append).run(
        **_run_args(tmp_path, history=_prior_round()))

    assert result.proposals == ["Replace the cache layout."]
    assert result.annotations == [
        {"ref": "r0c0", "text": "Lookup is the cost source."},
    ]
    assert result.usage == [{"t": 1}, {"t": 2}, {"t": 3}, {"t": 4}]
    assert usage == result.usage
    assert [item[0]["action"] for item in FakeTools.instances[0].actions] == [
        "inspect_episode",
    ]


def test_submit_yields_per_candidate_annotations(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit(annotations=[
            {"ref": "r0c0", "text": "first candidate note"},
        ])),
    ])

    result = _agent(model).run(**_run_args(tmp_path, history=_prior_round()))

    assert result.annotations == [
        {"ref": "r0c0", "text": "first candidate note"},
    ]


def test_first_round_annotations_must_be_empty(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply(_conclude()),
        # No prior round -> annotations must be [].
        _reply(_submit(annotations=[])),
    ])

    result = _agent(model).run(**_run_args(tmp_path))

    assert result.annotations == []


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
    # The repair message names a legal action the model can take instead —
    # don't pin the exact prose of the explanation.
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
        _reply({"action": "inspect_episode", "ref": "PRIVATE_REF_BODY"}),
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit()),
    ])

    _agent(model, max_steps=10).run(**_run_args(tmp_path))

    output = capsys.readouterr().out
    # The proposer prints a safe, redacted trace: private command bodies and
    # tool outputs never reach stdout. Keep the load-bearing no-leak contract
    # and confirm action summaries appear — but don't pin the exact log format.
    assert "[proposer] started" in output
    assert "[proposer] finished" in output
    assert "run_research_command" in output
    assert "frame_research" in output
    assert "submit_proposals" in output
    assert "TOOL_RESULT_BODY" not in output
    assert "PRIVATE_COMMAND_BODY" not in output
    assert "PRIVATE_REF_BODY" not in output
    assert "HIDDEN_TAIL" not in output


def test_agent_injects_directory_and_protocol(tmp_path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "proposer.md").write_text(
        "ACTIVE SCIENTIST", encoding="utf-8",
    )
    model = FakeModel([
        _reply(_frame()),
        _reply(_conclude()),
        _reply(_submit(annotations=[
            {"ref": "r1c0", "text": "annotated recent result"},
        ])),
    ])
    args = _run_args(
        tmp_path,
        history=[
            {"round": 0, "candidates": [
                {"candidate": 0, "note": "old result"}]},
            {"round": 1, "candidates": [
                {"candidate": 0, "note": "recent result"}]},
        ],
    )
    args.update({"prompt_dir": prompt_dir})

    _agent(model, max_steps=10).run(**args)

    call = model.calls[0]
    assert call["system"].startswith("ACTIVE SCIENTIST")  # active prompt wins
    # The protocol's action vocabulary is documented in the system prompt —
    # these action names are the contract, not prose to pin.
    for action in ("run_research_command", "frame_research", "conclude_research",
                   "continue_investigation", "reframe_research"):
        assert action in call["system"]
    context = call["messages"][0]["content"]
    # The history directory is rendered into context: a fixture note appears.
    # Do not pin the directory header wording — that is prompt semantics.
    assert "recent result" in context


def test_budget_reminder_injected_at_80pct(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    # max_steps=5 -> reminder at step 4. Loop never submits -> budget error,
    # but we only assert the reminder was injected before the error.
    model = FakeModel([
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
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

    assert result.proposals == ["Replace the cache layout."]
    assert result.usage == [{"t": 1}, {"t": 2}, {"t": 3}, {"t": 4}, {"t": 5}]
    assert observed_usage == result.usage
    assert model.calls[1]["timeout_seconds"] <= model.calls[0][
        "timeout_seconds"
    ]
    assert [item[0]["action"] for item in FakeTools.instances[0].actions] == [
        "inspect_episode",
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
    # control actions missing required fields
    '{"action":"frame_research","observations":["a"]}',
    '{"action":"conclude_research","findings":["a"]}',
    '{"action":"continue_investigation","gap":"g"}',
    '{"action":"reframe_research","reason":"r"}',
    # submit without annotations key
    '{"action":"submit_proposals","proposals":["x"]}',
    # non-empty annotations when there are no prior candidates (round 0)
    '{"action":"submit_proposals","proposals":["x"],'
    '"annotations":[{"ref":"r0c0","text":"t"}]}',
    # extra field on an annotation
    '{"action":"submit_proposals","proposals":["x"],'
    '"annotations":[{"ref":"r0c0","text":"t","extra":1}]}',
])
def test_action_parser_rejects_malformed_contract(text):
    # These all fail with an empty prior round (no prior candidates).
    with pytest.raises(ProposerError):
        _parse_action(text, candidates_per_round=1, prior_refs=set())


@pytest.mark.parametrize("text", [
    # annotations empty string text (count matches the 2 prior candidates)
    '{"action":"submit_proposals","proposals":["x"],'
    '"annotations":[{"ref":"r0c0","text":""},{"ref":"r0c1","text":"u"}]}',
    # annotations over-long text
    '{"action":"submit_proposals","proposals":["x"],'
    '"annotations":[{"ref":"r0c0","text":"' + "x" * 201 + '"},'
    '{"ref":"r0c1","text":"u"}]}',
    # annotations unknown ref
    '{"action":"submit_proposals","proposals":["x"],'
    '"annotations":[{"ref":"r9c9","text":"t"},{"ref":"r0c1","text":"u"}]}',
    # annotations duplicate ref
    '{"action":"submit_proposals","proposals":["x"],'
    '"annotations":[{"ref":"r0c0","text":"a"},{"ref":"r0c0","text":"b"}]}',
    # annotations wrong count (prior round has 2 candidates, only 1 given)
    '{"action":"submit_proposals","proposals":["x"],'
    '"annotations":[{"ref":"r0c0","text":"t"}]}',
])
def test_action_parser_rejects_bad_annotations_with_prior_round(text):
    with pytest.raises(ProposerError):
        _parse_action(
            text, candidates_per_round=1, prior_refs={"r0c0", "r0c1"},
        )


def test_action_parser_normalizes_optional_cwd_and_proposals():
    command = _parse_action(
        '{"action":"run_research_command","command":"rg cache"}', 1, set(),
    )
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":["  Try A  "],'
        '"annotations":[]}', 1, set(),
    )

    assert command["cwd"] == "source"
    assert submit["proposals"] == ["Try A"]


def test_action_parser_accepts_full_annotations():
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":["A","B"],'
        '"annotations":['
        '{"ref":"r0c1","text":"second"},'
        '{"ref":"r0c0","text":"first"}'
        ']}',
        2, {"r0c0", "r0c1"},
    )
    assert submit["proposals"] == ["A", "B"]
    assert {a["ref"] for a in submit["annotations"]} == {"r0c0", "r0c1"}


def test_phase_transition_table():
    from simpleloop.roles.proposer import _validate_phase_transition as v

    # Research tools never change phase.
    assert v(ResearchPhase.OBSERVE, "run_research_command") == ResearchPhase.OBSERVE
    assert v(ResearchPhase.INVESTIGATE, "inspect_episode") == ResearchPhase.INVESTIGATE
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