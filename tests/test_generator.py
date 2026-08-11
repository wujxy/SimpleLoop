from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.roles.generator import (
    GeneratorAgent,
    GeneratorError,
    GenerationResult,
    _parse_generator_action,
    _parse_hypothesis_card,
    _replace_basis,
    _g_definition,
)
from simpleloop.roles.hypothesis import HypothesisCard
from simpleloop.roles.model import ModelReply
from simpleloop.roles import generator as gen_mod


class FakeModel:
    """Returns replies in sequence, one per complete() call."""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, *, system, messages, timeout_seconds):
        self.calls.append({"system": system, "messages": list(messages)})
        return self.replies.pop(0)


class FakeTools:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.actions = []
        self.__class__.instances.append(self)

    def execute(self, action, *, deadline):
        self.actions.append(action)
        if action["action"] == "run_research_command":
            return {"ok": True, "returncode": 0, "timed_out": False,
                    "truncated": False, "output": "SOURCE_TREE_CONTENT"}
        return {"ok": True, "result": {}}


def _card_json(op="G6", region="src/foo.cc", mech="lookup",
               interv="cache", facts=None):
    if facts is None:
        facts = ["the function foo() exists in src/foo.cc"]
    return {
        "generative_op": op, "region": region, "mechanism": mech,
        "intervention_family": interv, "why_plausible": "w",
        "critical_unknown": "u", "facts_read": facts,
    }


def _submit_hypothesis(card=None):
    card = card or _card_json()
    return json.dumps({"action": "submit_hypothesis", "hypothesis": card})


def _run_research(cmd="ls OMILRECV2/src/"):
    return json.dumps({"action": "run_research_command", "command": cmd,
                        "cwd": "workspace"})


def _emit_lever_map():
    return json.dumps({
        "action": "emit_lever_map",
        "levers": [
            {"part": "foo function", "role": "hot path",
             "structural_space": "can cache results"},
        ],
    })


def _full_3step_sequence(cmd="ls src/", card=None):
    """A complete valid flow: survey -> map -> submit."""
    if card is None:
        card = _card_json()
    return [
        ModelReply(_run_research(cmd)),
        ModelReply(_emit_lever_map()),
        ModelReply(_submit_hypothesis(card)),
    ]


def _agent(model, monkeypatch=None):
    """Build a GeneratorAgent with FakeTools."""
    FakeTools.instances.clear()
    if monkeypatch:
        monkeypatch.setattr(gen_mod, "ResearchTools", FakeTools)
    return GeneratorAgent(
        model=model, runtime=None, timeout_seconds=30,
        max_steps=20, command_timeout_seconds=10,
        command_output_cap_chars=10000,
    )


def _paths(tmp_path):
    return dict(
        source_path=tmp_path / "source",
        repo_path=tmp_path / "repo",
        run_dir=tmp_path / "run",
    )


# --- Action parsing --------------------------------------------------------

class TestParseAction:
    def test_parses_submit_hypothesis(self):
        action = _parse_generator_action(_submit_hypothesis(
            _card_json(mech="cache", interv="hoist")
        ))
        assert action["action"] == "submit_hypothesis"
        assert isinstance(action["hypothesis"], HypothesisCard)
        assert action["hypothesis"].mechanism == "cache"

    def test_parses_run_research_command(self):
        action = _parse_generator_action(_run_research("grep -rn 'FCN' src/"))
        assert action["action"] == "run_research_command"
        assert action["command"] == "grep -rn 'FCN' src/"
        assert action["cwd"] == "workspace"

    def test_parses_emit_lever_map(self):
        action = _parse_generator_action(_emit_lever_map())
        assert action["action"] == "emit_lever_map"
        assert len(action["levers"]) == 1

    def test_invalid_json_raises(self):
        with pytest.raises(GeneratorError):
            _parse_generator_action("not json")

    def test_unknown_action_raises(self):
        with pytest.raises(GeneratorError):
            _parse_generator_action(json.dumps({"action": "block"}))

    def test_submit_with_empty_card_raises(self):
        with pytest.raises(GeneratorError):
            _parse_generator_action(json.dumps({
                "action": "submit_hypothesis",
                "hypothesis": _card_json(region="", mech="", interv=""),
            }))

    def test_submit_without_facts_read_raises(self):
        card = _card_json()
        del card["facts_read"]
        with pytest.raises(GeneratorError, match="facts_read"):
            _parse_generator_action(json.dumps({
                "action": "submit_hypothesis", "hypothesis": card,
            }))

    def test_submit_with_empty_facts_read_raises(self):
        with pytest.raises(GeneratorError, match="facts_read"):
            _parse_generator_action(json.dumps({
                "action": "submit_hypothesis",
                "hypothesis": _card_json(facts=[]),
            }))

    def test_submit_with_facts_read_parsed(self):
        action = _parse_generator_action(_submit_hypothesis(_card_json(
            facts=["file X contains loop Y", "function Z calls W"],
        )))
        card = action["hypothesis"]
        assert card.facts_read == (
            "file X contains loop Y", "function Z calls W",
        )

    def test_empty_lever_map_raises(self):
        with pytest.raises(GeneratorError, match="non-empty"):
            _parse_generator_action(json.dumps({
                "action": "emit_lever_map", "levers": [],
            }))


class TestParseCard:
    def test_parses_valid_card(self):
        card = _parse_hypothesis_card(_card_json())
        assert card is not None
        assert card.generative_op == "G6"

    def test_unknown_op_falls_back_to_g6(self):
        card = _parse_hypothesis_card(_card_json(op="GX"))
        assert card.generative_op == "G6"

    def test_all_empty_returns_none(self):
        card = _parse_hypothesis_card(_card_json(region="", mech="", interv=""))
        assert card is None


# --- Basis replacement -----------------------------------------------------

class TestBasis:
    def test_replace_basis_preserves_header(self):
        from simpleloop.prompts import load_semantic
        semantic = load_semantic("generator")
        replaced = _replace_basis(semantic, ("G6", "G2"))
        assert "## The Generative Basis" in replaced
        assert "You are not required to use every G" in replaced

    def test_g_definition_extracts_one_paragraph(self):
        from simpleloop.prompts import load_semantic
        semantic = load_semantic("generator")
        g6 = _g_definition(semantic, "G6")
        assert g6.startswith("G6 —")
        assert "Algorithm/representation/paradigm sweep" in g6
        assert "Invert" not in g6


# --- Agent run() with survey -> map -> submit flow ------------------------

class TestAgentRun:
    def test_run_full_flow(self, tmp_path, monkeypatch):
        """The generator surveys, emits a lever map, then submits."""
        model = FakeModel(_full_3step_sequence())
        agent = _agent(model, monkeypatch)
        result = agent.run(context="objective: go fast", **_paths(tmp_path))
        assert isinstance(result, GenerationResult)
        assert len(result.cards) == 1
        # One tool call executed (the survey command)
        assert len(FakeTools.instances[0].actions) == 1

    def test_submit_before_map_rejected(self, tmp_path, monkeypatch):
        """Prerequisite coupling: submit_hypothesis before emit_lever_map
        is rejected. The generator gets a repair, then completes the flow."""
        model = FakeModel([
            ModelReply(_run_research()),
            # Skip map, try to submit directly -- should be rejected
            ModelReply(_submit_hypothesis()),
            # Now emit the map
            ModelReply(_emit_lever_map()),
            # And submit
            ModelReply(_submit_hypothesis()),
        ])
        agent = _agent(model, monkeypatch)
        result = agent.run(context="ctx", **_paths(tmp_path))
        assert len(result.cards) == 1

    def test_map_before_survey_rejected(self, tmp_path, monkeypatch):
        """Prerequisite coupling: emit_lever_map before any survey is
        rejected."""
        model = FakeModel([
            # Skip survey, try to emit map -- should be rejected
            ModelReply(_emit_lever_map()),
            # Now survey
            ModelReply(_run_research()),
            ModelReply(_emit_lever_map()),
            ModelReply(_submit_hypothesis()),
        ])
        agent = _agent(model, monkeypatch)
        result = agent.run(context="ctx", **_paths(tmp_path))
        assert len(result.cards) == 1

    def test_context_is_first_user_message(self, tmp_path, monkeypatch):
        model = FakeModel(_full_3step_sequence())
        agent = _agent(model, monkeypatch)
        agent.run(context="my objective context", **_paths(tmp_path))
        msgs = model.calls[0]["messages"]
        assert msgs[0]["content"] == "my objective context"

    def test_system_prompt_has_generator_semantics(self, tmp_path, monkeypatch):
        model = FakeModel(_full_3step_sequence())
        agent = _agent(model, monkeypatch)
        agent.run(context="ctx", **_paths(tmp_path))
        sys_prompt = model.calls[0]["system"]
        assert "Hypothesis Generator" in sys_prompt
        assert "submit_hypothesis" in sys_prompt
        assert "run_research_command" in sys_prompt
        assert "facts_read" in sys_prompt
        assert "emit_lever_map" in sys_prompt

    def test_assigned_ops_replaces_basis(self, tmp_path, monkeypatch):
        model = FakeModel(_full_3step_sequence())
        agent = _agent(model, monkeypatch)
        agent.run(
            context="ctx", **_paths(tmp_path),
            assigned_ops=("G2", "G6", "G9", "G1", "G4"),
        )
        sys_prompt = model.calls[0]["system"]
        for op in ("G1", "G2", "G4", "G6", "G9"):
            assert op in sys_prompt
        assert "Idealize and take limit" not in sys_prompt    # G3
        assert "Invert: don't accelerate" not in sys_prompt    # G5

    def test_assigned_ops_none_keeps_full_basis(self, tmp_path, monkeypatch):
        model = FakeModel(_full_3step_sequence())
        agent = _agent(model, monkeypatch)
        agent.run(context="ctx", **_paths(tmp_path))
        sys_prompt = model.calls[0]["system"]
        for op in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"):
            assert op in sys_prompt

    def test_budget_exhausted_raises(self, tmp_path, monkeypatch):
        """If the generator never submits, it raises."""
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_emit_lever_map()),
        ] + [ModelReply(_run_research()) for _ in range(10)])
        agent = _agent(model, monkeypatch)
        with pytest.raises(GeneratorError, match="budget exhausted"):
            agent.run(context="ctx", **_paths(tmp_path), max_steps=5)

    def test_generative_op_not_in_assigned_ops_rejected(
        self, tmp_path, monkeypatch,
    ):
        """When assigned_ops is set, a hypothesis with a generative_op outside
        that set is rejected with a repair, then accepted when corrected."""
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_emit_lever_map()),
            # G3 is NOT in assigned_ops -> rejected
            ModelReply(_submit_hypothesis(_card_json(op="G3"))),
            # G6 IS in assigned_ops -> accepted
            ModelReply(_submit_hypothesis(_card_json(op="G6"))),
        ])
        agent = _agent(model, monkeypatch)
        result = agent.run(
            context="ctx", **_paths(tmp_path),
            assigned_ops=("G1", "G2", "G4", "G6", "G9"),
        )
        assert len(result.cards) == 1
        assert result.cards[0].generative_op == "G6"

    def test_generative_op_checked_in_regenerate(self, tmp_path, monkeypatch):
        """The generative_op check also applies in regenerate()."""
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_emit_lever_map()),
            # G5 is NOT in assigned_ops -> rejected
            ModelReply(_submit_hypothesis(_card_json(op="G5"))),
            # G1 IS in assigned_ops -> accepted
            ModelReply(_submit_hypothesis(_card_json(op="G1"))),
        ])
        agent = _agent(model, monkeypatch)
        result = agent.regenerate(
            context="ctx", feedback={
                "observation": "no gain",
                "relation_to_seed": "same",
                "evidence_refs": [],
                "implication": "try elsewhere",
            },
            transcript=[], **_paths(tmp_path),
            assigned_ops=("G1", "G2", "G4", "G6", "G9"),
        )
        assert len(result.cards) == 1
        assert result.cards[0].generative_op == "G1"


# --- Agent regenerate() ----------------------------------------------------

class TestRegenerate:
    def _feedback(self):
        return {
            "action": "feedback_generator",
            "evidence_refs": ("experiment:r3c0",),
            "observation": "tried this, no gain",
            "relation_to_seed": "same mechanism",
            "implication": "the gain margin here is small",
        }

    def test_regenerate_produces_one_card(self, tmp_path, monkeypatch):
        model = FakeModel(_full_3step_sequence())
        agent = _agent(model, monkeypatch)
        result = agent.regenerate(
            context="ctx", feedback=self._feedback(), transcript=[],
            **_paths(tmp_path),
        )
        assert len(result.cards) == 1

    def test_regenerate_sends_context_transcript_feedback(self, tmp_path, monkeypatch):
        model = FakeModel(_full_3step_sequence())
        agent = _agent(model, monkeypatch)
        transcript = [
            {"role": "user", "content": "initial ctx"},
            {"role": "assistant", "content": "initial card"},
        ]
        agent.regenerate(
            context="history-free ctx", feedback=self._feedback(),
            transcript=transcript, **_paths(tmp_path),
        )
        msgs = model.calls[0]["messages"]
        assert msgs[0]["content"] == "history-free ctx"
        assert msgs[1] == transcript[0]
        assert msgs[2] == transcript[1]
        assert "History feedback" in msgs[3]["content"]
        assert "tried this, no gain" in msgs[3]["content"]  # observation
        assert "the gain margin" in msgs[3]["content"]  # implication

    def test_regenerate_system_prompt_mentions_partner(self, tmp_path, monkeypatch):
        model = FakeModel(_full_3step_sequence())
        agent = _agent(model, monkeypatch)
        agent.regenerate(
            context="ctx", feedback=self._feedback(), transcript=[],
            **_paths(tmp_path),
        )
        sys_prompt = model.calls[0]["system"]
        assert "cognitive partner" in sys_prompt

    def test_regenerate_respects_assigned_ops(self, tmp_path, monkeypatch):
        model = FakeModel(_full_3step_sequence(
            card=_card_json(op="G1")))
        agent = _agent(model, monkeypatch)
        agent.regenerate(
            context="ctx", feedback=self._feedback(), transcript=[],
            **_paths(tmp_path),
            assigned_ops=("G1", "G3", "G5", "G7", "G9"),
        )
        sys_prompt = model.calls[0]["system"]
        for op in ("G1", "G3", "G5", "G7", "G9"):
            assert op in sys_prompt
        assert "Algorithm/representation/paradigm sweep" not in sys_prompt  # G6

    def test_regenerate_uses_survey_map_submit(self, tmp_path, monkeypatch):
        """regenerate() also enforces the survey -> map -> submit flow."""
        model = FakeModel(_full_3step_sequence(
            cmd="grep -rn 'EVLikelihood' OMILRECV2/src/",
            card=_card_json(region="OMILRECV2/src/OMILRECV2.cc"),
        ))
        agent = _agent(model, monkeypatch)
        result = agent.regenerate(
            context="ctx", feedback=self._feedback(), transcript=[],
            **_paths(tmp_path),
        )
        assert result.cards[0].region == "OMILRECV2/src/OMILRECV2.cc"
        assert len(FakeTools.instances[0].actions) == 1
