from __future__ import annotations

import json
import traceback

import pytest

from simpleloop.memory import MemoryService
from simpleloop.memory.models import ExistingFindingTarget, NewFindingTarget
from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles.hypothesis import HypothesisCard
from simpleloop.roles.model import ModelError, ModelReply
from simpleloop.roles.proposer import ProposerAgent, ProposerError, _parse_action


_METRICS_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
                   "gates": []}


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
        self.actions.append(action)
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


def _card():
    return HypothesisCard(
        generative_op="G6", region="src/foo.cc", mechanism="getter",
        intervention_family="cache", why_plausible="getter overhead",
        critical_unknown="whether getters are inlined")


def _new_target(question="Replace the cache layout.", mechanisms=None,
                code_regions=None):
    t = {"mode": "new", "question": question}
    if mechanisms is not None:
        t["mechanisms"] = mechanisms
    if code_regions is not None:
        t["code_regions"] = code_regions
    return t


def _submit(instruction="Cache invariant constants.",
            evidence_refs=None, material_difference=None):
    prop = {"instruction": instruction,
            "research_target": _new_target()}
    if evidence_refs is not None:
        prop["evidence_refs"] = evidence_refs
    if material_difference is not None:
        prop["material_difference"] = material_difference
    return {"action": "submit_proposals", "proposals": [prop]}


def _block(reason_kind="false_claim", explanation="the fn is absent",
           refs=("source:src/foo.cc",)):
    return {"action": "block", "reason_kind": reason_kind,
            "explanation": explanation, "evidence_refs": list(refs)}


def _branch_args(tmp_path, *, frozen=None):
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    run_dir = tmp_path / "run"
    for path in (source, repo, run_dir):
        path.mkdir(exist_ok=True)
    # A real source file so a block's source: ref can resolve.
    (source / "src").mkdir(exist_ok=True)
    (source / "src" / "foo.cc").write_text("// target\n", encoding="utf-8")
    return {"goal": "make it faster", "editable": ["src/**"],
            "frozen": frozen if frozen is not None else ["tests/**"],
            "memory_service": MemoryService(
                run_dir=run_dir, metrics_schema=_METRICS_SCHEMA),
            "base_sha": "abc123", "source_path": source, "repo_path": repo,
            "run_dir": run_dir, "current_round": 1,
            "gate_block": "- physics: must pass", "prompt_dir": None}


def _agent(model, *, max_steps=12, observer=None):
    return ProposerAgent(model=model, runtime=object(), timeout_seconds=30,
                         max_steps=max_steps, command_timeout_seconds=5,
                         command_output_cap_chars=1000, usage_observer=observer)


# --- action parser --------------------------------------------------------

@pytest.mark.parametrize("text", [
    "not json", "[]", '{"action":"unknown"}',
    # submit_proposals malformed
    '{"action":"submit_proposals","proposals":[]}',
    '{"action":"submit_proposals","proposals":[" "]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x"}]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x","research_target":{"mode":"existing"}}]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x","research_target":{"mode":"new"}}]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x","research_target":{"mode":"bogus"}}]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"a","research_target":{"mode":"new","question":"q?"}},{"instruction":"b","research_target":{"mode":"new","question":"q?"}}]}',
    # block malformed
    '{"action":"block","reason_kind":"too_hard","explanation":"y","evidence_refs":["source:a"]}',
    '{"action":"block","reason_kind":"false_claim","explanation":" ","evidence_refs":["source:a"]}',
    '{"action":"block","reason_kind":"false_claim","explanation":"y","evidence_refs":[]}',
    '{"action":"block","reason_kind":"false_claim","explanation":"y"}',
    # removed control actions are now unknown
    '{"action":"generate","candidate_directions":"a"}',
    '{"action":"assess_candidate","current_judgment":"a"}',
    '{"action":"reject_candidate","what_failed":"r"}',
    '{"action":"abandon_round","reason":"r"}',
    # tools malformed
    '{"action":"search_findings"}',
    '{"action":"inspect_finding"}',
    '{"action":"list_findings","state":"bogus"}',
    '{"action":"run_research_command","command":"","cwd":"source"}',
    '{"action":"run_research_command","command":"true","cwd":"host"}',
])
def test_action_parser_rejects_malformed_contract(text):
    with pytest.raises(ProposerError):
        _parse_action(text, candidates_per_round=1)


def test_action_parser_normalizes_cwd_and_proposals():
    command = _parse_action(
        '{"action":"run_research_command","command":"rg cache"}', 1)
    assert command["cwd"] == "source"
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":[{"instruction":"  Try A  ",'
        '"research_target":{"mode":"new","question":"is A the fix?"},'
        '"evidence_refs":["experiment:r0c0"],"material_difference":"new baseline"}]}',
        1)
    prop = submit["proposals"][0]
    assert prop.instruction == "Try A"
    assert prop.evidence_refs == ("experiment:r0c0",)
    assert prop.material_difference == "new baseline"
    assert isinstance(prop.research_target, NewFindingTarget)


def test_action_parser_accepts_existing_and_new_targets():
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":['
        '{"instruction":"A","research_target":{"mode":"existing","finding_id":"F-001"}},'
        '{"instruction":"B","research_target":{"mode":"new","question":"is B the fix?","mechanisms":["cache"],"code_regions":["src/foo"]}}]}',
        2)
    assert len(submit["proposals"]) == 2
    assert isinstance(submit["proposals"][0].research_target, ExistingFindingTarget)
    assert submit["proposals"][0].research_target.finding_id == "F-001"
    new_t = submit["proposals"][1].research_target
    assert isinstance(new_t, NewFindingTarget)
    assert new_t.mechanisms == ("cache",)
    assert new_t.code_regions == ("src/foo",)


def test_action_parser_accepts_block():
    block = _parse_action(
        '{"action":"block","reason_kind":"contradiction",'
        '"explanation":"claims are inconsistent",'
        '"evidence_refs":["source:src/a.cc:Fn","source:src/b.cc"]}', 1)
    assert block["reason_kind"] == "contradiction"
    assert block["explanation"] == "claims are inconsistent"
    assert block["evidence_refs"] == ("source:src/a.cc:Fn", "source:src/b.cc")


def test_submit_no_longer_carries_challenge_response():
    """The challenge_response block is gone; submitting it is an unknown key."""
    action = {"action": "submit_proposals", "proposals": [
        {"instruction": "x", "research_target": _new_target()}],
        "challenge_response": {"null_hypothesis": "x"}}
    with pytest.raises(ProposerError):
        _parse_action(json.dumps(action), candidates_per_round=1)


# --- research_branch behavior --------------------------------------------

def test_submit_always_allowed_no_evidence_gate(tmp_path, monkeypatch):
    """No merit/evidence gate on submit: a branch may submit immediately,
    even with zero tool calls. The Harness is the only merit judge."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([_reply(_submit())])
    result = _agent(model).research_branch(
        hypothesis=_card(), **_branch_args(tmp_path), max_steps=5)
    assert result.outcome == "submit"
    assert result.proposal is not None
    assert result.enrichment_partial is False


def test_budget_exhaust_submits_partial_not_abandon(tmp_path, monkeypatch):
    """Budget exhaustion submits a partial proposal — it never abandons an
    idea. The orchestrator's tool_calls tie-break deprioritizes it."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r1c0"}),
    ])
    result = _agent(model).research_branch(
        hypothesis=_card(), **_branch_args(tmp_path), max_steps=2)
    assert result.outcome == "submit"
    assert result.enrichment_partial is True
    assert result.proposal is not None
    assert "Enrichment incomplete" in result.proposal.instruction
    assert result.deliberation_telemetry["source_reads"] == 0


def test_block_requires_source_evidence(tmp_path, monkeypatch, capsys):
    """A block without a resolving source: ref is repaired, not accepted."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    bad_block = _block(refs=("source:src/missing.cc",))  # parses, but unresolved
    model = FakeModel([_reply(bad_block), _reply(_submit())])
    result = _agent(model).research_branch(
        hypothesis=_card(), **_branch_args(tmp_path), max_steps=5)
    assert result.outcome == "submit"  # block was rejected, then submit
    assert "reason=block_needs_source" in capsys.readouterr().out


def test_block_succeeds_with_valid_source_ref(tmp_path, monkeypatch):
    """After a real source read, a block citing that source is accepted."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply({"action": "run_research_command", "command": "grep x src/",
                "cwd": "source"}),
        _reply(_block(explanation="the getter is absent")),
    ])
    result = _agent(model).research_branch(
        hypothesis=_card(), **_branch_args(tmp_path), max_steps=5)
    assert result.outcome == "block"
    assert result.reason_kind == "false_claim"
    assert "source:src/foo.cc" in result.block_evidence_refs
    assert result.proposal is None


def test_repeated_tool_is_repaired(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),  # exact repeat
        _reply(_submit()),
    ])
    result = _agent(model).research_branch(
        hypothesis=_card(), **_branch_args(tmp_path), max_steps=8)
    assert result.outcome == "submit"
    assert "reason=repeated_tool" in capsys.readouterr().out


def test_no_taboo_two_same_family_submits_pass(tmp_path, monkeypatch):
    """There is no taboo set: submitting in a family is never refused on
    judgment. (Two branches in the same family both submit — exercised at the
    orchestrator level; here we confirm a single submit needs no novelty.)"""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([_reply(_submit())])
    result = _agent(model).research_branch(
        hypothesis=_card(), **_branch_args(tmp_path), max_steps=5)
    assert result.outcome == "submit"
    assert result.deliberation_telemetry.get("taboo_count", 0) == 0


# --- protocol repair mechanics (salvaged) --------------------------------

def test_repairs_protocol_without_consuming_a_step(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    rejected = '{"action":"inspect_episode","ref":"r0c0"} PRIVATE_TAIL'
    model = FakeModel([
        ModelReply(rejected, usage={"t": 1}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}, {"t": 2}),
        _reply(_submit(), {"t": 3}),
    ])
    result = _agent(model, max_steps=8, observer=[].append).research_branch(
        hypothesis=_card(), **_branch_args(tmp_path), max_steps=8)
    assert result.outcome == "submit"
    out = capsys.readouterr().out
    assert "protocol repair 1/2 reason=invalid_json" in out
    assert "PRIVATE_TAIL" not in out


def test_fails_closed_after_two_protocol_repairs(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([ModelReply("{} trailing") for _ in range(3)])
    with pytest.raises(ProposerError, match="after 2 repairs"):
        _agent(model).research_branch(
            hypothesis=_card(), **_branch_args(tmp_path), max_steps=8)
    assert FakeTools.instances[0].actions == []


def test_failed_protocol_repair_traceback_hides_rejected_content(
        tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    marker = "PRIVATE_ACTION_MARKER"
    model = FakeModel([_reply({"action": marker}) for _ in range(3)])
    with pytest.raises(ProposerError) as exc_info:
        _agent(model).research_branch(
            hypothesis=_card(), **_branch_args(tmp_path), max_steps=8)
    rendered = "".join(traceback.format_exception(exc_info.value))
    assert marker not in rendered


def test_does_not_repair_model_transport_errors(tmp_path):
    class RaisingModel:
        def __init__(self):
            self.calls = 0

        def complete(self, **_kwargs):
            self.calls += 1
            raise ModelError("transport failed")

    with pytest.raises(ModelError, match="transport failed"):
        _agent(RaisingModel()).research_branch(
            hypothesis=_card(), **_branch_args(tmp_path), max_steps=8)
