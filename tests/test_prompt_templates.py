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


def test_load_semantic_default_proposer_loads():
    # Architecture only: the default proposer prompt loads as non-empty text.
    # Tests must not pin prompt semantics — the wording is free to change.
    text = load_semantic("proposer")
    assert isinstance(text, str) and text.strip()


def test_generator_is_not_an_active_prompt_role():
    with pytest.raises(ValueError, match="unknown prompt role"):
        load_semantic("generator")


def test_proposer_identity_is_scientific_not_a_phase_checklist():
    text = load_semantic("proposer")
    lowered = " ".join(text.lower().split())
    for concept in ("working model", "explanation", "prediction", "counterfactual"):
        assert concept in lowered
    for concept in (
        "next experiment",
        "meaningful opportunity remains",
        "sampled terrain",
        "not an implementation design",
        "second executor",
    ):
        assert concept in lowered
    assert "precise research-backed direction" not in lowered
    assert text.rstrip().endswith("prose outside that JSON object.")
    for legacy in (
        "cognitive element", "sieve", "enrich", "generator partner",
        "feedback_generator", "eventcontext", "omilrec",
    ):
        assert legacy not in lowered


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
        workspace=EmptyWorkspace(), worktree=tmp_path, round_id=0,
        prompt_dir=prompt_dir,
    )

    assert result.reason == "executor made no changes"
    assert agent.prompt.startswith("ACTIVE EXECUTOR")
    assert "Direction to implement:\nreplace lookup" in agent.prompt
    assert "create, delete, move, replace, or reorganize" in agent.prompt
    assert "Editable paths" not in agent.prompt
    assert "Frozen paths" not in agent.prompt
