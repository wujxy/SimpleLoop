"""S3c.2 Host tests: self-review transition and resume behavior.

All deterministic (no model, no subprocess).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from simpleloop.loop import _starting_state
from simpleloop.self_repo import LegacyRsiRunner, SelfRepo, ViabilityResult


# --- _run_self_review_round (the loop's self-review round helper) ----------

def _runner(run_dir: Path, payload: dict, *, executor=None, viability=None):
    sr = SelfRepo(run_dir)
    sr.setup(resume=False)
    return LegacyRsiRunner(
        self_repo=sr,
        reviewer=SimpleNamespace(review=lambda round_id: payload),
        executor=executor or SimpleNamespace(run_text=lambda *a, **k: ""),
        viability=viability,
        run_dir=run_dir,
    )


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
    runner = _runner(tmp_path, _keep_payload(sr.active_self_sha, defer=5))

    result = runner.run(5)

    assert result.decision == "KEEP"
    # commitment advanced to round_id + defer
    assert runner.self_repo.next_self_review_round == 10
    # reviews.jsonl got one contract-§9.2 record
    rec = json.loads(runner.self_repo.reviews_path.read_text().strip())
    assert rec["round"] == 5 and rec["decision"] == "KEEP"
    assert rec["next_review_round"] == 10
    assert rec["change"] is None and rec["adopted"] is None  # S3d fields null
    assert rec["incumbent_self_sha"] == sr.active_self_sha


def test_run_self_review_round_change_uses_default_defer(tmp_path: Path):
    """A CHANGE with no suggested commitment defers by _DEFAULT_SELF_REVIEW_DEFER
    (it would otherwise churn re-suggesting an unactioned change)."""
    sr = SelfRepo(tmp_path)
    sr.setup(resume=False)
    runner = _runner(tmp_path, _change_payload(sr.active_self_sha))

    result = runner.run(8)

    assert result.decision == "CHANGE"
    assert runner.self_repo.next_self_review_round == 16
    rec = json.loads(runner.self_repo.reviews_path.read_text().strip())
    assert rec["decision"] == "CHANGE"
    assert rec["change"]["target"] == "prompt"


def test_run_self_review_round_change_drives_full_adoption(tmp_path):
    """S3d: a CHANGE drives the full transition — self-executor edits -> candidate
    -> smoke -> adopt — and the reviews.jsonl record carries candidate/viable/
    adopted. check_viability is mocked so the verdict is deterministic (the real
    smoke runs a model episode)."""
    sr = SelfRepo(tmp_path)
    sr.setup(resume=False)
    old_sha = sr.active_self_sha
    class _EditingAgent:
        def run_text(self, prompt, *, cwd, label="agent"):
            (Path(cwd) / "proposer" / "prompts" / "proposer.md").write_text(
                "# self-revised\n", encoding="utf-8")
            return "done"

    runner = LegacyRsiRunner(
        self_repo=sr,
        reviewer=SimpleNamespace(
            review=lambda round_id: _change_payload(sr.active_self_sha)),
        executor=_EditingAgent(), run_dir=tmp_path,
        viability=lambda candidate, run_dir: ViabilityResult(
            True, "smoke COMPLETED"),
    )

    result = runner.run(8)

    assert result.decision == "CHANGE"
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
