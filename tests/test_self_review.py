"""S3c.1 tests: the submit_self_decision action + the ScientistAgent.self_review()
path (RSI self-review). The fake-model tests are the deterministic gate; the
real-model isolation test (spawn the worker --mode self) is a manual/behind-a-token
check listed in the plan.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from proposer import scientist as proposer_mod
from proposer.model import ModelReply
from proposer.scientist import (
    ProposerError, ScientistAgent, SelfReviewResult, parse_response,
)
from proposer.scientist_session import ScientistSession


# --- fake model + tools (mirror tests/test_scientist.py) -------------------

class _FakeModel:
    """Pops scripted JSON-string replies from a queue."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, *, system, messages, timeout_seconds):
        self.calls += 1
        return ModelReply(text=self._replies.pop(0), usage=None)


class _FakeResearchTools:
    """Replaces ResearchTools so no container is needed."""

    def __init__(self, **_kwargs):
        pass

    def execute(self, action, *, deadline):
        return {"ok": True, "output": "fake observation", "returncode": 0}


def _tool_reply(cmd="rg foo"):
    return json.dumps({"action": {"action": "run_research_command",
                                  "command": cmd, "cwd": "work"}})


def _notebook_reply(text="self-review note"):
    return json.dumps({"notebook": text})


def _keep_reply(*, reason="progress is strong and sustained", defer=5):
    return json.dumps({"action": {"action": "submit_self_decision",
                                  "decision": "KEEP",
                                  "diagnosis": "current self advances the Goal sufficiently",
                                  "keep_reason": reason,
                                  "next_review_after_rounds": defer}})


def _change_reply(*, target="prompt", intent="sharpen attention to coverage gaps",
                  instruction="add a coverage-oriented preamble"):
    return json.dumps({"action": {"action": "submit_self_decision",
                                  "decision": "CHANGE",
                                  "diagnosis": "I keep re-proposing covered ground",
                                  "self_change": {"target": target, "intent": intent,
                                                  "instruction": instruction,
                                                  "evidence_refs": ["proposer/scientist.py"]}}})


def _make_agent(replies, *, max_steps=20):
    return ScientistAgent(
        model=_FakeModel(replies), runtime=object(), timeout_seconds=60,
        max_steps=max_steps, command_timeout_seconds=10,
        command_output_cap_chars=1000,
    )


# --- parse_response unit tests (the action-validation gate) ----------------

def test_parse_self_decision_keep():
    action = parse_response(_keep_reply(), 1)
    assert action["action"] == "submit_self_decision"
    assert action["decision"] == "KEEP"
    assert action["keep_reason"].startswith("progress is strong")
    assert action["next_review_after_rounds"] == 5
    assert action["self_change"] is None


def test_parse_self_decision_change():
    action = parse_response(_change_reply(), 1)
    assert action["decision"] == "CHANGE"
    sc = action["self_change"]
    assert sc["target"] == "prompt"
    assert sc["intent"].startswith("sharpen")
    assert sc["evidence_refs"] == ("proposer/scientist.py",)
    assert action["keep_reason"] is None


@pytest.mark.parametrize("payload, hint", [
    ({"action": "submit_self_decision", "decision": "MAYBE", "diagnosis": "x"}, "bad decision"),
    ({"action": "submit_self_decision", "decision": "KEEP", "diagnosis": "x"}, "KEEP needs keep_reason+defer"),
    ({"action": "submit_self_decision", "decision": "KEEP", "diagnosis": "x",
      "keep_reason": "r"}, "KEEP needs defer"),
    ({"action": "submit_self_decision", "decision": "CHANGE", "diagnosis": "x"}, "CHANGE needs self_change"),
    ({"action": "submit_self_decision", "decision": "CHANGE", "diagnosis": "x",
      "self_change": {"target": "nope", "intent": "i", "instruction": "j"}}, "bad target"),
    ({"action": "submit_self_decision", "decision": "CHANGE", "diagnosis": "x",
      "self_change": {"target": "prompt", "intent": "i"}}, "self_change needs instruction"),
])
def test_parse_self_decision_rejects_malformed(payload, hint):
    with pytest.raises(ProposerError):
        parse_response(json.dumps({"action": payload}), 1)


# --- self_review() fake-model path tests -----------------------------------

def _run_self_review(agent, tmp_path, *, session_round=0):
    session = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v3")
    return agent.self_review(
        goal="optimize the FCN", self_repo=tmp_path, run_dir=tmp_path,
        reviews_path=tmp_path / "self" / "reviews.jsonl",
        incumbent_self_sha="abc123def456", objective_key="SPEED_MS",
        current_round=session_round, prompt_dir=None, session=session,
        memory_service=None, max_steps=None,  # use the agent's own max_steps
    )


def test_self_review_keep(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(proposer_mod, "ResearchTools", _FakeResearchTools)
    agent = _make_agent([_keep_reply(), _notebook_reply()])
    result = _run_self_review(agent, tmp_path)
    assert isinstance(result, SelfReviewResult)
    assert result.decision == "KEEP"
    assert result.keep_reason.startswith("progress is strong")
    assert result.next_review_after_rounds == 5
    assert result.self_change is None
    assert not result.abstained


def test_self_review_change(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(proposer_mod, "ResearchTools", _FakeResearchTools)
    agent = _make_agent([_change_reply(), _notebook_reply()])
    result = _run_self_review(agent, tmp_path)
    assert result.decision == "CHANGE"
    assert result.self_change["target"] == "prompt"
    assert result.self_change["intent"].startswith("sharpen")
    assert result.keep_reason is None


def test_self_review_budget_exhaustion_defaults_to_keep(monkeypatch, tmp_path: Path):
    """If the budget runs out before a decision, the result is a KEEP with a
    short default commitment so the Host's scheduler re-opens self-attention."""
    monkeypatch.setattr(proposer_mod, "ResearchTools", _FakeResearchTools)
    # two DISTINCT tool calls (identical ones trip the repeated_tool guard), no
    # terminal action, then the suspension notebook reply
    agent = _make_agent([_tool_reply("rg a"), _tool_reply("rg b"),
                         _notebook_reply()], max_steps=2)
    result = _run_self_review(agent, tmp_path)
    assert result.decision == "KEEP"
    assert result.abstained is True
    assert result.next_review_after_rounds == 3  # _SELF_REVIEW_DEFAULT_DEFER


def test_self_review_can_investigate_own_source_first(monkeypatch, tmp_path: Path):
    """The Scientist may read its own source (a tool call) before deciding — the
    self-world tools_factory wires the self-repo as the workspace."""
    monkeypatch.setattr(proposer_mod, "ResearchTools", _FakeResearchTools)
    agent = _make_agent([_tool_reply("cat proposer/scientist.py"),
                         _change_reply(), _notebook_reply()])
    result = _run_self_review(agent, tmp_path)
    assert result.decision == "CHANGE"


# --- worker glue: run_self_review_lane -> result.json shape (no model) -----

def test_run_self_review_lane_result_shape(tmp_path: Path):
    """The worker self-mode glue (no model): SelfRepo path resolution +
    _self_review_result_to_dict. Spans the worker → orchestrator seam without a
    live model, so it is deterministic."""
    from simpleloop.self_repo import SelfRepo
    from simpleloop.proposer_lane_worker import (
        ProposerLaneDeps, ProposerLaneSpec, run_self_review_lane,
    )

    SelfRepo(tmp_path).setup(resume=False)  # creates self/repo + state.json (S0)

    class _FakeOrch:
        captured = None

        def run_self_review(self, **kw):
            _FakeOrch.captured = kw
            return SelfReviewResult(
                decision="CHANGE", diagnosis="I repeat covered ground",
                self_change={"target": "prompt", "intent": "sharpen coverage",
                             "instruction": "add a coverage preamble",
                             "evidence_refs": ("proposer/scientist.py",)})

    deps = ProposerLaneDeps(
        cfg={"goal": "optimize the FCN",
             "metrics": {"objective": {"key": "SPEED_MS"}}},
        run_dir=tmp_path, runtime=None, repo_path=tmp_path / "repo",
        orchestrator=_FakeOrch(), memory_service=None)
    spec = ProposerLaneSpec(lane_id=0, round_id=7, base_sha="x",
                            run_dir=str(tmp_path), mode="self")

    out = run_self_review_lane(deps, spec)

    # the orchestrator was handed the incumbent self-repo + reviews path
    assert _FakeOrch.captured["self_repo"] == tmp_path / "self" / "repo"
    assert _FakeOrch.captured["reviews_path"] == tmp_path / "self" / "reviews.jsonl"
    assert _FakeOrch.captured["incumbent_self_sha"]  # read from state.json
    assert _FakeOrch.captured["goal"] == "optimize the FCN"
    assert _FakeOrch.captured["objective_key"] == "SPEED_MS"

    # result.json self-mode shape
    assert out["mode"] == "self"
    sr = out["self_review"]
    assert sr["decision"] == "CHANGE"
    assert sr["self_change"]["target"] == "prompt"
    assert sr["self_change"]["evidence_refs"] == ["proposer/scientist.py"]
    assert sr["contract_version"] == "proposer-cli-v0"
    assert sr["incumbent_self_sha"]  # read from state.json, non-empty
