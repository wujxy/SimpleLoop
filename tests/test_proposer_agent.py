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
    IdeaPhase,
    _parse_action,
)


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, **kwargs):
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
            return {"ok": True, "returncode": 0, "timed_out": False,
                    "truncated": False, "output": "TOOL_RESULT_BODY"}
        if action["action"] == "inspect_episode":
            return {"ok": True, "result": {"experiment_id": action["ref"],
                                            "ref": action["ref"]}}
        if action["action"] == "list_findings":
            return {"ok": True, "result": []}
        if action["action"] == "search_findings":
            return {"ok": True, "result": []}
        if action["action"] == "inspect_finding":
            return {"ok": True, "result": {"id": action["finding_id"]}}
        if action["action"] == "search_experiments":
            return {"ok": True, "result": {"relevant": [], "contrasting": [],
                                           "diverse": []}}
        raise AssertionError(f"unexpected fake action: {action['action']}")


def _reply(action, usage=None):
    return ModelReply(json.dumps(action), usage=usage)


def _generate(directions="Replace the cache layout with a flat array.",
              unknown="whether the flat layout preserves access order"):
    return {"action": "generate", "candidate_directions": directions,
            "generative_operations": ["G6", "G2"],
            "decision_relevant_unknown": unknown,
            "known_evidence": ["the dashboard shows prior cache attempts"]}


def _assess(progress="advancing", draft="Hoist the cache lookup.",
            premise="the lookup is on the hot path",
            refs=("experiment:r0c0",)):
    return {"action": "assess_candidate", "current_judgment": "lookup is the likely cost",
            "research_progress": progress, "draft_proposal": draft,
            "weakest_premise": premise, "evidence_refs": list(refs)}


def _new_target(question="Replace the cache layout.",
                mechanisms=None, code_regions=None):
    t = {"mode": "new", "question": question}
    if mechanisms is not None:
        t["mechanisms"] = mechanisms
    if code_regions is not None:
        t["code_regions"] = code_regions
    return t


def _submit(instruction="Replace the cache layout.",
            evidence_refs=None, material_difference=None,
            mechanisms=None, code_regions=None):
    prop = {"instruction": instruction,
            "research_target": _new_target(mechanisms=mechanisms,
                                           code_regions=code_regions)}
    if evidence_refs is not None:
        prop["evidence_refs"] = evidence_refs
    if material_difference is not None:
        prop["material_difference"] = material_difference
    return {"action": "submit_proposals", "proposals": [prop]}


def _reject(what_failed="the cache layout doesn't help",
            mechanism="cache-locality", region="src/a.cc"):
    return {"action": "reject_candidate", "what_failed": what_failed,
            "failed_mechanism": mechanism, "failed_region": region,
            "evidence_summary": "r0c0 showed no improvement"}


def _abandon(reason="No mechanism has direct evidence.",
             blocking_unknown="whether QPDF is still hot"):
    action = {"action": "abandon_round", "reason": reason}
    if blocking_unknown is not None:
        action["blocking_unknown"] = blocking_unknown
    return action


_METRICS_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
                   "gates": []}


def _memory_service(run_dir):
    return MemoryService(run_dir=run_dir, metrics_schema=_METRICS_SCHEMA)


def _seed_history(run_dir, *, proposal="prior attempt", fid=None,
                  objective=100.0):
    record = {"round": 0, "parent_sha": "abc123", "selected_candidate": 0,
              "selected_sha": "sha0", "candidates": [{
                  "candidate": 0, "experiment_id": "r0c0", "finding_id": fid,
                  "proposal": proposal, "parent_sha": "abc123", "sha": "sha0",
                  "status": "COMPLETED", "metrics": {"SPEED_MS": objective},
                  "changed_paths": ["src/a.cc"], "gates": {},
                  "gate_passed": True, "eligible": True, "selected": True}]}
    (run_dir / "history.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")


def _run_args(tmp_path, *, hints=None):
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    run_dir = tmp_path / "run"
    for path in (source, repo, run_dir):
        path.mkdir(exist_ok=True)
    return {"goal": "make reconstruction faster", "editable": ["src/**"],
            "frozen": ["tests/**"], "memory_service": _memory_service(run_dir),
            "base_sha": "abc123", "source_path": source, "repo_path": repo,
            "run_dir": run_dir, "current_round": 1, "candidates_per_round": 1,
            "gate_block": "- physics: must pass", "prompt_dir": None,
            "hints": hints}


def _seq_full():
    """Full happy-path sequence: generate -> assess -> submit."""
    return [_reply(_generate()), _reply(_assess()), _reply(_submit())]


def _agent(model, *, max_steps=12, observer=None):
    return ProposerAgent(model=model, runtime=object(), timeout_seconds=30,
                         max_steps=max_steps, command_timeout_seconds=5,
                         command_output_cap_chars=1000, usage_observer=observer)


# --- happy paths ----------------------------------------------------------

def test_happy_path_with_history_evidence_submits(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel(_seq_full())
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert result.abstained is False
    assert result.trace.get("authority") == "non_authoritative"
    assert result.trace.get("inject_into_future_context") is False


def test_first_round_requires_tool_then_submits(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_generate()),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply(_assess(refs=("experiment:r0c0",))),
        _reply(_submit()),
    ])
    result = _agent(model).run(**_run_args(tmp_path))
    assert len(result.proposals) == 1
    assert [a[0]["action"] for a in FakeTools.instances[0].actions] == ["inspect_episode"]


def test_submit_accepts_existing_target(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_generate()), _reply(_assess()),
        _reply({"action": "submit_proposals", "proposals": [{
            "instruction": "Rerun the hoist on the new baseline.",
            "research_target": {"mode": "existing", "finding_id": "F-008"}}]}),
    ])
    result = _agent(model).run(**args)
    assert isinstance(result.proposals[0].research_target, ExistingFindingTarget)
    assert result.proposals[0].research_target.finding_id == "F-008"


# --- the guards -----------------------------------------------------------

def test_first_round_assess_without_evidence_is_repaired(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_generate()),
        _reply(_assess()),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply(_assess()),
        _reply(_submit()),
    ])
    result = _agent(model).run(**_run_args(tmp_path))
    assert len(result.proposals) == 1
    assert "reason=needs_evidence" in capsys.readouterr().out


def test_hint_provides_evidence_basis(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path, hints=["the hotspot is the QPDF lookup"])
    model = FakeModel(_seq_full())
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert "reason=needs_evidence" not in capsys.readouterr().out


def test_repeated_tool_is_repaired(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_generate()),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r1c0"}),
        _reply(_assess(refs=("experiment:r1c0",))),
        _reply(_submit()),
    ])
    result = _agent(model).run(**_run_args(tmp_path))
    assert len(result.proposals) == 1
    assert "reason=repeated_tool" in capsys.readouterr().out




def test_near_duplicate_repaired_then_passes(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"], proposal="hoist the cache lookup")
    model = FakeModel([
        _reply(_generate()),
        _reply(_assess(draft="hoist the cache5 cache lookup")),
        _reply(_submit(instruction="hoist the cache lookup")),
        _reply(_submit(instruction="hoist the cache lookup",
                       evidence_refs=["experiment:r0c0"],
                       material_difference="uses the new baseline accepted this round")),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert "reason=near_duplicate" in capsys.readouterr().out


# --- reject_candidate / taboo set -----------------------------------------

def test_reject_opens_new_episode_and_enforces_taboo(tmp_path, monkeypatch, capsys):
    """reject_candidate writes a taboo record. A submit in the taboo family
    without new evidence is refused; with new evidence it passes."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_generate()),
        _reply(_assess(progress="contradicted")),
        _reply(_reject(mechanism="cache-locality", region="src/a.cc")),
        _reply(_generate(directions="Switch to a GPU pipeline")),
        _reply(_assess()),
        # Submit in the taboo family (cache-locality / src/a.cc) without
        # new evidence -> refused.
        _reply(_submit(instruction="Try a different cache layout",
                       evidence_refs=["experiment:r0c0"],
                       material_difference="different layout",
                       mechanisms=["cache-locality"],
                       code_regions=["src/a.cc"])),
        # Examine new evidence, then submit with it -> passes.
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply(_assess(refs=("experiment:r0c0",))),
        _reply(_submit(instruction="Try a different cache layout",
                       evidence_refs=["experiment:r0c0"],
                       material_difference="new evidence from r0c0",
                       mechanisms=["cache-locality"],
                       code_regions=["src/a.cc"])),
    ])
    result = _agent(model, max_steps=20).run(**args)
    assert len(result.proposals) == 1
    out = capsys.readouterr().out
    assert "reason=taboo_family" in out
    tel = result.deliberation_telemetry
    assert tel["episode_count"] >= 2
    assert tel["taboo_count"] >= 1


def test_reject_truncates_messages(tmp_path, monkeypatch):
    """After reject_candidate, the conversation history is truncated. The
    model's first call after reject should NOT see the first episode's
    assistant text."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    first_direction = "cache-locality approach"
    model = FakeModel([
        _reply(_generate(directions=first_direction)),
        _reply(_assess(progress="contradicted")),
        _reply(_reject(mechanism="cache-locality", region="src/a.cc")),
        _reply(_generate(directions="GPU pipeline")),
        _reply(_assess()),
        _reply(_submit()),
    ])
    _agent(model, max_steps=20).run(**args)
    post_reject_messages = model.calls[3]["messages"]
    assistant_texts = [m.get("content", "") for m in post_reject_messages
                      if m.get("role") == "assistant"]
    assert not any(first_direction in t for t in assistant_texts)


# --- Explore challenge_response guard -------------------------------------

def _seed_stall_history(run_dir, *, rounds=4, objective=100.0):
    records = []
    parent = "abc123"
    sha = "sha0"
    for r in range(rounds):
        sel = (r == 0)
        records.append({"round": r, "parent_sha": parent, "selected_candidate": 0,
                        "selected_sha": sha, "candidates": [{
                            "candidate": 0, "experiment_id": f"r{r}c0",
                            "finding_id": None, "proposal": f"neutral variant {r}",
                            "parent_sha": parent, "sha": sha, "status": "COMPLETED",
                            "metrics": {"SPEED_MS": objective},
                            "changed_paths": ["src/a.cc"], "gates": {},
                            "gate_passed": True, "eligible": True, "selected": sel}]})
        parent = sha
        sha = f"sha{r + 1}"
    (run_dir / "history.jsonl").write_text(
        "\n".join(json.dumps(rec) for rec in records) + "\n", encoding="utf-8")


def _challenge_response(refs=("experiment:r0c0",)):
    return {"triggered_policy": "global_stall", "stalled_family": "src/a.cc::lookup",
            "what_was_exhausted": "repeated neutral variants of the lookup",
            "null_hypothesis": "this variant changes the cost no more than noise",
            "why_this_is_not_same_family_variant": "it removes the lookup entirely",
            "why_worth_one_more_experiment": "the source shows a now-dead branch",
            "evidence_refs": list(refs)}


def _submit_with_challenge(instruction="Remove the dead lookup branch.", challenge=None):
    action = _submit(instruction=instruction, evidence_refs=["experiment:r0c0"])
    action["challenge_response"] = challenge or _challenge_response()
    return action


def test_challenge_required_submit_no_longer_gated(tmp_path, monkeypatch, capsys):
    """In the branch-then-deepen architecture, Explore's challenge signal
    steers the GENERATOR (via the generation boundary), not the submit gate.
    A branch that reaches submit with evidence_basis passes — no
    challenge_response is required or repaired."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_stall_history(args["run_dir"])
    model = FakeModel([
        _reply(_generate()),
        _reply(_assess(refs=("experiment:r0c0",))),
        _reply(_submit()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    out = capsys.readouterr().out
    assert "reason=challenge_response_required" not in out
    tel = result.deliberation_telemetry
    # Explore still reports the challenge for telemetry, but does not gate.
    assert tel["explore_challenge_required"] is True
    assert result.trace["explore"]["challenge_required"] is True


def test_challenge_response_no_longer_required(tmp_path, monkeypatch, capsys):
    """Even with invalid evidence refs in a challenge_response, submit passes
    because the gate is removed — the response is ignored, not validated."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_stall_history(args["run_dir"])
    model = FakeModel([
        _reply(_generate()),
        _reply(_assess(refs=("experiment:r0c0",))),
        _reply(_submit()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert "reason=challenge_response_required" not in capsys.readouterr().out


def test_reject_under_challenge_needs_no_response(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_stall_history(args["run_dir"])
    model = FakeModel([
        _reply(_generate()),
        _reply(_assess(progress="stalled")),
        _reply(_reject()),
        _reply(_generate(directions="Is the cost in the branch predictor?")),
        _reply(_assess(progress="stalled")),
        _reply(_abandon()),
    ])
    result = _agent(model, max_steps=20).run(**args)
    assert result.abstained is True
    assert "reason=challenge_response_required" not in capsys.readouterr().out


def test_no_challenge_means_no_response_required(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"], objective=100.0)
    model = FakeModel([_reply(_generate()), _reply(_assess(refs=("experiment:r0c0",))),
                       _reply(_submit())])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    tel = result.deliberation_telemetry
    assert tel["explore_challenge_required"] is False
    assert tel["challenge_response_provided"] is False


def test_parse_challenge_response_round_trips_and_rejects_unknown_keys():
    action = {"action": "submit_proposals",
              "proposals": [{"instruction": "x", "research_target": _new_target()}],
              "challenge_response": _challenge_response()}
    parsed = _parse_action(json.dumps(action), candidates_per_round=1)
    cr = parsed["challenge_response"]
    assert cr["null_hypothesis"] == "this variant changes the cost no more than noise"
    assert cr["evidence_refs"] == ["experiment:r0c0"]
    bad = {**action, "challenge_response": {**_challenge_response(), "bogus": 1}}
    with pytest.raises(ProposerError):
        _parse_action(json.dumps(bad), candidates_per_round=1)
    bad2 = {**action, "challenge_response": {**_challenge_response(), "null_hypothesis": "  "}}
    with pytest.raises(ProposerError):
        _parse_action(json.dumps(bad2), candidates_per_round=1)


# --- control flow ---------------------------------------------------------

def test_abandon_round_terminates_with_zero_proposals(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([_reply(_generate()), _reply(_assess(progress="stalled")),
                       _reply(_abandon())])
    result = _agent(model).run(**args)
    assert result.abstained is True
    assert result.proposals == []
    assert result.abstain_reason == "No mechanism has direct evidence."
    assert result.deliberation_telemetry["abandoned"] is True
    assert "[proposer] abstained" in capsys.readouterr().out


def test_state_header_is_injected(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel(_seq_full())
    _agent(model).run(**args)
    second_call_messages = model.calls[1]["messages"]
    assert any("Working state" in m.get("content", "")
              for m in second_call_messages if m.get("role") == "user")


# --- logging / budget / protocol mechanics --------------------------------

def test_agent_prints_safe_action_summaries(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    long_command = "printf PRIVATE_COMMAND_BODY\n" + ("x" * 180) + "HIDDEN_TAIL"
    model = FakeModel([
        _reply({"action": "run_research_command", "command": long_command, "cwd": "source"}),
        _reply(_generate()), _reply(_assess()), _reply(_submit())])
    _agent(model, max_steps=12).run(**args)
    out = capsys.readouterr().out
    assert "[proposer] started" in out
    assert "[proposer] finished" in out
    assert "generate" in out
    assert "submit_proposals" in out
    assert "TOOL_RESULT_BODY" not in out
    assert "PRIVATE_COMMAND_BODY" not in out
    assert "HIDDEN_TAIL" not in out


def test_budget_reminder_injected_at_80pct(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([_reply({"action": "inspect_episode", "ref": f"r{i}c0"}) for i in range(5)])
    with pytest.raises(ProposerError, match="max_steps"):
        _agent(model, max_steps=5).run(**_run_args(tmp_path))
    messages_at_step4 = model.calls[3]["messages"]
    assert any(item.get("content") == proposer_mod._BUDGET_REMINDER for item in messages_at_step4)


def test_agent_stops_at_step_budget(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([_reply({"action": "inspect_episode", "ref": "r0c0"})])
    with pytest.raises(ProposerError, match="max_steps"):
        _agent(model, max_steps=1).run(**_run_args(tmp_path))


def test_agent_repairs_protocol_without_consuming_a_step(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    rejected = '{"action":"inspect_episode","ref":"r0c0"} PRIVATE_TAIL'
    model = FakeModel([
        ModelReply(rejected, usage={"t": 1}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}, {"t": 2}),
        _reply(_generate(), {"t": 3}), _reply(_assess(), {"t": 4}), _reply(_submit(), {"t": 5})])
    result = _agent(model, max_steps=12, observer=[].append).run(**args)
    assert len(result.proposals) == 1
    out = capsys.readouterr().out
    assert "protocol repair 1/2 reason=invalid_json" in out
    assert "PRIVATE_TAIL" not in out


def test_agent_fails_closed_after_two_protocol_repairs(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([ModelReply("{} trailing") for _ in range(3)])
    with pytest.raises(ProposerError, match="after 2 repairs"):
        _agent(model).run(**_run_args(tmp_path))
    assert len(model.calls) == 3
    assert FakeTools.instances[0].actions == []


def test_failed_protocol_repair_traceback_hides_rejected_content(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    marker = "PRIVATE_ACTION_MARKER"
    model = FakeModel([_reply({"action": marker}) for _ in range(3)])
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


def test_startup_pack_advertises_new_actions(tmp_path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "proposer.md").write_text("ACTIVE SCIENTIST", encoding="utf-8")
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    args["prompt_dir"] = prompt_dir
    model = FakeModel(_seq_full())
    _agent(model).run(**args)
    call = model.calls[0]
    assert call["system"].startswith("ACTIVE SCIENTIST")
    for action in ("run_research_command", "generate", "assess_candidate",
                   "reject_candidate", "submit_proposals", "abandon_round",
                   "list_findings", "search_findings", "inspect_finding",
                   "search_experiments"):
        assert action in call["system"]
    context = call["messages"][0]["content"]
    assert "notebook" not in context.lower()
    assert "ref: note" not in context.lower()


# --- action parser --------------------------------------------------------

@pytest.mark.parametrize("text", [
    "not json", "[]", '{"action":"unknown"}',
    '{"action":"submit_proposals","proposals":[]}',
    '{"action":"submit_proposals","proposals":[" "]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x"}]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x","research_target":{"mode":"existing"}}]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x","research_target":{"mode":"new"}}]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x","research_target":{"mode":"bogus"}}]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"a","research_target":{"mode":"new","question":"q?"}},{"instruction":"b","research_target":{"mode":"new","question":"q?"}}]}',
    '{"action":"generate","candidate_directions":"a"}',
    '{"action":"assess_candidate","current_judgment":"a"}',
    '{"action":"assess_candidate","current_judgment":"a","research_progress":"advancing"}',
    '{"action":"reject_candidate","what_failed":"r"}',
    '{"action":"abandon_round"}',
    '{"action":"abandon_round","reason":" "}',
    '{"action":"abandon_round","reason":"r","blocking_unknown":5}',
    '{"action":"frame_research","research_question":"q"}',
    '{"action":"assess_research","current_judgment":"j","research_progress":"advancing"}',
    '{"action":"continue_research","next_information_goal":"g","why_it_matters":"w"}',
    '{"action":"reframe_research","what_failed":"r","new_research_question":"q","new_decision_relevant_unknown":"u"}',
    '{"action":"begin_verification","draft_proposal":"d","weakest_premise":"w","verification_question":"vq"}',
    '{"action":"abandon_direction","reason":"r"}',
    '{"action":"search_findings"}',
    '{"action":"inspect_finding"}',
    '{"action":"search_experiments","filters":{}}',
    '{"action":"list_findings","state":"bogus"}',
    '{"action":"run_research_command","command":"","cwd":"source"}',
    '{"action":"run_research_command","command":"true","cwd":"host"}',
])
def test_action_parser_rejects_malformed_contract(text):
    with pytest.raises(ProposerError):
        _parse_action(text, candidates_per_round=1)


def test_action_parser_normalizes_cwd_and_proposals():
    command = _parse_action('{"action":"run_research_command","command":"rg cache"}', 1)
    assert command["cwd"] == "source"
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":[{"instruction":"  Try A  ",'
        '"research_target":{"mode":"new","question":"is A the fix?"},'
        '"evidence_refs":["experiment:r0c0"],"material_difference":"new baseline"}]}', 1)
    prop = submit["proposals"][0]
    assert prop.instruction == "Try A"
    assert prop.evidence_refs == ("experiment:r0c0",)
    assert prop.material_difference == "new baseline"
    assert isinstance(prop.research_target, NewFindingTarget)


def test_action_parser_accepts_existing_and_new_targets():
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":[{"instruction":"A","research_target":{"mode":"existing","finding_id":"F-001"}},'
        '{"instruction":"B","research_target":{"mode":"new","question":"is B the fix?","mechanisms":["cache"],"code_regions":["src/foo"]}}]}', 2)
    assert len(submit["proposals"]) == 2
    assert isinstance(submit["proposals"][0].research_target, ExistingFindingTarget)
    assert submit["proposals"][0].research_target.finding_id == "F-001"
    new_t = submit["proposals"][1].research_target
    assert isinstance(new_t, NewFindingTarget)
    assert new_t.mechanisms == ("cache",)
    assert new_t.code_regions == ("src/foo",)


def test_action_parser_accepts_control_actions():
    gen = _parse_action('{"action":"generate","candidate_directions":"try G6","generative_operations":["G6"]}', 1)
    assert gen["candidate_directions"] == "try G6"
    assess = _parse_action('{"action":"assess_candidate","current_judgment":"j","research_progress":"stalled","weakest_premise":"w","evidence_refs":[]}', 1)
    assert assess["research_progress"].value == "stalled"
    reject = _parse_action('{"action":"reject_candidate","what_failed":"did not help","failed_mechanism":"cache","failed_region":"src/a.cc"}', 1)
    assert reject["failed_mechanism"] == "cache"


def test_weakest_premise_accepts_list():
    assess = _parse_action(
        '{"action":"assess_candidate","current_judgment":"j","research_progress":"advancing",'
        '"weakest_premise":["p1","p2"],"evidence_refs":["source:src/foo.cc"]}', 1)
    assert assess["weakest_premise"] == "p1; p2"


def test_premise_list_rejects_empty_items():
    with pytest.raises(ProposerError):
        _parse_action(
            '{"action":"assess_candidate","current_judgment":"j","research_progress":"advancing",'
            '"weakest_premise":["p1",""],"evidence_refs":[]}', 1)


# --- phase transition table -----------------------------------------------

def test_phase_transition_table():
    from simpleloop.roles.proposer import _validate_phase_transition as v
    assert v(IdeaPhase.GENERATE, "run_research_command") == IdeaPhase.GENERATE
    assert v(IdeaPhase.VALIDATE, "inspect_episode") == IdeaPhase.VALIDATE
    assert v(IdeaPhase.GENERATE, "list_findings") == IdeaPhase.GENERATE
    assert v(IdeaPhase.VALIDATE, "search_experiments") == IdeaPhase.VALIDATE
    assert v(IdeaPhase.GENERATE, "generate") == IdeaPhase.VALIDATE
    assert v(IdeaPhase.VALIDATE, "assess_candidate") == IdeaPhase.COMMIT
    assert v(IdeaPhase.COMMIT, "reject_candidate") == IdeaPhase.GENERATE
    assert v(IdeaPhase.COMMIT, "submit_proposals") is None
    assert v(IdeaPhase.COMMIT, "abandon_round") is None
    for phase, action in [
        (IdeaPhase.GENERATE, "submit_proposals"),
        (IdeaPhase.GENERATE, "assess_candidate"),
        (IdeaPhase.VALIDATE, "submit_proposals"),
        (IdeaPhase.VALIDATE, "generate"),
        (IdeaPhase.COMMIT, "generate"),
        (IdeaPhase.COMMIT, "assess_candidate"),
    ]:
        with pytest.raises(ProposerError, match="not legal in phase"):
            v(phase, action)
