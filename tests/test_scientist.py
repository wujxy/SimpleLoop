"""Tests for the Scientist proposer: protocol parsing, session continuity,
and the research loop.

The research loop is exercised with a scripted FakeModel + a FakeResearchTools
(the container is never touched) so we can assert the Scientist thinks → acts →
submits, persists its trajectory + notebook, and resumes as the same persona.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles.proposer import (
    ContextPolicy,
    ProposerError,
    ScientistAgent,
    _build_system_prompt,
    _build_world_event,
    _cap_tail,
    _compact_live_messages,
    parse_response,
)
from simpleloop.roles.research_agent import WorkingState
from simpleloop.roles.scientist_session import ScientistSession
from simpleloop.roles.model import ModelReply


# ---------------- protocol parsing ----------------

def test_parse_response_message_is_optional_and_ignored():
    # message present -> ignored, inner action returned
    a = parse_response(
        '{"message":"let me check the hit loop",'
        '"action":{"action":"run_research_command","command":"rg foo",'
        '"cwd":"work"}}',
        3,
    )
    assert a["action"] == "run_research_command"
    assert a["command"] == "rg foo"
    # message omitted entirely -> still valid
    b = parse_response(
        '{"action":{"action":"run_research_command","command":"rg bar",'
        '"cwd":"scratch"}}',
        3,
    )
    assert b["cwd"] == "scratch"


def test_parse_response_submit_zero_is_legal_abstention():
    a = parse_response(
        '{"action":{"action":"submit_proposals","proposals":[]}}', 3
    )
    assert a["action"] == "submit_proposals"
    assert a["proposals"] == []


def test_parse_response_submit_up_to_n():
    payload = json.dumps({"action": {"action": "submit_proposals", "proposals": [
        {"instruction": "do X",
         "research_target": {"mode": "new", "question": "why X"}},
        {"instruction": "do Y",
         "research_target": {"mode": "new", "question": "why Y"}},
    ]}})
    a = parse_response(payload, 3)
    assert len(a["proposals"]) == 2


def test_parse_response_over_budget_rejected():
    two = json.dumps({"action": {"action": "submit_proposals", "proposals": [
        {"instruction": "a",
         "research_target": {"mode": "new", "question": "q"}},
        {"instruction": "b",
         "research_target": {"mode": "new", "question": "q"}},
    ]}})
    with pytest.raises(ProposerError, match="at most 1 proposal"):
        parse_response(two, 1)


@pytest.mark.parametrize("old", ["select_for_enrich", "block", "feedback_generator"])
def test_old_pipeline_actions_are_now_unknown(old):
    with pytest.raises(ProposerError, match="unknown action"):
        parse_response(json.dumps({"action": {"action": old}}), 3)


def test_parse_response_requires_action_object():
    with pytest.raises(ProposerError, match="action"):
        parse_response('{"message":"thinking"}', 3)
    with pytest.raises(ProposerError, match="JSON"):
        parse_response("not json", 3)


# ---------------- session continuity ----------------

def test_scientist_id_persisted_on_creation_not_at_round_end(tmp_path):
    """A fresh scientist_id lands in meta.json at load_or_create time, before
    any round runs — so a worker killed mid-first-round keeps its identity."""
    sess = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")
    # NO save_meta call — simulate a kill before round end.
    meta_path = tmp_path / "scientists" / "lane-0" / "meta.json"
    assert meta_path.exists(), "meta.json must be written at creation"
    persisted = json.loads(meta_path.read_text())
    assert persisted["scientist_id"] == sess.scientist_id
    # a reload resumes the SAME identity even though save_meta never ran
    resumed = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")
    assert resumed.scientist_id == sess.scientist_id


def test_session_cold_start_then_resume(tmp_path):
    sess = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")
    assert sess.is_first_round()
    assert sess.scientist_id
    sid = sess.scientist_id
    sess.append_message("user", "BEGIN", round_id=0)
    for i in range(3):
        sess.append_message("assistant", f"think{i}", round_id=0)
        sess.append_message("user", f"obs{i}", round_id=0)
    sess.write_notebook("the hit loop is the bottleneck")
    sess.save_meta(round_id=0, base_sha="abc123")

    resumed = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")
    assert resumed.scientist_id == sid  # identity stable across rounds
    assert not resumed.is_first_round()
    assert "hit loop" in resumed.notebook


def test_session_tail_never_orphans_observation(tmp_path):
    sess = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="v")
    # an assistant message with no following observation must be dropped
    sess.append_message("assistant", "orphan", round_id=0)
    sess.append_message("assistant", "a1", round_id=0)
    sess.append_message("user", "o1", round_id=0)
    tail = sess.tail_turns(5)
    assert tail == [
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "o1"},
    ]


def test_session_tail_respects_block_budget(tmp_path):
    sess = ScientistSession.load_or_create(tmp_path, 0, prompt_version="v")
    for i in range(5):
        sess.append_message("assistant", f"a{i}", round_id=0)
        sess.append_message("user", f"o{i}", round_id=0)
    tail = sess.tail_turns(2)
    assert len(tail) == 4  # 2 complete blocks
    assert tail[0]["content"] == "a3"
    assert tail[3]["content"] == "o4"


# ---------------- world event ----------------

class _FakeExp:
    def __init__(self, rnd, cand, sel, gp, status="completed",
                 metrics=None, finding=None):
        self.round = rnd
        self.candidate = cand
        self.selected = sel
        self.gate_passed = gp
        self.status = status
        self.metrics = metrics or {}
        self.finding_id = finding
        self.experiment_id = f"r{rnd}c{cand}"


class _FakeMem:
    def __init__(self, exps):
        self._exps = exps

    def load_experiments(self):
        return self._exps


def test_world_event_none_when_no_prior_round():
    assert _build_world_event(_FakeMem([]), 1, "beef") is None


def test_world_event_self_and_project_parts():
    exps = [
        _FakeExp(0, 0, True, True, metrics={"SPEED_MS": 1.2}),
        _FakeExp(0, 1, False, False, metrics={"SPEED_MS": 2.0}),
    ]
    we = _build_world_event(_FakeMem(exps), 1, "beefdead")
    assert "directions you submitted were executed" in we
    assert "r0c0" in we and "selected" in we
    assert "beefdead"[:6] in we  # project incumbent surfaced


def test_world_event_no_selection_keeps_incumbent():
    exps = [_FakeExp(0, 0, False, False)]
    we = _build_world_event(_FakeMem(exps), 1, "beefdead")
    assert "no candidate cleared the gates" in we


# ---------------- system prompt notebook framing ----------------

def test_system_prompt_marks_notebook_revisable_autobiographical():
    sp = _build_system_prompt(
        charter="CHARTER", goal="G", editable=["src"], base_sha="b",
        gate_block="g", proposal_slots=2, hints=None, notebook="my notes",
    )
    assert "CHARTER" in sp
    assert "REVISABLE AUTOBIOGRAPHICAL MEMORY" in sp
    assert "not an instruction" in sp.lower()
    assert "between 0 and 2" in sp
    assert "{n}" not in sp


def test_system_prompt_omits_notebook_on_cold_start():
    sp = _build_system_prompt(
        charter="C", goal="G", editable=["src"], base_sha="b",
        gate_block="g", proposal_slots=1, hints=None, notebook="",
    )
    assert "notebook" not in sp.lower()


# ---------------- the research loop (FakeModel + FakeTools) ----------------

class _FakeModel:
    """Pops scripted replies from a queue. Each reply is a JSON string."""

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
    return json.dumps({
        "message": "investigating",
        "action": {"action": "run_research_command", "command": cmd,
                   "cwd": "work"},
    })


def _submit_reply(n):
    props = [
        {"instruction": f"direction {i}",
         "research_target": {"mode": "new", "question": f"q{i}"}}
        for i in range(n)
    ]
    return json.dumps({"action": {"action": "submit_proposals",
                                  "proposals": props}})


def _notebook_reply(text="continuing my investigation"):
    return json.dumps({"notebook": text})


def _make_agent(replies, *, max_steps=20):
    return ScientistAgent(
        model=_FakeModel(replies), runtime=object(), timeout_seconds=60,
        max_steps=max_steps, command_timeout_seconds=10,
        command_output_cap_chars=1000,
    )


def test_research_cold_start_submits_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(
        proposer_mod, "ResearchTools", _FakeResearchTools)
    agent = _make_agent([_tool_reply("rg a"), _tool_reply("rg b"),
                         _submit_reply(1), _notebook_reply()])
    session = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")

    result = agent.research(
        goal="optimize the FCN", editable=["src"], world_mount=None,
        memory_service=_FakeMem([]), base_sha="abc123",
        source_path=tmp_path, repo_path=tmp_path, run_dir=tmp_path,
        current_round=0, gate_block="g", prompt_dir=None,
        proposal_slots=3, session=session, max_steps=10,
    )
    assert len(result.proposals) == 1
    assert not result.abstained
    # trajectory archived (2 tool turns: 2 assistant + 2 user obs)
    archived = (tmp_path / "scientists" / "lane-0" / "session.jsonl").read_text()
    assert archived.count("rg a") >= 1 and archived.count("rg b") >= 1
    # notebook written at suspension
    assert "continuing my investigation" in session.notebook


def test_suspension_checkpoint_sees_the_terminal_submit(tmp_path, monkeypatch):
    """Regression: the checkpoint model call must include the Scientist's own
    submit reply, so the continuation note is written by a Scientist that has
    just seen its decision (not by one that has forgotten what it submitted)."""
    monkeypatch.setattr(proposer_mod, "ResearchTools", _FakeResearchTools)
    captured: list[list[dict]] = []

    class _CapturingModel:
        def __init__(self, replies):
            self._r = list(replies)

        def complete(self, *, system, messages, timeout_seconds):
            captured.append([dict(m) for m in messages])
            return ModelReply(text=self._r.pop(0), usage=None)

    agent = ScientistAgent(
        model=_CapturingModel([_tool_reply("rg a"), _submit_reply(1),
                               _notebook_reply()]),
        runtime=object(), timeout_seconds=60, max_steps=20,
        command_timeout_seconds=10, command_output_cap_chars=1000,
    )
    session = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")
    agent.research(
        goal="g", editable=["src"], world_mount=None,
        memory_service=_FakeMem([]), base_sha="abc",
        source_path=tmp_path, repo_path=tmp_path, run_dir=tmp_path,
        current_round=0, gate_block="g", prompt_dir=None,
        proposal_slots=3, session=session, max_steps=10,
    )
    # 3 model calls: tool → submit → suspension checkpoint
    assert len(captured) == 3
    checkpoint_msgs = "\n".join(m["content"] for m in captured[2])
    assert "direction 0" in checkpoint_msgs, (
        "checkpoint must see the submitted direction it is about to summarize")


def test_research_abstain_on_zero_proposals(tmp_path, monkeypatch):
    monkeypatch.setattr(proposer_mod, "ResearchTools", _FakeResearchTools)
    agent = _make_agent([_submit_reply(0), _notebook_reply()])
    session = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")
    result = agent.research(
        goal="g", editable=["src"], world_mount=None,
        memory_service=_FakeMem([]), base_sha="abc",
        source_path=tmp_path, repo_path=tmp_path, run_dir=tmp_path,
        current_round=0, gate_block="g", prompt_dir=None,
        proposal_slots=3, session=session, max_steps=10,
    )
    assert result.abstained
    assert result.proposals == []


def test_research_budget_exhaustion_abstains(tmp_path, monkeypatch):
    monkeypatch.setattr(proposer_mod, "ResearchTools", _FakeResearchTools)
    # never submits; only ever calls tools (distinct commands so the
    # repeated_tool guard does not fire). max_steps=2 -> exhausts.
    agent = _make_agent([_tool_reply("rg a"), _tool_reply("rg b"),
                         _notebook_reply()], max_steps=2)
    session = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")
    result = agent.research(
        goal="g", editable=["src"], world_mount=None,
        memory_service=_FakeMem([]), base_sha="abc",
        source_path=tmp_path, repo_path=tmp_path, run_dir=tmp_path,
        current_round=0, gate_block="g", prompt_dir=None,
        proposal_slots=3, session=session, max_steps=2,
    )
    assert result.abstained
    assert "budget exhausted" in (result.abstain_reason or "")


def test_research_resume_injects_world_event(tmp_path, monkeypatch):
    """Round 0 runs and persists; round 1 resumes and sees a world event
    built from the prior round's experiments — same scientist_id throughout."""
    monkeypatch.setattr(proposer_mod, "ResearchTools", _FakeResearchTools)

    # round 0: cold start, submit 1
    session = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")
    sid0 = session.scientist_id
    agent0 = _make_agent([_submit_reply(1), _notebook_reply("round 0 note")])
    agent0.research(
        goal="g", editable=["src"], world_mount=None,
        memory_service=_FakeMem([]), base_sha="abc",
        source_path=tmp_path, repo_path=tmp_path, run_dir=tmp_path,
        current_round=0, gate_block="g", prompt_dir=None,
        proposal_slots=3, session=session, max_steps=10,
    )
    # research appends trajectory + writes notebook, but meta.json (scientist_id)
    # is the orchestrator's responsibility — persist it so round 1 resumes the
    # same persona rather than cold-starting.
    session.save_meta(round_id=0, base_sha="abc")

    # round 1: resume — there is now a prior-round experiment in the world
    session1 = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")
    assert session1.scientist_id == sid0  # same Scientist
    assert not session1.is_first_round()

    captured = {}

    def fake_complete(self, *, system, messages, timeout_seconds):
        # The live context the resumed Scientist sees:
        joined = "\n".join(m["content"] for m in messages)
        captured["context"] = joined
        captured["system"] = system
        return ModelReply(text=_submit_reply(0), usage=None)

    agent1 = _make_agent([])
    monkeypatch.setattr(_FakeModel, "complete", fake_complete)
    agent1.model.calls = 0
    # suspension checkpoint also calls complete -> supply a notebook reply next
    replies = [_submit_reply(0), _notebook_reply()]
    agent1.model._replies = replies
    result = agent1.research(
        goal="g", editable=["src"], world_mount=None,
        memory_service=_FakeMem([_FakeExp(0, 0, True, True,
                                          metrics={"SPEED_MS": 1.2})]),
        base_sha="newincumbent", source_path=tmp_path, repo_path=tmp_path,
        run_dir=tmp_path, current_round=1, gate_block="g", prompt_dir=None,
        proposal_slots=3, session=session1, max_steps=10,
    )
    # the world event was injected into the resumed context
    assert "directions you submitted were executed" in captured["context"]
    assert "round 0 note" in captured["system"]  # notebook carried into system
    assert result.abstained  # submitted 0 this round
    # Regression: the world event is also archived as lived history
    archive = (tmp_path / "scientists" / "lane-0" / "session.jsonl").read_text()
    assert "directions you submitted were executed" in archive


# ---------------- live-context compaction (Option A) ----------------

def _pair(a: str, u: str) -> list[dict]:
    return [{"role": "assistant", "content": a},
            {"role": "user", "content": u}]


def _pairs(n: int, *, obs_chars: int = 4) -> list[dict]:
    msgs: list[dict] = []
    for i in range(n):
        msgs += _pair(f"a{i}", "u" * obs_chars + str(i))
    return msgs


def test_cap_tail_pair_budget_keeps_most_recent():
    msgs = _pairs(5)
    kept = _cap_tail(msgs, window_pairs=2, window_max_chars=10**6)
    # the 2 most-recent pairs: a3/u3, a4/u4
    assert [m["content"] for m in kept] == ["a3", "uuuu3", "a4", "uuuu4"]


def test_cap_tail_char_budget_truncates_oldest():
    # each pair ~ obs_chars + small; char budget admits only the newest pair
    msgs = _pairs(5, obs_chars=100)
    kept = _cap_tail(msgs, window_pairs=100, window_max_chars=150)
    # pair 4 (~104 chars) admitted; pair 3 would push past 150 → stop
    assert len(kept) == 2
    assert kept[0]["content"] == "a4"


def test_cap_tail_most_recent_pair_always_survives():
    # budget smaller than a single pair AND window_pairs=0 — the sentinel
    # still admits the most-recent pair, so it is never dropped alone.
    msgs = _pair("a0", "x" * 1000)
    kept = _cap_tail(msgs, window_pairs=0, window_max_chars=10)
    assert len(kept) == 2 and kept[0]["content"] == "a0"


def test_cap_tail_never_orphans_observation():
    # a user observation is only ever kept alongside the assistant action that
    # produced it — never as a dangling result with no recollection of wanting it
    msgs = _pairs(4)
    kept = _cap_tail(msgs, window_pairs=2, window_max_chars=10**6)
    for i in range(0, len(kept), 2):
        assert kept[i]["role"] == "assistant"
        assert kept[i + 1]["role"] == "user"


def test_compact_preserves_seed_preamble():
    seed = [{"role": "user", "content": "COLD START framing"}]
    msgs = seed + _pairs(5)
    new, info = _compact_live_messages(
        msgs, window_pairs=2, window_max_chars=10**6)
    assert info["compacted"] is True
    assert new[0]["content"] == "COLD START framing"  # preamble preserved
    assert [m["content"] for m in new[1:]] == ["a3", "uuuu3", "a4", "uuuu4"]


def test_compact_noop_when_nothing_to_shed():
    # only one pair, window allows it → no compaction
    msgs = [{"role": "user", "content": "seed"}] + _pairs(1)
    new, info = _compact_live_messages(
        msgs, window_pairs=3, window_max_chars=10**6)
    assert info["compacted"] is False
    assert new is msgs


def test_compact_noop_when_too_few_messages():
    msgs = _pair("a0", "u0")
    new, info = _compact_live_messages(
        msgs, window_pairs=3, window_max_chars=10**6)
    assert info["compacted"] is False


def test_policy_from_config_rejects_unknown_keys():
    with pytest.raises(ValueError, match="unknown key"):
        ContextPolicy.from_config({"not_a_key": 1})


def test_policy_from_config_rejects_bad_threshold():
    with pytest.raises(ValueError, match="positive integer"):
        ContextPolicy.from_config({"emergency_threshold_tokens": 0})


def _make_agent_with_policy(replies, *, policy, max_steps=20):
    return ScientistAgent(
        model=_FakeModel(replies), runtime=object(), timeout_seconds=60,
        max_steps=max_steps, command_timeout_seconds=10,
        command_output_cap_chars=1000, context_policy=policy,
    )


def test_maybe_compact_triggers_on_token_threshold():
    agent = _make_agent_with_policy(
        [], policy=ContextPolicy(
            emergency_threshold_tokens=1000, window_pairs=2,
            window_max_chars=10**6))
    msgs = [{"role": "user", "content": "seed"}] + _pairs(5)
    state = WorkingState()
    agent._maybe_compact(msgs, [{"prompt_tokens": 5000}], state)
    assert state.counts.get("compact") == 1
    assert len(msgs) == 1 + 4  # seed + last 2 pairs
    assert msgs[0]["content"] == "seed"
    assert msgs[-1]["content"] == "uuuu4"  # most-recent observation kept


def test_maybe_compact_disabled_when_threshold_none():
    agent = _make_agent_with_policy(
        [], policy=ContextPolicy(emergency_threshold_tokens=None))
    msgs = [{"role": "user", "content": "seed"}] + _pairs(5)
    state = WorkingState()
    agent._maybe_compact(msgs, [{"prompt_tokens": 999999}], state)
    assert state.counts.get("compact") is None
    assert len(msgs) == 1 + 10  # unchanged


def test_maybe_compact_char_fallback_when_no_usage():
    # provider reports no prompt_tokens → fall back to char estimate
    agent = _make_agent_with_policy(
        [], policy=ContextPolicy(
            emergency_threshold_tokens=50, window_pairs=1,
            window_max_chars=10**6))
    # build messages whose char/4 estimate exceeds 50 tokens
    msgs = [{"role": "user", "content": "seed"}]
    for i in range(5):
        msgs += _pair(f"a{i}", "y" * 100)
    state = WorkingState()
    agent._maybe_compact(msgs, [None], state)  # usage None → char fallback
    assert state.counts.get("compact") == 1
    assert len(msgs) < 1 + 10  # shed something


def test_research_loop_compacts_live_but_keeps_full_archive(
    tmp_path, monkeypatch,
):
    """The safety guarantee: compaction shrinks the LIVE context the model sees,
    but the session.jsonl archive receives every observation in full — the
    Scientist's lived record is never lossy, only its short-term window is."""
    monkeypatch.setattr(proposer_mod, "ResearchTools", _FakeResearchTools)

    # 8 tool steps then submit + notebook. Large observations so the char
    # fallback trips a low token threshold each step.
    replies = []
    for i in range(8):
        replies.append(_tool_reply(f"rg cmd{i}"))
    replies.append(_submit_reply(1))
    replies.append(_notebook_reply("compacted but I remember"))

    agent = ScientistAgent(
        model=_FakeModel(replies), runtime=object(), timeout_seconds=60,
        max_steps=20, command_timeout_seconds=10,
        command_output_cap_chars=1000,
        context_policy=ContextPolicy(
            emergency_threshold_tokens=1, window_pairs=2,
            window_max_chars=10**6),
    )
    session = ScientistSession.load_or_create(
        tmp_path, 0, prompt_version="scientist-v1")

    result = agent.research(
        goal="g", editable=["src"], world_mount=None,
        memory_service=_FakeMem([]), base_sha="abc",
        source_path=tmp_path, repo_path=tmp_path, run_dir=tmp_path,
        current_round=0, gate_block="g", prompt_dir=None,
        proposal_slots=3, session=session, max_steps=20,
    )
    # compaction fired during the round
    assert result.deliberation_telemetry.get("compactions", 0) >= 1
    # ...but the archive recorded ALL 8 tool observations + the submit reply
    archive = (tmp_path / "scientists" / "lane-0" / "session.jsonl").read_text()
    for i in range(8):
        assert f"rg cmd{i}" in archive, (
            f"observation {i} missing from archive despite compaction")
    assert "compacted but I remember" in archive  # notebook checkpoint too
