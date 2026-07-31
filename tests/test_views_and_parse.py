"""Unit tests for factual Researcher views, history, and configuration."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod
from simpleloop.harness import views
from simpleloop.harness import store as store_mod
from simpleloop.harness.store import Store


# --- views.for_proposer: projects landing-state signals to the proposer ---

def test_for_proposer_projects_only_factual_candidate_state():
    history = [{
        "round": 0,
        "parent_sha": "base",
        "selected_candidate": 0,
        "selected_sha": "candidate",
        "base_sha": "candidate",
        "candidates": [{
            "candidate": 0,
            "proposal": "rewrite kernel",
            "parent_sha": "base",
            "sha": "candidate",
            "status": "COMPLETED",
            "selected": True,
            "gate_passed": True,
            "eligible": True,
            "gates": {"PATHS": {"passed": True, "detail": ""}},
            "metrics": {"SPEED_MS": 90.0},
            "changed_paths": ["src/kernel.cc"],
            "eval_block": "raw evaluator output",
            "feedback": "legacy narrative",
            "score": 0.9,
        }],
    }]

    projected = views.for_proposer(history)

    assert projected[0]["candidates"] == [{
        "candidate": 0,
        "proposal": "rewrite kernel",
        "parent_sha": "base",
        "sha": "candidate",
        "status": "COMPLETED",
        "selected": True,
        "gate_passed": True,
        "eligible": True,
        "gates": {"PATHS": {"passed": True, "detail": ""}},
        "metrics": {"SPEED_MS": 90.0},
        "changed_paths": ["src/kernel.cc"],
    }]

def test_for_proposer_empty_history():
    assert views.for_proposer([]) == []


def test_for_proposer_preserves_order():
    history = [_serial_history_record(r, f"p{r}") for r in range(5)]
    out = views.for_proposer(history)
    assert [row["round"] for row in out] == [0, 1, 2, 3, 4]


def _serial_history_record(round_id: int, proposal: str) -> dict:
    """A one-candidate generation record (the shape append_generation writes)."""
    return {
        "round": round_id,
        "parent_sha": f"parent-{round_id}",
        "selected_candidate": 0,
        "selected_sha": f"sha-{round_id}",
        "base_sha": f"sha-{round_id}",
        "candidates": [
            {
                "candidate": 0,
                "proposal": proposal,
                "parent_sha": f"parent-{round_id}",
                "sha": f"sha-{round_id}",
                "status": "COMPLETED",
                "selected": True,
                "gate_passed": True,
                "eligible": True,
                "gates": {"CORRECTNESS": {"passed": True, "detail": ""}},
                "metrics": {"SPEED_MS": 500.0 + round_id},
                "changed_paths": ["src/a.cc"],
                "eval_block": "raw output",
            }
        ],
    }


def _parallel_history_record(round_id: int, proposals: list[str]) -> dict:
    return {
        "round": round_id,
        "parent_sha": f"parent-{round_id}",
        "selected_candidate": 1,
        "selected_sha": f"candidate-{round_id}-1",
        "base_sha": f"candidate-{round_id}-1",
        "candidates": [
            {
                "candidate": candidate_id,
                "proposal": proposal,
                "parent_sha": f"parent-{round_id}",
                "sha": f"candidate-{round_id}-{candidate_id}",
                "status": "COMPLETED",
                "selected": candidate_id == 1,
                "gate_passed": True,
                "eligible": True,
                "gates": {"CORRECTNESS": {"passed": True, "detail": ""}},
                "metrics": {"SPEED_MS": 600.0 - candidate_id},
                "changed_paths": [f"src/c{candidate_id}.cc"],
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
    assert [row["candidates"][0]["proposal"] for row in projected] == [
        f"proposal-{round_id}" for round_id in round_ids[1:]
    ]
    assert all(
        "proposal_head" not in c for row in projected for c in row["candidates"]
    )


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

    store.append_generation(
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


def test_legacy_high_risk_candidate_stays_ineligible():
    legacy = {
        "sha": "old",
        "risk": "high",
        "metrics": {"SPEED_MS": 80.0, "CORRECTNESS": True},
    }

    assert store_mod.eligible(legacy, _STORE_SCHEMA) is False


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
    """append_generation stores changed_paths so the proposer can see what each
    round touched without running git."""
    store = Store(tmp_path, metrics_schema=_STORE_SCHEMA)
    store.append_generation(
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
    store.append_generation(
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
    store.append_generation(
        0, parent_sha="parent", selected_candidate=None, selected_sha=None,
        candidates=[{
            "candidate": 0, "proposal": "p0", "sha": "sha0",
            "status": "WORKER_FAILED", "gate_passed": False, "eligible": False,
        }])
    rows = store.history()
    assert rows[0]["candidates"][0]["changed_paths"] == []


def test_resume_chain_skips_rejected_tail_and_uses_last_accepted_metrics():
    from simpleloop import loop as loop_mod

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
    from simpleloop import loop as loop_mod

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


def _write_min_task(tmp_path: Path, loop: dict) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"fake-sif")
    raw = {
        "kind": "task",
        "task": {"goal": "test"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": loop,
        "source": {"path": str(repo)},
        "runtime": {"image": str(image)},
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [{"key": "CORRECTNESS"}],
            },
        },
    }
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_config_proposer_recent_rounds_defaults_to_three(tmp_path: Path):
    cfg = config_mod.load(_write_min_task(tmp_path, {"max_rounds": 1}))
    assert cfg["proposer_recent_rounds"] == 6


def test_config_proposer_recent_rounds_is_configurable(tmp_path: Path):
    cfg = config_mod.load(
        _write_min_task(tmp_path, {"max_rounds": 1, "proposer_recent_rounds": 5})
    )
    assert cfg["proposer_recent_rounds"] == 5


def test_config_proposer_recent_rounds_rejects_non_positive(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="proposer_recent_rounds"):
        config_mod.load(
            _write_min_task(tmp_path, {"max_rounds": 1, "proposer_recent_rounds": 0})
        )
