from __future__ import annotations

from pathlib import Path

import pytest

from proposer.prompts import load_semantic
from simpleloop.stages.executor import (
    AgentExecutor,
    ExecutionRequest,
    ExecutorConfig,
)
from simpleloop.stages.proposer import Proposal
from simpleloop.world import SourceWorkspace


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


def test_load_semantic_default_proposer_loads():
    # Architecture only: the default proposer prompt loads as non-empty text.
    # Tests must not pin prompt semantics — the wording is free to change.
    text = load_semantic("proposer")
    assert isinstance(text, str) and text.strip()


def test_load_semantic_uses_active_prompt_directory(tmp_path: Path):
    (tmp_path / "proposer.md").write_text("active proposer", encoding="utf-8")
    assert load_semantic("proposer", tmp_path) == "active proposer"


def test_executor_assembles_active_semantics_and_safety(tmp_path: Path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "executor.md").write_text("ACTIVE EXECUTOR", encoding="utf-8")
    agent = CapturingAgent()

    result = AgentExecutor(
        agent,
        ExecutorConfig(goal="faster", prompt_dir=prompt_dir),
    ).execute(
        ExecutionRequest(
            0, 0, Proposal("replace lookup"),
            SourceWorkspace("0-c0", tmp_path, "parent"),
        ),
    )

    assert result.status == "EXECUTED"
    assert agent.prompt.startswith("ACTIVE EXECUTOR")
    assert "Direction to implement:\nreplace lookup" in agent.prompt
    # editable/frozen are no longer injected into the executor prompt — the
    # file world is constructed by the container mount map, not stated in prose.
    assert "Editable paths" not in agent.prompt
    assert "Frozen paths" not in agent.prompt
