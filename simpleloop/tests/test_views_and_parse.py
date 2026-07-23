"""Unit-level assertions for the role-boundary + landing-state refactor.

These do NOT spawn the claude agent — they test the pure pieces:
  - views.for_proposer projects sha/metrics/changed_paths (landing-state signals)
    while still excluding eval_block
  - views.for_executor / for_judger carry only their role's fields
  - judger._parse handles the four-field contract + the missing-report fallback
    (LANDED_STATE prefix is a feedback-string convention, not a parsed field)
  - Store.append persists both feedback fields + changed_paths

Run: python -m pytest simpleloop/tests/   (from SimpleLoop/)
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop import views
from simpleloop.agent import normalize_free_text
from simpleloop.judger import Judgment, _parse, _build_prompt, _judger_schema, judge
from simpleloop.store import Store

EXAMPLES = Path(__file__).parents[2] / "examples"


def test_normalize_free_text_accepts_limit_without_warning(capsys):
    value = "x" * 1100

    assert normalize_free_text(
        value, limit=1100, label="proposer", field="reflection"
    ) == value
    assert capsys.readouterr().out == ""


def test_normalize_free_text_warns_and_truncates_after_strip(capsys):
    value = "  " + ("x" * 1101) + "  "

    assert normalize_free_text(
        value, limit=1100, label="proposer", field="reflection"
    ) == "x" * 1100
    assert capsys.readouterr().out == (
        "[proposer] warning: reflection length 1101 exceeds 1100; "
        "truncated to 1100\n"
    )


# --- views.for_proposer: projects landing-state signals to the proposer ---

def test_for_proposer_projects_landing_state():
    """The proposer sees full candidate/base SHAs + acceptance state so it can
    self-audit whether a direction is already landed (git diff) and whether its
    payoff is exhausted (metric trend), and tell "sound but didn't land" (low
    risk + empty) from "latent bug" (high risk). The proposer must NOT see eval_block
    (raw, noisy, hallucination risk)."""
    history = [
        {"round": 0, "proposal": "p0", "sha": "aaaa1111bbbb2222", "score": 0.7,
         "accepted": True, "base_sha": "aaaa1111bbbb2222",
         "risk": "low", "feedback": "LANDED_STATE: not-implemented\nImplemented: x\nResult: y\nAnalysis: z", "eval_block": "e0",
         "feedback_for_proposer": "The mechanism remains promising.",
         "metrics": {"SPEED_MS": 700.0, "CORRECTNESS": True},
         "changed_paths": ["OMILRECV2/src/OMILRECV2.cc"]},
        {"round": 1, "proposal": "p1", "sha": "cccc3333dddd4444", "score": 0.05,
         "accepted": False, "base_sha": "aaaa1111bbbb2222", "risk": "low",
         "feedback": "LANDED_STATE: not-implemented correctness FAIL",
         "feedback_for_proposer": "This attempt failed correctness.",
         "eval_block": "e1",
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
         "landing_state": "not-implemented",
         "feedback_for_proposer": "The mechanism remains promising."},
        {"round": 1, "proposal": "p1", "sha": "cccc3333dddd4444",
         "accepted": False, "base_sha": "aaaa1111bbbb2222",
         "score": 0.05, "risk": "low",
         "metrics": {"CORRECTNESS": False},
         "changed_paths": ["OMILRECV2/src/OMILRECV2.cc"],
         "landing_state": "not-implemented",
         "feedback_for_proposer": "This attempt failed correctness."},
    ]
    # belt-and-braces: the noisy/raw fields never leak
    for row in out:
        assert "eval_block" not in row
        assert "feedback" not in row


def test_for_proposer_legacy_feedback_does_not_fall_back_to_full_text():
    projected = views.for_proposer([{
        "round": 0,
        "proposal": "legacy",
        "sha": "sha",
        "score": 0.2,
        "feedback": "FULL_TECHNICAL_SENTINEL",
    }])

    assert projected[0]["feedback_for_proposer"] == ""
    assert "feedback" not in projected[0]


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
        "feedback": f"LANDED_STATE: not-implemented\nImplemented: x{round_id}\nResult: y{round_id}\nAnalysis: z{round_id}",
        "feedback_for_proposer": f"short lesson {round_id}",
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
                "feedback": f"LANDED_STATE: not-implemented\nImplemented: x{candidate_id}\nResult: y{candidate_id}\nAnalysis: z{candidate_id}",
                "feedback_for_proposer": f"short candidate lesson {candidate_id}",
                "eval_block": "raw output",
            }
            for candidate_id, proposal in enumerate(proposals)
        ],
    }


def test_for_proposer_recent_window_uses_position_and_full_proposals():
    round_ids = [3, 5, 12, 20, 21, 40, 99]
    history = [
        _serial_history_record(round_id, f"proposal-{round_id}")
        for round_id in round_ids
    ]

    projected = views.for_proposer(history)

    assert [row["round"] for row in projected] == round_ids[1:]
    assert [row["proposal"] for row in projected] == [
        f"proposal-{round_id}" for round_id in round_ids[1:]
    ]
    assert all("proposal_head" not in row for row in projected)


def test_for_proposer_recent_window_does_not_mutate_history():
    history = [
        _serial_history_record(round_id, f"proposal-{round_id}")
        for round_id in range(7)
    ]
    original = deepcopy(history)

    projected = views.for_proposer(history)

    assert [row["round"] for row in projected] == [1, 2, 3, 4, 5, 6]
    assert history == original


def test_for_proposer_drops_old_generation_and_keeps_recent_candidates():
    old_generation = _parallel_history_record(
        10, ["old candidate zero", "old candidate one"]
    )
    recent_generation = _parallel_history_record(
        100, ["recent candidate zero", "recent candidate one"]
    )
    history = [old_generation]
    history.extend(
        _serial_history_record(round_id, f"recent-{round_id}")
        for round_id in [20, 30, 40, 50, 60]
    )
    history.append(recent_generation)

    projected = views.for_proposer(history)

    assert [row["round"] for row in projected] == [20, 30, 40, 50, 60, 100]
    recent_candidates = projected[-1]["candidates"]
    assert [c["proposal"] for c in recent_candidates] == [
        "recent candidate zero",
        "recent candidate one",
    ]
    assert all("proposal_head" not in c for c in recent_candidates)
    assert all("eval_block" not in c for c in recent_candidates)


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

def test_parse_preserves_feedback_for_proposer():
    jd = _parse({
        "score": 0.5,
        "risk": "low",
        "feedback": "LANDED_STATE: not-implemented\nImplemented: x\nResult: y\nAnalysis: z",
        "feedback_for_proposer": "The mechanism remains inconclusive.",
    })
    assert jd.feedback_for_proposer == "The mechanism remains inconclusive."


@pytest.mark.parametrize("value", [None, "", "   ", 7])
def test_parse_rejects_invalid_feedback_for_proposer(value):
    with pytest.raises(ValueError):
        _parse({
            "score": 0.5,
            "risk": "low",
            "feedback": "full",
            "feedback_for_proposer": value,
        })


def test_judger_schema_requires_feedback_for_proposer():
    schema = _judger_schema()
    assert schema["required"] == [
        "score", "risk", "feedback", "feedback_for_proposer",
    ]
    assert schema["properties"]["feedback_for_proposer"]["pattern"] == r"\S"


def test_parse_four_field_contract():
    jd = _parse({"score": 0.83, "risk": "low",
                 "feedback": "LANDED_STATE: not-implemented\nImplemented: precompute sqrt\nResult: 843ms vs 945ms -11%\nAnalysis: cache locality improvement",
                 "feedback_for_proposer": "Precomputation remains promising."})
    assert isinstance(jd, Judgment)
    assert jd.score == 0.83
    assert jd.risk == "low"
    assert "LANDED_STATE" in jd.feedback


def test_parse_rejects_missing_risk():
    """risk is required for best selection (high-risk rounds never ship).
    Older judger output without risk must fail loudly, not silently default."""
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "feedback": "short",
                "feedback_for_proposer": "short lesson"})


def test_parse_rejects_bad_risk():
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "risk": "maybe", "feedback": "x",
                "feedback_for_proposer": "short lesson"})
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "risk": "", "feedback": "x",
                "feedback_for_proposer": "short lesson"})


def test_parse_rejects_bad_score():
    with pytest.raises(ValueError):
        _parse({"score": 1.5, "risk": "low", "feedback": "x",
                "feedback_for_proposer": "short lesson"})
    with pytest.raises(ValueError):
        _parse({"score": "nan", "risk": "low", "feedback": "x",
                "feedback_for_proposer": "short lesson"})


@pytest.mark.parametrize(
    "data",
    [
        {"score": 0.5, "risk": "low", "feedback": "x",
         "feedback_for_proposer": "short lesson", "extra": 1},
        {"score": "0.5", "risk": "low", "feedback": "x",
         "feedback_for_proposer": "short lesson"},
        {"score": True, "risk": "low", "feedback": "x",
         "feedback_for_proposer": "short lesson"},
        {"score": 0.5, "risk": 1, "feedback": "x",
         "feedback_for_proposer": "short lesson"},
    ],
)
def test_parse_rejects_values_outside_exact_schema_contract(data):
    with pytest.raises(ValueError):
        _parse(data)


def test_parse_rejects_empty_feedback():
    with pytest.raises(ValueError):
        _parse({"score": 0.5, "risk": "low", "feedback": "   ",
                "feedback_for_proposer": "short lesson"})


def test_judger_schema_keeps_structure_without_text_max_lengths():
    schema = _judger_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "score", "risk", "feedback", "feedback_for_proposer",
    ]
    assert schema["properties"]["score"] == {
        "type": "number", "minimum": 0.0, "maximum": 1.0,
    }
    assert schema["properties"]["risk"]["enum"] == ["low", "medium", "high"]
    assert "maxLength" not in schema["properties"]["feedback"]
    assert schema["properties"]["feedback"]["pattern"] == r"\S"
    assert "maxLength" not in schema["properties"]["feedback_for_proposer"]
    assert schema["properties"]["feedback_for_proposer"]["pattern"] == r"\S"


def test_judge_passes_schema_and_custom_label(tmp_path: Path, capsys):
    class CapturingAgent:
        schema = None
        label = None
        prompt = None

        def run_json(self, prompt, *, json_schema=None, label=None, **_kwargs):
            self.prompt = prompt
            self.schema = json_schema
            self.label = label
            return {"score": 0.5, "risk": "low", "feedback": "x" * 1001,
                    "feedback_for_proposer": "short lesson"}

    agent = CapturingAgent()
    judgment = judge(
        agent,
        goal="g",
        proposal="p",
        sha=None,
        reason="no change",
        parent_sha="base",
        workspace=None,
        eval_block="",
        cwd=tmp_path,
        label="judger r1-c0",
    )

    assert judgment.feedback == "x" * 1000
    assert agent.schema == _judger_schema()
    assert '"feedback": "<300-500 chars, four-part>"' in agent.prompt
    assert '"feedback_for_proposer"' in agent.prompt
    assert agent.label == "judger r1-c0"
    assert capsys.readouterr().out == (
        "[judger r1-c0] warning: feedback length 1001 exceeds 1000; "
        "truncated to 1000\n"
    )


def test_parse_accepts_judger_n_plus_500_without_warning(capsys):
    judgment = _parse(
        {
            "score": 0.5,
            "risk": "low",
            "feedback": "f" * 1000,
            "feedback_for_proposer": "p" * 800,
        },
        label="judger r1-c0",
    )

    assert len(judgment.feedback) == 1000
    assert len(judgment.feedback_for_proposer) == 800
    assert capsys.readouterr().out == ""


def test_parse_warns_and_truncates_judger_text_with_candidate_label(capsys):
    judgment = _parse(
        {
            "score": 0.5,
            "risk": "low",
            "feedback": "f" * 1001,
            "feedback_for_proposer": "p" * 801,
        },
        label="judger r14-c2",
    )

    assert len(judgment.feedback) == 1000
    assert len(judgment.feedback_for_proposer) == 800
    assert capsys.readouterr().out == (
        "[judger r14-c2] warning: feedback length 1001 exceeds 1000; "
        "truncated to 1000\n"
        "[judger r14-c2] warning: feedback_for_proposer length 801 exceeds 800; "
        "truncated to 800\n"
    )



# --- Store: feedback fields remain persisted in history ---

def test_store_persists_feedback(tmp_path: Path):
    store = Store(tmp_path)
    store.append(0, "p0", "sha0", 0.7, "LANDED_STATE: not-implemented\nImplemented: precompute sqrt\nResult: -10% speed\nAnalysis: cache locality",
                 eval_block="e0",
                 feedback_for_proposer="Precomputation is worth revisiting.")
    rows = store.history()
    assert len(rows) == 1
    assert "LANDED_STATE" in rows[0]["feedback"]
    assert "Implemented:" in rows[0]["feedback"]
    assert rows[0]["feedback_for_proposer"] == "Precomputation is worth revisiting."
    assert rows[0]["eval_block"] == "e0"


# --- Store: changed_paths persisted (landing-state signal for the proposer) ---

def test_store_persists_changed_paths(tmp_path: Path):
    """append stores changed_paths so the proposer can see what each round
    touched without running git."""
    store = Store(tmp_path)
    store.append(0, "p0", "sha0", 0.7, "LANDED_STATE: not-implemented\nImplemented: precompute\nResult: -5% speed\nAnalysis: good",
                 eval_block="e0", changed_paths=["a.cc", "a.h"])
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
                 "feedback_for_proposer": "The mechanism was already present."})
    assert jd.score == 0.05
    assert jd.risk == "low"
    assert jd.feedback == "LANDED_STATE: already-implemented empty diff, 459.3ms"


def test_parse_accepts_legacy_feedback_without_prefix():
    """Backward compat: older rounds' feedback (no LANDED_STATE prefix) still
    parses — _parse never looked at the prefix, and it must keep not looking."""
    jd = _parse({"score": 0.83, "risk": "low",
                 "feedback": "843ms vs 945ms -11%",
                 "feedback_for_proposer": "The mechanism improved speed."})
    assert jd.feedback == "843ms vs 945ms -11%"
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
def test_parse_batch_enforces_exact_count():
    """The 'produce exactly N candidates' invariant moved out of prompt prose
    into structure: the JSON Schema pins minItems=maxItems=N, and _parse_batch
    rejects a response whose proposals count != candidates_per_round. This is
    the structural anchor for the prose sentence that was deleted."""
    from simpleloop.proposer import _parse_batch, ProposalBatch
    one = {"family": "f", "decision": "continue", "proposal": "p"}
    two = [one, dict(one, family="g")]
    # right count passes
    assert isinstance(_parse_batch({"reflection": "", "insight": "", "insight_refs": [], "proposals": two},
                                   candidates_per_round=2), ProposalBatch)
    # too few / too many -> ValueError (the sentence 'Produce exactly N' used to carry)
    with pytest.raises(ValueError):
        _parse_batch({"reflection": "", "insight": "", "insight_refs": [], "proposals": [one]},
                      candidates_per_round=2)
    with pytest.raises(ValueError):
        _parse_batch({"reflection": "", "insight": "", "insight_refs": [], "proposals": [one, one, one]},
                      candidates_per_round=2)


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
def test_gate_block_renders_key_and_description_lines():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [
            {"key": "FCN", "description": "likelihood drift at 1e-13"},
            {"key": "CONSISTENCY", "description": "physics within tolerance"},
        ],
    }
    block = views.gate_block(schema)
    assert "- FCN: likelihood drift at 1e-13" in block
    assert "- CONSISTENCY: physics within tolerance" in block
    # header/framing prose lives in the prompt, not here:
    assert "Gates" not in block


def test_gate_block_empty_when_no_description():
    # gates without a description produce nothing (degrades to today's prompt):
    schema = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
              "gates": [{"key": "CORRECTNESS"}, {"key": "EVAL_RESULT"}]}
    assert views.gate_block(schema) == ""


def test_gate_block_empty_when_no_schema():
    assert views.gate_block(None) == ""


def test_config_gate_description_is_parsed_and_optional():
    # gates with description are kept; gates without stay key-only.
    cfg = yaml.safe_load(
        (EXAMPLES / "omilrec-post-v107-opt" / "task.yaml").read_text(
            encoding="utf-8"
        )
    )
    gates = cfg["eval"]["metrics"]["gates"]
    keys = [g["key"] for g in gates]
    assert keys == ["FCN", "CONSISTENCY", "EVAL_RESULT"]
    for g in gates:
        assert isinstance(g["description"], str) and g["description"].strip()
