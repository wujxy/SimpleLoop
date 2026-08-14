from __future__ import annotations

import pytest

from simpleloop.persistence.journal import JobJournal
from simpleloop.scheduling.envelope import ProtocolError


def test_journal_preserves_stage_context_and_jobs(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("candidates", 3, {"parent_sha": "abc"}, [])
    journal.save_jobs([{"request_id": "r3-c0", "attempt": 1}])

    record = journal.load()

    assert record is not None
    assert record.stage == "candidates"
    assert record.context == {"parent_sha": "abc"}
    assert record.jobs[0]["request_id"] == "r3-c0"
    journal.clear()
    assert journal.load() is None


def test_journal_refuses_to_replace_a_different_active_stage(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("proposer", 1, {}, [])

    with pytest.raises(ProtocolError, match="active stage"):
        journal.begin("candidates", 1, {}, [])


def test_journal_rejects_unknown_schema(tmp_path):
    path = tmp_path / "inflight.json"
    path.write_text('{"schema":"wrong","round_id":1,"stage":"proposer",'
                    '"context":{},"jobs":[]}')

    with pytest.raises(ProtocolError, match="schema"):
        JobJournal(path).load()
