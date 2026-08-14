from __future__ import annotations

import json
from types import SimpleNamespace

from simpleloop.persistence.journal import JobJournal
from simpleloop.rsi.models import (
    SelfChange,
    SelfDecisionKind,
    SelfEditRequest,
    SelfReviewRequest,
    ViabilityRequest,
)
from simpleloop.scheduling.envelope import WorkerResult, WorkerStatus
from simpleloop.scheduling.rsi import (
    ScheduledSelfEditor,
    ScheduledSelfReviewer,
    ScheduledViabilityChecker,
)
from simpleloop.world import SourceWorkspace


class Telemetry:
    def __init__(self):
        self.records = []

    def record_usage(self, row):
        self.records.append(row)


class Jobs:
    def __init__(self, journal, envelope):
        self.journal = journal
        self.envelope = envelope
        self.calls = []
        self.cleared = 0

    def inflight(self):
        return self.journal.load()

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            completed=(SimpleNamespace(result=self.envelope),), outcomes=(),
        )

    def clear(self):
        self.cleared += 1
        self.journal.clear()


def reviewer(tmp_path, jobs, telemetry=None):
    return ScheduledSelfReviewer(
        run_dir=tmp_path,
        jobs=jobs,
        telemetry=telemetry or Telemetry(),
        scientist_steps=12,
        prompt_dir=None,
    )


def review_request(tmp_path, round_id=5):
    body = tmp_path / "self" / "repo"
    body.mkdir(parents=True, exist_ok=True)
    reviews = tmp_path / "self" / "reviews.jsonl"
    reviews.touch()
    return SelfReviewRequest(round_id, "goal", "s0", body, reviews)


def test_review_uses_explicit_body_history_and_returns_typed_decision(tmp_path):
    envelope = WorkerResult(
        "self_review",
        "r5-self",
        WorkerStatus.COMPLETED,
        {"self_review": {
            "decision": "KEEP",
            "diagnosis": "progress",
            "keep_reason": "enough",
            "next_review_after_rounds": 5,
        }},
        ({"model": "r"},),
    )
    telemetry = Telemetry()
    jobs = Jobs(JobJournal(tmp_path / "inflight.json"), envelope)

    result = reviewer(tmp_path, jobs, telemetry).review(review_request(tmp_path))

    job = jobs.calls[0]["jobs"][0]
    assert result.kind is SelfDecisionKind.KEEP
    assert job.kind == "self_review"
    assert job.payload["self_repo"] == str(tmp_path / "self" / "repo")
    assert job.payload["reviews_path"] == str(
        tmp_path / "self" / "reviews.jsonl"
    )
    assert job.payload["incumbent_self_sha"] == "s0"
    assert telemetry.records == [{"model": "r"}]
    assert jobs.cleared == 0


def test_review_resumes_persisted_payload(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    payload = {
        "lane_id": 0,
        "round_id": 7,
        "base_sha": "s0",
        "run_dir": str(tmp_path),
        "self_repo": str(tmp_path / "persisted-body"),
        "reviews_path": str(tmp_path / "persisted-reviews"),
        "incumbent_self_sha": "s0",
        "result_dir": str(tmp_path / "persisted"),
        "scientist_steps": 3,
    }
    journal.begin("self_review", 7, {"payload": payload}, [])
    jobs = Jobs(journal, WorkerResult(
        "self_review",
        "r7-self",
        WorkerStatus.COMPLETED,
        {"self_review": {
            "decision": "KEEP", "diagnosis": "d", "keep_reason": "k",
            "next_review_after_rounds": 3,
        }},
    ))

    reviewer(tmp_path, jobs).review(review_request(tmp_path, 7))

    assert jobs.calls[0]["jobs"][0].payload["scientist_steps"] == 3
    assert jobs.calls[0]["jobs"][0].payload["self_repo"].endswith(
        "persisted-body"
    )


def test_edit_transitions_review_stage_and_never_clears(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("self_review", 5, {"payload": {}}, [])
    jobs = Jobs(journal, WorkerResult(
        "self_edit",
        "r5-self-edit",
        WorkerStatus.COMPLETED,
        {"self_edit": {"status": "EDITED", "output": "done"}},
    ))
    editor = ScheduledSelfEditor(
        run_dir=tmp_path, jobs=jobs, telemetry=Telemetry(), prompt_dir=None,
    )
    workspace = SourceWorkspace("self-5", tmp_path / "body", "s0")
    workspace.path.mkdir()

    result = editor.edit(SelfEditRequest(
        5, SelfChange("prompt", "broaden", "edit charter"), workspace,
    ))

    assert result.status == "EDITED"
    assert jobs.calls[0]["transition_from"] == "self_review"
    assert jobs.calls[0]["jobs"][0].kind == "self_edit"
    assert jobs.calls[0]["jobs"][0].workspace == workspace
    assert jobs.cleared == 0


def test_viability_transitions_from_self_edit_and_classifies_lane(tmp_path):
    (tmp_path / "config.resolved.json").write_text(
        json.dumps({"goal": "g"}), encoding="utf-8"
    )
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("self_edit", 5, {"payload": {}}, [])
    jobs = Jobs(journal, WorkerResult(
        "viability",
        "r5-viability",
        WorkerStatus.COMPLETED,
        {"status": "COMPLETED", "outcome": "abstain", "proposals": []},
    ))
    body = tmp_path / "candidate"
    body.mkdir()
    checker = ScheduledViabilityChecker(
        run_dir=tmp_path, jobs=jobs, telemetry=Telemetry(), scientist_steps=20,
    )

    result = checker.check(ViabilityRequest(5, "s1", body))

    assert result.viable is True
    assert jobs.calls[0]["transition_from"] == "self_edit"
    assert jobs.calls[0]["jobs"][0].kind == "viability"
    assert jobs.calls[0]["jobs"][0].payload["self_repo"] == str(body)
    assert jobs.cleared == 0
