"""Tests for the 1:1 generator-cognitive partner-lane orchestrator.

The old pool-filter architecture (dedup → frozen prefilter → cap → list-wise
selection) is gone. Each lane binds one Generator to one Cognitive element for
its full lifetime. These tests verify:
  - the scheduler (5-of-9 ops, mode selection)
  - N independent lanes each get one card, no intermediate filtering
  - all submitted proposals are collected (no cap, no dedup)
  - the feedback_generator → regenerate loop (≤3 budget)
  - the history-free context is what the generator receives
  - the lane trace structure
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from simpleloop.memory import MemoryService
from simpleloop.roles.orchestrator import (
    ProposerOrchestrator, _lane_quotas, _Mode,
    _sample_generative_ops, _SCHEDULED_OP_COUNT,
    _MAX_REGENERATIONS, LaneState, LaneResult,
)
from simpleloop.roles.hypothesis import HypothesisCard
from simpleloop.roles.model import ModelReply
from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles import generator as generator_mod


_METRICS_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True}}


# --- helpers ---------------------------------------------------------------

def _card(op="G6", region="src/foo.cc", mech="getter", interv="cache"):
    return HypothesisCard(
        generative_op=op, region=region, mechanism=mech,
        intervention_family=interv, why_plausible="w",
        critical_unknown="u",
    )


def _card_json(op="G6", region="src/foo.cc", mech="getter", interv="cache"):
    return {
        "generative_op": op, "region": region, "mechanism": mech,
        "intervention_family": interv, "why_plausible": "w",
        "critical_unknown": "u", "slot": "guided",
        "facts_read": ["function foo() exists in src/foo.cc"],
    }


def _gen_reply(cards):
    return ModelReply(json.dumps({"hypotheses": [_card_json() for _ in cards]}))


def _submit_action():
    return {
        "action": "submit_proposals",
        "proposals": [{
            "instruction": "Cache invariant LPMT constants",
            "research_target": {
                "mode": "new",
                "question": "Can caching LPMT constants speed up FCN?",
                "mechanisms": ["cache-locality"],
                "code_regions": ["src/foo.cc"],
            },
        }],
    }


def _block_action():
    return {
        "action": "block",
        "reason_kind": "false_claim",
        "explanation": "the claimed function is not present",
        "evidence_refs": ["source:src/foo.cc"],
    }


def _feedback_action():
    return {
        "action": "feedback_generator",
        "evidence_refs": ["experiment:r3c0"],
        "observation": "r3c0 tried caching here and gained <1%",
        "relation_to_seed": "same mechanism in the same region",
        "implication": "the cache appears already effective here",
    }


def _run_args(tmp_path, candidates_per_round=2):
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    run_dir = tmp_path / "run"
    for p in (source, repo, run_dir):
        p.mkdir(exist_ok=True)
    (source / "src").mkdir(exist_ok=True)
    (source / "src" / "foo.cc").write_text("// target\n", encoding="utf-8")
    return {"goal": "make it faster", "editable": ["src/**"],
            "frozen": ["tests/**"], "memory_service": MemoryService(
                run_dir=run_dir, metrics_schema=_METRICS_SCHEMA),
            "base_sha": "abc", "source_path": source, "repo_path": repo,
            "run_dir": run_dir, "current_round": 0,
            "candidates_per_round": candidates_per_round,
            "gate_block": "- gate: pass", "prompt_dir": None}


def _orchestrator(model, **kw):
    return ProposerOrchestrator(
        model=model, runtime=object(), timeout_seconds=60,
        command_timeout_seconds=5,
        command_output_cap_chars=1000,
    )


# --- scheduler -------------------------------------------------------------

class TestGenerativeOpScheduler:
    def test_sample_returns_five_distinct_valid_ops(self):
        ops = _sample_generative_ops()
        assert len(ops) == _SCHEDULED_OP_COUNT == 5
        assert len(set(ops)) == 5
        valid = {"G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"}
        assert set(ops) <= valid

    def test_sample_varies_across_calls(self):
        draws = {_sample_generative_ops() for _ in range(20)}
        assert len(draws) > 1


class TestLaneQuotas:
    def test_n4_k2_two_lanes(self):
        assert _lane_quotas(4, 2) == [2, 2]

    def test_n5_k2_three_lanes(self):
        assert _lane_quotas(5, 2) == [2, 2, 1]

    def test_n4_k1_four_lanes(self):
        assert _lane_quotas(4, 1) == [1, 1, 1, 1]

    def test_n7_k2_four_lanes(self):
        assert _lane_quotas(7, 2) == [2, 2, 2, 1]

    def test_n1_k1_one_lane(self):
        assert _lane_quotas(1, 1) == [1]

    def test_n3_k2_two_lanes(self):
        assert _lane_quotas(3, 2) == [2, 1]

    def test_total_equals_n(self):
        for n in range(1, 20):
            for k in range(1, 6):
                assert sum(_lane_quotas(n, k)) == n


# --- 1:1 lane architecture -------------------------------------------------

class TestPartnerLanes:
    def test_each_lane_gets_one_card_no_intermediate_filter(
            self, tmp_path, monkeypatch):
        """N lanes → N generator calls (one card each) → N branches. No dedup,
        no frozen prefilter, no cap — every lane runs to completion."""
        gen_calls = []
        lock = threading.Lock()

        class OneCardGenerator:
            def run(self, **kwargs):
                with lock:
                    gen_calls.append(kwargs)
                return generator_mod.GenerationResult(
                    cards=[_card(mech=f"m{len(gen_calls)}")])

        # cpr=4, K=2 → ceil(4/2) = 2 lanes.
        args = _run_args(tmp_path, candidates_per_round=4)
        orch = _orchestrator(_gen_reply([_card()]))
        orch.generator = OneCardGenerator()
        branch_cards = []

        def fake_research_batch(*, hypotheses, **_kwargs):
            branch_cards.extend(hypotheses)
            return proposer_mod.BranchResult(
                hypothesis=hypotheses[0],
                proposals=tuple(
                    SimpleNamespace(instruction="ok") for _ in hypotheses
                ),
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_batch",
                            fake_research_batch)
        result = orch.run(**args)

        n = len(gen_calls)
        assert n == 2                            # one call per lane
        assert len(branch_cards) == n            # no filtering — all run
        assert len(result.proposals) == n        # all submitted

    def test_no_dedup_duplicate_signatures_both_run(
            self, tmp_path, monkeypatch):
        """Two lanes produce identical-signature cards. Both run — the old
        dedup-by-signature is gone."""
        cards = [_card(mech="same"), _card(mech="same")]

        class ScheduledGenerator:
            def __init__(self):
                self.i = 0
            def run(self, **kwargs):
                c = cards[self.i]
                self.i += 1
                return generator_mod.GenerationResult(cards=[c])

        # cpr=4, K=2 → 2 lanes.
        args = _run_args(tmp_path, candidates_per_round=4)
        orch = _orchestrator(_gen_reply([_card()]))
        orch.generator = ScheduledGenerator()
        seen = []

        def fake_research_batch(*, hypotheses, **_kwargs):
            seen.extend(hypotheses)
            return proposer_mod.BranchResult(
                hypothesis=hypotheses[0],
                proposals=tuple(
                    SimpleNamespace(instruction="ok") for _ in hypotheses
                ),
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_batch",
                            fake_research_batch)
        result = orch.run(**args)

        assert len(seen) == 2                    # both, despite same signature
        assert len(result.proposals) == 2

    def test_no_frozen_prefilter_frozen_region_still_runs(
            self, tmp_path, monkeypatch):
        """A card whose region is under a frozen path still gets a branch —
        the cognitive element's Sieve handles frozen blocks, not the
        orchestrator."""
        class FrozenRegionGenerator:
            def run(self, **kwargs):
                return generator_mod.GenerationResult(
                    cards=[_card(region="tests/ref.cc")])

        args = _run_args(tmp_path, candidates_per_round=1)
        args["frozen"] = ["tests/**"]
        orch = _orchestrator(_gen_reply([_card()]))
        orch.generator = FrozenRegionGenerator()
        seen = []

        def fake_research_batch(*, hypotheses, **_kwargs):
            seen.extend(hypotheses)
            return proposer_mod.BranchResult(
                hypothesis=hypotheses[0],
                proposals=tuple(
                    SimpleNamespace(instruction="ok") for _ in hypotheses
                ),
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_batch",
                            fake_research_batch)
        result = orch.run(**args)

        assert len(seen) == 1                    # not prefilered
        assert len(result.proposals) == 1

    def test_blocked_lane_not_collected_submitted_lane_is(
            self, tmp_path, monkeypatch):
        """Lane outcomes are respected: block → no proposal, submit → proposal.
        No selection/ranking — both lanes ran."""
        cards = [_card(mech="live"), _card(mech="doomed")]

        class TwoCardGenerator:
            def __init__(self):
                self.i = 0
            def run(self, **kwargs):
                c = cards[self.i]
                self.i += 1
                return generator_mod.GenerationResult(cards=[c])

        args = _run_args(tmp_path, candidates_per_round=4)
        orch = _orchestrator(_gen_reply([_card()]))
        orch.generator = TwoCardGenerator()

        def fake_research_batch(*, hypotheses, **_kwargs):
            h = hypotheses[0]
            if h.mechanism == "doomed":
                return proposer_mod.BranchResult(
                    hypothesis=h, outcome="block",
                    reason_kind="false_claim", explanation="no",
                    block_evidence_refs=("source:src/foo.cc",),
                    deliberation_telemetry={"tool_calls": 1},
                )
            return proposer_mod.BranchResult(
                hypothesis=h,
                proposals=(SimpleNamespace(instruction="ok"),),
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_batch",
                            fake_research_batch)
        result = orch.run(**args)

        assert len(result.proposals) == 1        # only the live lane
        assert not result.abstained
        # trace records both lane outcomes
        lanes = result.trace["lanes"]
        assert len(lanes) == 2
        outcomes = {lr["outcome"] for lr in lanes}
        assert "submit" in outcomes
        assert "block" in outcomes

    def test_all_lanes_block_abstains(self, tmp_path, monkeypatch):
        class BlockingGenerator:
            def run(self, **kwargs):
                return generator_mod.GenerationResult(cards=[_card()])

        args = _run_args(tmp_path, candidates_per_round=2)
        orch = _orchestrator(_gen_reply([_card()]))
        orch.generator = BlockingGenerator()

        def fake_research_batch(*, hypotheses, **_kwargs):
            h = hypotheses[0]
            return proposer_mod.BranchResult(
                hypothesis=h, outcome="block",
                reason_kind="false_claim", explanation="no",
                block_evidence_refs=("source:src/foo.cc",),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_batch",
                            fake_research_batch)
        result = orch.run(**args)

        assert result.abstained
        assert result.proposals == []

    def test_generator_receives_history_free_context(
            self, tmp_path, monkeypatch):
        """The generator's context must NOT contain history/dashboard/explore —
        only objective/gates/paths/base_sha."""
        received_context = []

        class CapturingGenerator:
            def run(self, **kwargs):
                received_context.append(kwargs["context"])
                return generator_mod.GenerationResult(cards=[_card()])

        args = _run_args(tmp_path, candidates_per_round=1)
        orch = _orchestrator(_gen_reply([_card()]))
        orch.generator = CapturingGenerator()

        monkeypatch.setattr(orch.proposer, "research_batch",
            lambda *, hypotheses, **_: proposer_mod.BranchResult(
                hypothesis=hypotheses[0],
                proposals=(SimpleNamespace(instruction="ok"),),
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 0},
            ))
        orch.run(**args)

        assert len(received_context) == 1
        ctx = received_context[0]
        assert "make it faster" in ctx              # objective
        assert "abc" in ctx                          # base_sha
        assert "src/**" in ctx                       # editable
        assert "tests/**" in ctx                     # frozen
        # No history/dashboard/explore/frontier in the generation context.
        assert "dashboard" not in ctx.lower()
        assert "frontier" not in ctx.lower()
        assert "exhausted" not in ctx.lower()

    def test_lane_trace_structure(self, tmp_path, monkeypatch):
        class SimpleGenerator:
            def run(self, **kwargs):
                return generator_mod.GenerationResult(
                    cards=[_card(mech="m", region="src/foo.cc")])

        args = _run_args(tmp_path, candidates_per_round=4)
        orch = _orchestrator(_gen_reply([_card()]))
        orch.generator = SimpleGenerator()

        monkeypatch.setattr(orch.proposer, "research_batch",
            lambda *, hypotheses, **_: proposer_mod.BranchResult(
                hypothesis=hypotheses[0],
                proposals=(SimpleNamespace(instruction="ok"),),
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 2},
            ))
        result = orch.run(**args)

        assert "lanes" in result.trace
        assert len(result.trace["lanes"]) == 2
        for lane in result.trace["lanes"]:
            assert "lane_id" in lane
            assert "assigned_ops" in lane
            assert "all_cards" in lane
            assert "sig" in lane
            assert "outcome" in lane
            assert "tool_calls" in lane
            assert len(lane["assigned_ops"]) == 5


# --- feedback_generator → regenerate loop -----------------------------------

class TestFeedbackLoop:
    def test_feedback_triggers_regeneration(self, tmp_path, monkeypatch):
        """When the cognitive element issues feedback_generator, the callback
        invokes generator.regenerate() and the new hypothesis is used."""
        regen_calls = []

        class RegeneratingGenerator:
            def __init__(self):
                self.first = True
            def run(self, **kwargs):
                self.first = False
                return generator_mod.GenerationResult(
                    cards=[_card(mech="initial")])
            def regenerate(self, *, context, feedback, transcript,
                           source_path=None, repo_path=None, run_dir=None,
                           prompt_dir=None, assigned_ops=None, max_steps=None,
                           hypotheses_per_lane=1, ideas_per_lens=1):
                regen_calls.append(feedback)
                return generator_mod.GenerationResult(
                    cards=[_card(mech="regenerated")])

        args = _run_args(tmp_path, candidates_per_round=1)
        orch = _orchestrator(_gen_reply([_card()]))
        gen = RegeneratingGenerator()
        orch.generator = gen

        branch_calls = []

        def fake_research_batch(*, hypotheses, generator_regenerate, **_kwargs):
            h = hypotheses[0]
            branch_calls.append(h)
            if h.mechanism == "initial":
                # Cognitive finds history evidence → feed back to generator.
                new_card = generator_regenerate(_feedback_action())
                # The callback returns the new hypothesis; the batch would
                # continue with it. For this test we simulate the batch
                # re-auditing the new card and submitting.
                return proposer_mod.BranchResult(
                    hypothesis=new_card,
                    proposals=(SimpleNamespace(instruction="ok"),),
                    proposal=SimpleNamespace(instruction="ok"),
                    deliberation_telemetry={"tool_calls": 1},
                )
            return proposer_mod.BranchResult(
                hypothesis=h,
                proposals=(SimpleNamespace(instruction="ok"),),
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_batch",
                            fake_research_batch)
        result = orch.run(**args)

        assert len(regen_calls) == 1
        assert regen_calls[0]["action"] == "feedback_generator"
        assert len(result.proposals) == 1

    def test_regeneration_budget_enforced(self, tmp_path, monkeypatch):
        """After _MAX_REGENERATIONS regenerations, the callback raises."""
        class RegeneratingGenerator:
            def __init__(self):
                self.n = 0
            def run(self, **kwargs):
                return generator_mod.GenerationResult(
                    cards=[_card(mech="initial")])
            def regenerate(self, *, context, feedback, transcript,
                           source_path=None, repo_path=None, run_dir=None,
                           prompt_dir=None, assigned_ops=None, max_steps=None,
                           hypotheses_per_lane=1, ideas_per_lens=1):
                self.n += 1
                return generator_mod.GenerationResult(
                    cards=[_card(mech=f"regen-{self.n}")])

        args = _run_args(tmp_path, candidates_per_round=1)
        orch = _orchestrator(_gen_reply([_card()]))
        gen = RegeneratingGenerator()
        orch.generator = gen

        def fake_research_batch(*, hypotheses, generator_regenerate, **_kwargs):
            # The feedback loop happens INSIDE research_batch. Call the
            # callback repeatedly until it raises, then submit.
            last = hypotheses[0]
            for _ in range(_MAX_REGENERATIONS):
                last = generator_regenerate(_feedback_action())
            # One more should exhaust the budget.
            with pytest.raises(RuntimeError, match="budget exhausted"):
                generator_regenerate(_feedback_action())
            return proposer_mod.BranchResult(
                hypothesis=last,
                proposals=(SimpleNamespace(instruction="ok"),),
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_batch",
                            fake_research_batch)
        result = orch.run(**args)

        assert gen.n == _MAX_REGENERATIONS
        assert len(result.proposals) == 1


# --- generator.regenerate unit tests ---------------------------------------

class TestGeneratorRegenerate:
    def test_regenerate_uses_transcript_and_feedback(self, monkeypatch, tmp_path):
        """regenerate() sends context + transcript + feedback to the model."""
        from simpleloop.roles.generator import GeneratorAgent
        from simpleloop.roles import generator as gen_mod

        captured = {}

        class CapturingModel:
            def __init__(self):
                self._n = 0
            def complete(self, *, system, messages, timeout_seconds):
                self._n += 1
                if self._n == 1:
                    captured["messages"] = list(messages)
                    captured["system"] = system
                    return ModelReply(json.dumps({
                        "action": "run_research_command",
                        "command": "ls src/", "cwd": "source",
                    }))
                if self._n == 2:
                    return ModelReply(json.dumps({
                        "action": "emit_lever_map",
                        "levers": [{"part": "foo", "role": "hot",
                                    "structural_space": "cache"}],
                    }))
                return ModelReply(json.dumps({
                    "action": "submit_hypothesis",
                    "hypothesis": _card_json(),
                }))

        class FakeTools:
            def __init__(self, **kwargs):
                pass
            def execute(self, action, *, deadline):
                return {"ok": True, "returncode": 0, "output": ""}

        monkeypatch.setattr(gen_mod, "ResearchTools", FakeTools)
        gen = GeneratorAgent(
            model=CapturingModel(), runtime=None, timeout_seconds=30,
            max_steps=10, command_timeout_seconds=10,
            command_output_cap_chars=10000,
        )
        feedback = {
            "action": "feedback_generator",
            "evidence_refs": ("experiment:r3c0",),
            "observation": "tried this before, gained nothing",
            "relation_to_seed": "same mechanism",
            "implication": "the gain margin here is small",
        }
        transcript = [
            {"role": "user", "content": "initial context"},
            {"role": "assistant", "content": "initial hypothesis"},
        ]
        result = gen.regenerate(
            context="history-free context", feedback=feedback,
            transcript=transcript,
            source_path=tmp_path / "src", repo_path=tmp_path / "repo",
            run_dir=tmp_path / "run",
        )
        msgs = captured["messages"]
        # context is first, then transcript, then feedback
        assert msgs[0]["content"] == "history-free context"
        assert len(msgs) == 4                          # 1 ctx + 2 transcript + 1 feedback
        assert "History feedback" in msgs[-1]["content"]
        assert "the gain margin here is small" in msgs[-1]["content"]
        assert len(result.cards) == 1


# --- history-free context builder ------------------------------------------

class TestGenerationContext:
    def test_context_contains_only_objective_and_gates(self):
        from simpleloop.memory.context import build_generation_context
        ctx = build_generation_context(
            goal="make it faster", editable=["src/**"], frozen=["tests/**"],
            base_sha="abc123", gate_block="- gate: pass",
        )
        assert "make it faster" in ctx
        assert "abc123" in ctx
        assert "src/**" in ctx
        assert "tests/**" in ctx
        assert "- gate: pass" in ctx
        # No history/dashboard/explore/frontier.
        assert "dashboard" not in ctx.lower()
        assert "frontier" not in ctx.lower()
        assert "exhausted" not in ctx.lower()

    def test_service_facade_delegates_to_context_builder(
            self, tmp_path, monkeypatch):
        from simpleloop.memory.context import build_generation_context
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        svc = MemoryService(
            run_dir=run_dir, metrics_schema=_METRICS_SCHEMA)
        expected = build_generation_context(
            goal="g", editable=["a"], frozen=["b"],
            base_sha="s", gate_block="gb",
        )
        actual = svc.build_generation_context(
            goal="g", editable=["a"], frozen=["b"],
            base_sha="s", gate_block="gb",
        )
        assert actual == expected
