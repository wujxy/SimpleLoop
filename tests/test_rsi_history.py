from __future__ import annotations

import json
from dataclasses import replace

import pytest

from simpleloop.rsi.history import (
    JsonlSelfHistoryStore,
    SelfHistoryConflictError,
    candidate_adopted_event,
    candidate_created_event,
    candidate_rejected_event,
    initialized_event,
    reviewed_change_event,
    reviewed_keep_event,
)
from simpleloop.rsi.models import SelfChange, SelfDecision, SelfDecisionKind, SelfState


def change() -> SelfDecision:
    return SelfDecision(
        SelfDecisionKind.CHANGE,
        "repeated covered ground",
        change=SelfChange("prompt", "broaden search", "edit the charter", ("a",)),
    )


def keep() -> SelfDecision:
    return SelfDecision(
        SelfDecisionKind.KEEP,
        "progress is sufficient",
        keep_reason="sustained improvement",
        next_review_after_rounds=5,
    )


def initialized_store(tmp_path, sha="s0") -> JsonlSelfHistoryStore:
    store = JsonlSelfHistoryStore(tmp_path / "self")
    store.append(initialized_event(sha, 4))
    return store


def test_events_are_idempotent_and_conflicting_ids_fail(tmp_path):
    store = JsonlSelfHistoryStore(tmp_path / "self")
    event = initialized_event("seed", 4)

    store.append(event)
    store.append(event)

    assert store.events() == (event,)
    with pytest.raises(SelfHistoryConflictError, match="INITIALIZED"):
        store.append(replace(event, incumbent_sha="other"))


def test_keep_is_terminal_and_advances_commitment(tmp_path):
    store = initialized_store(tmp_path)

    store.append(reviewed_keep_event(4, "s0", keep(), 9))

    assert store.state() == SelfState("s0", 9, 4)


def test_projection_adopts_only_from_terminal_event(tmp_path):
    store = initialized_store(tmp_path)
    store.append(reviewed_change_event(4, "s0", change()))
    store.append(candidate_created_event(4, "s0", "s1", "prompt.py"))

    assert store.state().active_sha == "s0"
    assert store.state().last_review_round is None

    store.append(candidate_adopted_event(4, "s0", "s1", 12, "viable"))

    assert store.state() == SelfState("s1", 12, 4)


def test_rejection_retains_incumbent_and_records_terminal_review(tmp_path):
    store = initialized_store(tmp_path)
    store.append(reviewed_change_event(4, "s0", change()))
    store.append(candidate_rejected_event(4, "s0", 12, "no changes"))

    assert store.state() == SelfState("s0", 12, 4)
    row = json.loads(store.review_view_path.read_text().strip())
    assert row["decision"] == "CHANGE"
    assert row["adopted"] is False
    assert row["change"]["target"] == "prompt"
    assert row["detail"] == "no changes"


def test_review_view_is_rebuilt_from_terminal_events(tmp_path):
    store = initialized_store(tmp_path)
    store.append(reviewed_keep_event(4, "s0", keep(), 9))
    store.review_view_path.unlink()

    path = store.ensure_review_view()

    row = json.loads(path.read_text().strip())
    assert row["round"] == 4
    assert row["decision"] == "KEEP"
    assert row["keep_reason"] == "sustained improvement"


def test_out_of_order_adoption_is_rejected(tmp_path):
    store = initialized_store(tmp_path)

    with pytest.raises(ValueError, match="reviewed CHANGE"):
        store.append(candidate_adopted_event(4, "s0", "s1", 12, "viable"))

    assert len(store.events()) == 1
