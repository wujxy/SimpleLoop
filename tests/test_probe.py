from __future__ import annotations

import time
import pytest

from simpleloop.roles.hypothesis import HypothesisCard, ProbeResult
from simpleloop.roles.probe import (
    _probe_commands,
    _keyword,
    _looks_like_path,
    probe_hypothesis,
    probe_batch,
)


def _card(region="src/foo.cc", mech="repeated getter calls",
          interv="cache"):
    return HypothesisCard(
        generative_op="G6", region=region, mechanism=mech,
        intervention_family=interv, why_plausible="w", critical_unknown="u",
    )


class FakeRunner:
    """Fake command runner. Returns canned outputs in order."""
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = []

    def run(self, command, *, cwd="source", timeout_seconds=None):
        self.calls.append({"command": command, "cwd": cwd})
        if self._outputs:
            return self._outputs.pop(0)
        return {"ok": True, "returncode": 0, "output": ""}


class TestKeyword:
    def test_extracts_longest_non_stopword(self):
        assert _keyword("repeated getter calls") == "getter"

    def test_strips_stopwords(self):
        assert _keyword("the repeated per pmt") == "pmt"

    def test_empty_returns_none(self):
        assert _keyword("") is None

    def test_only_stopwords_returns_none(self):
        assert _keyword("the a an in of") is None


class TestProbeCommands:
    def test_path_region_greps_file(self):
        cmds = _probe_commands(_card(region="src/EVL.cc", mech="getter"))
        assert len(cmds) >= 1
        assert "src/EVL.cc" in cmds[0].command
        assert "getter" in cmds[0].command

    def test_tag_region_greps_tree(self):
        cmds = _probe_commands(_card(region="geometry", mech="lookup"))
        assert " ." in cmds[0].command or cmds[0].command.endswith(".")

    def test_intervention_fallback_probe(self):
        cmds = _probe_commands(_card(mech="getter", interv="cache"))
        # Second probe uses intervention keyword
        assert len(cmds) == 2
        assert "cache" in cmds[1].command

    def test_cap_at_two(self):
        cmds = _probe_commands(_card(mech="getter", interv="cache"))
        assert len(cmds) <= 2

    def test_no_keywords_emits_trivial(self):
        cmds = _probe_commands(_card(region="src/x.cc", mech="", interv=""))
        assert len(cmds) == 1
        assert "src/x.cc" in cmds[0].command


class TestProbeHypothesis:
    def test_confirmed_on_nonempty_output(self):
        runner = FakeRunner([{"ok": True, "returncode": 0, "output": "src/foo.cc:42:getter\n"}])
        result = probe_hypothesis(_card(), runner, deadline=1e18)
        assert result.confirmed
        assert result.evidence_ref == "source:src/foo.cc:42:getter"

    def test_unconfirmed_on_empty_output(self):
        runner = FakeRunner([{"ok": True, "returncode": 0, "output": ""}])
        result = probe_hypothesis(_card(), runner, deadline=1e18)
        assert not result.confirmed

    def test_falls_through_to_second_probe(self):
        runner = FakeRunner([
            {"ok": True, "returncode": 0, "output": ""},  # first misses
            {"ok": True, "returncode": 0, "output": "src/bar.cc:cache\n"},  # second hits
        ])
        result = probe_hypothesis(_card(), runner, deadline=1e18)
        assert result.confirmed
        assert "bar.cc" in (result.evidence_ref or "")

    def test_deadline_exceeded(self):
        runner = FakeRunner([])
        result = probe_hypothesis(_card(), runner, deadline=time.monotonic() - 1)
        assert not result.confirmed
        assert "deadline" in result.note

    def test_error_output_treated_as_miss(self):
        runner = FakeRunner([{"ok": False, "returncode": 1, "output": ""}])
        result = probe_hypothesis(_card(), runner, deadline=1e18)
        assert not result.confirmed


class TestProbeBatch:
    def test_probes_all_cards(self):
        cards = [_card(mech="getter"), _card(mech="search", interv="replace")]
        runner = FakeRunner([
            {"ok": True, "returncode": 0, "output": "src/a.cc\n"},
            {"ok": True, "returncode": 0, "output": "src/b.cc\n"},
        ])
        results = probe_batch(cards, runner, deadline=1e18)
        assert len(results) == 2
        assert all(r.confirmed for _, r in results)

    def test_stops_on_deadline(self):
        cards = [_card() for _ in range(5)]
        runner = FakeRunner([{"ok": True, "returncode": 0, "output": "x\n"}] * 5)
        results = probe_batch(cards, runner, deadline=time.monotonic() - 1)
        assert len(results) < 5
