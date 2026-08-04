"""Tests for the append-only FindingStore."""
from __future__ import annotations

from pathlib import Path

import pytest

from simpleloop.memory.finding_store import FindingStore
from simpleloop.memory.models import Finding


def _finding(id: str = "F-001", *, state: str = "active",
             experiment_refs=()) -> Finding:
    return Finding(
        id=id,
        question=f"question for {id}",
        mechanisms=("hoist",),
        code_regions=("src/",),
        state=state,
        created_round=0,
        last_touched_round=0,
        experiment_refs=tuple(experiment_refs),
        parent_finding_id=None,
        stats={"attempts": 0, "eligible": 0, "selected": 0,
               "best_objective": None},
    )


def test_append_creates_file(tmp_path: Path):
    store = FindingStore(tmp_path)
    assert store.load_all() == {}
    store.append(_finding())
    assert store.path.is_file()
    assert set(store.load_all()) == {"F-001"}


def test_load_uses_last_record_per_id(tmp_path: Path):
    store = FindingStore(tmp_path)
    store.append(_finding("F-001", state="open"))
    store.append(_finding("F-001", state="active",
                          experiment_refs=("r0c0",)))
    store.append(_finding("F-002", state="dormant"))
    findings = store.load_all()
    assert findings["F-001"].state == "active"
    assert findings["F-001"].experiment_refs == ("r0c0",)
    assert findings["F-002"].state == "dormant"


def test_allocate_next_id_is_monotonic(tmp_path: Path):
    store = FindingStore(tmp_path)
    assert store.allocate_next_id() == "F-001"
    store.append(_finding("F-001"))
    assert store.allocate_next_id() == "F-002"
    store.append(_finding("F-042"))
    assert store.allocate_next_id() == "F-043"


def test_torn_last_line_is_skipped(tmp_path: Path):
    store = FindingStore(tmp_path)
    store.append(_finding("F-001"))
    with store.path.open("a", encoding="utf-8") as f:
        f.write('{"id": "F-002", "state": "active"')  # no newline, no closing
    findings = store.load_all()
    assert set(findings) == {"F-001"}


def test_finding_rejects_bad_state():
    with pytest.raises(ValueError):
        Finding(
            id="F-001",
            question="q",
            mechanisms=(),
            code_regions=(),
            state="bogus",
            created_round=0,
            last_touched_round=0,
        )


def test_get_and_exists(tmp_path: Path):
    store = FindingStore(tmp_path)
    assert store.get("F-001") is None
    assert not store.exists("F-001")
    store.append(_finding())
    assert store.exists("F-001")
    assert store.get("F-001").question == "question for F-001"
