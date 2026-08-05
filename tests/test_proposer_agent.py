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
    VerificationStatus,
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
            return {
                "ok": True,
                "returncode": 0,
                "timed_out": False,
                "truncated": False,
                "output": "TOOL_RESULT_BODY",
            }
        if action["action"] == "inspect_episode":
            return {"ok": True, "result": {
                "experiment_id": action["ref"], "ref": action["ref"],
            }}
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


def _frame(question="Is lookup the real cost source?",
           unknown="whether the inner loop dominates") -> dict:
    return {
        "action": "frame_research",
        "research_question": question,
        "decision_relevant_unknown": unknown,
        "known_evidence": ["the dashboard shows prior cache attempts"],
    }


def _verify_block(status="supported", refs=("experiment:r0c0",)) -> dict:
    return {
        "weakest_premise": "the lookup is on the hot path",
        "status": status,
        "evidence_refs": list(refs),
    }


def _assess(progress="advancing", verification=None,
            draft="Hoist the cache lookup.") -> dict:
    action = {
        "action": "assess_research",
        "current_judgment": "lookup is the likely cost",
        "research_progress": progress,
        "draft_proposal": draft,
    }
    if verification is not None:
        action["verification"] = verification
    return action


def _new_target(question="Replace the cache layout."):
    return {"mode": "new", "question": question}


def _submit(instruction="Replace the cache layout.",
            evidence_refs=None, material_difference=None) -> dict:
    prop = {"instruction": instruction, "research_target": _new_target()}
    if evidence_refs is not None:
        prop["evidence_refs"] = evidence_refs
    if material_difference is not None:
        prop["material_difference"] = material_difference
    return {"action": "submit_proposals", "proposals": [prop]}


def _abandon(reason="No mechanism has direct evidence.",
             blocking_unknown="whether QPDF is still hot") -> dict:
    action = {"action": "abandon_direction", "reason": reason}
    if blocking_unknown is not None:
        action["blocking_unknown"] = blocking_unknown
    return action


_METRICS_SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [],
}


def _memory_service(run_dir: Path) -> MemoryService:
    return MemoryService(run_dir=run_dir, metrics_schema=_METRICS_SCHEMA)


def _seed_history(run_dir: Path, *, proposal="prior attempt", fid=None,
                  objective=100.0) -> None:
    """Write one round into history.jsonl so the proposer wakes with evidence."""
    record = {
        "round": 0,
        "parent_sha": "abc123",
        "selected_candidate": 0,
        "selected_sha": "sha0",
        "candidates": [{
            "candidate": 0,
            "experiment_id": "r0c0",
            "finding_id": fid,
            "proposal": proposal,
            "parent_sha": "abc123",
            "sha": "sha0",
            "status": "COMPLETED",
            "metrics": {"SPEED_MS": objective},
            "changed_paths": ["src/a.cc"],
            "gates": {},
            "gate_passed": True,
            "eligible": True,
            "selected": True,
        }],
    }
    (run_dir / "history.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8",
    )


def _run_args(tmp_path: Path, *, hints=None) -> dict:
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
        "current_round": 1,
        "candidates_per_round": 1,
        "gate_block": "- physics: must pass",
        "prompt_dir": None,
        "hints": hints,
    }


def _agent(model, *, max_steps=12, observer=None):
    return ProposerAgent(
        model=model,
        runtime=object(),
        timeout_seconds=30,
        max_steps=max_steps,
        command_timeout_seconds=5,
        command_output_cap_chars=1000,
        usage_observer=observer,
    )


# --- happy paths ----------------------------------------------------------

def test_happy_path_with_history_evidence_submits(tmp_path, monkeypatch):
    """With prior history, the Scientist may cite a real experiment ref to
    verify, then submit (no decorative tool call needed)."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block())),
        _reply(_submit()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert result.abstained is False
    assert result.trace.get("authority") == "non_authoritative"
    assert result.trace.get("inject_into_future_context") is False
    assert result.deliberation_telemetry["verification_status"] == "supported"


def test_first_round_requires_tool_then_verifies(tmp_path, monkeypatch):
    """First round, no history, no hints: the Scientist must actually
    investigate before it can verify and submit."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply(_assess(verification=_verify_block(refs=("experiment:r0c0",)))),
        _reply(_submit()),
    ])
    result = _agent(model).run(**_run_args(tmp_path))
    assert len(result.proposals) == 1
    assert [a[0]["action"] for a in FakeTools.instances[0].actions] == [
        "inspect_episode",
    ]


def test_submit_accepts_existing_target(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block())),
        _reply({"action": "submit_proposals", "proposals": [{
            "instruction": "Rerun the hoist on the new baseline.",
            "research_target": {"mode": "existing", "finding_id": "F-008"},
        }]}),
    ])
    result = _agent(model).run(**args)
    assert isinstance(result.proposals[0].research_target, ExistingFindingTarget)
    assert result.proposals[0].research_target.finding_id == "F-008"


# --- the four P0 guards ---------------------------------------------------

def test_submit_without_verification_is_repaired(tmp_path, monkeypatch, capsys):
    """submit requires verification_status == supported."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess()),                       # no verification -> DECIDE
        _reply(_submit()),                       # -> needs_verification
        _reply({                                 # go back to Research to verify
            "action": "begin_verification",
            "draft_proposal": "hoist the lookup",
            "weakest_premise": "it is on the hot path",
            "verification_question": "is it innermost?",
        }),
        _reply(_assess(verification=_verify_block())),
        _reply(_submit()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert "reason=needs_verification" in capsys.readouterr().out


def test_verify_invalid_refs_are_rejected(tmp_path, monkeypatch, capsys):
    """Declaring 'supported' with a ref never examined is refused."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block(refs=("experiment:r9c9",)))),
        _reply(_assess(verification=_verify_block(refs=("experiment:r0c0",)))),
        _reply(_submit()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert "reason=unverified_evidence" in capsys.readouterr().out


def test_finding_only_ref_is_insufficient(tmp_path, monkeypatch, capsys):
    """A finding: ref alone cannot support a mechanism (need experiment/source)."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block(refs=("finding:F-001",)))),
        _reply(_assess(verification=_verify_block(refs=("experiment:r0c0",)))),
        _reply(_submit()),
    ])
    _agent(model).run(**args)
    assert "reason=unverified_evidence" in capsys.readouterr().out


def test_first_round_assess_without_evidence_is_repaired(
    tmp_path, monkeypatch, capsys,
):
    """No hint, no history: assess before any tool call is refused."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess()),                          # no evidence basis -> refused
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply(_assess(verification=_verify_block())),
        _reply(_submit()),
    ])
    result = _agent(model).run(**_run_args(tmp_path))
    assert len(result.proposals) == 1
    assert "reason=needs_evidence" in capsys.readouterr().out


def test_hint_provides_evidence_basis(tmp_path, monkeypatch, capsys):
    """A hint counts as decision-relevant evidence: assess is NOT blocked by
    needs_evidence (the hint satisfies evidence_basis). But a cited ref must
    still be real, so a non-existent experiment ref is refused as
    unverified_evidence, not needs_evidence."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path, hints=["the hotspot is the QPDF lookup"])
    model = FakeModel([
        _reply(_frame()),
        # cites an experiment that does not exist (no history) -> unverified,
        # but NOT needs_evidence, because the hint gave an evidence basis.
        _reply(_assess(verification=_verify_block(refs=("experiment:r0c0",)))),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply(_assess(verification=_verify_block(refs=("experiment:r0c0",)))),
        _reply(_submit()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    out = capsys.readouterr().out
    assert "reason=unverified_evidence" in out
    assert "reason=needs_evidence" not in out


def test_repeated_tool_is_repaired(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply(_frame()),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}),  # exact repeat
        _reply({"action": "inspect_episode", "ref": "r1c0"}),
        _reply(_assess(verification=_verify_block(refs=("experiment:r1c0",)))),
        _reply(_submit()),
    ])
    result = _agent(model).run(**_run_args(tmp_path))
    assert len(result.proposals) == 1
    assert "reason=repeated_tool" in capsys.readouterr().out


def test_near_duplicate_repaired_then_passes(tmp_path, monkeypatch, capsys):
    """A proposal identical to a prior experiment must declare a material
    difference and cite a history experiment ref."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"], proposal="hoist the cache lookup")
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block(), draft="hoist the cache lookup")),
        # identical instruction, no justification -> near_duplicate
        _reply(_submit(instruction="hoist the cache lookup")),
        # with material_difference + evidence ref -> passes
        _reply(_submit(
            instruction="hoist the cache lookup",
            evidence_refs=["experiment:r0c0"],
            material_difference="uses the new baseline accepted this round",
        )),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert "reason=near_duplicate" in capsys.readouterr().out


# --- Explore challenge_response guard -------------------------------------

def _seed_stall_history(run_dir: Path, *, rounds=4, objective=100.0) -> None:
    """Seed ``rounds`` history rows that form a no-improvement chain so that
    Explore health marks challenge_required (global_stall). Round 0 is the
    baseline (parent unresolvable → unclassified); rounds 1..N-1 are neutral
    relative to the prior selected candidate."""
    records = []
    parent = "abc123"
    sha = "sha0"
    for r in range(rounds):
        sel = (r == 0)  # only the baseline is selected
        records.append({
            "round": r,
            "parent_sha": parent,
            "selected_candidate": 0,
            "selected_sha": sha,
            "candidates": [{
                "candidate": 0,
                "experiment_id": f"r{r}c0",
                "finding_id": None,
                "proposal": f"neutral variant {r}",
                "parent_sha": parent,
                "sha": sha,
                "status": "COMPLETED",
                "metrics": {"SPEED_MS": objective},
                "changed_paths": ["src/a.cc"],
                "gates": {},
                "gate_passed": True,
                "eligible": True,
                "selected": sel,
            }],
        })
        parent = sha
        sha = f"sha{r + 1}"
    (run_dir / "history.jsonl").write_text(
        "\n".join(json.dumps(rec) for rec in records) + "\n",
        encoding="utf-8",
    )


def _challenge_response(refs=("experiment:r0c0",)) -> dict:
    return {
        "triggered_policy": "global_stall",
        "stalled_family": "src/a.cc::lookup",
        "what_was_exhausted": "repeated neutral variants of the lookup",
        "null_hypothesis": "this variant changes the cost no more than noise",
        "why_this_is_not_same_family_variant": "it removes the lookup entirely",
        "why_worth_one_more_experiment": "the source shows a now-dead branch",
        "evidence_refs": list(refs),
    }


def _submit_with_challenge(instruction="Remove the dead lookup branch.",
                            challenge=None) -> dict:
    action = _submit(instruction=instruction, evidence_refs=["experiment:r0c0"])
    action["challenge_response"] = challenge or _challenge_response()
    return action


def test_challenge_required_submit_without_response_is_repaired(
    tmp_path, monkeypatch, capsys,
):
    """Under active Explore challenge, submit_proposals without a
    challenge_response is repaired with reason=challenge_response_required."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_stall_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block(refs=("experiment:r0c0",)))),
        # submit without challenge_response -> repaired
        _reply(_submit()),
        # submit with a valid challenge_response -> passes
        _reply(_submit_with_challenge()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    out = capsys.readouterr().out
    assert "reason=challenge_response_required" in out
    tel = result.deliberation_telemetry
    assert tel["explore_challenge_required"] is True
    assert tel["challenge_response_provided"] is True
    assert result.trace["explore"]["challenge_required"] is True
    assert result.trace["explore"]["challenge_reasons"]


def test_challenge_response_invalid_evidence_is_repaired(
    tmp_path, monkeypatch, capsys,
):
    """A challenge_response whose evidence_refs do not resolve to real evidence
    examined this round is also repaired."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_stall_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block(refs=("experiment:r0c0",)))),
        # challenge_response points at a non-existent experiment ref
        _reply(_submit_with_challenge(
            challenge=_challenge_response(refs=("experiment:r99c9",)),
        )),
        _reply(_submit_with_challenge()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert "reason=challenge_response_required" in capsys.readouterr().out


def test_reframe_under_challenge_needs_no_response(tmp_path, monkeypatch, capsys):
    """reframe_research and abandon_direction are valid escapes from a
    challenge and never need a challenge_response — they must not be blocked.

    (Note: challenge_required reflects the Ledger at wakeup and is not cleared
    by a round-local reframe, so a *submit* after reframe would still need a
    challenge_response. This test instead exercises the reframe→abandon path,
    which carries no challenge obligation at all.)"""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_stall_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(progress="stalled")),
        _reply({
            "action": "reframe_research",
            "what_failed": "the lookup direction keeps stalling",
            "new_research_question": "Is the cost in the branch predictor?",
            "new_decision_relevant_unknown": "branch density",
        }),
        _reply(_frame(
            question="Is the cost in the branch predictor?",
            unknown="branch density",
        )),
        _reply(_assess(progress="stalled")),
        _reply(_abandon()),
    ])
    result = _agent(model).run(**args)
    assert result.abstained is True
    # Neither reframe nor abandon triggered a challenge repair.
    assert "reason=challenge_response_required" not in capsys.readouterr().out


def test_no_challenge_means_no_response_required(tmp_path, monkeypatch):
    """A healthy single-improvement history has no challenge_required, so a
    plain submit passes with no challenge repair and telemetry reflects it."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"], objective=100.0)  # single baseline round
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block(refs=("experiment:r0c0",)))),
        _reply(_submit()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    tel = result.deliberation_telemetry
    assert tel["explore_challenge_required"] is False
    assert tel["challenge_response_provided"] is False


def test_parse_challenge_response_round_trips_and_rejects_unknown_keys():
    action = {
        "action": "submit_proposals",
        "proposals": [{"instruction": "x", "research_target": _new_target()}],
        "challenge_response": _challenge_response(),
    }
    parsed = _parse_action(json.dumps(action), candidates_per_round=1)
    cr = parsed["challenge_response"]
    assert cr["null_hypothesis"] == "this variant changes the cost no more than noise"
    assert cr["evidence_refs"] == ["experiment:r0c0"]

    # unknown key -> ProposerError
    bad = {**action, "challenge_response": {**_challenge_response(), "bogus": 1}}
    with pytest.raises(ProposerError):
        _parse_action(json.dumps(bad), candidates_per_round=1)

    # empty required text field -> ProposerError
    bad2 = {**action, "challenge_response": {
        **_challenge_response(), "null_hypothesis": "  ",
    }}
    with pytest.raises(ProposerError):
        _parse_action(json.dumps(bad2), candidates_per_round=1)


# --- control flow ---------------------------------------------------------

def test_abandon_direction_terminates_with_zero_proposals(
    tmp_path, monkeypatch, capsys,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(progress="stalled")),
        _reply(_abandon()),
    ])
    result = _agent(model).run(**args)
    assert result.abstained is True
    assert result.proposals == []
    assert result.abstain_reason == "No mechanism has direct evidence."
    assert result.deliberation_telemetry["abandoned"] is True
    assert "[proposer] abstained" in capsys.readouterr().out


def test_reframe_resets_verification(tmp_path, monkeypatch, capsys):
    """reframe resets verification_status, so a submit after reframe without
    re-verifying is refused even though it was supported before."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block())),    # supported -> DECIDE
        _reply({                                            # reframe -> FRAME
            "action": "reframe_research",
            "what_failed": "lookup isn't the cost",
            "new_research_question": "is allocation the cost?",
            "new_decision_relevant_unknown": "allocation hot path",
        }),
        _reply(_frame(question="is allocation the cost?",
                      unknown="allocation hot path")),     # -> RESEARCH
        _reply(_assess()),                                 # no verify -> DECIDE
        _reply(_submit()),                                 # -> needs_verification
        _reply({"action": "continue_research",
                "next_information_goal": "read alloc loop",
                "why_it_matters": "confirm hotspot"}),
        _reply(_assess(verification=_verify_block())),
        _reply(_submit()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert "reason=needs_verification" in capsys.readouterr().out


def test_begin_verification_does_not_self_certify(tmp_path, monkeypatch, capsys):
    """begin_verification sets needs_evidence, NOT supported; a later submit
    is refused until an assess carries a supported, evidence-backed block."""
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply({
            "action": "begin_verification",
            "draft_proposal": "hoist the lookup",
            "weakest_premise": "it is on the hot path",
            "verification_question": "is it innermost?",
        }),                                              # -> RESEARCH, needs_evidence
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply(_assess()),                               # no verify block -> DECIDE
        _reply(_submit()),                               # -> needs_verification
        _reply({"action": "continue_research",
                "next_information_goal": "read loop",
                "why_it_matters": "confirm"}),
        _reply(_assess(verification=_verify_block())),
        _reply(_submit()),
    ])
    result = _agent(model).run(**args)
    assert len(result.proposals) == 1
    assert "reason=needs_verification" in capsys.readouterr().out


def test_state_header_is_injected(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block())),
        _reply(_submit()),
    ])
    _agent(model).run(**args)
    # After frame_research the runtime injects a compact working-state note.
    second_call_messages = model.calls[1]["messages"]
    assert any(
        "Working state" in m.get("content", "")
        for m in second_call_messages if m.get("role") == "user"
    )


# --- logging / budget / protocol mechanics --------------------------------

def test_agent_prints_safe_action_summaries(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    long_command = "printf PRIVATE_COMMAND_BODY\n" + ("x" * 180) + "HIDDEN_TAIL"
    model = FakeModel([
        _reply({"action": "run_research_command", "command": long_command,
                "cwd": "source"}),
        _reply(_frame()),
        _reply(_assess(verification=_verify_block())),
        _reply(_submit()),
    ])
    _agent(model, max_steps=12).run(**args)
    out = capsys.readouterr().out
    assert "[proposer] started" in out
    assert "[proposer] finished" in out
    assert "frame_research" in out
    assert "submit_proposals" in out
    assert "TOOL_RESULT_BODY" not in out
    assert "PRIVATE_COMMAND_BODY" not in out
    assert "HIDDEN_TAIL" not in out


def test_budget_reminder_injected_at_80pct(tmp_path, monkeypatch):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([
        _reply({"action": "inspect_episode", "ref": "r0c0"}),
        _reply({"action": "inspect_episode", "ref": "r1c0"}),
        _reply({"action": "inspect_episode", "ref": "r2c0"}),
        _reply({"action": "inspect_episode", "ref": "r3c0"}),
        _reply({"action": "inspect_episode", "ref": "r4c0"}),
    ])
    with pytest.raises(ProposerError, match="max_steps"):
        _agent(model, max_steps=5).run(**_run_args(tmp_path))
    messages_at_step4 = model.calls[3]["messages"]
    assert any(
        item.get("content") == proposer_mod._BUDGET_REMINDER
        for item in messages_at_step4
    )


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
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    rejected = '{"action":"inspect_episode","ref":"r0c0"} PRIVATE_TAIL'
    model = FakeModel([
        ModelReply(rejected, usage={"t": 1}),
        _reply(_frame(), ),  # not used; frame first
        _reply(_frame()),
        _reply(_assess(verification=_verify_block())),
        _reply(_submit()),
    ])
    # The first reply is malformed; repair happens, then the agent proceeds.
    # Rebuild with a clean sequence to keep the test focused:
    model = FakeModel([
        ModelReply(rejected, usage={"t": 1}),
        _reply({"action": "inspect_episode", "ref": "r0c0"}, {"t": 2}),
        _reply(_frame(), {"t": 3}),
        _reply(_assess(verification=_verify_block()), {"t": 4}),
        _reply(_submit(), {"t": 5}),
    ])
    result = _agent(
        model, max_steps=12, observer=[].append,
    ).run(**args)
    assert len(result.proposals) == 1
    out = capsys.readouterr().out
    assert "protocol repair 1/2 reason=invalid_json" in out
    assert "PRIVATE_TAIL" not in out


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


def test_startup_pack_advertises_new_actions(tmp_path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "proposer.md").write_text("ACTIVE SCIENTIST", encoding="utf-8")
    args = _run_args(tmp_path)
    _seed_history(args["run_dir"])
    args["prompt_dir"] = prompt_dir
    model = FakeModel([
        _reply(_frame()),
        _reply(_assess(verification=_verify_block())),
        _reply(_submit()),
    ])
    _agent(model).run(**args)
    call = model.calls[0]
    assert call["system"].startswith("ACTIVE SCIENTIST")
    for action in (
        "run_research_command", "frame_research", "assess_research",
        "continue_research", "reframe_research", "begin_verification",
        "submit_proposals", "abandon_direction", "list_findings",
        "search_findings", "inspect_finding", "search_experiments",
    ):
        assert action in call["system"]
    context = call["messages"][0]["content"]
    assert "notebook" not in context.lower()
    assert "ref: note" not in context.lower()


# --- action parser --------------------------------------------------------

@pytest.mark.parametrize("text", [
    "not json",
    "[]",
    '{"action":"unknown"}',
    '{"action":"submit_proposals","proposals":[]}',
    '{"action":"submit_proposals","proposals":[" "]}',
    '{"action":"submit_proposals","proposals":[{"instruction":"x"}]}',
    '{"action":"submit_proposals","proposals":[{'
    '"instruction":"x","research_target":{"mode":"existing"}}]}',
    '{"action":"submit_proposals","proposals":[{'
    '"instruction":"x","research_target":{"mode":"new"}}]}',
    '{"action":"submit_proposals","proposals":[{'
    '"instruction":"x","research_target":{"mode":"bogus"}}]}',
    # over budget
    '{"action":"submit_proposals","proposals":['
    '{"instruction":"a","research_target":{"mode":"new","question":"q?"}},'
    '{"instruction":"b","research_target":{"mode":"new","question":"q?"}}]}',
    # control actions missing required fields
    '{"action":"frame_research","research_question":"a"}',
    '{"action":"assess_research","current_judgment":"a"}',
    '{"action":"assess_research","current_judgment":"a",'
    '"research_progress":"advancing","verification":{"status":"supported"}}',
    '{"action":"continue_research","next_information_goal":"g"}',
    '{"action":"reframe_research","what_failed":"r"}',
    '{"action":"begin_verification","draft_proposal":"d"}',
    '{"action":"abandon_direction"}',
    '{"action":"abandon_direction","reason":" "}',
    '{"action":"abandon_direction","reason":"r","blocking_unknown":5}',
    # old action names are now invalid
    '{"action":"conclude_research","findings":["a"]}',
    '{"action":"continue_investigation","gap":"g","next_question":"q"}',
    # tool actions missing required fields
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
    command = _parse_action(
        '{"action":"run_research_command","command":"rg cache"}', 1,
    )
    assert command["cwd"] == "source"
    submit = _parse_action(
        '{"action":"submit_proposals","proposals":[{'
        '"instruction":"  Try A  ",'
        '"research_target":{"mode":"new","question":"is A the fix?"},'
        '"evidence_refs":["experiment:r0c0"],'
        '"material_difference":"new baseline"}]}',
        1,
    )
    prop = submit["proposals"][0]
    assert prop.instruction == "Try A"
    assert prop.evidence_refs == ("experiment:r0c0",)
    assert prop.material_difference == "new baseline"
    assert isinstance(prop.research_target, NewFindingTarget)


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
    assert isinstance(submit["proposals"][0].research_target, ExistingFindingTarget)
    assert submit["proposals"][0].research_target.finding_id == "F-001"
    new_t = submit["proposals"][1].research_target
    assert isinstance(new_t, NewFindingTarget)
    assert new_t.mechanisms == ("cache",)
    assert new_t.code_regions == ("src/foo",)


def test_action_parser_accepts_control_actions():
    frame = _parse_action(
        '{"action":"frame_research","research_question":"q?",'
        '"decision_relevant_unknown":"u?"}', 1,
    )
    assert frame["research_question"] == "q?"
    assess = _parse_action(
        '{"action":"assess_research","current_judgment":"j",'
        '"research_progress":"stalled","verification":{"status":"failed",'
        '"evidence_refs":[]}}', 1,
    )
    assert assess["research_progress"].value == "stalled"
    assert assess["verification"]["status"] == "failed"
    begin = _parse_action(
        '{"action":"begin_verification","draft_proposal":"d",'
        '"weakest_premise":"w","verification_question":"vq"}', 1,
    )
    assert begin["weakest_premise"] == "w"


# --- phase transition table -----------------------------------------------

def test_phase_transition_table():
    from simpleloop.roles.proposer import _validate_phase_transition as v

    # Research tools never change phase.
    assert v(ResearchPhase.FRAME, "run_research_command") == ResearchPhase.FRAME
    assert v(ResearchPhase.RESEARCH, "inspect_episode") == ResearchPhase.RESEARCH
    assert v(ResearchPhase.FRAME, "list_findings") == ResearchPhase.FRAME
    assert v(ResearchPhase.RESEARCH, "search_experiments") == ResearchPhase.RESEARCH
    # Control transitions.
    assert v(ResearchPhase.FRAME, "frame_research") == ResearchPhase.RESEARCH
    assert v(ResearchPhase.RESEARCH, "assess_research") == ResearchPhase.DECIDE
    assert v(ResearchPhase.DECIDE, "continue_research") == ResearchPhase.RESEARCH
    assert v(ResearchPhase.DECIDE, "reframe_research") == ResearchPhase.FRAME
    assert v(ResearchPhase.DECIDE, "begin_verification") == ResearchPhase.RESEARCH
    assert v(ResearchPhase.DECIDE, "submit_proposals") is None
    assert v(ResearchPhase.DECIDE, "abandon_direction") is None
    # Illegal.
    for phase, action in [
        (ResearchPhase.FRAME, "submit_proposals"),
        (ResearchPhase.FRAME, "assess_research"),
        (ResearchPhase.RESEARCH, "submit_proposals"),
        (ResearchPhase.RESEARCH, "frame_research"),
        (ResearchPhase.DECIDE, "frame_research"),
        (ResearchPhase.DECIDE, "assess_research"),
    ]:
        with pytest.raises(ProposerError, match="not legal in phase"):
            v(phase, action)
