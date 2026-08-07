from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from simpleloop.memory import MemoryService
from simpleloop.roles.orchestrator import (
    ProposerOrchestrator, _select_mode, _Mode,
    _sample_generative_ops, _SCHEDULED_OP_COUNT,
)
from simpleloop.roles.hypothesis import HypothesisCard
from simpleloop.roles.model import ModelReply
from simpleloop.roles import proposer as proposer_mod
from simpleloop.roles import generator as generator_mod


_METRICS_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True}}


class FakeModel:
    """Thread-safe fake for the always-parallel orchestrator.

    A generation call carries only the startup pack in its messages — no
    ``mechanism:`` line — so ``_mechanism_in`` returns ``None`` for it. The
    orchestrator fires N independent two-card generation calls (all non-branch),
    so EVERY such call returns ``gen``. A branch call embeds the hypothesis
    mechanism in its intro (``mechanism: <m>``) and is routed to that branch's
    own response list, keeping each branch's scripted action sequence
    deterministic no matter how the pool interleaves the branches.
    """

    def __init__(self, gen, by_mechanism=None):
        self.gen = gen
        self.by_mechanism = by_mechanism or {}
        self._cursors = {}
        self._lock = threading.Lock()
        self.calls = []

    def complete(self, *, system, messages, timeout_seconds):
        with self._lock:
            self.calls.append(messages)
            # No embedded mechanism ⇒ generation call. Serve the generation
            # response for all of them (N independent calls).
            mech = _mechanism_in(messages)
            if mech is None:
                return self.gen
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


def _block_action():
    return {
        "action": "block",
        "reason_kind": "false_claim",
        "explanation": "the claimed function is not present",
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


def _run_args(tmp_path, candidates_per_round=2):
    source = tmp_path / "source"
    repo = tmp_path / "repo"
    run_dir = tmp_path / "run"
    for p in (source, repo, run_dir):
        p.mkdir(exist_ok=True)
    # A real source file so a block's source: ref can resolve.
    (source / "src").mkdir(exist_ok=True)
    (source / "src" / "foo.cc").write_text("// target\n", encoding="utf-8")
    return {"goal": "make it faster", "editable": ["src/**"],
            "frozen": ["tests/**"], "memory_service": MemoryService(
                run_dir=run_dir, metrics_schema=_METRICS_SCHEMA),
            "base_sha": "abc", "source_path": source, "repo_path": repo,
            "run_dir": run_dir, "current_round": 0,
            "candidates_per_round": candidates_per_round,
            "gate_block": "- gate: pass", "prompt_dir": None}


def _orchestrator(model, *, max_steps=20, **kw):
    return ProposerOrchestrator(
        model=model, runtime=object(), timeout_seconds=60,
        max_steps=max_steps, command_timeout_seconds=5,
        command_output_cap_chars=1000,
        hypothesis_count=kw.get("hypothesis_count", 4),
        branch_steps=kw.get("branch_steps"),
    )


def _branch_script(submit):
    """A research -> (submit|block) response sequence for one branch."""
    return [
        ModelReply(json.dumps({
            "action": "run_research_command",
            "command": "grep -r mechanism src/", "cwd": "source",
        })),
        ModelReply(json.dumps(_submit_action() if submit else _block_action())),
    ]


class TestGenerativeOpScheduler:
    def test_sample_returns_five_distinct_valid_ops(self):
        ops = _sample_generative_ops()
        assert len(ops) == _SCHEDULED_OP_COUNT == 5
        assert len(set(ops)) == 5                   # no duplicates
        valid = {"G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9"}
        assert set(ops) <= valid

    def test_sample_varies_across_calls(self):
        """Two draws are not always identical (randomness is wired)."""
        draws = {_sample_generative_ops() for _ in range(20)}
        assert len(draws) > 1

    def test_independent_calls_each_get_a_scheduled_subset(self, tmp_path):
        """Every independent generator call receives an assigned_ops subset;
        the delivery contract (self-reported generative_op) is unchanged."""
        seen_ops = []
        lock = threading.Lock()

        class RecordingGenerator:
            def run(self, **kwargs):
                with lock:
                    seen_ops.append(kwargs.get("assigned_ops"))
                card = HypothesisCard(
                    generative_op="G6", region="src/foo.cc",
                    mechanism="m", intervention_family="cache",
                    why_plausible="w", critical_unknown="u",
                )
                return generator_mod.GenerationResult(cards=[card])

        orch = _orchestrator(
            FakeModel(_gen_response([_card_json()])),
            hypothesis_count=4,
        )
        orch.generator = RecordingGenerator()
        orch._generate_independent_hypotheses(
            call_count=8, context="c", explore=None, prompt_dir=None,
        )
        assert len(seen_ops) == 8
        for ops in seen_ops:
            assert ops is not None
            assert len(ops) == 5
            assert len(set(ops)) == 5


class TestSelectMode:
    def test_first_round_depth_first(self):
        m = _select_mode(first_round=True, n_experiments=0,
                         max_steps=50, hypothesis_count=8, candidates_per_round=3)
        assert m.label == "depth-first"
        assert m.n_hypotheses <= 3

    def test_later_round_breadth_first(self):
        m = _select_mode(first_round=False, n_experiments=5,
                         max_steps=50, hypothesis_count=8, candidates_per_round=3)
        assert m.label == "breadth-first"
        assert m.n_hypotheses == 8

    def test_depth_first_gives_more_steps_per_branch(self):
        depth = _select_mode(first_round=True, n_experiments=0,
                             max_steps=50, hypothesis_count=8, candidates_per_round=3)
        breadth = _select_mode(first_round=False, n_experiments=5,
                               max_steps=50, hypothesis_count=8, candidates_per_round=3)
        assert depth.max_branch_steps > breadth.max_branch_steps

    def test_explicit_branch_steps_overrides_formula(self):
        depth = _select_mode(first_round=True, n_experiments=0, max_steps=50,
                             hypothesis_count=8, candidates_per_round=3, branch_steps=28)
        breadth = _select_mode(first_round=False, n_experiments=5, max_steps=50,
                               hypothesis_count=8, candidates_per_round=3, branch_steps=28)
        assert depth.max_branch_steps == 28
        assert breadth.max_branch_steps == 28


class TestOrchestratorRun:
    def test_independent_generator_calls_use_two_cards_and_same_context(
            self, tmp_path):
        class IndependentGenerator:
            def __init__(self):
                self.calls = []
                self.lock = threading.Lock()

            def run(self, **kwargs):
                with self.lock:
                    index = len(self.calls)
                    self.calls.append(kwargs)
                # n=2 → return two cards per call.
                cards = [
                    HypothesisCard(
                        generative_op="G6", region="src/foo.cc",
                        mechanism=f"mechanism-{index}-a",
                        intervention_family="cache",
                        why_plausible="w", critical_unknown="u",
                    ),
                    HypothesisCard(
                        generative_op="G6", region="src/foo.cc",
                        mechanism=f"mechanism-{index}-b",
                        intervention_family="cache",
                        why_plausible="w", critical_unknown="u",
                    ),
                ]
                return generator_mod.GenerationResult(cards=cards)

        orch = _orchestrator(
            FakeModel(_gen_response([_card_json()])),
            hypothesis_count=4,
        )
        generator = IndependentGenerator()
        orch.generator = generator
        explore = object()

        cards = orch._generate_independent_hypotheses(
            call_count=4,
            context="same context",
            explore=explore,
            prompt_dir="same prompts",
        )

        assert len(cards) == 8                       # 4 calls × 2 cards
        assert len(generator.calls) == 4
        assert {call["n"] for call in generator.calls} == {2}
        assert {call["context"] for call in generator.calls} == {"same context"}
        assert {call["explore"] for call in generator.calls} == {explore}
        assert {call["prompt_dir"] for call in generator.calls} == {"same prompts"}

    def test_orchestrator_generates_twice_target_before_branch_selection(
            self, tmp_path, monkeypatch):
        class IndependentGenerator:
            def __init__(self):
                self.calls = []

            def run(self, **kwargs):
                self.calls.append(kwargs)
                index = len(self.calls)
                # n=2 → two cards per call.
                cards = [
                    HypothesisCard(
                        generative_op="G6", region="src/foo.cc",
                        mechanism=f"mechanism-{index}-a",
                        intervention_family="cache",
                        why_plausible="w", critical_unknown="u",
                    ),
                    HypothesisCard(
                        generative_op="G6", region="src/foo.cc",
                        mechanism=f"mechanism-{index}-b",
                        intervention_family="cache",
                        why_plausible="w", critical_unknown="u",
                    ),
                ]
                return generator_mod.GenerationResult(cards=cards)

        args = _run_args(tmp_path, candidates_per_round=1)
        orch = _orchestrator(
            FakeModel(_gen_response([_card_json()])),
            hypothesis_count=2,
        )
        generator = IndependentGenerator()
        orch.generator = generator
        branch_cards = []

        def fake_research_branch(*, hypothesis, **_kwargs):
            branch_cards.append(hypothesis)
            return proposer_mod.BranchResult(
                hypothesis=hypothesis,
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_branch",
                            fake_research_branch)
        result = orch.run(**args)

        # hypothesis_count=2 → 2 calls (n=2 each) → 4 cards; cpr=1 → 1 branch.
        assert len(generator.calls) == 2
        assert all(call["n"] == 2 for call in generator.calls)
        assert len(branch_cards) == 1
        assert len(result.proposals) == 1

    def test_out_of_order_completion_collects_every_slot(self, tmp_path):
        """Concurrent calls held and released together must not lose or
        duplicate a slot — the helper keys results by submission index, not
        completion order."""
        release = threading.Event()
        started = threading.Event()
        served = []

        class BlockingGenerator:
            def run(self, **kwargs):
                started.set()
                release.wait()                 # park every call on one gate
                served.append(1)
                # n=2 → two cards per call.
                cards = [
                    HypothesisCard(
                        generative_op="G6", region="src/foo.cc",
                        mechanism=f"m-{len(served)}-a",
                        intervention_family="cache",
                        why_plausible="w", critical_unknown="u",
                    ),
                    HypothesisCard(
                        generative_op="G6", region="src/foo.cc",
                        mechanism=f"m-{len(served)}-b",
                        intervention_family="cache",
                        why_plausible="w", critical_unknown="u",
                    ),
                ]
                return generator_mod.GenerationResult(cards=cards)

        orch = _orchestrator(
            FakeModel(_gen_response([_card_json()])),
            hypothesis_count=2,
        )
        orch.generator = BlockingGenerator()

        out = {}
        t = threading.Thread(target=lambda: out.__setitem__(
            "cards", orch._generate_independent_hypotheses(
                call_count=4, context="c", explore=None, prompt_dir=None,
            )))
        t.start()
        started.wait()                         # at least one call is parked
        time.sleep(0.05)                       # let all four park on the gate
        release.set()                          # release together → racy completion
        t.join()

        assert len(served) == 4
        assert len(out["cards"]) == 8          # 4 calls × 2 cards, no slot lost

    def test_duplicate_and_frozen_cards_filtered_branch_cap_held(
            self, tmp_path, monkeypatch):
        """N independent draws yield duplicate-signature and frozen-region
        cards; the existing dedup → frozen prefilter → candidates_per_round
        cap is unchanged."""
        schedule = queue.Queue()
        for mech, region in [
            ("dup", "src/a.cc"), ("dup", "src/a.cc"),    # duplicate signature
            ("frozen", "tests/ref.cc"),                  # frozen region
            ("live1", "src/b.cc"), ("live2", "src/c.cc"),
            ("live3", "src/d.cc"),
        ]:
            schedule.put(HypothesisCard(
                generative_op="G6", region=region, mechanism=mech,
                intervention_family="cache", why_plausible="w",
                critical_unknown="u",
            ))

        class ScheduledGenerator:
            def run(self, **kwargs):
                # n=2 → two cards per call.
                return generator_mod.GenerationResult(
                    cards=[schedule.get(), schedule.get()])

        args = _run_args(tmp_path, candidates_per_round=2)
        args["frozen"] = ["tests/**"]
        orch = _orchestrator(
            FakeModel(_gen_response([_card_json()])),
            hypothesis_count=3,
        )
        orch.generator = ScheduledGenerator()
        seen = []

        def fake_research_branch(*, hypothesis, **_kwargs):
            seen.append(hypothesis)
            return proposer_mod.BranchResult(
                hypothesis=hypothesis,
                proposal=SimpleNamespace(instruction="ok"),
                deliberation_telemetry={"tool_calls": 1},
            )

        monkeypatch.setattr(orch.proposer, "research_branch",
                            fake_research_branch)
        result = orch.run(**args)

        # Five distinct signatures after dedup; the frozen one is prefilered;
        # the candidates_per_round cap holds regardless → exactly two branches.
        assert len(seen) == 2
        assert all(h.mechanism != "frozen" for h in seen)
        assert len([h for h in seen if h.mechanism == "dup"]) <= 1
        assert len(result.proposals) == 2

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
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2)
        result = orch.run(**args)
        assert len(result.proposals) == 1
        assert not result.abstained
        assert result.deliberation_telemetry["mode"] == "depth-first"
        # Trace stores the full instruction text for observability.
        branch0 = result.trace["branches"][0]
        assert branch0["outcome"] == "submit"
        assert branch0["proposal"] is True
        assert branch0["instruction"] == result.proposals[0].instruction
        branch1 = result.trace["branches"][1]
        assert branch1["outcome"] == "block"
        assert branch1["proposal"] is False
        assert branch1["instruction"] is None
        assert branch1["reason_kind"] == "false_claim"
        # The block's free-text rationale is persisted so a human can audit
        # whether the sieve blocked on a genuine source conflict or read the
        # code wrongly (the one place the sieve can quietly over-restrict).
        assert branch1["explanation"] == "the claimed function is not present"
        assert branch1["evidence_refs"] == ["source:src/foo.cc"]

    def test_all_branches_block_yields_abstain(self, tmp_path, monkeypatch):
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path, candidates_per_round=1)
        gen = _gen_response([_card_json()])
        model = FakeModel(gen, {"getter": _branch_script(submit=False)})
        orch = _orchestrator(model, max_steps=20, hypothesis_count=1)
        result = orch.run(**args)
        assert result.abstained
        assert len(result.proposals) == 0
        assert result.trace["branches"][0]["outcome"] == "block"

    def test_no_cards_after_dedup_abstains(self, tmp_path, monkeypatch):
        """If all cards collapse to one signature, we still proceed (per_bin=1
        keeps one). Abstain only if generator produces nothing usable."""
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path, candidates_per_round=1)
        # All empty structural fields → parser drops them → ModelError
        gen = _gen_response([{
            "generative_op": "G6", "region": "", "mechanism": "",
            "intervention_family": "", "why_plausible": "w",
            "critical_unknown": "u", "slot": "guided",
        }])
        model = FakeModel(gen)
        orch = _orchestrator(model, max_steps=20, hypothesis_count=1)
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
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2)
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
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2)

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
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2)

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

        # The failing branch was quarantined as an error; the survivor's
        # proposal came through and the pool was not killed.
        assert len(result.proposals) == 1
        assert not result.abstained
        outcomes = {b["outcome"] for b in result.trace["branches"]}
        assert "submit" in outcomes   # survivor
        assert "error" in outcomes    # quarantined failure


class TestFrozenPrefilterAndTrace:
    def test_frozen_region_card_dropped_and_backfilled(self, tmp_path, monkeypatch):
        """A card whose region names a frozen path is dropped before any branch
        runs; the next distinct card backfills the branch slot."""
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path, candidates_per_round=1)
        args["frozen"] = ["tests/**"]
        gen = _gen_response([
            _card_json(region="tests/ref.cc", mech="frozen_m", interv="i"),
            _card_json(region="src/foo.cc", mech="live_m", interv="j"),
        ])
        model = FakeModel(gen, {"live_m": _branch_script(submit=True)})
        orch = _orchestrator(model, max_steps=20, hypothesis_count=2)
        result = orch.run(**args)
        # The frozen card was dropped pre-LLM; only the live card ran a branch.
        assert len(result.trace["branches"]) == 1
        assert len(result.proposals) == 1

    def test_trace_branch_keys_match_new_schema(self, tmp_path, monkeypatch):
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path, candidates_per_round=1)
        gen = _gen_response([_card_json()])
        model = FakeModel(gen, {"getter": _branch_script(submit=True)})
        orch = _orchestrator(model, max_steps=20, hypothesis_count=1)
        result = orch.run(**args)
        keys = set(result.trace["branches"][0])
        assert keys == {"sig", "outcome", "proposal", "instruction",
                        "reason_kind", "explanation", "evidence_refs",
                        "tool_calls", "partial"}

    def test_zero_read_partial_is_dropped_before_execution(
            self, tmp_path, monkeypatch):
        """A partial submit with zero source reads carries no enrichment; the
        orchestrator drops it before spending an executor+harness pass."""
        FakeTools.instances.clear()
        monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
        args = _run_args(tmp_path, candidates_per_round=1)
        gen = _gen_response([_card_json()])
        # The branch exhausts its budget with memory-tool calls only (no source
        # read), then partial-submits. Two distinct calls so repeated_tool
        # does not fire.
        model = FakeModel(gen, {"getter": [
            ModelReply(json.dumps({"action": "list_findings",
                                   "state": "active"})),
            ModelReply(json.dumps({"action": "list_findings",
                                   "state": "open"})),
        ]})
        orch = _orchestrator(model, max_steps=20, hypothesis_count=1,
                             branch_steps=2)
        result = orch.run(**args)
        # Branch submitted partial with 0 source reads → dropped → abstain.
        assert result.abstained
        assert len(result.proposals) == 0
        assert result.trace["branches"][0]["partial"] is True
