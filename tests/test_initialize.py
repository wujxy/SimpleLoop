from __future__ import annotations

from pathlib import Path
import subprocess

import pytest
import yaml

from simpleloop import cli as cli_mod
from simpleloop import config as config_mod
from simpleloop import initialize as initialize_mod
from simpleloop.container.runtime import RuntimePreflightError


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _git_output(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


def _committed_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    (path / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(path, "init")
    _git(path, "add", "-A")
    _git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "baseline",
    )
    return path


def _write_task_config(
    path: Path,
    source: Path,
    *,
    image_exists: bool,
) -> Path:
    definition = path.parent / "runtime.def"
    definition.write_text(
        "Bootstrap: docker\nFrom: almalinux:9\n",
        encoding="utf-8",
    )
    if image_exists:
        (path.parent / "runtime.sif").write_bytes(b"sif")
    raw = {
        "kind": "task",
        "task": {"goal": "test"},
        "safety": {"editable_paths": ["src"]},
        "loop": {"max_rounds": 1},
        "runtime": {"image": "runtime.sif"},
        "eval": {
            "commands": ["python -m pytest -q"],
            "metrics": {
                "objective": {
                    "key": "SPEED_MS",
                    "lower_is_better": True,
                },
                "gates": [],
            },
        },
        "source": {"path": str(source), "baseline_ref": "HEAD"},
    }
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


@pytest.fixture
def task_path(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    return _write_task_config(
        tmp_path / "task.yaml",
        source,
        image_exists=False,
    )


@pytest.fixture
def ready_task_path(tmp_path: Path) -> Path:
    source = _committed_repo(tmp_path / "source")
    return _write_task_config(
        tmp_path / "task.yaml",
        source,
        image_exists=True,
    )


def test_prepare_git_initializes_nested_source_with_baseline(tmp_path: Path):
    from simpleloop.initialize import prepare_git

    outer = tmp_path / "outer"
    source = outer / "source"
    source.mkdir(parents=True)
    (source / "code.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(outer, "init")

    result = prepare_git(source, "HEAD")

    assert result == "initialized"
    assert _git_output(source, "rev-parse", "--show-toplevel") == str(source)
    assert _git_output(source, "log", "-1", "--format=%an <%ae>") == (
        "SimpleLoop <simpleloop@localhost>"
    )
    assert _git_output(source, "log", "-1", "--format=%s") == (
        "simpleloop baseline"
    )
    assert _git_output(source, "show", "HEAD:code.py") == "VALUE = 1"


def test_prepare_git_creates_allow_empty_baseline(tmp_path: Path):
    from simpleloop.initialize import prepare_git

    source = tmp_path / "empty"
    source.mkdir()

    assert prepare_git(source, "HEAD") == "initialized"
    assert _git_output(source, "rev-parse", "--verify", "HEAD^{commit}")


def test_prepare_git_finishes_unborn_repository(tmp_path: Path):
    from simpleloop.initialize import prepare_git

    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    (source / "code.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert prepare_git(source, "HEAD") == "initialized"
    assert _git_output(source, "show", "HEAD:code.py") == "VALUE = 1"


def test_prepare_git_leaves_existing_repository_unchanged(tmp_path: Path):
    from simpleloop.initialize import prepare_git

    source = _committed_repo(tmp_path / "source")
    before = _git_output(source, "rev-parse", "HEAD")

    assert prepare_git(source, "HEAD") == "ready"
    assert _git_output(source, "rev-parse", "HEAD") == before


def test_prepare_git_rejects_missing_baseline_in_existing_repo(
    tmp_path: Path,
):
    from simpleloop.initialize import InitError, prepare_git

    source = _committed_repo(tmp_path / "source")

    with pytest.raises(InitError, match="baseline_ref.*missing"):
        prepare_git(source, "missing")


def test_initialize_builds_missing_image(monkeypatch, task_path: Path):
    cfg = config_mod.load(task_path, require_ready=False)
    calls = []

    def fake_build(definition, output, *, force=False):
        calls.append((Path(definition), Path(output), force))
        Path(output).write_bytes(b"sif")
        return Path(output)

    monkeypatch.setattr(initialize_mod, "build_image", fake_build)
    monkeypatch.setattr(initialize_mod, "_preflight_image", lambda cfg: None)

    result = initialize_mod.initialize(task_path)

    assert result.image_status == "built"
    assert calls == [
        (
            Path(cfg["runtime_definition"]),
            Path(cfg["runtime_image"]),
            False,
        )
    ]


def test_initialize_reuses_preflighted_image(
    monkeypatch,
    ready_task_path: Path,
):
    monkeypatch.setattr(
        initialize_mod,
        "prepare_git",
        lambda *args: "ready",
    )
    monkeypatch.setattr(initialize_mod, "_preflight_image", lambda cfg: None)
    monkeypatch.setattr(
        initialize_mod,
        "build_image",
        lambda *args, **kwargs: pytest.fail(
            "must not rebuild usable image"
        ),
    )

    result = initialize_mod.initialize(ready_task_path)

    assert result.image_status == "ready"


def test_initialize_reports_bad_existing_image_without_force(
    monkeypatch,
    ready_task_path: Path,
):
    monkeypatch.setattr(
        initialize_mod,
        "prepare_git",
        lambda *args: "ready",
    )
    monkeypatch.setattr(
        initialize_mod,
        "_preflight_image",
        lambda cfg: (_ for _ in ()).throw(
            RuntimePreflightError("bad image")
        ),
    )

    with pytest.raises(
        initialize_mod.InitError,
        match="bad image.*--force",
    ):
        initialize_mod.initialize(ready_task_path)


def test_initialize_force_rebuilds_existing_image(
    monkeypatch,
    ready_task_path: Path,
):
    calls = []
    monkeypatch.setattr(
        initialize_mod,
        "prepare_git",
        lambda *args: "ready",
    )
    monkeypatch.setattr(
        initialize_mod,
        "build_image",
        lambda definition, output, force=False: (
            calls.append(force) or Path(output)
        ),
    )
    monkeypatch.setattr(initialize_mod, "_preflight_image", lambda cfg: None)

    result = initialize_mod.initialize(ready_task_path, force=True)

    assert calls == [True]
    assert result.image_status == "rebuilt"


def test_initialize_is_idempotent(monkeypatch, ready_task_path: Path):
    monkeypatch.setattr(initialize_mod, "_preflight_image", lambda cfg: None)
    monkeypatch.setattr(
        initialize_mod,
        "build_image",
        lambda *args, **kwargs: pytest.fail("unexpected rebuild"),
    )

    first = initialize_mod.initialize(ready_task_path)
    second = initialize_mod.initialize(ready_task_path)

    assert first.repo_status == second.repo_status == "ready"
    assert first.image_status == second.image_status == "ready"


def test_initialize_rejects_malformed_config_before_mutation(
    monkeypatch,
    task_path: Path,
):
    raw = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    raw["loop"]["max_rounds"] = 0
    task_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(
        initialize_mod,
        "prepare_git",
        lambda *args: pytest.fail("must not initialize Git"),
    )
    monkeypatch.setattr(
        initialize_mod,
        "build_image",
        lambda *args, **kwargs: pytest.fail("must not build an image"),
    )

    with pytest.raises(config_mod.ConfigError, match="loop.max_rounds"):
        initialize_mod.initialize(task_path)


def test_initialize_reports_missing_definition(
    monkeypatch,
    task_path: Path,
):
    (task_path.parent / "runtime.def").unlink()
    monkeypatch.setattr(
        initialize_mod,
        "prepare_git",
        lambda *args: "initialized",
    )

    with pytest.raises(
        initialize_mod.InitError,
        match="runtime.definition.*runtime.def",
    ):
        initialize_mod.initialize(task_path)


def test_init_cli_forwards_config_and_force(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    config = tmp_path / "task.yaml"
    seen = {}
    result = initialize_mod.InitResult(
        repo_status="initialized",
        image_status="built",
        repo_path=tmp_path / "repo",
        image_path=tmp_path / "runtime.sif",
    )

    def fake_initialize(config_path, *, force):
        seen["args"] = (config_path, force)
        return result

    monkeypatch.setattr(cli_mod, "initialize", fake_initialize, raising=False)

    cli_mod.main(["init", "--config", str(config), "--force"])

    assert seen["args"] == (str(config), True)
    output = capsys.readouterr().out
    assert "Git source: initialized" in output
    assert "Apptainer image: built" in output
    assert "Configuration: valid" in output
    assert "Ready to run." in output


def test_init_cli_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_mod,
        "initialize",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            initialize_mod.InitError("source.path does not exist")
        ),
        raising=False,
    )

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["init", "--config", "task.yaml"])

    assert exc.value.code == 1
    assert (
        "Init error: source.path does not exist"
        in capsys.readouterr().err
    )
