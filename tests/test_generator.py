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
                        "cwd": "source"})


def _agent(model, monkeypatch=None):
    """Build a GeneratorAgent with FakeTools."""
    FakeTools.instances.clear()
    if monkeypatch:
        monkeypatch.setattr(gen_mod, "ResearchTools", FakeTools)
    return GeneratorAgent(
        model=model, runtime=None, timeout_seconds=30,
        max_steps=10, command_timeout_seconds=10,
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
        assert action["cwd"] == "source"

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
        """Gate 2: facts_read is required."""
        card = _card_json()
        del card["facts_read"]
        with pytest.raises(GeneratorError, match="facts_read"):
            _parse_generator_action(json.dumps({
                "action": "submit_hypothesis", "hypothesis": card,
            }))

    def test_submit_with_empty_facts_read_raises(self):
        """Gate 2: facts_read must be non-empty."""
        with pytest.raises(GeneratorError, match="facts_read"):
            _parse_generator_action(json.dumps({
                "action": "submit_hypothesis",
                "hypothesis": _card_json(facts=[]),
            }))

    def test_submit_with_facts_read_parsed(self):
        """facts_read is parsed into a tuple on the card."""
        action = _parse_generator_action(_submit_hypothesis(_card_json(
            facts=["file X contains loop Y", "function Z calls W"],
        )))
        card = action["hypothesis"]
        assert card.facts_read == (
            "file X contains loop Y", "function Z calls W",
        )


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


# --- Agent run() -----------------------------------------------------------

class TestAgentRun:
    def test_run_reads_source_then_submits(self, tmp_path, monkeypatch):
        """The generator reads the source tree, then submits a hypothesis."""
        model = FakeModel([
            ModelReply(_run_research("ls OMILRECV2/src/")),
            ModelReply(_submit_hypothesis(_card_json(
                region="OMILRECV2/src/OMILRECV2.cc", mech="recompute",
            ))),
        ])
        agent = _agent(model, monkeypatch)
        result = agent.run(context="objective: go fast", **_paths(tmp_path))
        assert isinstance(result, GenerationResult)
        assert len(result.cards) == 1
        assert result.cards[0].region == "OMILRECV2/src/OMILRECV2.cc"
        # The FakeTools should have received the research command
        assert len(FakeTools.instances[0].actions) == 1
        assert FakeTools.instances[0].actions[0]["action"] == "run_research_command"

    def test_run_rejects_submit_before_read(self, tmp_path, monkeypatch):
        """Gate 1: submit_hypothesis before any run_research_command is
        rejected. The generator gets a repair message, then reads and submits."""
        model = FakeModel([
            ModelReply(_submit_hypothesis()),       # rejected (no read yet)
            ModelReply(_run_research("ls src/")),   # read source
            ModelReply(_submit_hypothesis()),       # now accepted
        ])
        agent = _agent(model, monkeypatch)
        result = agent.run(context="ctx", **_paths(tmp_path))
        assert len(result.cards) == 1
        # The repair consumed the first submit; the tool was called once.
        assert len(FakeTools.instances[0].actions) == 1

    def test_run_submit_before_read_then_budget_exhausts(self, tmp_path, monkeypatch):
        """Gate 1: if the generator keeps submitting without reading, it
        eventually exhausts the budget and raises."""
        model = FakeModel([
            ModelReply(_submit_hypothesis()) for _ in range(5)
        ])
        agent = _agent(model, monkeypatch)
        with pytest.raises(GeneratorError, match="budget exhausted"):
            agent.run(context="ctx", **_paths(tmp_path), max_steps=3)

    def test_run_context_is_first_user_message(self, tmp_path, monkeypatch):
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_submit_hypothesis()),
        ])
        agent = _agent(model, monkeypatch)
        agent.run(context="my objective context", **_paths(tmp_path))
        msgs = model.calls[0]["messages"]
        assert msgs[0]["content"] == "my objective context"

    def test_run_system_prompt_has_generator_semantics(self, tmp_path, monkeypatch):
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_submit_hypothesis()),
        ])
        agent = _agent(model, monkeypatch)
        agent.run(context="ctx", **_paths(tmp_path))
        sys_prompt = model.calls[0]["system"]
        assert "Hypothesis Generator" in sys_prompt
        assert "submit_hypothesis" in sys_prompt
        assert "run_research_command" in sys_prompt
        assert "facts_read" in sys_prompt

    def test_assigned_ops_replaces_basis(self, tmp_path, monkeypatch):
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_submit_hypothesis()),
        ])
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
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_submit_hypothesis()),
        ])
        agent = _agent(model, monkeypatch)
        agent.run(context="ctx", **_paths(tmp_path))
        sys_prompt = model.calls[0]["system"]
        for op in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"):
            assert op in sys_prompt

    def test_budget_exhausted_raises(self, tmp_path, monkeypatch):
        """If the generator never submits, it raises (orchestrator treats as error)."""
        model = FakeModel([
            ModelReply(_run_research()) for _ in range(10)
        ])
        agent = _agent(model, monkeypatch)
        with pytest.raises(GeneratorError, match="budget exhausted"):
            agent.run(context="ctx", **_paths(tmp_path), max_steps=3)


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
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_submit_hypothesis(_card_json(mech="new"))),
        ])
        agent = _agent(model, monkeypatch)
        result = agent.regenerate(
            context="ctx", feedback=self._feedback(), transcript=[],
            **_paths(tmp_path),
        )
        assert len(result.cards) == 1
        assert result.cards[0].mechanism == "new"

    def test_regenerate_sends_context_transcript_feedback(self, tmp_path, monkeypatch):
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_submit_hypothesis()),
        ])
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
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_submit_hypothesis()),
        ])
        agent = _agent(model, monkeypatch)
        agent.regenerate(
            context="ctx", feedback=self._feedback(), transcript=[],
            **_paths(tmp_path),
        )
        sys_prompt = model.calls[0]["system"]
        assert "cognitive partner" in sys_prompt

    def test_regenerate_respects_assigned_ops(self, tmp_path, monkeypatch):
        model = FakeModel([
            ModelReply(_run_research()),
            ModelReply(_submit_hypothesis()),
        ])
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

    def test_regenerate_can_read_source(self, tmp_path, monkeypatch):
        """regenerate() also has the tool loop — the generator can read code
        again to find a real region for the new hypothesis."""
        model = FakeModel([
            ModelReply(_run_research("grep -rn 'EVLikelihood' OMILRECV2/src/")),
            ModelReply(_submit_hypothesis(_card_json(
                region="OMILRECV2/src/OMILRECV2.cc", mech="precompute",
            ))),
        ])
        agent = _agent(model, monkeypatch)
        result = agent.regenerate(
            context="ctx", feedback=self._feedback(), transcript=[],
            **_paths(tmp_path),
        )
        assert result.cards[0].region == "OMILRECV2/src/OMILRECV2.cc"
        assert len(FakeTools.instances[0].actions) == 1
