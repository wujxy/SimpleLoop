from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from simpleloop import config
from simpleloop.prompt_self_improvement.gate import OptimizerReport, PromptGate
from simpleloop.prompt_self_improvement.history import PromptHistory
from simpleloop.prompt_self_improvement.optimizer import MetaOptimizer
from simpleloop.prompts import PROMPT_NAMES, load_semantic


def _task_file(tmp_path: Path, block=None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    raw = {
        "kind": "task",
        "task": {"goal": "faster"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": {"max_rounds": 5},
        "runtime": {"image": str(image)},
        "source": {"path": str(repo)},
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [],
            },
        },
    }
    if block is not None:
        raw["prompt_self_improvement"] = block
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _enabled(**overrides):
    return {
        "enabled": True,
        "interval_rounds": 2,
        "optimizer_command": "claude",
        "prompt_dir": "prompts",
        "history_dir": "prompt_history",
        "max_prompt_chars": 30000,
        **overrides,
    }


def test_prompt_self_improvement_defaults_disabled(tmp_path: Path):
    cfg = config.load(_task_file(tmp_path))
    assert cfg["prompt_self_improvement"] == {"enabled": False}


def test_prompt_self_improvement_resolves_enabled_block(tmp_path: Path):
    cfg = config.load(_task_file(tmp_path, _enabled()))
    block = cfg["prompt_self_improvement"]
    assert block == {
        "enabled": True,
        "interval_rounds": 2,
        "optimizer_command": "claude",
        "prompt_dir": str((tmp_path / "prompts").resolve()),
        "history_dir": str((tmp_path / "prompt_history").resolve()),
        "max_prompt_chars": 30000,
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"enabled": "yes"}, "enabled"),
        ({"interval_rounds": 0}, "interval_rounds"),
        ({"max_prompt_chars": 999}, "max_prompt_chars"),
        ({"optimizer_command": ""}, "optimizer_command"),
        ({"prompt_dir": ""}, "prompt_dir"),
        ({"history_dir": ""}, "history_dir"),
        ({"unknown": True}, "unknown"),
    ],
)
def test_prompt_self_improvement_rejects_invalid_values(
    tmp_path: Path, overrides: dict, message: str,
):
    with pytest.raises(config.ConfigError, match=message):
        config.load(_task_file(tmp_path, _enabled(**overrides)))


def test_history_initializes_new_v000_from_package_prompts(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    assert history.initialize() == "v000"
    for role in PROMPT_NAMES:
        expected = load_semantic(role) + "\n"
        assert (history.prompt_dir / f"{role}.md").read_text() == expected
        assert (history.history_dir / "v000" / f"{role}.md").read_text() == expected
    assert history.state["active_version"] == "v000"


def test_history_detects_changes_snapshots_and_restores(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    proposer = history.prompt_dir / "proposer.md"
    proposer.write_text("rewritten proposer\n", encoding="utf-8")
    assert history.has_changes("v000")

    version = history.snapshot(2, OptimizerReport(
        status="changed", diagnosis="anchored", evidence=["r0c0"],
        intent="broaden search",
    ))
    assert version == "v001"
    assert (history.history_dir / "v001/proposer.md").read_text() == "rewritten proposer\n"

    proposer.write_text("partial", encoding="utf-8")
    history.restore("v001")
    assert proposer.read_text(encoding="utf-8") == "rewritten proposer\n"


def test_history_records_idempotent_trigger_and_recovers_inflight(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    assert history.mark_inflight("run-a", 2)
    assert not history.mark_inflight("run-a", 2)
    (history.prompt_dir / "proposer.md").write_text("partial")

    recovered = history.recover_inflight()
    assert recovered == {"run_id": "run-a", "trigger_round": 2, "parent": "v000"}
    assert "You are the PROPOSER" in (history.prompt_dir / "proposer.md").read_text()
    assert history.events()[-1]["status"] == "interrupted"


def _write_report(prompt_dir: Path, **overrides) -> Path:
    data = {
        "status": "changed",
        "diagnosis": "role drift",
        "evidence": ["r0c0"],
        "intent": "restore ownership",
        **overrides,
    }
    path = prompt_dir / "optimizer_report.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_report_parser_is_exact_and_gate_allows_role_rewrite(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    (history.prompt_dir / "proposer.md").write_text("wholly new semantics")
    report_path = _write_report(history.prompt_dir)

    report = OptimizerReport.load(report_path)
    assert report.diagnosis == "role drift"
    assert PromptGate(30000).check(history.prompt_dir, report, changed=True) == []


def test_gate_rejects_changed_meta_core_and_report_mismatch(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()
    meta = history.prompt_dir / "meta_optimizer.md"
    meta.write_text(meta.read_text().replace("META OPTIMIZER", "TASK SOLVER"))
    report = OptimizerReport.load(_write_report(history.prompt_dir, status="no_change"))

    errors = PromptGate(30000).check(history.prompt_dir, report, changed=True)
    assert any("META_IDENTITY_CORE" in error for error in errors)
    assert any("no_change" in error for error in errors)


def test_report_rejects_unknown_fields(tmp_path: Path):
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    path = _write_report(prompt_dir, unknown=True)
    with pytest.raises(ValueError, match="exactly"):
        OptimizerReport.load(path)


def test_optimizer_receives_absolute_read_write_boundaries(tmp_path: Path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    history.initialize()

    class CapturingAgent:
        prompt = ""
        cwd = None

        def run_text(self, prompt, *, cwd, **_kwargs):
            self.prompt = prompt
            self.cwd = cwd
            return ""

    fake = CapturingAgent()
    optimizer = MetaOptimizer(
        "claude", 60, 8000, agent_factory=lambda **_kwargs: fake,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    optimizer.run(
        run_dir=run_dir, prompt_dir=history.prompt_dir,
        history_dir=history.history_dir, source_dir=source_dir,
        goal="faster", gate_block="CORRECTNESS",
    )

    assert str(run_dir.resolve()) in fake.prompt
    assert str(history.history_dir.resolve()) in fake.prompt
    assert str(source_dir.resolve()) in fake.prompt
    assert "Fixed artifacts:" in fake.prompt
    assert "optimizer_report.yaml" in fake.prompt
    assert fake.cwd == history.prompt_dir.resolve()
