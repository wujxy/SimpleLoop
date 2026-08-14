"""Golden tests for the durable protocol shapes at the Phase 0 boundary."""
from __future__ import annotations

import json
from pathlib import Path

from simpleloop.candidate_worker import CandidateDeps, CandidateSpec, run_candidate
from simpleloop.execution.proposer_lanes import (
    read_lane_result,
    read_self_review_result,
)
from simpleloop.harness.evals import EvalResult
from simpleloop.harness.store import Store
from simpleloop.loop import _InflightJournal, _load_inflight
from simpleloop.roles.executor import ExecResult


FIXTURES = Path(__file__).parent / "fixtures" / "phase0"
SCHEMA = {
    "objective": {"key": "SPEED_MS", "lower_is_better": True},
    "gates": [{"key": "CORRECTNESS"}],
}


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _write_result(tmp_path: Path, name: str) -> Path:
    result_dir = tmp_path / name.removesuffix(".json")
    result_dir.mkdir()
    (result_dir / "result.json").write_text(
        json.dumps(load(name), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result_dir


def test_candidate_result_shape(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "simpleloop.candidate_worker.executor_mod.execute",
        lambda *args, **kwargs: ExecResult(
            sha="child",
            reason=None,
            changed_paths=["src/cache.cc"],
            path_gate_passed=True,
            path_gate_violations=[],
        ),
    )
    monkeypatch.setattr(
        "simpleloop.candidate_worker.evals.run_eval",
        lambda *args, **kwargs: EvalResult(
            "SPEED_MS=90\nCORRECTNESS=PASS",
            {"SPEED_MS": 90.0, "CORRECTNESS": True},
            (0,),
        ),
    )
    deps = CandidateDeps(
        cfg={
            "goal": "make it faster",
            "eval_commands": ["evaluate"],
            "metrics": SCHEMA,
        },
        run_dir=tmp_path,
        runtime=object(),
        workspace=object(),
        executor_agent=object(),
    )
    spec = CandidateSpec(
        round_id=2,
        candidate_id=1,
        parent_sha="parent",
        proposal="cache the transform",
        worktree_path=str(tmp_path / "worktree"),
    )

    assert run_candidate(deps, spec) == load("candidate-result.json")


def test_proposer_lane_result_shape(tmp_path: Path):
    result_dir = _write_result(tmp_path, "proposer-lane-result.json")
    assert read_lane_result(result_dir) == load("proposer-lane-result.json")


def test_self_review_result_shape(tmp_path: Path):
    result_dir = _write_result(tmp_path, "self-review-result.json")
    assert read_self_review_result(result_dir) == load("self-review-result.json")


def test_history_round_shape(tmp_path: Path):
    store = Store(tmp_path, metrics_schema=SCHEMA)
    store.append_generation(
        2,
        parent_sha="parent",
        selected_candidate=1,
        selected_sha="child",
        candidates=[load("candidate-result.json")],
    )

    assert store.history() == [load("history-round.json")]


def test_inflight_round_shape(tmp_path: Path):
    expected = load("inflight-round.json")
    journal = _InflightJournal(
        tmp_path / "inflight_round.json",
        meta={key: value for key, value in expected.items() if key != "jobs"},
    )
    journal.save(expected["jobs"])

    assert _load_inflight(tmp_path) == expected
