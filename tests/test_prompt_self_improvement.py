from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from simpleloop import config


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
