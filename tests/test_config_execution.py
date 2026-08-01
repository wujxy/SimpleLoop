"""Config validation for the execution block: default backend is local,
hepjob requires schedd_name + accounting_group, unknown keys are errors,
and the resolved snapshot is self-describing (defaults filled in)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from simpleloop import config as config_mod


def _base_task(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"SIF-test")
    return {
        "kind": "task",
        "task": {"goal": "test"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": {"max_rounds": 3},
        "runtime": {"image": str(image)},
        "source": {"path": str(repo), "baseline_ref": "HEAD"},
        "eval": {
            "commands": ["run-eval"],
            "metrics": {
                "objective": {"key": "SPEED_MS", "lower_is_better": True},
                "gates": [{"key": "CORRECTNESS"}],
            },
        },
    }


def _write(tmp_path: Path, execution: dict) -> Path:
    raw = _base_task(tmp_path)
    raw["execution"] = execution
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_default_backend_is_local(tmp_path: Path):
    cfg = config_mod.load(_write(tmp_path, {}))
    assert cfg["execution_backend"] == "local"
    # hepjob cfg carries defaults even for the local backend so a resolved
    # snapshot stays self-describing.
    assert cfg["hepjob"]["poll_seconds"] == 30
    assert cfg["hepjob"]["max_attempts"] == 2


def test_researcher_defaults(tmp_path: Path):
    raw = _base_task(tmp_path)
    raw["researcher"] = {}
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    cfg = config_mod.load(path)

    assert cfg["researcher"] == {
        "api": "hepai",
        "model": "gpt-5.5",
        "base_url": "https://aiapi.ihep.ac.cn/apiv2",
        "max_steps": 50,
        "command_timeout_seconds": 120,
        "command_output_cap_chars": 12000,
    }


def test_missing_researcher_is_preserved_for_static_mode(tmp_path: Path):
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(_base_task(tmp_path)), encoding="utf-8")

    assert config_mod.load(path)["researcher"] is None


def test_researcher_rejects_unknown_key(tmp_path: Path):
    raw = _base_task(tmp_path)
    raw["researcher"] = {"temperature": 0.2}
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(config_mod.ConfigError, match="researcher.*temperature"):
        config_mod.load(path)


@pytest.mark.parametrize(
    ("researcher", "message"),
    [
        (None, "must be an object"),
        ({"api": "openai"}, "supports only 'hepai'"),
        ({"model": " "}, "researcher.model"),
        ({"base_url": ""}, "researcher.base_url"),
        ({"max_steps": 0}, "researcher.max_steps"),
        ({"command_timeout_seconds": 0}, "command_timeout_seconds"),
        ({"command_output_cap_chars": 999}, "command_output_cap_chars"),
    ],
)
def test_researcher_rejects_invalid_values(
    tmp_path: Path, researcher: object, message: str,
):
    raw = _base_task(tmp_path)
    raw["researcher"] = researcher
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(config_mod.ConfigError, match=message):
        config_mod.load(path)


@pytest.mark.parametrize("key", ["PATHS", "EVAL_COMMANDS"])
def test_metrics_reject_reserved_keys(tmp_path: Path, key: str):
    raw = _base_task(tmp_path)
    raw["eval"]["metrics"]["gates"] = [{"key": key}]
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(config_mod.ConfigError, match="reserved"):
        config_mod.load(path)


def test_metrics_reject_duplicate_and_objective_gate_keys(tmp_path: Path):
    for gates in (
        [{"key": "CORRECTNESS"}, {"key": "CORRECTNESS"}],
        [{"key": "SPEED_MS"}],
    ):
        raw = _base_task(tmp_path)
        raw["eval"]["metrics"]["gates"] = gates
        path = tmp_path / "task.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")

        with pytest.raises(config_mod.ConfigError, match="unique"):
            config_mod.load(path)


def test_explicit_local_backend(tmp_path: Path):
    cfg = config_mod.load(_write(tmp_path, {"backend": "local"}))
    assert cfg["execution_backend"] == "local"


def test_hepjob_requires_schedd_and_accounting_group(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="schedd_name"):
        config_mod.load(_write(tmp_path, {"backend": "hepjob"}))
    with pytest.raises(config_mod.ConfigError, match="accounting_group"):
        config_mod.load(_write(tmp_path, {
            "backend": "hepjob",
            "hepjob": {"schedd_name": "s"},
        }))


def test_hepjob_resolves_full_block(tmp_path: Path):
    cfg = config_mod.load(_write(tmp_path, {
        "backend": "hepjob",
        "hepjob": {
            "schedd_name": "scheduler@pvm069.ihep.ac.cn",
            "accounting_group": "JUNO.juno.default",
            "memory_mb": 8000,
            "poll_seconds": 15,
            "max_attempts": 3,
        },
    }))
    assert cfg["execution_backend"] == "hepjob"
    h = cfg["hepjob"]
    assert h["schedd_name"] == "scheduler@pvm069.ihep.ac.cn"
    assert h["accounting_group"] == "JUNO.juno.default"
    assert h["memory_mb"] == 8000
    assert h["max_attempts"] == 3
    # defaults still filled for unset keys
    assert h["run_timeout_seconds"] == 21600
    assert h["request_os"] == "AlmaLinux9"
    assert h["accounting_group_user"]  # current user
    assert h["python_executable"]  # sys.executable


def test_hepjob_cpu_model_resolved_and_normalized(tmp_path: Path):
    cfg = config_mod.load(_write(tmp_path, {
        "backend": "hepjob",
        "hepjob": {
            "schedd_name": "s", "accounting_group": "g",
            "cpu_model": "Genoa",
        },
    }))
    assert cfg["hepjob"]["cpu_model"] == "genoa"


def test_hepjob_unknown_cpu_model_is_error(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="cpu_model"):
        config_mod.load(_write(tmp_path, {
            "backend": "hepjob",
            "hepjob": {
                "schedd_name": "s", "accounting_group": "g",
                "cpu_model": "bogus",
            },
        }))


def test_unknown_execution_key_is_error(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="unknown"):
        config_mod.load(_write(tmp_path, {"backend": "local",
                                         "bogus": True}))


def test_unknown_hepjob_key_is_error(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="unknown"):
        config_mod.load(_write(tmp_path, {
            "backend": "hepjob",
            "hepjob": {
                "schedd_name": "s", "accounting_group": "g", "bogus": True,
            },
        }))


def test_bad_backend_value_is_error(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="backend"):
        config_mod.load(_write(tmp_path, {"backend": "slurm"}))


def test_hepjob_int_bounds(tmp_path: Path):
    with pytest.raises(config_mod.ConfigError, match="poll_seconds"):
        config_mod.load(_write(tmp_path, {
            "backend": "hepjob", "hepjob": {
                "schedd_name": "s", "accounting_group": "g",
                "poll_seconds": 1,
            },
        }))
