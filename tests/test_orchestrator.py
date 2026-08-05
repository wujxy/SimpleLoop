from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from simpleloop.memory import MemoryService
from simpleloop.roles.orchestrator import ProposerOrchestrator, _select_mode, _Mode
from simpleloop.roles.hypothesis import HypothesisCard
from simpleloop.roles.model import ModelReply
from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles import generator as generator_mod


_METRICS_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True}}


class FakeModel:
    """Thread-safe fake for the always-parallel orchestrator.

    The generator runs once, serially, before any branch, so the first
    ``complete()`` call returns ``gen``. Each later call belongs to a branch and
    is routed to that branch's own response list, keyed by the hypothesis
    mechanism embedded in the branch's intro message (``mechanism: <m>``). That
    keeps each branch's scripted action sequence deterministic no matter how the
    pool interleaves the branches.
    """

    def __init__(self, gen, by_mechanism=None):
        self.gen = gen
        self.by_mechanism = by_mechanism or {}
        self._cursors = {}
        self._gen_served = False
        self._lock = threading.Lock()
        self.calls = []

    def complete(self, *, system, messages, timeout_seconds):
        with self._lock:
            self.calls.append(messages)
            if not self._gen_served:
                self._gen_served = True
                return self.gen
            mech = _mechanism_in(messages)
            i = self._cursors.get(mech, 0)
            self._cursors[mech] = i + 1
            return self.by_mechanism[mech][i]


def _mechanism_in(messages):
    """Pull the hypothesis mechanism out of a branch's injected direction."""
    for msg in messages:
        text = msg.get("content", "")
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("mechanism:"):
                return s.split("mechanism:", 1)[1].strip()
    return None


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


def _branch_script(submit):
    """A research -> assess -> (submit|abandon) response sequence for one
    branch."""
    return [
        ModelReply(json.dumps({
            "action": "run_research_command",
            "command": "grep -r mechanism src/", "cwd": "source",
        })),
        ModelReply(json.dumps(_assess_action())),
        ModelReply(json.dumps(_submit_action() if submit else _abandon_action())),
    ]


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
        gen = _gen_response([
            _card_json(mech="getter", interv="cache"),
            _card_json(mech="search", interv="replace"),
        ])
        model = FakeModel(gen, {
            "getter": _branch_script(submit=True),
            "search": _branch_script(submit=False),
        })
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2,
                             branch_count=2)
        result = orch.run(**args)
        assert len(result.proposals) == 1
        assert not result.abstained
        assert result.deliberation_telemetry["mode"] == "depth-first"
        # Trace stores the full instruction text for observability.
        branch0 = result.trace["branches"][0]
        assert branch0["proposal"] is True
        assert branch0["instruction"] == result.proposals[0].instruction
        branch1 = result.trace["branches"][1]
        assert branch1["proposal"] is False
        assert branch1["instruction"] is None

    def test_all_branches_abandon_yields_abstain(self, tmp_path, monkeypatch):
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path)
        gen = _gen_response([_card_json()])
        model = FakeModel(gen, {"getter": _branch_script(submit=False)})
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
        model = FakeModel(gen)
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
        model = FakeModel(gen, {
            "getter": _branch_script(submit=True),
            "search": _branch_script(submit=True),
        })
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2,
                             branch_count=2)
        result = orch.run(**args)
        # Both branches produced a proposal.
        assert len(result.proposals) == 2
        assert not result.abstained

    def test_branches_run_concurrently(self, tmp_path, monkeypatch):
        """The pool fans the branches out so they overlap in time. FakeModel
        serves only the generator; research_branch is stubbed so the test
        targets the pool itself."""
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path)
        gen = _gen_response([
            _card_json(op="G6", mech="getter", interv="cache"),
            _card_json(op="G9", mech="search", interv="replace"),
        ])
        model = FakeModel(gen)
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2,
                             branch_count=2)

        state = {"in_flight": 0, "max_in_flight": 0}
        lock = threading.Lock()

        def fake_research_branch(*, hypothesis, **_kwargs):
            with lock:
                state["in_flight"] += 1
                state["max_in_flight"] = max(
                    state["max_in_flight"], state["in_flight"])
            time.sleep(0.05)  # force overlap so concurrency is observable
            with lock:
                state["in_flight"] -= 1
            return proposer_mod.BranchResult(
                hypothesis=hypothesis,
                proposal=SimpleNamespace(
                    instruction=f"do {hypothesis.generative_op}"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_branch",
                            fake_research_branch)
        result = orch.run(**args)

        # The two branches overlapped in flight -> real concurrency.
        assert state["max_in_flight"] == 2
        assert not result.abstained
        assert len(result.proposals) == 2

    def test_branch_worker_failure_is_isolated(self, tmp_path, monkeypatch):
        """A branch that raises becomes an abandoned result; its siblings still
        complete. Mirrors test_run_candidates_normalizes_serial_worker_failure."""
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path)
        gen = _gen_response([
            _card_json(op="G6", mech="getter", interv="cache"),
            _card_json(op="G9", mech="search", interv="replace"),
        ])
        model = FakeModel(gen)
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2,
                             branch_count=2)

        def fake_research_branch(*, hypothesis, **_kwargs):
            if hypothesis.generative_op == "G9":
                raise RuntimeError("boom")
            return proposer_mod.BranchResult(
                hypothesis=hypothesis,
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_branch",
                            fake_research_branch)
        result = orch.run(**args)

        # The failing branch was quarantined as abandoned; the survivor's
        # proposal came through and the pool was not killed.
        assert len(result.proposals) == 1
        assert not result.abstained
        statuses = {
            (b["proposal"], b["abandoned"])
            for b in result.trace["branches"]
        }
        assert (True, False) in statuses   # survivor
        assert (False, True) in statuses   # quarantined failure
