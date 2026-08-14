from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from simpleloop import config


def _task(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    return {
        "schema": "simpleloop.v1",
        "goal": "make it faster",
        "hints": ["preserve results"],
        "loop": {
            "max_rounds": 3,
            "candidates_per_round": 2,
            "max_parallel_candidates": 2,
        },
        "source": {"repo": str(repo), "baseline": "HEAD"},
        "world": {
            "image": str(image),
            "writable": ["src"],
            "external_readonly": [str(tmp_path)],
        },
        "evaluation": {
            "commands": ["python -m pytest -q"],
            "objective": {"key": "runtime", "direction": "minimize"},
            "gates": [{"key": "correctness"}],
        },
        "providers": {
            "sandbox": {"kind": "apptainer", "userns": True},
            "scheduler": {"kind": "local"},
        },
        "proposer": {"max_steps": 123},
        "executor": {
            "api": "anthropic",
            "model": "model",
            "base_url": "https://example.invalid/anthropic",
        },
        "rsi": {"enabled": True, "first_review_round": 4},
    }


def _write(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_v1_config_resolves_once_to_runtime_contract(tmp_path: Path):
    cfg = config.load(_write(tmp_path, _task(tmp_path)))

    assert cfg["goal"] == "make it faster"
    assert cfg["hints"] == ["preserve results"]
    assert cfg["max_workers"] == 2
    assert cfg["scientist_steps"] == 123
    assert cfg["editable_paths"] == ["src"]
    assert cfg["read_only_binds"] == [str(tmp_path.resolve())]
    assert cfg["metrics"]["objective"] == {
        "key": "runtime", "lower_is_better": True,
    }
    assert cfg["execution_backend"] == "local"
    assert cfg["roles"]["researcher"] is None
    assert cfg["roles"]["executor"]["model"] == "model"
    assert cfg["rsi"] == {
        "enabled": True, "first_self_review_round": 4,
    }


def test_v1_hpc_scheduler_preserves_hepjob_settings(tmp_path: Path):
    raw = _task(tmp_path)
    raw["providers"]["scheduler"] = {
        "kind": "hepjob",
        "schedd_name": "scheduler@example",
        "accounting_group": "group",
        "cpus": 4,
    }

    cfg = config.load(_write(tmp_path, raw))

    assert cfg["execution_backend"] == "hepjob"
    assert cfg["hepjob"]["schedd_name"] == "scheduler@example"
    assert cfg["hepjob"]["accounting_group"] == "group"
    assert cfg["hepjob"]["cpus"] == 4


def test_v1_rejects_unknown_intent_fields(tmp_path: Path):
    raw = _task(tmp_path)
    raw["world"]["magic"] = True

    with pytest.raises(config.ConfigError, match="world.*magic"):
        config.load(_write(tmp_path, raw))


def test_v1_rejects_unimplemented_sandbox_provider(tmp_path: Path):
    raw = _task(tmp_path)
    raw["providers"]["sandbox"]["kind"] = "docker"

    with pytest.raises(config.ConfigError, match="apptainer"):
        config.load(_write(tmp_path, raw))


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda raw: raw["world"].pop("writable"), "world.writable"),
        (
            lambda raw: raw["proposer"].update({"model": ""}),
            "proposer.model",
        ),
        (
            lambda raw: raw["evaluation"]["objective"].update({"key": ""}),
            "evaluation.objective.key",
        ),
        (lambda raw: raw.update({"hints": "bad"}), "hints"),
        (
            lambda raw: raw["providers"]["scheduler"].update({
                "kind": "hepjob", "unknown": True,
            }),
            "providers.scheduler",
        ),
    ],
)
def test_v1_validation_errors_use_v1_field_names(
    tmp_path: Path, mutate, field: str,
):
    raw = _task(tmp_path)
    mutate(raw)

    with pytest.raises(config.ConfigError, match=field.replace(".", r"\.")):
        config.load(_write(tmp_path, raw))


def test_v1_does_not_depend_on_the_removable_legacy_adapter(
    tmp_path: Path, monkeypatch,
):
    def fail(*_args, **_kwargs):
        raise AssertionError("legacy adapter was called")

    monkeypatch.setattr(config.legacy_config, "resolve", fail)

    assert config.load(_write(tmp_path, _task(tmp_path)))["goal"] == (
        "make it faster"
    )


def test_legacy_task_still_loads_at_boundary(tmp_path: Path, monkeypatch):
    new = _task(tmp_path)
    old = {
        "kind": "task",
        "task": {"goal": new["goal"]},
        "safety": {"editable_paths": new["world"]["writable"]},
        "loop": {"max_rounds": new["loop"]["max_rounds"]},
        "runtime": {"image": new["world"]["image"]},
        "eval": {
            "commands": new["evaluation"]["commands"],
            "metrics": {
                "objective": {
                    "key": "runtime", "lower_is_better": True,
                },
                "gates": [],
            },
        },
        "source": {"path": new["source"]["repo"]},
    }

    monkeypatch.setenv("SIMPLELOOP_APPTAINER_USERNS", "0")
    resolved = config.load(_write(tmp_path, old))
    assert resolved["goal"] == "make it faster"
    assert resolved["sandbox_userns"] is False


# ---------------- reflection block ----------------

def test_v1_reflection_defaults_on_without_block(tmp_path: Path):
    raw = _task(tmp_path)
    raw.pop("rsi", None)
    cfg = config.load(_write(tmp_path, raw))
    assert cfg["reflection"] == {
        "enabled": True, "interval_rounds": 8, "first_reflection_round": 8,
    }


def test_v1_reflection_block_resolves(tmp_path: Path):
    raw = _task(tmp_path)
    raw["reflection"] = {
        "enabled": True, "interval_rounds": 4, "first_reflection_round": 2,
    }
    cfg = config.load(_write(tmp_path, raw))
    assert cfg["reflection"] == {
        "enabled": True, "interval_rounds": 4, "first_reflection_round": 2,
    }


def test_v1_reflection_disabled_rejects_other_keys(tmp_path: Path):
    raw = _task(tmp_path)
    raw["reflection"] = {"enabled": False, "interval_rounds": 4}
    with pytest.raises(config.ConfigError, match="reflection"):
        config.load(_write(tmp_path, raw))


def test_v1_reflection_rejects_unknown_key(tmp_path: Path):
    raw = _task(tmp_path)
    raw["reflection"] = {"enabled": True, "cadence": 3}
    with pytest.raises(config.ConfigError, match="reflection.*cadence"):
        config.load(_write(tmp_path, raw))


@pytest.mark.parametrize("block", [
    {"enabled": True, "interval_rounds": 0},
    {"enabled": True, "interval_rounds": True},
    {"enabled": True, "first_reflection_round": -1},
    {"enabled": "yes"},
])
def test_v1_reflection_rejects_bad_values(tmp_path: Path, block: dict):
    raw = _task(tmp_path)
    raw["reflection"] = block
    with pytest.raises(config.ConfigError, match="reflection"):
        config.load(_write(tmp_path, raw))
