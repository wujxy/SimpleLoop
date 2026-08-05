from __future__ import annotations

import json

import pytest

from simpleloop.roles.generator import (
    GeneratorAgent,
    _parse_hypotheses,
    GenerationResult,
)
from simpleloop.roles.hypothesis import HypothesisCard
from simpleloop.roles.model import ModelError, ModelReply
from simpleloop.explore.render import render_generation_boundary
from simpleloop.explore.models import (
    ExploreReport,
    FamilyExploreHealth,
    PolicySignal,
    SEVERITY_CHALLENGE,
)


class FakeModel:
    def __init__(self, text):
        self._text = text
        self.calls = []

    def complete(self, *, system, messages, timeout_seconds):
        self.calls.append({"system": system, "messages": messages})
        return ModelReply(text=self._text, usage={"x": 1})


def _card_json(op="G6", region="src/foo.cc", mech="lookup",
               interv="cache", slot="guided"):
    return {
        "generative_op": op, "region": region, "mechanism": mech,
        "intervention_family": interv, "why_plausible": "w",
        "critical_unknown": "u", "slot": slot,
    }


def _response(cards):
    return json.dumps({"hypotheses": cards})


class TestParse:
    def test_parses_cards(self):
        text = _response([_card_json(), _card_json(mech="search", interv="replace")])
        cards = _parse_hypotheses(text, expected=2)
        assert len(cards) == 2
        assert cards[0].mechanism == "lookup"
        assert cards[1].intervention_family == "replace"

    def test_invalid_json_raises(self):
        with pytest.raises(ModelError):
            _parse_hypotheses("not json", expected=1)

    def test_missing_hypotheses_key_raises(self):
        with pytest.raises(ModelError):
            _parse_hypotheses(json.dumps({"foo": []}), expected=1)

    def test_unknown_op_falls_back_to_g6(self):
        text = _response([_card_json(op="GX")])
        cards = _parse_hypotheses(text, expected=1)
        assert cards[0].generative_op == "G6"

    def test_invalid_slot_falls_back_to_guided(self):
        text = _response([_card_json(slot="weird")])
        cards = _parse_hypotheses(text, expected=1)
        assert cards[0].slot == "guided"

    def test_all_empty_structural_fields_skipped(self):
        text = _response([
            _card_json(region="", mech="", interv=""),
            _card_json(mech="search"),
        ])
        cards = _parse_hypotheses(text, expected=2)
        assert len(cards) == 1
        assert cards[0].mechanism == "search"

    def test_no_usable_cards_raises(self):
        text = _response([_card_json(region="", mech="", interv="")])
        with pytest.raises(ModelError):
            _parse_hypotheses(text, expected=1)


class TestBoundary:
    def test_first_round_no_boundary(self):
        report = ExploreReport(first_round=True, analysis_eligible=False)
        out = render_generation_boundary(report)
        assert "first round" in out

    def test_exhausted_family_listed(self):
        fam = FamilyExploreHealth(
            family_id="src/foo.cc::cache", code_region="src/foo.cc",
            mechanisms=("cache",), finding_ids=("F-001",), attempts=5,
            evaluable_attempts=5, implementation_failures=0,
            improvements=0, neutral=5, regressions=0, selected=0,
            consecutive_no_improve=5, recent_rounds=(1, 2, 3),
        )
        report = ExploreReport(
            first_round=False, analysis_eligible=True,
            families=(fam,), challenge_required=False,
        )
        out = render_generation_boundary(report)
        assert "EXHAUSTED" in out
        assert "src/foo.cc" in out
        assert "cache" in out

    def test_below_threshold_not_listed(self):
        fam = FamilyExploreHealth(
            family_id="x::y", code_region="x", mechanisms=("y",),
            finding_ids=(), attempts=3, evaluable_attempts=3,
            implementation_failures=0, improvements=0, neutral=3,
            regressions=0, selected=0, consecutive_no_improve=3,
            recent_rounds=(1,),
        )
        report = ExploreReport(
            first_round=False, analysis_eligible=True,
            families=(fam,), challenge_required=False,
        )
        out = render_generation_boundary(report)
        assert "no exhausted" in out


class TestAgentRun:
    def test_run_produces_cards(self):
        cards_json = [
            _card_json(mech="cache", interv="hoist"),
            _card_json(mech="search", interv="replace"),
            _card_json(mech="alloc", interv="reuse", slot="free"),
        ]
        model = FakeModel(_response(cards_json))
        agent = GeneratorAgent(model=model, timeout_seconds=30)
        result = agent.run(
            n=3, frame_free_ratio=0.33, context="objective: go fast",
        )
        assert isinstance(result, GenerationResult)
        assert len(result.cards) == 3
        assert result.cards[2].slot == "free"

    def test_run_includes_boundary_in_system_prompt(self):
        fam = FamilyExploreHealth(
            family_id="x::y", code_region="x.cc", mechanisms=("y",),
            finding_ids=(), attempts=6, evaluable_attempts=6,
            implementation_failures=0, improvements=0, neutral=6,
            regressions=0, selected=0, consecutive_no_improve=6,
            recent_rounds=(1,),
        )
        report = ExploreReport(
            first_round=False, analysis_eligible=True,
            families=(fam,), challenge_required=False,
        )
        model = FakeModel(_response([_card_json()]))
        agent = GeneratorAgent(model=model, timeout_seconds=30)
        agent.run(n=1, frame_free_ratio=0.0, context="ctx", explore=report)
        sys_prompt = model.calls[0]["system"]
        assert "EXHAUSTED" in sys_prompt
        assert "x.cc" in sys_prompt

    def test_free_slot_count_in_prompt(self):
        model = FakeModel(_response([_card_json()]))
        agent = GeneratorAgent(model=model, timeout_seconds=30)
        agent.run(n=9, frame_free_ratio=0.33, context="ctx")
        sys_prompt = model.calls[0]["system"]
        # n=9, ratio 0.33 → n_free = int(9*0.33) = 2, n_guided = 7
        assert "7 guided" in sys_prompt
        assert "2 free" in sys_prompt
