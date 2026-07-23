from __future__ import annotations

from simpleloop import loop as loop_mod
from simpleloop.loop import _record_failure, _run_candidates
from simpleloop.proposer import Proposal
from simpleloop.store import Store


class SnapshotTracker:
    def __init__(self):
        self.value = 0
        self.persist_flags = []

    def snapshot(self, *, persist=False):
        self.value += 1
        self.persist_flags.append(persist)
        return {
            "worktime_seconds": float(self.value),
            "processed_tokens": self.value * 10,
        }

    def plot_context(self):
        return {}


def test_store_persists_serial_telemetry(tmp_path):
    store = Store(tmp_path)
    snapshot = {"worktime_seconds": 12.5, "processed_tokens": 100}

    store.append(
        0,
        "proposal",
        "sha",
        0.5,
        "feedback",
        telemetry=snapshot,
    )

    assert store.history()[0]["telemetry"] == snapshot


def test_store_persists_candidate_and_generation_telemetry(tmp_path):
    store = Store(tmp_path)
    candidate_snapshot = {"worktime_seconds": 2.0, "processed_tokens": 10}
    generation_snapshot = {"worktime_seconds": 3.0, "processed_tokens": 12}

    store.append_generation(
        0,
        parent_sha="base",
        selected_candidate=0,
        selected_sha="sha",
        candidates=[{
            "candidate": 0,
            "sha": "sha",
            "telemetry": candidate_snapshot,
        }],
        telemetry=generation_snapshot,
    )

    row = store.history()[0]
    assert row["telemetry"] == generation_snapshot
    assert row["candidates"][0]["telemetry"] == candidate_snapshot


def test_run_candidates_attaches_persisted_snapshot_after_each_worker(
    monkeypatch,
):
    def fake_candidate(candidate_id, proposal, *_args):
        return {
            "candidate": candidate_id,
            "proposal": proposal.proposal,
            "score": 0.5,
        }

    monkeypatch.setattr(loop_mod, "_run_one_candidate", fake_candidate)
    tracker = SnapshotTracker()

    candidates = _run_candidates(
        [Proposal("p0"), Proposal("p1")],
        0,
        "base",
        {"max_workers": 2},
        object(),
        object(),
        object(),
        {},
        {},
        None,
        "",
        tracker,
    )

    assert {c["candidate"] for c in candidates} == {0, 1}
    assert {
        c["telemetry"]["worktime_seconds"] for c in candidates
    } == {1.0, 2.0}
    assert tracker.persist_flags == [True, True]


def test_record_failure_persists_completion_snapshot(monkeypatch, tmp_path):
    store = Store(tmp_path)
    tracker = SnapshotTracker()
    monkeypatch.setattr(loop_mod, "_refresh_progress_plot", lambda *_args: None)

    _record_failure(
        store,
        round_id=0,
        proposal="proposal",
        reason="boom",
        base_sha="base",
        telemetry_tracker=tracker,
    )

    assert store.history()[0]["telemetry"] == {
        "worktime_seconds": 1.0,
        "processed_tokens": 10,
    }
    assert tracker.persist_flags == [True]
