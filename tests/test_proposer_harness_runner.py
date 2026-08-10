from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from scripts import proposer_harness as harness
from simpleloop.memory.models import NewFindingTarget, ResearchProposal
def _proposal(*, instruction, research_target):
    return ResearchProposal(
        instruction=instruction,
        research_target=research_target,
        model_claim_refs=("M1",),
        explanation_refs=("E1",),
        hypothesis_id="H1",
        evidence_refs=("source:src/a.cc",),
        mechanism="test mechanism",
        prediction="test prediction",
        affected_scope="src/a.cc",
    )


from simpleloop.roles.proposer import ProposerResult


def _history_row(round_id: int, *, parent: str, selected: str | None) -> dict:
    return {
        "round": round_id,
        "parent_sha": parent,
        "selected_candidate": 0 if selected else None,
        "selected_sha": selected,
        "candidates": [],
    }


def _resolved_config(tmp_path: Path) -> dict:
    (tmp_path / "source-repo").mkdir(exist_ok=True)
    return {
        "goal": "make it faster",
        "hints": ["preserve numerical behavior"],
        "editable_paths": ["src/**"],
        "frozen_paths": ["tests/**"],
        "agent_timeout_seconds": 120,
        "candidates_per_round": 4,
        "scientist_steps": 364,
        "runtime_image": str(tmp_path / "runtime.sif"),
        "runtime_binds": [],
        "metrics": {
            "objective": {"key": "SPEED_MS", "lower_is_better": True},
            "gates": [{"key": "FCN", "description": "FCN must match"}],
        },
        "repo_path": str(tmp_path / "source-repo"),
        "baseline_ref": "HEAD",
        "roles": {
            "researcher": {
                "api": "hepai",
                "model": "test-model",
                "base_url": "https://example.invalid",
                "command_timeout_seconds": 10,
                "command_output_cap_chars": 2000,
            },
            "executor": None,
        },
    }


def _install_runner_fakes(monkeypatch, tmp_path, *, result=None, failure=None):
    calls: dict = {}
    cfg = _resolved_config(tmp_path)
    monkeypatch.setattr(harness.config_mod, "load", lambda _path: cfg)

    class FakeRuntime:
        def __init__(self, *, image, binds, run_dir):
            calls["runtime"] = {
                "image": image, "binds": binds, "run_dir": Path(run_dir),
            }

        def preflight(self):
            calls["preflight"] = True

    class FakeWorkspace:
        def __init__(self, *, run_dir, repo_path, baseline_ref, editable):
            self.run_dir = Path(run_dir)
            self.repo = self.run_dir / "repo"
            calls["workspace"] = {
                "run_dir": self.run_dir,
                "repo_path": repo_path,
                "baseline_ref": baseline_ref,
                "editable": editable,
            }

        def setup(self):
            self.repo.mkdir(parents=True)
            calls["workspace_setup"] = True

        def baseline_sha(self):
            return "baseline-sha"

        def add_worktree(self, worktree_id, parent_sha):
            calls["worktree_added"] = (worktree_id, parent_sha)
            source = self.run_dir / "source"
            source.mkdir()
            return source

        def remove_worktree(self, worktree_id):
            calls.setdefault("workspace_removed", []).append(worktree_id)

    class FakeMemoryService:
        def __init__(self, *, run_dir, metrics_schema):
            calls["memory"] = {
                "run_dir": Path(run_dir), "metrics_schema": metrics_schema,
            }

    class FakeOrchestrator:
        def __init__(self, **kwargs):
            calls["orchestrator_init"] = kwargs
            observer = kwargs.get("usage_observer")
            if observer is not None:
                observer({"input_tokens": 7, "output_tokens": 3})

        def run(self, **kwargs):
            calls["orchestrator_run"] = kwargs
            if failure is not None:
                raise failure
            if result is not None:
                return result
            return ProposerResult(proposals=[_proposal(
                instruction="try SoA",
                research_target=NewFindingTarget(question="layout question"),
            )])

    monkeypatch.setattr(harness, "ApptainerRuntime", FakeRuntime)
    monkeypatch.setattr(harness, "Workspace", FakeWorkspace)
    monkeypatch.setattr(harness, "MemoryService", FakeMemoryService)
    monkeypatch.setattr(harness, "ProposerOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(
        harness.model_mod, "build_chat_model", lambda _cfg: "fake-model",
    )
    return calls


def test_run_proposer_invokes_complete_pipeline_without_executor(
    monkeypatch, tmp_path,
):
    calls = _install_runner_fakes(monkeypatch, tmp_path)

    summary = harness.run_proposer(
        tmp_path / "task.yaml",
        tmp_path / "out",
        seed=42,
    )

    kwargs = calls["orchestrator_run"]
    assert kwargs["goal"] == "make it faster"
    assert kwargs["base_sha"] == "baseline-sha"
    assert kwargs["current_round"] == 0
    assert kwargs["candidates_per_round"] == 4
    assert kwargs["scientist_steps"] == 364
    assert kwargs["run_dir"] == tmp_path / "out"
    assert calls["workspace_removed"] == ["proposer-test"]
    assert summary.proposal_count == 1
    result = json.loads(summary.result_path.read_text(encoding="utf-8"))
    assert result["status"] == "completed"
    assert result["proposals"][0]["instruction"] == "try SoA"
    assert result["usage"] == [{"input_tokens": 7, "output_tokens": 3}]


def test_run_proposer_reads_existing_run_without_modifying_it(
    monkeypatch, tmp_path,
):
    calls = _install_runner_fakes(monkeypatch, tmp_path)
    history_dir = tmp_path / "old-run"
    history_dir.mkdir()
    (history_dir / "repo").mkdir()
    history_path = history_dir / "history.jsonl"
    history_path.write_text(
        json.dumps(_history_row(0, parent="baseline-sha", selected="winner"))
        + "\n",
        encoding="utf-8",
    )
    before = history_path.read_bytes()

    summary = harness.run_proposer(
        tmp_path / "task.yaml",
        tmp_path / "out",
        from_run=history_dir,
    )

    assert calls["memory"]["run_dir"] == history_dir
    assert calls["workspace"]["repo_path"] == str(history_dir / "repo")
    assert calls["orchestrator_run"]["run_dir"] == history_dir
    assert calls["orchestrator_run"]["base_sha"] == "winner"
    assert calls["orchestrator_run"]["current_round"] == 1
    assert calls["worktree_added"] == ("proposer-test", "winner")
    assert history_path.read_bytes() == before
    assert summary.base_sha == "winner"


def test_run_proposer_serializes_abstention(monkeypatch, tmp_path):
    abstained = ProposerResult(
        proposals=[], abstained=True, abstain_reason="no grounded direction",
    )
    _install_runner_fakes(monkeypatch, tmp_path, result=abstained)

    summary = harness.run_proposer(
        tmp_path / "task.yaml", tmp_path / "out",
    )

    result = json.loads(summary.result_path.read_text(encoding="utf-8"))
    assert summary.abstained is True
    assert result["proposals"] == []
    assert result["abstain_reason"] == "no grounded direction"


def test_run_proposer_restores_random_state(monkeypatch, tmp_path):
    calls = _install_runner_fakes(monkeypatch, tmp_path)
    random.seed(913)
    state = random.getstate()

    harness.run_proposer(
        tmp_path / "task.yaml", tmp_path / "out", seed=42,
    )

    assert random.getstate() == state
    assert calls["orchestrator_run"]["random_seed"] == 42


def test_run_proposer_failure_records_error_and_cleans_worktree(
    monkeypatch, tmp_path,
):
    failure = RuntimeError("model broke")
    calls = _install_runner_fakes(
        monkeypatch, tmp_path, failure=failure,
    )

    with pytest.raises(RuntimeError, match="model broke"):
        harness.run_proposer(tmp_path / "task.yaml", tmp_path / "out")

    result = json.loads(
        (tmp_path / "out" / "result.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "failed"
    assert result["error"] == {
        "type": "RuntimeError", "message": "model broke",
    }
    assert calls["workspace_removed"] == ["proposer-test"]


def test_run_proposer_refuses_completed_output(monkeypatch, tmp_path):
    _install_runner_fakes(monkeypatch, tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    result_path = output / "result.json"
    original = '{"status":"completed"}\n'
    result_path.write_text(original, encoding="utf-8")

    with pytest.raises(harness.ProposerHarnessError, match="completed"):
        harness.run_proposer(tmp_path / "task.yaml", output)

    assert result_path.read_text(encoding="utf-8") == original
