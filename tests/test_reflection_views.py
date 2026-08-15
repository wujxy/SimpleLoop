"""Tests for the deterministic Reflection aggregate views (workstream B9).

Pure derivations — no IO, no interpretation. The point under test is that
trajectory-level patterns (mechanism concentration, expectation misses,
diminishing returns, attention locality) become NUMBERS the Reflecting
Scientist can be held to cite.
"""
from __future__ import annotations

import json
from pathlib import Path

from proposer.memory.models import Finding
from proposer.memory.reflection_views import (
    expectation_ledger,
    improvement_trajectory,
    mechanism_family_distribution,
    path_prefix_streak,
    render_reflection_pack,
)
from proposer.memory.service import MemoryService


class _Exp:
    """Minimal experiment stand-in with the fields the views read."""

    def __init__(self, rnd, cand, *, selected=False, gate_passed=False,
                 metrics=None, changed_paths=(), finding_id=None):
        self.round = rnd
        self.candidate = cand
        self.selected = selected
        self.gate_passed = gate_passed
        self.eligible = gate_passed
        self.metrics = metrics or {}
        self.changed_paths = tuple(changed_paths)
        self.finding_id = finding_id
        self.experiment_id = f"r{rnd}c{cand}"


def _finding(fid, mechanisms, *, refs=()):
    return Finding(
        id=fid, question=f"q-{fid}", mechanisms=tuple(mechanisms),
        code_regions=(), state="active", created_round=0,
        last_touched_round=0, experiment_refs=tuple(refs),
    )


def test_mechanism_family_distribution_counts_window_only():
    findings = {
        "F-001": _finding("F-001", ["lookup elimination"]),
        "F-002": _finding("F-002", ["data layout"]),
    }
    exps = [
        _Exp(1, 0, finding_id="F-002"),   # before the window (1 < 10-8)
        _Exp(3, 0, finding_id="F-001"),
        _Exp(4, 0, finding_id="F-001"),
        _Exp(4, 1, finding_id="F-001"),
        _Exp(9, 0, finding_id="F-002"),   # window: rounds 2..9
        _Exp(11, 0, finding_id="F-002"),  # future round: excluded
    ]
    dist = mechanism_family_distribution(
        findings, exps, current_round=10, last_k_rounds=8)
    assert dist == {"lookup elimination": 3, "data layout": 1}
    # sorted by count desc
    assert list(dist) == ["lookup elimination", "data layout"]


def test_expectation_ledger_pairs_and_flags_missing():
    exps = [
        _Exp(4, 0, gate_passed=True, metrics={"SPEED_MS": 100.0}),
        _Exp(4, 1, gate_passed=False),
    ]
    rows = {4: {"round": 4, "captured": True, "expectations": [
        {"slot": 0, "expectation": "big gain if lookup dominates",
         "would_weaken": "neutral means no"},
    ]}}
    ledger = expectation_ledger(exps, rows, current_round=5)
    assert len(ledger) == 2
    paired = ledger[0]
    assert paired["preregistered"] is True
    assert paired["expectation"] == "big gain if lookup dominates"
    assert paired["outcome"] == "PASSED_GATES_NOT_IMPROVED"
    unpaired = ledger[1]
    assert unpaired["preregistered"] is False
    assert unpaired["outcome"] == "FAILED_GATES"


def test_expectation_ledger_capture_failure_stays_visible():
    exps = [_Exp(4, 0, gate_passed=True)]
    rows = {4: {"round": 4, "captured": False, "expectations": []}}
    ledger = expectation_ledger(exps, rows, current_round=5)
    assert ledger[0]["preregistered"] is False


def test_improvement_trajectory_streak_and_incumbent():
    def row(rnd, values, selected_idx):
        return {
            "round": rnd,
            "candidates": [
                {"candidate": i, "eligible": True, "selected": i == selected_idx,
                 "metrics": {"SPEED_MS": v}}
                for i, v in enumerate(values)
            ],
        }
    history = [
        row(0, [100.0], 0),   # selected 100 -> incumbent 100
        row(1, [99.0], None),  # no selection: candidates present, none selected
        row(2, [99.5], None),
    ]
    # NOTE: selected_idx None means no candidate selected
    for record in history[1:]:
        for cand in record["candidates"]:
            cand["selected"] = False
    traj = improvement_trajectory(
        history, objective_key="SPEED_MS", lower_is_better=True)
    assert traj[0]["incumbent_after"] == 100.0
    assert traj[-1]["no_incumbent_streak"] == 2
    assert traj[-1]["incumbent_after"] == 100.0
    assert traj[1]["best_eligible"] == 99.0


def test_path_prefix_streak_counts_consecutive_same_region():
    exps = [
        _Exp(1, 0, changed_paths=["src/other.cc"]),
        _Exp(2, 0, changed_paths=["src/loop/a.cc"]),
        _Exp(3, 0, changed_paths=["src/loop/b.cc"]),
        _Exp(4, 0, changed_paths=["src/loop/c.cc", "src/x.cc"]),
    ]
    prefix, streak = path_prefix_streak(exps, current_round=5)
    assert prefix == "src/loop"
    assert streak == 3  # rounds 2-4 share src/loop; round 4 also touches x

    # a round in a different region breaks the streak from the recent end
    exps2 = exps + [_Exp(5, 0, changed_paths=["util/z.cc"])]
    prefix2, streak2 = path_prefix_streak(exps2, current_round=6)
    assert prefix2 == "util/z.cc"  # two-segment path is its own bucket
    assert streak2 == 1


def test_render_pack_is_deterministic_and_cites_ids(tmp_path):
    findings = {"F-001": _finding("F-001", ["lookup elimination"])}
    exps = [_Exp(4, 0, gate_passed=True, changed_paths=["src/loop/a.cc"],
                 finding_id="F-001")]
    history = [{
        "round": 4,
        "candidates": [{
            "candidate": 0, "eligible": True, "selected": False,
            "metrics": {"SPEED_MS": 99.0},
        }],
    }]
    rows = {4: {"round": 4, "captured": True, "expectations": [
        {"slot": 0, "expectation": "gain", "would_weaken": "neutral"},
    ]}}
    kwargs = dict(
        current_round=5, experiments=exps, findings=findings,
        history_rows=history, expectation_rows=rows,
        previous_handoffs=["stop anchoring on lookup"],
        metrics_schema={"objective": {"key": "SPEED_MS",
                                      "lower_is_better": True}},
    )
    pack1 = render_reflection_pack(**kwargs)
    pack2 = render_reflection_pack(**kwargs)
    assert pack1 == pack2
    assert "r4c0" in pack1
    assert "PASSED_GATES_NOT_IMPROVED" in pack1
    assert "gain" in pack1 and "neutral" in pack1
    assert "lookup elimination" in pack1
    assert "src/loop" in pack1
    assert "stop anchoring on lookup" in pack1
    assert "no-new-incumbent streak: 1" in pack1


def test_build_reflection_pack_end_to_end(tmp_path):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(json.dumps({
        "round": 1,
        "candidates": [{
            "candidate": 0, "experiment_id": "r1c0", "status": "COMPLETED",
            "selected": False, "gate_passed": True, "eligible": True,
            "metrics": {"SPEED_MS": 120.0},
            "changed_paths": ["src/loop/a.cc"],
        }],
    }) + "\n")
    (tmp_path / "proposer").mkdir()
    (tmp_path / "proposer" / "expectations.jsonl").write_text(json.dumps({
        "round": 1, "captured": True,
        "expectations": [{"slot": 0, "expectation": "improvement",
                          "would_weaken": "neutral"}],
    }) + "\n")
    (tmp_path / "reflection").mkdir()
    (tmp_path / "reflection" / "history.jsonl").write_text(json.dumps({
        "round_id": 0, "handoff": "earlier warning",
        "self_limitation_suspected": False, "abstained": False, "note": "",
    }) + "\n")
    svc = MemoryService(
        tmp_path, {"objective": {"key": "SPEED_MS", "lower_is_better": True}})
    pack = svc.build_reflection_pack(current_round=2)
    assert "r1c0" in pack
    assert "earlier warning" in pack
    assert "improvement" in pack


def test_build_reflection_pack_safe_at_round_zero(tmp_path):
    svc = MemoryService(tmp_path, {"objective": {"key": "x",
                                                 "lower_is_better": True}})
    pack = svc.build_reflection_pack(current_round=0)
    assert "REFLECTION EVIDENCE PACK" in pack
    assert "no recorded task rounds yet" in pack


def test_prompt_templates_available():
    from proposer.prompts import load_semantic
    charter = load_semantic("reflection")
    assert "How might the recent version of me have failed" in charter
    # anti-churn guard: pre-registration bounds the demand for abandonment
    assert "bounded by" in charter or "pre-registration" in charter
    self_review = load_semantic("self_review")
    assert "Deliberation order (mandatory)" in self_review
    assert "Prosecution first" in self_review


def test_expectation_ledger_marks_not_performed_untested():
    from proposer.memory.reflection_views import expectation_ledger

    class _Exp:
        def __init__(self, status, selected=False, gate_passed=False):
            self.experiment_id = "r1c0"
            self.round = 1
            self.candidate = 0
            self.status = status
            self.selected = selected
            self.gate_passed = gate_passed
            self.metrics = {}

    rows = expectation_ledger(
        [_Exp("IMPLEMENTATION_INCOMPLETE"), _Exp("GATE_REJECTED"),
         _Exp("COMPLETED", gate_passed=True)],
        {1: {"expectations": [
            {"slot": 0, "expectation": "e", "would_weaken": "w"}]}},
        current_round=2,
    )
    outcomes = [row["outcome"] for row in rows]
    assert "INTERVENTION_NOT_PERFORMED" in outcomes
    assert "FAILED_GATES" in outcomes
    assert "PASSED_GATES_NOT_IMPROVED" in outcomes


def test_execution_outcomes_counts_by_cause():
    from proposer.memory.reflection_views import execution_outcomes

    history = [
        {"round": 0, "candidates": [
            {"status": "IMPLEMENTATION_INCOMPLETE",
             "eval_block": "[harness post-mortem] ...; stop_cause=timed_out;"},
            {"status": "COMPLETED", "eligible": True},
        ]},
        {"round": 1, "candidates": [
            {"status": "IMPLEMENTATION_INCOMPLETE",
             "eval_block": "...; stop_cause=session_ended_without_report;"},
            {"status": "WORKER_FAILED", "eval_block": "[loop failure] x"},
        ]},
    ]
    out = execution_outcomes(history)
    assert out["performed"] == 1
    assert out["not_performed_by_cause"] == {
        "IMPLEMENTATION_INCOMPLETE/timed_out": 1,
        "IMPLEMENTATION_INCOMPLETE/session_ended_without_report": 1,
        "WORKER_FAILED": 1,
    }
    # and the pack renders the section
    from proposer.memory.reflection_views import render_reflection_pack
    pack = render_reflection_pack(
        current_round=2, experiments=[], findings={}, history_rows=history,
        expectation_rows={}, previous_handoffs=[], metrics_schema={},
    )
    assert "## Experimenter session outcomes" in pack
    assert "reached evaluation: 1" in pack
    assert "session_ended_without_report" in pack
