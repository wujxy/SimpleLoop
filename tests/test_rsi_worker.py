from __future__ import annotations

from pathlib import Path

from simpleloop.scheduling.handlers import rsi


class FakeAgent:
    def __init__(self, *, error=None):
        self.error = error
        self.calls = []

    def run_text(self, prompt, *, cwd, label):
        self.calls.append((prompt, Path(cwd), label))
        if self.error:
            raise self.error
        return "edited"


def payload(tmp_path):
    body = tmp_path / "body"
    (body / "proposer").mkdir(parents=True)
    return {
        "round_id": 4,
        "run_dir": str(tmp_path),
        "worktree_path": str(body),
        "result_dir": str(tmp_path / "result"),
        "change": {
            "target": "prompt",
            "intent": "broaden search",
            "instruction": "edit the charter",
            "evidence_refs": [],
        },
    }


def test_self_edit_handler_runs_explicit_change_without_git(tmp_path, monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr(rsi, "_build_agent", lambda raw, observe: agent)

    result = rsi.handle_self_edit(payload(tmp_path), lambda row: None)

    assert result["self_edit"]["status"] == "EDITED"
    prompt, cwd, label = agent.calls[0]
    assert "edit the charter" in prompt
    assert cwd == tmp_path / "body"
    assert label == "self-exec r4"


def test_self_edit_handler_normalizes_agent_failure(tmp_path, monkeypatch):
    agent = FakeAgent(error=ValueError("model unavailable"))
    monkeypatch.setattr(rsi, "_build_agent", lambda raw, observe: agent)

    result = rsi.handle_self_edit(payload(tmp_path), lambda row: None)

    assert result["self_edit"] == {
        "status": "EDITOR_FAILED", "output": "", "reason": "model unavailable",
    }
