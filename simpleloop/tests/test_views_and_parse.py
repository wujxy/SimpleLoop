"""Unit-level assertions for the role-boundary + landing-state refactor.

These do NOT spawn the claude agent — they test the pure pieces:
  - views.for_proposer projects sha/metrics/changed_paths (landing-state signals)
    while still excluding eval_block / feedback_for_report
  - views.for_executor / for_judger carry only their role's fields
  - judger._parse handles the four-field contract + the missing-report fallback
    (LANDED_STATE prefix is a feedback-string convention, not a parsed field)
  - Store.append persists both feedback fields + changed_paths, final_report uses
    the report one

Run: python -m pytest simpleloop/tests/   (from SimpleLoop/)
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from simpleloop import views
from simpleloop.judger import Judgment, _parse, _build_prompt
from simpleloop.store import Store


# --- views.for_proposer: projects landing-state signals to the proposer ---

def test_for_proposer_projects_landing_state():
    """The proposer sees full candidate/base SHAs + acceptance state so it can
    self-audit whether a direction is already landed (git diff) and whether its
    payoff is exhausted (metric trend), and tell "sound but didn't land" (low
    risk + empty) from "latent bug" (high risk). It now sees the concise
    feedback_for_report diagnostic, but still must NOT see eval_block (raw,
    noisy, hallucination risk)."""
    history = [
        {"round": 0, "proposal": "p0", "sha": "aaaa1111bbbb2222", "score": 0.7,
         "accepted": True, "base_sha": "aaaa1111bbbb2222",
         "risk": "low", "feedback": "f0", "feedback_for_report": "r0", "eval_block": "e0",
         "metrics": {"SPEED_MS": 700.0, "CORRECTNESS": True},
         "changed_paths": ["OMILRECV2/src/OMILRECV2.cc"]},
        {"round": 1, "proposal": "p1", "sha": "cccc3333dddd4444", "score": 0.05,
         "accepted": False, "base_sha": "aaaa1111bbbb2222", "risk": "low",
         "feedback": "LANDED_STATE: not-implemented correctness FAIL",
         "feedback_for_report": "r1", "eval_block": "e1",
         "metrics": {"CORRECTNESS": False},
         "changed_paths": ["OMILRECV2/src/OMILRECV2.cc"]},
    ]
    out = views.for_proposer(history)
    assert out == [
        {"round": 0, "proposal": "p0", "sha": "aaaa1111bbbb2222",
         "accepted": True, "base_sha": "aaaa1111bbbb2222",
         "score": 0.7, "risk": "low",
         "metrics": {"SPEED_MS": 700.0, "CORRECTNESS": True},
         "changed_paths": ["OMILRECV2/src/OMILRECV2.cc"],
         "feedback": "f0", "feedback_for_report": "r0"},
        {"round": 1, "proposal": "p1", "sha": "cccc3333dddd4444",
         "accepted": False, "base_sha": "aaaa1111bbbb2222",
         "score": 0.05, "risk": "low",
         "metrics": {"CORRECTNESS": False},
         "changed_paths": ["OMILRECV2/src/OMILRECV2.cc"],
         "feedback": "LANDED_STATE: not-implemented correctness FAIL",
         "feedback_for_report": "r1"},
    ]
    # belt-and-braces: the noisy/raw fields never leak
    for row in out:
        assert "eval_block" not in row


def test_for_proposer_empty_history():
    assert views.for_proposer([]) == []


def test_for_proposer_preserves_order():
    history = [{"round": r, "proposal": f"p{r}", "sha": f"x{r}", "score": 0.1 * r,
                "feedback": f"f{r}", "feedback_for_report": "R", "eval_block": "E",
                "metrics": {}, "changed_paths": []}
               for r in range(5)]
    out = views.for_proposer(history)
    assert [row["round"] for row in out] == [0, 1, 2, 3, 4]


def _serial_history_record(round_id: int, proposal: str) -> dict:
    return {
        "round": round_id,
        "proposal": proposal,
        "sha": f"sha-{round_id}",
        "accepted": True,
        "base_sha": f"sha-{round_id}",
        "score": 0.5,
        "risk": "low",
        "metrics": {"SPEED_MS": 500.0 + round_id},
        "changed_paths": ["src/a.cc"],
        "feedback": f"feedback-{round_id}",
        "feedback_for_report": f"diagnostic-{round_id}",
        "eval_block": "raw output",
    }


def _parallel_history_record(round_id: int, proposals: list[str]) -> dict:
    return {
        "round": round_id,
        "parent_sha": f"parent-{round_id}",
        "selected_candidate": 1,
        "selected_sha": f"candidate-{round_id}-1",
        "base_sha": f"candidate-{round_id}-1",
        "reflection": "reflection",
        "candidates": [
            {
                "candidate": candidate_id,
                "family": f"family-{candidate_id}",
                "proposal": proposal,
                "sha": f"candidate-{round_id}-{candidate_id}",
                "selected": candidate_id == 1,
                "accepted": True,
                "score": 0.4 + candidate_id / 10,
                "risk": "low",
                "metrics": {"SPEED_MS": 600.0 - candidate_id},
                "changed_paths": [f"src/c{candidate_id}.cc"],
                "feedback": f"feedback-{candidate_id}",
                "feedback_for_report": f"diagnostic-{candidate_id}",
                "eval_block": "raw output",
            }
            for candidate_id, proposal in enumerate(proposals)
        ],
    }


def test_for_proposer_keeps_last_six_records_full_by_position():
    round_ids = [3, 5, 12, 20, 21, 40, 99]
    history = [
        _serial_history_record(round_id, f"proposal-{round_id}")
        for round_id in round_ids
    ]

    projected = views.for_proposer(history)

    assert projected[0]["round"] == 3
    assert projected[0]["proposal_head"] == "proposal-3"
    assert "proposal" not in projected[0]
    assert [row["round"] for row in projected[1:]] == round_ids[1:]
    assert [row["proposal"] for row in projected[1:]] == [
        f"proposal-{round_id}" for round_id in round_ids[1:]
    ]
    assert all("proposal_head" not in row for row in projected[1:])


def test_for_proposer_compacts_old_proposal_without_mutating_history():
    long_proposal = "  Compact\n\tthe   live list  " + ("x" * 320)
    history = [_serial_history_record(0, long_proposal)]
    history.extend(
        _serial_history_record(round_id, f"recent-{round_id}")
        for round_id in range(1, 7)
    )
    original = deepcopy(history)
    normalized = " ".join(long_proposal.split())

    projected = views.for_proposer(history)

    assert projected[0]["proposal_head"] == normalized[:300] + "…"
    assert len(projected[0]["proposal_head"]) == 301
    assert projected[0]["sha"] == "sha-0"
    assert projected[0]["metrics"] == {"SPEED_MS": 500.0}
    assert projected[0]["score"] == 0.5
    assert projected[0]["risk"] == "low"
    assert projected[0]["changed_paths"] == ["src/a.cc"]
    assert projected[0]["feedback"] == "feedback-0"
    assert projected[0]["feedback_for_report"] == "diagnostic-0"
    assert "eval_block" not in projected[0]
    assert history == original


def test_for_proposer_applies_one_window_state_to_all_generation_candidates():
    old_generation = _parallel_history_record(
        10,
        ["  old\n candidate zero  ", "old candidate one"],
    )
    recent_generation = _parallel_history_record(
        100,
        ["recent candidate zero", "recent candidate one"],
    )
    history = [old_generation]
    history.extend(
        _serial_history_record(round_id, f"recent-{round_id}")
        for round_id in [20, 30, 40, 50, 60]
    )
    history.append(recent_generation)

    projected = views.for_proposer(history)

    old_candidates = projected[0]["candidates"]
    assert [c["proposal_head"] for c in old_candidates] == [
        "old candidate zero",
        "old candidate one",
    ]
    assert all("proposal" not in c for c in old_candidates)
    assert old_candidates[1]["sha"] == "candidate-10-1"
    assert old_candidates[1]["selected"] is True
    assert old_candidates[1]["feedback_for_report"] == "diagnostic-1"
    assert all("eval_block" not in c for c in old_candidates)

    recent_candidates = projected[-1]["candidates"]
    assert [c["proposal"] for c in recent_candidates] == [
        "recent candidate zero",
        "recent candidate one",
    ]
    assert all("proposal_head" not in c for c in recent_candidates)


# --- views.for_executor / for_judger: role isolation ---

def test_for_executor_has_no_history():
    """The executor sees only the current proposal + safety + goal anchor."""
    out = views.for_executor("do X", "goal", ["a.py"], ["b.py"])
    assert out == {"proposal": "do X", "goal": "goal",
                   "editable": ["a.py"], "frozen": ["b.py"]}
    assert "history" not in out


def test_for_judger_carries_eval_axes_not_history():
    """The judger sees the parsed-metrics axes (passed by the loop), not other rounds."""
    out = views.for_judger(goal="g", proposal="p", diff="d", eval_block="e",
                           metrics={"SPEED_MS": 100.0}, prior_metrics=None,
                           baseline_metrics={"SPEED_MS": 945.0}, metrics_schema=None)
    assert out == {"goal": "g", "proposal": "p", "diff": "d", "eval_block": "e",
                   "metrics": {"SPEED_MS": 100.0}, "prior_metrics": None,
                   "baseline_metrics": {"SPEED_MS": 945.0}, "metrics_schema": None}
    assert "history" not in out


# --- judger._parse: four-field contract + fallback (risk now required) ---

def test_parse_four_field_contract():
    jd = _parse({"score": 0.83, "risk": "low",
                 "feedback": "843ms vs 945ms -11%; next: hoist bins",
                 "feedback_for_report": "Real measured improvement, correctness intact..."})
    assert isinstance(jd, Judgment)
    assert jd.score == 0.83
    assert jd.risk == "low"
    assert jd.feedback == "843ms vs 945ms -11%; next: hoist bins"
    assert jd.feedback_for_report == "Real measured improvement, correctness intact..."


def test_parse_rejects_missing_risk():
    """risk is required for best selection (high-risk rounds never ship).
    Older judger output without risk must fail loudly, not silently default."""
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "feedback": "short", "feedback_for_report": "y"})


def test_parse_rejects_bad_risk():
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "risk": "maybe", "feedback": "x"})
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "risk": "", "feedback": "x"})


def test_parse_missing_report_falls_back_to_feedback():
    """Older judger output (or a judger that ignored the contract) omits
    feedback_for_report — must fall back to `feedback`, not crash."""
    jd = _parse({"score": 0.5, "risk": "medium", "feedback": "short"})
    assert jd.feedback == "short"
    assert jd.feedback_for_report == "short"


def test_parse_empty_report_falls_back():
    jd = _parse({"score": 0.5, "risk": "low", "feedback": "short", "feedback_for_report": "   "})
    assert jd.feedback_for_report == "short"


def test_parse_rejects_bad_score():
    with pytest.raises(ValueError):
        _parse({"score": 1.5, "risk": "low", "feedback": "x", "feedback_for_report": "y"})
    with pytest.raises(ValueError):
        _parse({"score": "nan", "risk": "low", "feedback": "x"})


def test_parse_rejects_empty_feedback():
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "risk": "low", "feedback": "   ", "feedback_for_report": "y"})


# --- Store: both feedback fields persisted, final_report uses report (plan Step 3) ---

def test_store_persists_both_feedback_fields(tmp_path: Path):
    store = Store(tmp_path)
    store.append(0, "p0", "sha0", 0.7, "tight", eval_block="e0",
                 feedback_for_report="full narrative")
    rows = store.history()
    assert len(rows) == 1
    assert rows[0]["feedback"] == "tight"
    assert rows[0]["feedback_for_report"] == "full narrative"
    assert rows[0]["eval_block"] == "e0"


def test_store_report_fallback_when_omitted(tmp_path: Path):
    """append called without feedback_for_report (e.g. _record_failure) -> field
    falls back to feedback so the record is never missing it."""
    store = Store(tmp_path)
    store.append(0, "p0", "sha0", 0.0, "[loop failure] boom", eval_block="")
    rows = store.history()
    assert rows[0]["feedback_for_report"] == "[loop failure] boom"


def test_final_report_uses_feedback_for_report(tmp_path: Path):
    store = Store(tmp_path)
    store.append(0, "p0", "sha0", 0.7, "tight signal",
                 eval_block="e", feedback_for_report="the long narrative")
    report_path = store.write_final_report("goal")
    text = report_path.read_text(encoding="utf-8")
    assert "the long narrative" in text
    # the tight proposer-facing signal is the headline feedback line; the full
    # narrative is carried on its own line so humans get the rich version.
    assert "- feedback: tight signal" in text
    assert "- narrative: the long narrative" in text


def test_final_report_falls_back_for_old_records(tmp_path: Path):
    """A history.jsonl written before the two-field contract (no
    feedback_for_report key) must still render — final_report falls back to
    `feedback`."""
    store = Store(tmp_path)
    # hand-write an old-shape record directly
    with store.path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"round": 0, "proposal": "p0", "sha": "s", "score": 0.5,
                            "feedback": "legacy", "eval_block": ""}) + "\n")
    report_path = store.write_final_report("goal")
    assert "legacy" in report_path.read_text(encoding="utf-8")


# --- Store: changed_paths persisted (landing-state signal for the proposer) ---

def test_store_persists_changed_paths(tmp_path: Path):
    """append stores changed_paths so the proposer can see what each round
    touched without running git."""
    store = Store(tmp_path)
    store.append(0, "p0", "sha0", 0.7, "tight", eval_block="e0",
                 feedback_for_report="full", changed_paths=["a.cc", "a.h"])
    rows = store.history()
    assert rows[0]["changed_paths"] == ["a.cc", "a.h"]


def test_store_persists_candidate_acceptance_and_resulting_base(tmp_path: Path):
    """A rejected implementation keeps its candidate SHA while the accepted
    base stays on the prior commit."""
    store = Store(tmp_path)
    store.append(0, "p0", "candidate0", 0.1, "correctness failed",
                 eval_metrics={"CORRECTNESS": False},
                 changed_paths=["a.cc"], accepted=False, base_sha="baseline")
    row = store.history()[0]
    assert row["sha"] == "candidate0"
    assert row["accepted"] is False
    assert row["base_sha"] == "baseline"


def test_store_changed_paths_default_empty(tmp_path: Path):
    """Rounds without changed_paths (gate-rejected / failed / legacy records)
    read back as an empty list, not a missing key — the proposer can rely on
    the field always being present."""
    store = Store(tmp_path)
    store.append(0, "p0", "sha0", 0.0, "[loop failure] boom", eval_block="")
    rows = store.history()
    assert rows[0]["changed_paths"] == []


def test_store_changed_paths_legacy_record_defaults_empty(tmp_path: Path):
    """A history.jsonl written before changed_paths existed must read back with
    [] (the projection does .get('changed_paths') or []), so old runs still feed
    the proposer without a KeyError."""
    store = Store(tmp_path)
    with store.path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"round": 0, "proposal": "p0", "sha": "s", "score": 0.5,
                            "feedback": "legacy", "eval_block": ""}) + "\n")
    rows = store.history()
    # the raw record has no changed_paths key; for_proposer's .get() handles it
    proj = views.for_proposer(rows)
    assert proj[0]["changed_paths"] == []


# --- judger: LANDED_STATE prefix is a feedback convention, not a parsed field ---

def test_parse_accepts_landed_state_prefixed_feedback():
    """feedback with the LANDED_STATE: prefix (the new contract) parses the same
    as any other feedback string — the prefix is a convention the proposer reads,
    not a field _parse extracts."""
    jd = _parse({"score": 0.05, "risk": "low",
                 "feedback": "LANDED_STATE: already-implemented empty diff, 459.3ms",
                 "feedback_for_report": "executor found the direction already landed..."})
    assert jd.score == 0.05
    assert jd.risk == "low"
    assert jd.feedback == "LANDED_STATE: already-implemented empty diff, 459.3ms"


def test_parse_accepts_legacy_feedback_without_prefix():
    """Backward compat: older rounds' feedback (no LANDED_STATE prefix) still
    parses — _parse never looked at the prefix, and it must keep not looking."""
    jd = _parse({"score": 0.83, "risk": "low",
                 "feedback": "843ms vs 945ms -11%",
                 "feedback_for_report": "..."})
    assert jd.feedback == "843ms vs 945ms -11%"


def test_judger_prompt_has_no_direction_advice_and_requires_landed_state():
    """The judger is told NOT to choose/propose the next direction (that's the
    proposer's job) and is asked to prefix feedback with LANDED_STATE."""
    prompt = _build_prompt("g", "p", "d", "e",
                           {"SPEED_MS": 100.0}, {"SPEED_MS": 110.0},
                           {"SPEED_MS": 945.0},
                           {"objective": {"key": "SPEED_MS", "lower_is_better": True},
                            "gates": []})
    # the old direction-advice ASK ("give actionable feedback ... what to try next
    # or what to fix") is gone — the judger is no longer told to advise on direction.
    # (the phrase "what to try next" may still appear in a "do NOT ..." instruction;
    # what matters is the judger is not ASKED to produce it.)
    assert "Give concrete, actionable feedback for the next proposer" not in prompt
    assert "what to fix" not in prompt
    # the judger is told the next direction is NOT its job (wording may shift,
    # so check the intent not a fixed phrase):
    assert "Do not choose the next direction" in prompt
    # and the LANDED_STATE prefix is in the delivery contract:
    assert "LANDED_STATE:" in prompt
    assert "already-implemented" in prompt
    assert "not-implemented" in prompt
    assert "gate-rejected" in prompt
    # the "fully landed in the current source" deep-audit phrasing is gone
    # (it induced the judger to re-grep the source and blow its output budget);
    # already-implemented now relays the executor's call instead:
    assert "fully landed in the current source" not in prompt


# --- proposer: MUST stays only for hard limits; operations stay advisory; new
#     anti-death-loop MUST: don't re-propose an already-implemented direction ---

def test_proposer_prompt_must_stays_only_for_hard_limits():
    """The landing/payoff CHECKS (git diff, read trend) stay advisory (the
    prompt says the proposer MAY inspect a relevant prior SHA) — they are
    targeted judgment calls, not per-round musts. The hard delivery contract
    (three-field JSON, no prose) stays mandatory. And the anti-death-loop MUST
    is now wired to the `decision` token (continue requires a stated mechanism
    difference), so it has a forcing function the old prose-only "do not
    re-propose" rule lacked."""
    import inspect
    from simpleloop import proposer as prop_mod
    src = inspect.getsource(prop_mod)
    # the mandatory self-audit heading + per-round musts are gone:
    assert "Self-audit before proposing" not in src
    assert "DO NOT re-propose it" not in src
    # git inspection is explicitly optional, not a per-round audit:
    assert "MAY inspect the most relevant prior SHA" in src
    # the three roles are stated so the proposer knows its scope:
    assert "PROPOSER (you)" in src
    assert "EXECUTOR" in src
    assert "JUDGER" in src
    # the hard delivery contract is now a parseable JSON object that supports
    # batch proposals and legacy single-proposal shape:
    assert "MUST be exactly one parseable JSON object" in src
    assert '"proposals"' in src
    assert "reflection" in src and "decision" in src and "proposal" in src
    # the forcing function: continue decision binds to a stated mechanism difference
    assert "If `decision` is `continue`" in src
    assert "State the difference in `reflection`" in src


def test_proposer_prompt_keeps_routing_lightweight_and_git_targeted():
    """Reflection/decision only route the main proposal search. History is the
    default evidence; git inspection is optional, relevant-SHA-only, and must
    not turn into a systematic audit of every prior round."""
    import inspect
    from simpleloop import proposer as prop_mod
    src = inspect.getsource(prop_mod)
    normalized = " ".join(src.split())
    assert "lightweight routing judgement" in normalized
    assert "not the main task and not an audit of every round" in normalized
    assert "MAY inspect the most relevant prior SHA" in normalized
    assert "Git inspection is optional and targeted" in normalized
    assert "do not systematically re-audit all prior rounds" in normalized


def test_proposer_prompt_prioritizes_marginal_effect_for_attribution():
    """A round's mechanism is credited from its delta vs the direct prior
    accepted state, not inherited baseline gains or hard-gate acceptance."""
    import inspect
    from simpleloop import proposer as prop_mod
    src = inspect.getsource(prop_mod)
    normalized = " ".join(src.split())
    assert "objective delta vs the direct prior accepted state as the primary evidence" in normalized
    assert "comparison vs baseline describes cumulative progress" in normalized
    assert "`accepted=true` only means hard gates passed" in normalized


def test_proposer_prompt_makes_proposal_the_primary_output():
    """The delivery contract caps reflection, defines routing by the target
    bottleneck/optimization hypothesis, and explicitly assigns most reasoning
    effort to a concrete proposal grounded in the accepted base."""
    import inspect
    from simpleloop import proposer as prop_mod
    src = inspect.getsource(prop_mod)
    normalized = " ".join(src.split())
    assert "at most 1–2 sentences" in normalized
    assert "`proposal`: each proposal is a concrete candidate direction" in normalized
    assert "target bottleneck / optimization hypothesis" in normalized
    assert "Ground it in the current accepted base" in normalized


def test_proposer_prompt_uses_explicit_base_and_explains_rejected_candidate(tmp_path: Path):
    """The proposer reads the exact accepted base, while a rejected candidate
    remains inspectable evidence and is not mistaken for executor no-work."""
    from simpleloop import proposer as prop_mod

    class CapturingAgent:
        prompt = ""

        def run_json(self, prompt, **_kwargs):
            self.prompt = prompt
            return {"reflection": "brief", "decision": "continue", "proposal": "next"}

    agent = CapturingAgent()
    prop_mod.propose(
        agent,
        goal="g",
        editable=["src/**"],
        frozen=["tests/**"],
        history=[{
            "round": 0,
            "proposal": "attempt",
            "sha": "candidate-full-sha",
            "accepted": False,
            "base_sha": "accepted-full-sha",
            "score": 0.1,
            "risk": "low",
            "metrics": {"CORRECTNESS": False},
            "changed_paths": ["src/a.cc"],
            "feedback": "correctness FAIL",
        }],
        base_sha="accepted-full-sha",
        cwd=tmp_path,
    )
    assert "git show accepted-full-sha:<path>" in agent.prompt
    assert "candidate_sha=candidate-full-sha" in agent.prompt
    assert "accepted=false" in agent.prompt
    assert "real implementation" in agent.prompt
    assert "not part of the current accepted base" in agent.prompt
    assert "HEAD" not in agent.prompt


# --- loop: hard-gate acceptance and resume state ---

def test_candidate_acceptance_requires_every_declared_gate_to_pass():
    from simpleloop import loop as loop_mod

    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}, {"key": "EVAL_RESULT"}],
    }
    assert loop_mod._candidate_accepted(
        "candidate", {"CORRECTNESS": True, "EVAL_RESULT": True}, schema) is True
    assert loop_mod._candidate_accepted(
        "candidate", {"CORRECTNESS": False, "EVAL_RESULT": True}, schema) is False
    assert loop_mod._candidate_accepted(
        "candidate", {"CORRECTNESS": True}, schema) is False
    assert loop_mod._candidate_accepted(
        "candidate", {"CORRECTNESS": True, "EVAL_RESULT": None}, schema) is False


def test_candidate_acceptance_keeps_legacy_no_gate_behavior():
    from simpleloop import loop as loop_mod

    assert loop_mod._candidate_accepted("candidate", {}, None) is True
    assert loop_mod._candidate_accepted("candidate", {}, {"gates": []}) is True
    assert loop_mod._candidate_accepted(None, {}, None) is False


def test_resume_chain_skips_rejected_tail_and_uses_last_accepted_metrics():
    from simpleloop import loop as loop_mod

    schema = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
              "gates": [{"key": "CORRECTNESS"}]}
    history = [
        {"sha": "good", "accepted": True,
         "metrics": {"CORRECTNESS": True, "SPEED_MS": 100.0}},
        {"sha": "bad", "accepted": False, "base_sha": "good",
         "metrics": {"CORRECTNESS": False}},
    ]
    sha, record = loop_mod._resume_chain(history, "baseline", schema)
    assert sha == "good"
    assert record is history[0]


def test_resume_chain_infers_old_history_acceptance_from_gate_metrics():
    from simpleloop import loop as loop_mod

    schema = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
              "gates": [{"key": "CORRECTNESS"}]}
    legacy_history = [
        {"sha": "good", "metrics": {"CORRECTNESS": True}},
        {"sha": "bad", "metrics": {"CORRECTNESS": False}},
    ]
    sha, record = loop_mod._resume_chain(legacy_history, "baseline", schema)
    assert sha == "good"
    assert record is legacy_history[0]


def test_resume_chain_falls_back_to_baseline_when_no_candidate_was_accepted():
    from simpleloop import loop as loop_mod

    schema = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
              "gates": [{"key": "CORRECTNESS"}]}
    history = [{"sha": "bad", "accepted": False,
                "metrics": {"CORRECTNESS": False}}]
    assert loop_mod._resume_chain(history, "baseline", schema) == ("baseline", None)


def test_proposer_prompt_has_antideathloop_must():
    """selfloop-test-3 reproduced the death loop (proposer re-proposed an
    already-landed direction 9x; executor empty-committed each time). The old
    advisory LANDED_STATE hint was ignored, and the prose-only "Do NOT
    re-propose" rule had no forcing function. The fix: a MUST scoped to the
    `continue` decision token — continue requires the proposal to be
    substantively different in mechanism/call sites from a prior
    already-implemented round, with the difference stated in `reflection`;
    line-number/rephrasing drift is explicitly NOT a valid difference (test-3's
    r5->r6 "1177-1203" -> "1177/1178/1184" drift was the exact escape the
    proposer used to re-propose the same optimization). Also forbids frozen-path
    directions (gate would reject) and treats gate-rejected differently from
    already-implemented (retry-and-narrow vs switch-tracks)."""
    import inspect
    from simpleloop import proposer as prop_mod
    src = inspect.getsource(prop_mod)
    # the anti-death-loop MUST is present, scoped to the continue decision:
    assert "LANDED_STATE: already-implemented" in src
    assert "different mechanism or different call sites" in src
    assert "State the difference in `reflection`" in src
    # line-number/rephrasing drift is explicitly NOT a valid difference
    # (test-3's r5->r6 "1177-1203" -> "1177/1178/1184" drift was the exact
    # escape the proposer used to re-propose the same optimization):
    assert "drifting line numbers or rephrasing" in src
    # frozen-path directions are forbidden (gate would reject -> voided round):
    assert "frozen_paths" in src
    # gate-rejected is treated as "retry and narrow scope", NOT "switch tracks"
    # (so it does not get confused with already-implemented):
    assert "narrow its scope/mechanism" in src
    assert r'"done, switch tracks"' in src


# --- loop._print_objective: the run log shows each round's measured speed ---
# Before this the speed number lived inside judger feedback (truncated to 120
# chars in the log) or only in history.jsonl, so the run log couldn't show
# whether a round actually improved. Now the harness prints the authoritative
# parsed objective with vs-prior / vs-baseline deltas. Pure-print helper, so we
# capture stdout and assert on the line. (No agent spawned.)

def test_print_objective_emits_speed_with_deltas(capsys):
    from simpleloop.loop import _print_objective
    schema = {"objective": {"key": "SPEED_MS", "lower_is_better": True}, "gates": []}
    # this round 705, prior 762, baseline 910 -> both improvements (lower is better)
    _print_objective({"SPEED_MS": 705.6, "CORRECTNESS": True},
                     {"SPEED_MS": 762.7}, {"SPEED_MS": 910.0}, schema)
    out = capsys.readouterr().out
    assert "objective:" in out
    assert "SPEED_MS=705.6" in out
    assert "vs prior" in out and "762.7" in out and "-7.5%" in out and "better" in out
    assert "vs baseline" in out and "910" in out


def test_print_objective_silent_when_no_measurement(capsys):
    """A no-commit / eval-crashed round has no objective value. The score+feedback
    line above already says 'no commit', so a redundant 'objective: unknown' line
    would be noise. _print_objective stays silent then."""
    from simpleloop.loop import _print_objective
    schema = {"objective": {"key": "SPEED_MS", "lower_is_better": True}, "gates": []}
    _print_objective({}, {"SPEED_MS": 762.7}, {"SPEED_MS": 910.0}, schema)
    assert capsys.readouterr().out == ""
    # also silent when no schema (diff-only judger):
    _print_objective({"SPEED_MS": 705.0}, None, None, None)
    assert capsys.readouterr().out == ""


def test_print_objective_higher_is_better_arrow(capsys):
    """lower_is_better is read from the schema — a throughput objective (higher is
    better) flips the delta arrow so the log doesn't lie about which way is good."""
    from simpleloop.loop import _print_objective
    schema = {"objective": {"key": "THROUGHPUT", "lower_is_better": False}, "gates": []}
    # this 110, prior 100 -> +10% and that is BETTER for throughput
    _print_objective({"THROUGHPUT": 110.0}, {"THROUGHPUT": 100.0}, {"THROUGHPUT": 90.0}, schema)
    out = capsys.readouterr().out
    assert "THROUGHPUT=110" in out
    assert "+10.0%" in out and "better" in out


# --- agent.py: prompt goes via stdin, not argv (ARG_MAX fix) ---
# A prior 20-round run died mid-loop with `OSError: [Errno 7] Argument list too
# long` because the prompt (tens of KB of history) was passed as a `claude -p`
# argv element, which execve caps at ~128KB. Now the prompt is fed on stdin
# (unbounded) and -p reads it. We assert the argv/popen wiring without spawning
# claude: argv must NOT contain the prompt, and stdin must be a PIPE.

def test_agent_prompt_is_stdin_not_argv():
    import inspect
    from simpleloop import agent as agent_mod
    src = inspect.getsource(agent_mod)
    # the prompt is no longer an argv element:
    assert 'exe, "-p", prompt' not in src
    assert '"-p", prompt' not in src
    # stdin is a PIPE (we write the prompt to it), not DEVNULL:
    assert "stdin=subprocess.PIPE" in src
    assert "stdin=subprocess.DEVNULL" not in src
    # input-format text makes -p read the prompt from stdin:
    assert '"--input-format", "text"' in src


# --- proposer.py: cwd is the per-run repo (shas are valid objects), git plumbing ---
# The proposer used to run in the ORIGINAL source repo, where the per-round shas
# (commits in the per-run clone) were NOT valid objects — so `git diff <sha>`
# failed and the proposer could only reason from a pristine baseline, re-proposing
# already-landed directions. Now its cwd is the per-run repo and it reads committed
# content via git plumbing (the per-run clone has no working tree, so cat/grep of
# a live tree don't work).

def test_proposer_prompt_uses_per_run_repo_and_git_plumbing():
    import inspect
    from simpleloop import proposer as prop_mod
    from simpleloop import loop as loop_mod
    # loop passes the per-run repo (workspace.repo), NOT the original source path:
    lsrc = inspect.getsource(loop_mod)
    assert "cwd=workspace.repo" in lsrc
    assert "cwd=Path(cfg[\"repo_path\"])" not in lsrc
    # proposer prompt teaches git plumbing for reading committed content:
    psrc = inspect.getsource(prop_mod)
    assert "git show" in psrc        # read a file at a commit / show a round's diff
    assert "git diff" in psrc       # diff between two shas
    assert "git log" in psrc        # the commit chain
    # it no longer tells the proposer to cat/grep a live tree (there is none):
    assert "`cat`, `grep`" not in psrc
    # it tells the proposer the shas in history ARE valid objects here:
    assert "valid objects" in psrc or "valid commits" in psrc
