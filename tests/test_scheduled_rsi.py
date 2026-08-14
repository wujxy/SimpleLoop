from __future__ import annotations

from types import SimpleNamespace

from simpleloop.persistence.journal import JobJournal
from simpleloop.scheduling.envelope import WorkerResult, WorkerStatus
from simpleloop.scheduling.rsi import ScheduledSelfReview, ScheduledViability


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


def test_scheduled_self_review_uses_declared_kind_and_returns_decision(tmp_path):
    envelope = WorkerResult(
        "self_review", "r5-self", WorkerStatus.COMPLETED,
        {"self_review": {"decision": "KEEP"}}, ({"model": "r"},),
    )
    telemetry = Telemetry()
    jobs = Jobs(JobJournal(tmp_path / "inflight.json"), envelope)
    reviewer = ScheduledSelfReview(
        run_dir=tmp_path, jobs=jobs, telemetry=telemetry,
        scientist_steps=12, prompt_dir=None,
    )

    result = reviewer.review(5)

    assert result == {"decision": "KEEP"}
    assert jobs.calls[0]["jobs"][0].kind == "self_review"
    assert telemetry.records == [{"model": "r"}]
    assert jobs.cleared == 1


def test_scheduled_self_review_resumes_persisted_payload(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("self_review", 7, {"payload": {
        "lane_id": 0, "round_id": 7, "base_sha": "",
        "run_dir": str(tmp_path), "workspace_path": "",
        "result_dir": str(tmp_path / "persisted"), "scientist_steps": 3,
    }}, [])
    jobs = Jobs(journal, WorkerResult(
        "self_review", "r7-self", WorkerStatus.COMPLETED,
        {"self_review": {"decision": "CHANGE"}},
    ))
    reviewer = ScheduledSelfReview(
        run_dir=tmp_path, jobs=jobs, telemetry=Telemetry(),
        scientist_steps=99, prompt_dir=None,
    )

    reviewer.review(7)

    assert jobs.calls[0]["jobs"][0].payload["scientist_steps"] == 3


def test_scheduled_viability_uses_viability_kind_and_clears(tmp_path):
    jobs = Jobs(JobJournal(tmp_path / "inflight.json"), WorkerResult(
        "viability", "viability-candidate", WorkerStatus.COMPLETED,
        {"status": "COMPLETED", "outcome": "abstain", "proposals": []},
    ))
    checker = ScheduledViability(jobs=jobs, telemetry=Telemetry())

    result = checker.check({
        "round_id": 0, "base_sha": "candidate",
        "result_dir": str(tmp_path / "result"),
    })

    assert result["status"] == "COMPLETED"
    assert jobs.calls[0]["jobs"][0].kind == "viability"
    assert jobs.cleared == 1
