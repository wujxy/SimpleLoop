from __future__ import annotations

from pathlib import Path

import pytest

from simpleloop.prompts import load_semantic
from simpleloop.roles import executor, proposer


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
    assert text.startswith("You are the RESEARCHER")
    assert "achieving the user's Goal" in text


def test_load_semantic_uses_active_prompt_directory(tmp_path: Path):
    (tmp_path / "proposer.md").write_text("active proposer", encoding="utf-8")
    assert load_semantic("proposer", tmp_path) == "active proposer"


def test_proposer_assembles_semantics_context_and_fixed_protocol(tmp_path: Path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "proposer.md").write_text("ACTIVE PROPOSER", encoding="utf-8")
    agent = CapturingAgent({"proposals": ["rewrite the algorithm"]})

    batch = proposer.propose(
        agent, goal="faster", editable=["src/**"], frozen=[], history=[],
        base_sha="abc", cwd=tmp_path, prompt_dir=prompt_dir,
    )

    assert agent.prompt.startswith("ACTIVE PROPOSER")
    assert "Task goal:\nfaster" in agent.prompt
    assert "Available evidence:" in agent.prompt
    assert "Fixed boundaries:" in agent.prompt
    assert "git diff" not in agent.prompt
    assert "continue" not in agent.prompt
    assert "reflection" not in agent.prompt
    assert agent.schema["required"] == ["proposals"]
    assert batch.proposals == ["rewrite the algorithm"]


def test_proposer_schema_is_exact_k_strings():
    schema = proposer._proposer_schema(3)

    assert schema["required"] == ["proposals"]
    assert set(schema["properties"]) == {"proposals"}
    proposals = schema["properties"]["proposals"]
    assert proposals["minItems"] == proposals["maxItems"] == 3
    assert proposals["items"] == {
        "type": "string", "minLength": 1, "pattern": r"\S",
    }


def test_parse_batch_accepts_only_free_text_proposals():
    batch = proposer._parse_batch(
        {"proposals": ["rewrite the algorithm", " replace its data model "]},
        candidates_per_round=2,
    )

    assert batch.proposals == [
        "rewrite the algorithm", "replace its data model",
    ]


@pytest.mark.parametrize("data", [
    {"reflection": "r", "proposals": ["p"]},
    {"proposals": []},
    {"proposals": [""]},
    {"proposals": ["   "]},
    {"proposals": [7]},
])
def test_parse_batch_rejects_nonminimal_contract(data):
    with pytest.raises(ValueError):
        proposer._parse_batch(data, candidates_per_round=1)


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
