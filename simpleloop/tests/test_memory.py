from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop import cli
from simpleloop import memory


def _parallel_history() -> list[dict]:
    return [{
        "round": 2,
        "parent_sha": "parent",
        "selected_candidate": 1,
        "selected_sha": "sha-1",
        "candidates": [
            {
                "candidate": 0,
                "family": "hoist",
                "proposal": "hoist lookup",
                "sha": "sha-0",
                "selected": False,
                "accepted": True,
                "metrics": {"SPEED_MS": 120.0},
                "risk": "low",
                "feedback": "slower",
                "changed_paths": ["src/a.cc"],
                "eval_block": "must not be returned",
            },
            {
                "candidate": 1,
                "family": "layout",
                "proposal": "pack values",
                "sha": "sha-1",
                "selected": True,
                "accepted": True,
                "metrics": {"SPEED_MS": 90.0},
                "risk": "low",
                "feedback": "faster",
                "changed_paths": ["src/b.cc"],
                "eval_block": "must not be returned",
            },
        ],
    }]


def test_resolve_parallel_episode_returns_only_compact_fields():
    episode = memory.resolve_episode(_parallel_history(), "r2c1")

    assert episode == {
        "ref": "r2c1",
        "family": "layout",
        "proposal": "pack values",
        "parent_sha": "parent",
        "candidate_sha": "sha-1",
        "selected": True,
        "accepted": True,
        "metrics": {"SPEED_MS": 90.0},
        "risk": "low",
        "feedback": "faster",
        "changed_paths": ["src/b.cc"],
    }
    assert "eval_block" not in episode


def test_resolve_serial_episode_normalizes_candidate_zero():
    history = [{
        "round": 7,
        "proposal": "legacy proposal",
        "sha": "legacy-sha",
        "accepted": True,
        "base_sha": "legacy-sha",
        "score": 0.7,
        "risk": "low",
        "metrics": {"SPEED_MS": 100.0},
        "feedback": "legacy feedback",
        "changed_paths": ["src/legacy.cc"],
    }]

    episode = memory.resolve_episode(history, "r7c0")

    assert episode["ref"] == "r7c0"
    assert episode["proposal"] == "legacy proposal"
    assert episode["candidate_sha"] == "legacy-sha"
    assert episode["selected"] is True


@pytest.mark.parametrize("ref", ["r2", "2c1", "r-1c0", "r2c-1", "r2c9"])
def test_resolve_episode_rejects_invalid_or_missing_refs(ref: str):
    with pytest.raises(ValueError):
        memory.resolve_episode(_parallel_history(), ref)


def test_validate_insight_accepts_grounded_text_and_normalizes_refs():
    validated = memory.validate_insight(
        "  Sparse gathers benefit from packing.  ",
        [" r2c0 ", "r2c1"],
        _parallel_history(),
    )
    assert validated == (
        "Sparse gathers benefit from packing.",
        ["r2c0", "r2c1"],
    )


def test_validate_insight_treats_empty_text_as_no_write():
    assert memory.validate_insight("", [], _parallel_history()) is None


@pytest.mark.parametrize(
    ("text", "refs"),
    [
        ("lesson", []),
        ("", ["r2c0"]),
        ("lesson", ["r99c0"]),
        ("lesson", ["bad-ref"]),
    ],
)
def test_validate_insight_rejects_inconsistent_or_unknown_refs(text, refs):
    with pytest.raises(ValueError):
        memory.validate_insight(text, refs, _parallel_history())


def test_append_and_load_insight_are_append_only_and_idempotent(
    tmp_path: Path,
):
    path = tmp_path / "insights.jsonl"
    assert memory.append_insight(
        path, 31, "A compact lesson.", ["r2c0", "r2c1"]
    ) is True
    assert memory.append_insight(
        path, 31, "A compact lesson.", ["r2c0", "r2c1"]
    ) is False
    assert memory.load_insights(path) == [{
        "id": "I31",
        "text": "A compact lesson.",
        "refs": ["r2c0", "r2c1"],
    }]


def test_append_insight_rejects_conflicting_same_round(tmp_path: Path):
    path = tmp_path / "insights.jsonl"
    memory.append_insight(path, 4, "first", ["r2c0"])
    with pytest.raises(ValueError, match="conflicting insight I4"):
        memory.append_insight(path, 4, "different", ["r2c1"])


def test_load_insights_fails_clearly_on_corrupt_json(tmp_path: Path):
    path = tmp_path / "insights.jsonl"
    path.write_text("{bad json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="could not read insight memory"):
        memory.load_insights(path)


def test_render_insights_is_compact_and_includes_evidence():
    rendered = memory.render_insights([{
        "id": "I31",
        "text": "Dense copying only helped the sparse gather.",
        "refs": ["r29c0", "r30c0"],
    }])
    assert rendered == (
        "[I31] Dense copying only helped the sparse gather.\n"
        "Evidence: r29c0, r30c0"
    )


def test_render_empty_insights_has_explicit_first_run_message():
    assert memory.render_insights([]) == "  (none yet)"


def test_memory_show_cli_resolves_from_explicit_run_dir(
    tmp_path: Path, capsys
):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps(_parallel_history()[0]) + "\n",
        encoding="utf-8",
    )

    cli.main([
        "memory", "show", "r2c1",
        "--run-dir", str(tmp_path),
    ])

    output = json.loads(capsys.readouterr().out)
    assert output["ref"] == "r2c1"
    assert output["candidate_sha"] == "sha-1"
    assert "eval_block" not in output


def test_memory_show_cli_discovers_run_from_repo_cwd(
    tmp_path: Path, monkeypatch, capsys
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "history.jsonl").write_text(
        json.dumps(_parallel_history()[0]) + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    cli.main(["memory", "show", "r2c0"])

    assert json.loads(capsys.readouterr().out)["ref"] == "r2c0"
