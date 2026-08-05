from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop.memory import MemoryService
from simpleloop.roles.orchestrator import ProposerOrchestrator, _select_mode, _Mode
from simpleloop.roles.hypothesis import HypothesisCard
from simpleloop.roles.model import ModelReply
from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles import generator as generator_mod


_METRICS_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True}}


class FakeModel:
    """Returns canned ModelReply objects in order."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeTools:
    """Fake research tools for branch research."""
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.actions = []
        self.__class__.instances.append(self)

    def execute(self, action, *, deadline):
        self.actions.append(action)
        if action["action"] == "run_research_command":
            return {"ok": True, "returncode": 0, "timed_out": False,
                    "output": "src/foo.cc:42:hit"}
        if action["action"] == "inspect_episode":
            return {"ok": True, "result": {"experiment_id": action["ref"]}}
        if action["action"] == "list_findings":
            return {"ok": True, "result": []}
        if action["action"] == "search_findings":
            return {"ok": True, "result": []}
        if action["action"] == "inspect_finding":
            return {"ok": True, "result": {"id": action["finding_id"]}}
        if action["action"] == "search_experiments":
            return {"ok": True, "result": {"relevant": [], "contrasting": [],
                                           "diverse": []}}
        raise AssertionError(f"unexpected: {action['action']}")


def _gen_response(cards):
    return ModelReply(json.dumps({"hypotheses": cards}))


def _card_json(op="G6", region="src/foo.cc", mech="getter",
               interv="cache", slot="guided"):
    return {
        "generative_op": op, "region": region, "mechanism": mech,
        "intervention_family": interv, "why_plausible": "w",
        "critical_unknown": "u", "slot": slot,
    }


def _assess_action():
    return {
        "action": "assess_candidate",
        "current_judgment": "mechanism confirmed",
        "research_progress": "advancing",
        "evidence_refs": ["source:src/foo.cc"],
    }


def _submit_action():
    return {
        "action": "submit_proposals",
        "proposals": [{
            "instruction": "Cache invariant LPMT constants",
            "research_target": {
                "mode": "new", "question": "Can caching LPMT constants speed up FCN?",
                "mechanisms": ["cache-locality"],
                "code_regions": ["src/foo.cc"],
            },
        }],
    }


def _abandon_action():
    return {"action": "abandon_round", "reason": "mechanism not found"}


def _run_args(tmp_path):
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    run_dir = tmp_path / "run"
    for p in (source, repo, run_dir):
        p.mkdir(exist_ok=True)
    return {"goal": "make it faster", "editable": ["src/**"],
            "frozen": ["tests/**"], "memory_service": MemoryService(
                run_dir=run_dir, metrics_schema=_METRICS_SCHEMA),
            "base_sha": "abc", "source_path": source, "repo_path": repo,
            "run_dir": run_dir, "current_round": 0,
            "candidates_per_round": 2, "gate_block": "- gate: pass",
            "prompt_dir": None}


def _orchestrator(model, *, max_steps=20, **kw):
    return ProposerOrchestrator(
        model=model, runtime=object(), timeout_seconds=60,
        max_steps=max_steps, command_timeout_seconds=5,
        command_output_cap_chars=1000,
        hypothesis_count=kw.get("hypothesis_count", 4),
        branch_count=kw.get("branch_count", 3),
        frame_free_ratio=kw.get("frame_free_ratio", 0.33),
    )


class TestSelectMode:
    def test_first_round_depth_first(self):
        m = _select_mode(first_round=True, n_experiments=0,
                         max_steps=50, hypothesis_count=8, branch_count=3)
        assert m.label == "depth-first"
        assert m.n_hypotheses <= 3

    def test_later_round_breadth_first(self):
        m = _select_mode(first_round=False, n_experiments=5,
                         max_steps=50, hypothesis_count=8, branch_count=3)
        assert m.label == "breadth-first"
        assert m.n_hypotheses == 8

    def test_depth_first_gives_more_steps_per_branch(self):
        depth = _select_mode(first_round=True, n_experiments=0,
                             max_steps=50, hypothesis_count=8, branch_count=3)
        breadth = _select_mode(first_round=False, n_experiments=5,
                               max_steps=50, hypothesis_count=8, branch_count=3)
        assert depth.max_branch_steps > breadth.max_branch_steps


class TestOrchestratorRun:
    def test_produces_proposal_from_one_branch(self, tmp_path, monkeypatch):
        """Generator produces 2 cards, both enter branches, one submits."""
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)

        args = _run_args(tmp_path)
        # Generator response: 2 cards
        gen = _gen_response([
            _card_json(mech="getter", interv="cache"),
            _card_json(mech="search", interv="replace"),
        ])
        # Branch 1: research → assess → submit
        branch1 = [
            ModelReply(json.dumps({
                "action": "run_research_command",
                "command": "grep -r getter src/", "cwd": "source",
            })),
            ModelReply(json.dumps(_assess_action())),
            ModelReply(json.dumps(_submit_action())),
        ]
        # Branch 2: research → assess → abandon
        branch2 = [
            ModelReply(json.dumps({
                "action": "run_research_command",
                "command": "grep -r search src/", "cwd": "source",
            })),
            ModelReply(json.dumps(_assess_action())),
            ModelReply(json.dumps(_abandon_action())),
        ]
        model = FakeModel([gen] + branch1 + branch2)
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2,
                             branch_count=2)
        result = orch.run(**args)
        assert len(result.proposals) == 1
        assert not result.abstained
        assert result.deliberation_telemetry["mode"] == "depth-first"

    def test_all_branches_abandon_yields_abstain(self, tmp_path, monkeypatch):
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path)
        gen = _gen_response([_card_json()])
        branch = [
            ModelReply(json.dumps({
                "action": "run_research_command",
                "command": "grep -r getter src/", "cwd": "source",
            })),
            ModelReply(json.dumps(_assess_action())),
            ModelReply(json.dumps(_abandon_action())),
        ]
        model = FakeModel([gen] + branch)
        orch = _orchestrator(model, max_steps=20, hypothesis_count=1,
                             branch_count=1)
        result = orch.run(**args)
        assert result.abstained
        assert len(result.proposals) == 0

    def test_no_cards_after_dedup_abstains(self, tmp_path, monkeypatch):
        """If all cards collapse to one signature, we still proceed (per_bin=1
        keeps one). Abstain only if generator produces nothing usable."""
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path)
        # All empty structural fields → parser drops them → ModelError
        gen = _gen_response([{
            "generative_op": "G6", "region": "", "mechanism": "",
            "intervention_family": "", "why_plausible": "w",
            "critical_unknown": "u", "slot": "guided",
        }])
        model = FakeModel([gen])
        orch = _orchestrator(model, max_steps=20, hypothesis_count=1,
                             branch_count=1)
        from simpleloop.roles.model import ModelError
        with pytest.raises(ModelError):
            orch.run(**args)

    def test_all_cards_enter_branch_no_probe_gate(self, tmp_path, monkeypatch):
        """Without a probe gate, every dedup'd card enters a branch — even
        cards whose mechanism might not exist. The branch itself is the filter:
        it researches, then either submits or abandons."""
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path)
        # Two cards with different signatures → both enter branches.
        gen = _gen_response([
            _card_json(mech="getter", interv="cache", slot="guided"),
            _card_json(mech="search", interv="replace", slot="free"),
        ])
        # Both branches do a research command first (establish evidence_basis),
        # then assess + submit.
        branch1 = [
            ModelReply(json.dumps({
                "action": "run_research_command",
                "command": "grep -r getter src/", "cwd": "source",
            })),
            ModelReply(json.dumps(_assess_action())),
            ModelReply(json.dumps(_submit_action())),
        ]
        branch2 = [
            ModelReply(json.dumps({
                "action": "run_research_command",
                "command": "grep -r search src/", "cwd": "source",
            })),
            ModelReply(json.dumps(_assess_action())),
            ModelReply(json.dumps(_submit_action())),
        ]
        model = FakeModel([gen] + branch1 + branch2)
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2,
                             branch_count=2)
        result = orch.run(**args)
        # Both branches produced a proposal.
        assert len(result.proposals) == 2
        assert not result.abstained
