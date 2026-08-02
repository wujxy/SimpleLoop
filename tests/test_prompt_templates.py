from __future__ import annotations

from pathlib import Path

import pytest

from simpleloop.prompts import load_semantic
from simpleloop.roles import executor


class CapturingAgent:
    def __init__(self, data=None):
        self.data = data or {}
        self.prompt = ""
        self.schema = None

    def run_json(self, prompt, *, json_schema, **_kwargs):
        self.prompt = prompt
        self.schema = json_schema
        return self.data

    def run_text(self, prompt, **_kwargs):
        self.prompt = prompt
        return ""


class EmptyWorkspace:
    def changed_paths(self, _worktree):
        return []

    def diff(self, _parent, _sha):
        return ""


def test_load_semantic_uses_identity_internalized_v000():
    text = load_semantic("proposer")
    normalized = " ".join(text.split())
    assert text.startswith("You are one Scientist responsible")
    assert "experimental opportunity" in normalized
    assert "Executor is implementation capacity" in normalized
    assert "Harness" in text
    assert '"action"' not in text
    assert "run_research_command" not in text


def test_load_semantic_uses_active_prompt_directory(tmp_path: Path):
    (tmp_path / "proposer.md").write_text("active proposer", encoding="utf-8")
    assert load_semantic("proposer", tmp_path) == "active proposer"


def test_executor_assembles_active_semantics_and_safety(tmp_path: Path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "executor.md").write_text("ACTIVE EXECUTOR", encoding="utf-8")
    agent = CapturingAgent()

    result = executor.execute(
        agent, proposal="replace lookup", goal="faster",
        editable=["src/**"], frozen=["bench/**"], workspace=EmptyWorkspace(),
        worktree=tmp_path, round_id=0, prompt_dir=prompt_dir,
    )

    assert result.reason == "executor made no changes"
    assert agent.prompt.startswith("ACTIVE EXECUTOR")
    assert "Direction to implement:\nreplace lookup" in agent.prompt
    assert "bench/**" in agent.prompt
