"""Unit tests for the factual candidate store, gate views, and resume history."""
from __future__ import annotations

from pathlib import Path

import pytest

from simpleloop.stages import gate as views
from simpleloop.persistence import history as store_mod
from simpleloop.persistence.history import Store
from round_helpers import append_round


# --- Store: factual fields remain persisted in history ---

_STORE_SCHEMA = {"objective": {"key": "SPEED_MS", "lower_is_better": True},
                 "gates": [{"key": "CORRECTNESS"}]}


def test_store_persists_only_factual_candidate_fields(tmp_path: Path):
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    candidate = {
        "candidate": 0,
        "proposal": "replace the reconstruction kernel",
        "parent_sha": "parent",
        "sha": "candidate",
        "status": "COMPLETED",
        "metrics": {"SPEED_MS": 90.0, "CORRECTNESS": True},
        "changed_paths": ["src/new_kernel.cc"],
        "gates": {
            "PATHS": {"passed": True, "detail": ""},
            "EVAL_COMMANDS": {"passed": True, "detail": ""},
            "CORRECTNESS": {"passed": True, "detail": ""},
        },
        "gate_passed": True,
        "eligible": True,
        "selected": False,
    }

    append_round(store,
        0,
        parent_sha="parent",
        selected_candidate=0,
        selected_sha="candidate",
        candidates=[candidate],
    )

    row = store.history()[0]
    assert row["candidates"][0]["status"] == "COMPLETED"
    assert row["candidates"][0]["gates"]["PATHS"]["passed"] is True
    assert not ({"score", "risk", "feedback", "feedback_for_proposer",
                 "family", "decision", "accepted"}
                & row["candidates"][0].keys())


def test_append_round_records_abstained_round(tmp_path: Path):
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)

    append_round(store,
        0,
        parent_sha="parent",
        selected_candidate=None,
        selected_sha=None,
        candidates=[],
        abstention={
            "reason": "No mechanism had direct evidence.",
            "blocking_unknown": "whether QPDF is still hot",
        },
    )

    row = store.history()[0]
    assert row["candidates"] == []
    assert row["selected_sha"] is None
    assert row["base_sha"] == "parent"
    assert row["abstention"] == {
        "reason": "No mechanism had direct evidence.",
        "blocking_unknown": "whether QPDF is still hot",
    }


def test_append_round_omits_abstention_key_for_normal_round(
    tmp_path: Path,
):
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)

    append_round(store,
        0,
        parent_sha="parent",
        selected_candidate=None,
        selected_sha=None,
        candidates=[],
    )

    assert "abstention" not in store.history()[0]


def test_append_round_records_deliberation_telemetry(tmp_path: Path):
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    append_round(store,
        0,
        parent_sha="parent",
        selected_candidate=None,
        selected_sha=None,
        candidates=[],
        deliberation_telemetry={
            "steps": 7, "tool_calls": 4, "verification_status": "supported",
            "abandoned": False, "protocol_repairs": 1,
        },
    )
    row = store.history()[0]
    assert row["deliberation_telemetry"]["steps"] == 7
    assert row["deliberation_telemetry"]["verification_status"] == "supported"


def test_append_round_omits_telemetry_when_absent(tmp_path: Path):
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    append_round(store,
        0, parent_sha="parent", selected_candidate=None,
        selected_sha=None, candidates=[],
    )
    assert "deliberation_telemetry" not in store.history()[0]


def test_candidate_without_current_gate_facts_is_ineligible():
    incomplete = {
        "sha": "old",
        "metrics": {"SPEED_MS": 80.0, "CORRECTNESS": True},
    }

    assert store_mod.eligible(incomplete, _STORE_SCHEMA) is False


def test_new_candidate_cannot_override_failed_gate_with_eligible_flag():
    inconsistent = {
        "sha": "candidate",
        "gate_passed": False,
        "eligible": True,
        "metrics": {"SPEED_MS": 80.0, "CORRECTNESS": False},
    }

    assert store_mod.eligible(inconsistent, _STORE_SCHEMA) is False


@pytest.mark.parametrize("objective", [float("nan"), float("inf")])
def test_store_rejects_nonfinite_objective(objective: float):
    candidate = {
        "sha": "candidate",
        "gate_passed": True,
        "eligible": True,
        "metrics": {"SPEED_MS": objective, "CORRECTNESS": True},
    }

    assert store_mod.eligible(candidate, _STORE_SCHEMA) is False


# --- Store: changed_paths persisted (landing-state signal for the proposer) ---

def test_store_persists_changed_paths(tmp_path: Path):
    """append_round stores changed_paths so the proposer can see what each
    round touched without running git."""
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    append_round(store,
        0, parent_sha="parent", selected_candidate=0, selected_sha="sha0",
        candidates=[{
            "candidate": 0, "proposal": "p0", "sha": "sha0",
            "status": "COMPLETED", "gate_passed": True, "eligible": True,
            "changed_paths": ["a.cc", "a.h"],
        }])
    rows = store.history()
    assert rows[0]["changed_paths"] == ["a.cc", "a.h"]
    assert rows[0]["candidates"][0]["changed_paths"] == ["a.cc", "a.h"]


def test_store_persists_candidate_acceptance_and_resulting_base(tmp_path: Path):
    """A rejected implementation keeps its candidate SHA while the accepted
    base stays on the prior commit."""
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    append_round(store,
        0, parent_sha="baseline", selected_candidate=None, selected_sha=None,
        candidates=[{
            "candidate": 0, "proposal": "p0", "sha": "candidate0",
            "status": "COMPLETED", "gate_passed": False, "eligible": False,
            "metrics": {"CORRECTNESS": False}, "changed_paths": ["a.cc"],
        }])
    row = store.history()[0]
    assert row["candidates"][0]["sha"] == "candidate0"
    assert row["candidates"][0]["gate_passed"] is False
    assert row["candidates"][0]["eligible"] is False
    assert row["selected_sha"] is None
    assert row["base_sha"] == "baseline"


def test_store_changed_paths_default_empty(tmp_path: Path):
    """Candidates without changed_paths (gate-rejected / failed rounds) read
    back as an empty list, not a missing key — the proposer can rely on the
    field always being present."""
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    append_round(store,
        0, parent_sha="parent", selected_candidate=None, selected_sha=None,
        candidates=[{
            "candidate": 0, "proposal": "p0", "sha": "sha0",
            "status": "WORKER_FAILED", "gate_passed": False, "eligible": False,
        }])
    rows = store.history()
    assert rows[0]["candidates"][0]["changed_paths"] == []


def test_resume_chain_skips_rejected_tail_and_uses_last_accepted_metrics():
    from simpleloop import app as loop_mod

    history = [
        {"round": 0, "selected_candidate": 0, "selected_sha": "good",
         "candidates": [{"candidate": 0, "sha": "good", "selected": True,
                         "metrics": {"CORRECTNESS": True, "SPEED_MS": 100.0}}]},
        {"round": 1, "selected_candidate": None, "selected_sha": None,
         "candidates": [{"candidate": 0, "sha": "bad", "selected": False,
                         "metrics": {"CORRECTNESS": False}}]},
    ]
    sha, record = loop_mod._resume_chain(history, "baseline")
    assert sha == "good"
    assert record is history[0]["candidates"][0]
    assert record["metrics"]["SPEED_MS"] == 100.0


def test_resume_chain_falls_back_to_baseline_when_no_candidate_was_accepted():
    from simpleloop import app as loop_mod

    history = [
        {"round": 0, "selected_candidate": None, "selected_sha": None,
         "candidates": [{"candidate": 0, "sha": "bad", "selected": False,
                         "metrics": {"CORRECTNESS": False}}]},
    ]
    assert loop_mod._resume_chain(history, "baseline") == ("baseline", None)


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
