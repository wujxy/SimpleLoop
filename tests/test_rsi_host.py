"""S3c.2 Host tests: the self-review result reader, the loop's self-review round
helper (_run_self_review_round), and the --continue resume fix that accounts for
self-review rounds. All deterministic (no model, no subprocess).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from simpleloop.execution.proposer_lanes import read_self_review_result
from simpleloop.loop import _run_self_review_round, _starting_state
from simpleloop.self_repo import SelfRepo, ViabilityResult


# --- read_self_review_result (the self-mode result.json reader) ------------

def _valid_self_review_result() -> dict:
    return {
        "status": "COMPLETED", "mode": "self", "lane_id": 0, "round_id": 5,
        "self_review": {
            "contract_version": "proposer-cli-v0",
            "incumbent_self_sha": "abc123",
            "decision": "KEEP", "diagnosis": "d", "keep_reason": "r",
            "next_review_after_rounds": 5, "self_change": None, "abstained": False,
        },
        "trace": {}, "telemetry": {},
    }


def _write_result(result_dir: Path, obj: dict) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    (result_dir / "result.json").write_text(json.dumps(obj), encoding="utf-8")
    return result_dir


def test_read_self_review_result_accepts_self_shape(tmp_path: Path):
    res = read_self_review_result(_write_result(tmp_path / "r", _valid_self_review_result()))
    assert res["mode"] == "self"
    assert res["self_review"]["decision"] == "KEEP"


def test_read_self_review_result_rejects_lane_shape(tmp_path: Path):
    # a lane result (proposals list, no self_review) must be rejected
    lane = {"status": "COMPLETED", "outcome": "submit", "proposals": []}
    with pytest.raises(ValueError):
        read_self_review_result(_write_result(tmp_path / "r", lane))


def test_read_self_review_result_rejects_wrong_mode(tmp_path: Path):
    bad = _valid_self_review_result()
    bad["mode"] = "task"
    with pytest.raises(ValueError):
        read_self_review_result(_write_result(tmp_path / "r", bad))


def test_read_self_review_result_rejects_missing_file(tmp_path: Path):
    with pytest.raises(ValueError):
        read_self_review_result(tmp_path / "nope")


# --- _run_self_review_round (the loop's self-review round helper) ----------

def _host_ctx(run_dir: Path, payload: dict) -> SimpleNamespace:
    sr = SelfRepo(run_dir)
    sr.setup(resume=False)
    eb = SimpleNamespace(run_self_review=lambda *, round_id: payload)
    # A no-op self-executor so the CHANGE path runs transition but produces no
    # candidate (the defer logic is what's under test in those cases). The
    # full adoption path has its own test with a real-editing fake + mocked smoke.
    noop_exec = SimpleNamespace(run_text=lambda *a, **k: "")
    return SimpleNamespace(
        self_repo=sr, execution_backend=eb, run_dir=run_dir,
        self_executor_agent=noop_exec)


def _keep_payload(sha: str, *, defer: int = 5) -> dict:
    return {"decision": "KEEP", "diagnosis": "progress sufficient",
            "keep_reason": "strong sustained progress",
            "next_review_after_rounds": defer, "self_change": None,
            "incumbent_self_sha": sha, "abstained": False}


def _change_payload(sha: str) -> dict:
    return {"decision": "CHANGE", "diagnosis": "I repeat covered ground",
            "keep_reason": None, "next_review_after_rounds": None,
            "self_change": {"target": "prompt", "intent": "sharpen coverage",
                            "instruction": "add a coverage preamble",
                            "evidence_refs": ["proposer/scientist.py"]},
            "incumbent_self_sha": sha, "abstained": False}


def test_run_self_review_round_keep(tmp_path: Path):
    sr = SelfRepo(tmp_path)
    sr.setup(resume=False)
    ctx = _host_ctx(tmp_path, _keep_payload(sr.active_self_sha, defer=5))

    decision = _run_self_review_round(ctx, round_id=5)

    assert decision == "KEEP"
    # commitment advanced to round_id + defer
    assert ctx.self_repo.next_self_review_round == 10
    # reviews.jsonl got one contract-§9.2 record
    rec = json.loads(ctx.self_repo.reviews_path.read_text().strip())
    assert rec["round"] == 5 and rec["decision"] == "KEEP"
    assert rec["next_review_round"] == 10
    assert rec["change"] is None and rec["adopted"] is None  # S3d fields null
    assert rec["incumbent_self_sha"] == sr.active_self_sha


def test_run_self_review_round_change_uses_default_defer(tmp_path: Path):
    """A CHANGE with no suggested commitment defers by _DEFAULT_SELF_REVIEW_DEFER
    (it would otherwise churn re-suggesting an unactioned change)."""
    sr = SelfRepo(tmp_path)
    sr.setup(resume=False)
    ctx = _host_ctx(tmp_path, _change_payload(sr.active_self_sha))

    decision = _run_self_review_round(ctx, round_id=8)

    assert decision == "CHANGE"
    from simpleloop.loop import _DEFAULT_SELF_REVIEW_DEFER
    assert ctx.self_repo.next_self_review_round == 8 + _DEFAULT_SELF_REVIEW_DEFER
    rec = json.loads(ctx.self_repo.reviews_path.read_text().strip())
    assert rec["decision"] == "CHANGE"
    assert rec["change"]["target"] == "prompt"


def test_run_self_review_round_change_drives_full_adoption(tmp_path, monkeypatch):
    """S3d: a CHANGE drives the full transition — self-executor edits -> candidate
    -> smoke -> adopt — and the reviews.jsonl record carries candidate/viable/
    adopted. check_viability is mocked so the verdict is deterministic (the real
    smoke runs a model episode)."""
    sr = SelfRepo(tmp_path)
    sr.setup(resume=False)
    old_sha = sr.active_self_sha
    monkeypatch.setattr(
        "simpleloop.self_repo.check_viability",
        lambda candidate_repo, run_dir: ViabilityResult(True, "smoke COMPLETED"))

    class _EditingAgent:
        def run_text(self, prompt, *, cwd, label="agent"):
            (Path(cwd) / "proposer" / "prompts" / "proposer.md").write_text(
                "# self-revised\n", encoding="utf-8")
            return "done"

    ctx = SimpleNamespace(
        self_repo=sr,
        execution_backend=SimpleNamespace(
            run_self_review=lambda *, round_id: _change_payload(sr.active_self_sha)),
        self_executor_agent=_EditingAgent(),
        run_dir=tmp_path,
    )

    decision = _run_self_review_round(ctx, round_id=8)

    assert decision == "CHANGE"
    rec = json.loads(sr.reviews_path.read_text().strip())
    assert rec["decision"] == "CHANGE"
    assert rec["candidate_self_sha"] is not None
    assert rec["viable"] is True and rec["adopted"] is True
    assert sr.active_self_sha == rec["candidate_self_sha"]   # active advanced
    assert sr.active_self_sha != old_sha


# --- _starting_state resume fix (self-review rounds don't write history) ---

def _task_history(n: int) -> list[dict]:
    return [
        {"round": i, "selected_sha": f"sha{i}",
         "candidates": [{"candidate": i, "selected": True, "metrics": {"OBJ": i}}]}
        for i in range(n)
    ]


def _resume_ctx(tmp_path: Path, history: list[dict], last_self) -> SimpleNamespace:
    sr = SelfRepo(tmp_path)
    sr.setup(resume=False)
    if last_self is not None:
        sr.append_review(last_self, payload=_keep_payload("x", defer=1),
                         next_review_round=last_self + 1)
    return SimpleNamespace(
        self_repo=sr,
        store=SimpleNamespace(history=lambda: history),
        workspace=SimpleNamespace(baseline_sha=lambda: "abcdef1234"),
        execution_backend=SimpleNamespace(
            eval_baseline=lambda *, baseline_sha: ("", {"OBJ": 1.0})),
        telemetry=SimpleNamespace(),
        baseline_metrics={},
    )


def test_starting_state_resume_after_self_review(tmp_path: Path):
    # task rounds 0-4 in history (5 lines) + a self-review at round 5
    ctx = _resume_ctx(tmp_path, _task_history(5), last_self=5)
    start = _starting_state(ctx, continue_run=True, n_rounds=10)
    assert start is not None
    start_round, parent_sha, _prior = start
    assert start_round == 6  # NOT len(history)=5 — the self-review at r5 consumed a slot
    assert parent_sha == "sha4"  # from the last TASK round (self-review didn't move it)


def test_starting_state_resume_without_self_review_is_unchanged(tmp_path: Path):
    # no self-reviews → resume == len(history) (the historical behavior)
    ctx = _resume_ctx(tmp_path, _task_history(5), last_self=None)
    start = _starting_state(ctx, continue_run=True, n_rounds=10)
    assert start[0] == 5
