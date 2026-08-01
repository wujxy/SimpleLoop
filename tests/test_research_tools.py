from __future__ import annotations

import json

import pytest

from simpleloop.roles.research_tools import (
    Insight,
    InsightStore,
    render_insights,
    search_history,
)


def _candidate(round_id: int, *, proposal: str = "cache values") -> dict:
    return {
        "candidate": 0,
        "proposal": proposal,
        "parent_sha": f"parent-{round_id}",
        "sha": f"sha-{round_id}",
        "status": "COMPLETED",
        "selected": True,
        "gate_passed": True,
        "eligible": True,
        "gates": {"physics_gate": {"passed": True}},
        "metrics": {"SPEED_MS": 100 - round_id},
        "changed_paths": [f"src/r{round_id}.cc"],
        "eval_block": f"SPEED_MS={100 - round_id}",
    }


def _history(count: int = 3) -> list[dict]:
    return [
        {"round": round_id, "parent_sha": f"parent-{round_id}",
         "candidates": [_candidate(round_id)]}
        for round_id in range(count)
    ]


@pytest.mark.parametrize(
    "query",
    ["r2c0", "cache", "COMPLETED", "src/r2.cc", "SPEED_MS", "physics_gate"],
)
def test_search_history_matches_factual_index_fields(query: str):
    matches = search_history(_history(), query)

    assert matches[0]["ref"] == "r2c0"
    assert "eval_block" not in matches[0]


def test_search_history_is_newest_first_and_capped():
    matches = search_history(_history(25), "cache", limit=20)

    assert len(matches) == 20
    assert matches[0]["ref"] == "r24c0"
    assert matches[-1]["ref"] == "r5c0"


def test_search_history_requires_nonblank_query():
    with pytest.raises(ValueError, match="non-empty"):
        search_history(_history(), "   ")


def test_insight_store_is_append_only_and_idempotent(tmp_path):
    store = InsightStore(tmp_path / "insights.jsonl")
    insight = Insight(text="Cache misses dominate.", refs=("r0c0",))

    assert store.append(round_id=0, insight=insight) is True
    assert store.append(round_id=0, insight=insight) is False
    assert store.load() == [{
        "id": "I0",
        "round": 0,
        "text": "Cache misses dominate.",
        "refs": ["r0c0"],
    }]


def test_insight_store_rejects_conflict(tmp_path):
    store = InsightStore(tmp_path / "insights.jsonl")
    store.append(0, Insight("A", ("r0c0",)))

    with pytest.raises(ValueError, match="conflicting"):
        store.append(0, Insight("B", ("r0c0",)))


@pytest.mark.parametrize(
    "value",
    [
        {"text": "", "refs": ["r0c0"]},
        {"text": "x" * 501, "refs": ["r0c0"]},
        {"text": "fact", "refs": []},
        {"text": "fact", "refs": [1]},
        {"text": "fact", "refs": ["r0c0"], "extra": True},
    ],
)
def test_insight_rejects_invalid_shape(value):
    with pytest.raises(ValueError):
        Insight.from_dict(value)


def test_insight_store_rejects_noncurrent_file_schema(tmp_path):
    path = tmp_path / "insights.jsonl"
    path.write_text(json.dumps({
        "id": "I0", "round": 0, "summary": "old schema",
    }) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="current insight schema"):
        InsightStore(path).load()


def test_render_insights_keeps_all_records_compact():
    records = [
        {"id": "I0", "round": 0, "text": "A", "refs": ["r0c0"]},
        {"id": "I1", "round": 1, "text": "B", "refs": ["r1c0"]},
    ]

    assert json.loads(render_insights(records)) == records
