from __future__ import annotations

import json
from pathlib import Path

import pytest

from simpleloop import cli
from simpleloop.harness import memory


def _parallel_history() -> list[dict]:
    return [{
        "round": 2,
        "parent_sha": "parent",
        "selected_candidate": 1,
        "selected_sha": "sha-1",
        "candidates": [
            {
                "candidate": 0,
                "experiment_id": "r2c0",
                "finding_id": "F-001",
                "proposal": "hoist lookup",
                "parent_sha": "parent",
                "sha": "sha-0",
                "status": "COMPLETED",
                "selected": False,
                "gate_passed": True,
                "eligible": True,
                "gates": {"PATHS": {"passed": True, "detail": ""}},
                "metrics": {"SPEED_MS": 120.0},
                "changed_paths": ["src/a.cc"],
                "eval_block": "objective=120\nphysics_gate=1",
            },
            {
                "candidate": 1,
                "experiment_id": "r2c1",
                "finding_id": "F-002",
                "proposal": "pack values",
                "parent_sha": "parent",
                "sha": "sha-1",
                "status": "COMPLETED",
                "selected": True,
                "gate_passed": True,
                "eligible": True,
                "gates": {"PATHS": {"passed": True, "detail": ""}},
                "metrics": {"SPEED_MS": 90.0},
                "changed_paths": ["src/b.cc"],
                "eval_block": "objective=90\nphysics_gate=1",
            },
        ],
    }]


def test_read_history_rejects_records_without_current_candidate_facts(tmp_path):
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps({
        "round": 0,
        "candidates": [{"candidate": 0, "candidate_status": "COMPLETED"}],
    }) + "\n")

    with pytest.raises(ValueError, match="current candidate schema"):
        memory.read_history(path)


def test_resolve_parallel_episode_returns_complete_bounded_facts():
    episode = memory.resolve_episode(_parallel_history(), "r2c1")

    assert episode == {
        "ref": "r2c1",
        "experiment_id": "r2c1",
        "finding_id": "F-002",
        "proposal": "pack values",
        "parent_sha": "parent",
        "candidate_sha": "sha-1",
        "status": "COMPLETED",
        "selected": True,
        "gate_passed": True,
        "eligible": True,
        "gates": {"PATHS": {"passed": True, "detail": ""}},
        "metrics": {"SPEED_MS": 90.0},
        "changed_paths": ["src/b.cc"],
        "eval_block": "objective=90\nphysics_gate=1",
    }


@pytest.mark.parametrize("ref", ["r2", "2c1", "r-1c0", "r2c-1", "r2c9"])
def test_resolve_episode_rejects_invalid_or_missing_refs(ref: str):
    with pytest.raises(ValueError):
        memory.resolve_episode(_parallel_history(), ref)


def test_memory_show_cli_resolves_from_explicit_run_dir(
    tmp_path: Path, capsys,
):
    (tmp_path / "history.jsonl").write_text(
        json.dumps(_parallel_history()[0]) + "\n",
        encoding="utf-8",
    )

    cli.main(["memory", "show", "r2c1", "--run-dir", str(tmp_path)])

    output = json.loads(capsys.readouterr().out)
    assert output["ref"] == "r2c1"
    assert output["candidate_sha"] == "sha-1"
    assert output["finding_id"] == "F-002"
    assert output["gates"]["PATHS"]["passed"] is True
    assert output["eval_block"] == "objective=90\nphysics_gate=1"


def test_memory_show_cli_discovers_run_from_repo_cwd(
    tmp_path: Path, monkeypatch, capsys,
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
