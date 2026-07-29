# SimpleLoop Init Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an idempotent `simpleloop init --config TASK` command that turns an existing source directory into the minimum runnable SimpleLoop project by creating its Git baseline when needed and building or validating its Apptainer image.

**Architecture:** Extend the existing strict config resolver with a preparation mode that validates the complete document while temporarily allowing a missing Git repository and SIF. Put orchestration in a focused `simpleloop/initialize.py` module: prepare Git, reuse or build the configured image, run the existing Apptainer preflight, then perform the normal strict config load. Keep `runtime.definition` optional; infer `<image-stem>.def` beside the configured image when omitted.

**Tech Stack:** Python 3.9+, argparse, pathlib, subprocess, tempfile, PyYAML, Apptainer, pytest

---

## File map

- Create `simpleloop/initialize.py`: Git initialization, image readiness checks, build orchestration, and structured init results.
- Create `tests/test_initialize.py`: unit coverage for Git, image, idempotency, failures, and CLI behavior.
- Modify `simpleloop/config.py`: preparation-mode loading and optional `runtime.definition` resolution.
- Modify `simpleloop/cli.py`: expose `simpleloop init --config TASK [--force]`.
- Modify `tests/test_runtime.py`: config schema and preparation-mode tests.
- Modify `tests/test_example_apptainer.py`: assert runnable examples identify the correct definition/image pair.
- Modify `README.md`, `examples/README.md`, and example README files: make `simpleloop init` the primary setup path.
- Modify runnable YAML files under `examples/`: explicitly identify the shared JUNOSW definition where same-name inference is not applicable.
- Keep `examples/tiny_algo_opt/setup.sh` for backward compatibility, but remove it from the primary documented workflow.

### Task 1: Add preparation-mode config resolution

**Files:**
- Modify: `simpleloop/config.py`
- Modify: `tests/test_runtime.py`

- [ ] **Step 1: Write failing tests for optional definitions and preparation mode**

Add tests covering:

```python
def test_runtime_definition_defaults_beside_image(tmp_path: Path):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    cfg = config_mod.load(
        _write_task(tmp_path, {"image": "runtime.sif"})
    )
    assert cfg["runtime_definition"] == str(tmp_path / "runtime.def")


def test_runtime_accepts_explicit_relative_definition(tmp_path: Path):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    cfg = config_mod.load(
        _write_task(
            tmp_path,
            {"image": "runtime.sif", "definition": "containers/base.def"},
        )
    )
    assert cfg["runtime_definition"] == str(
        (tmp_path / "containers/base.def").resolve()
    )


def test_prepare_load_allows_missing_git_metadata_and_image(tmp_path: Path):
    task = _write_task(
        tmp_path,
        {"image": "missing.sif", "definition": "runtime.def"},
    )
    repo = tmp_path / "repo"
    (repo / ".git").rmdir()

    cfg = config_mod.load(task, require_ready=False)

    assert cfg["repo_path"] == str(repo.resolve())
    assert cfg["runtime_image"] == str((tmp_path / "missing.sif").resolve())


def test_prepare_load_still_requires_existing_source_directory(tmp_path: Path):
    task = _write_task(
        tmp_path,
        {"image": "missing.sif", "definition": "runtime.def"},
    )
    repo = tmp_path / "repo"
    (repo / ".git").rmdir()
    repo.rmdir()

    with pytest.raises(config_mod.ConfigError, match="source.path.*directory"):
        config_mod.load(task, require_ready=False)


def test_prepare_load_still_validates_unrelated_schema(tmp_path: Path):
    task = _write_task(
        tmp_path,
        {"image": "missing.sif", "definition": "runtime.def"},
    )
    raw = yaml.safe_load(task.read_text())
    raw["loop"]["max_rounds"] = 0
    task.write_text(yaml.safe_dump(raw))

    with pytest.raises(config_mod.ConfigError, match="loop.max_rounds"):
        config_mod.load(task, require_ready=False)
```

Update `_write_task` only as needed so its source directory exists independently from `.git`.

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```bash
pytest tests/test_runtime.py \
  -k 'definition or prepare_load' -v
```

Expected: failures because `definition` is currently rejected, `runtime_definition` is absent, and `load()` has no `require_ready` argument.

- [ ] **Step 3: Implement the smallest preparation-mode extension**

Change the public loader signature without changing existing callers:

```python
def load(
    config_path: str | Path,
    *,
    require_ready: bool = True,
) -> dict[str, Any]:
    """Load and validate a task config.

    ``require_ready=False`` validates the complete schema and resolves paths
    for ``simpleloop init``, while allowing Git metadata and the SIF to be
    created afterward.
    """
    path = Path(config_path).expanduser().resolve()
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        raw = json.loads(text)
    elif path.suffix.lower() in (".yaml", ".yml"):
        raw = yaml.safe_load(text)
    else:
        raise ConfigError(
            f"config: unsupported extension {path.suffix!r}; "
            "use .json/.yaml/.yml"
        )
    if not isinstance(raw, dict):
        raise ConfigError("config: top-level value must be an object")
    return _resolve(raw, path, require_ready=require_ready)
```

Thread `require_ready` through `_resolve` and `_resolve_runtime`. Resolve the runtime block as:

```python
def _resolve_runtime(
    raw: object,
    config_path: Path,
    *,
    require_ready: bool,
) -> tuple[str, str, list[str]]:
    if not isinstance(raw, dict):
        raise ConfigError("runtime: required and must be an object")
    unknown = set(raw) - {"image", "definition", "binds"}
    if unknown:
        raise ConfigError(f"runtime: unknown key(s): {sorted(unknown)}")

    image_value = raw.get("image")
    if not isinstance(image_value, str) or not image_value.strip():
        raise ConfigError("runtime.image: required non-empty path")
    image = Path(_rel(image_value, config_path)).expanduser().resolve()

    definition_value = raw.get("definition")
    if definition_value is None:
        definition = image.with_suffix(".def")
    elif not isinstance(definition_value, str) or not definition_value.strip():
        raise ConfigError("runtime.definition: must be a non-empty path")
    else:
        definition = Path(
            _rel(definition_value, config_path)
        ).expanduser().resolve()

    if require_ready:
        if not image.is_file():
            raise ConfigError(
                f"runtime.image: does not exist or is not a file: {image}"
            )
        if not os.access(image, os.R_OK):
            raise ConfigError(f"runtime.image: not readable: {image}")

    raw_binds = raw.get("binds", [])
    if not isinstance(raw_binds, list):
        raise ConfigError(
            "runtime.binds: must be a list of absolute directories"
        )
    binds: list[str] = []
    for index, value in enumerate(raw_binds):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"runtime.binds[{index}]: must be a non-empty path"
            )
        bind = Path(value).expanduser()
        if not bind.is_absolute():
            raise ConfigError(
                f"runtime.binds[{index}]: must be absolute: {value}"
            )
        bind = bind.resolve()
        if not bind.is_dir():
            raise ConfigError(
                f"runtime.binds[{index}]: not an existing directory: {bind}"
            )
        if ":" in str(bind) or "," in str(bind):
            raise ConfigError(
                f"runtime.binds[{index}]: contains an unsupported bind "
                f"separator (':' or ','): {bind}"
            )
        binds.append(str(bind))
    return str(image), str(definition), binds
```

Do not require the definition file during ordinary `load()`: an already-built image is runnable without retaining its definition. In preparation mode, require `source.path` to be an existing directory, but defer only the `.git` check:

```python
repo = Path(_rel(src_path, path)).resolve()
if not repo.is_dir():
    raise ConfigError(
        f"source.path: does not exist or is not a directory: {repo}"
    )
if require_ready and not (repo / ".git").exists():
    raise ConfigError(f"source.path: not a git repo: {repo}")
```

Add `"runtime_definition": runtime_definition` to the resolved dictionary and update the schema comment.

- [ ] **Step 4: Run config tests**

Run:

```bash
pytest tests/test_runtime.py tests/test_provenance_export_lock.py -q
```

Expected: all tests pass; existing strict `config.load(path)` behavior remains unchanged.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/config.py tests/test_runtime.py
git commit -m "feat: add config preparation mode"
```

### Task 2: Implement safe, idempotent Git preparation

**Files:**
- Create: `simpleloop/initialize.py`
- Create: `tests/test_initialize.py`

- [ ] **Step 1: Write failing Git preparation tests**

Create the test module with complete helpers that build a valid task YAML and
invoke real Git in temporary directories:

```python
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest
import yaml

from simpleloop import cli as cli_mod
from simpleloop import config as config_mod
from simpleloop import initialize as initialize_mod
from simpleloop.container.runtime import RuntimePreflightError
from simpleloop.initialize import (
    InitError,
    InitResult,
    initialize,
    prepare_git,
)


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
        "safety": {"editable_paths": ["**/*.py"]},
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
    outer = tmp_path / "outer"
    source = outer / "source"
    source.mkdir(parents=True)
    (source / "code.py").write_text("VALUE = 1\n")
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
    source = tmp_path / "empty"
    source.mkdir()

    assert prepare_git(source, "HEAD") == "initialized"
    assert _git_output(source, "rev-parse", "--verify", "HEAD^{commit}")


def test_prepare_git_finishes_unborn_repository(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    (source / "code.py").write_text("VALUE = 1\n")

    assert prepare_git(source, "HEAD") == "initialized"
    assert _git_output(source, "show", "HEAD:code.py") == "VALUE = 1"


def test_prepare_git_leaves_existing_repository_unchanged(tmp_path: Path):
    source = _committed_repo(tmp_path / "source")
    before = _git_output(source, "rev-parse", "HEAD")

    assert prepare_git(source, "HEAD") == "ready"
    assert _git_output(source, "rev-parse", "HEAD") == before


def test_prepare_git_rejects_missing_baseline_in_existing_repo(tmp_path: Path):
    source = _committed_repo(tmp_path / "source")

    with pytest.raises(InitError, match="baseline_ref.*missing"):
        prepare_git(source, "missing")
```

The nested-source test is mandatory: `git -C source rev-parse` can see a parent repository, but SimpleLoop needs `source` to become its own repository.

- [ ] **Step 2: Run the Git tests and confirm they fail**

Run:

```bash
pytest tests/test_initialize.py -k git -v
```

Expected: import/collection failure because `simpleloop.initialize` does not exist.

- [ ] **Step 3: Add the focused initialization module and Git implementation**

Create:

```python
"""Prepare a task's source repository and Apptainer image."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import config as config_mod
from .container.image import ImageBuildError, build_image
from .container.runtime import ApptainerRuntime, RuntimePreflightError


class InitError(RuntimeError):
    """User-facing failure while preparing a task."""


@dataclass(frozen=True)
class InitResult:
    repo_status: str
    image_status: str
    repo_path: Path
    image_path: Path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    executable = shutil.which("git")
    if not executable:
        raise InitError("git executable not found on host")
    completed = subprocess.run(
        [executable, "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise InitError(f"git {' '.join(args)} failed: {detail}")
    return completed


def _is_repo_root(repo: Path) -> bool:
    completed = _git(repo, "rev-parse", "--show-toplevel", check=False)
    if completed.returncode:
        return False
    return Path(completed.stdout.strip()).resolve() == repo.resolve()


def _has_head(repo: Path) -> bool:
    return _git(
        repo, "rev-parse", "--verify", "HEAD^{commit}", check=False
    ).returncode == 0


def _verify_ref(repo: Path, baseline_ref: str) -> None:
    completed = _git(
        repo,
        "rev-parse",
        "--verify",
        f"{baseline_ref}^{{commit}}",
        check=False,
    )
    if completed.returncode:
        raise InitError(
            f"source.baseline_ref does not resolve to a commit: {baseline_ref}"
        )


def prepare_git(repo: str | Path, baseline_ref: str) -> str:
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise InitError(
            f"source.path does not exist or is not a directory: {path}"
        )

    if _is_repo_root(path) and _has_head(path):
        _verify_ref(path, baseline_ref)
        return "ready"

    if not _is_repo_root(path):
        _git(path, "init")
    _git(path, "add", "-A")
    _git(
        path,
        "-c",
        "user.name=SimpleLoop",
        "-c",
        "user.email=simpleloop@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "simpleloop baseline",
    )
    _verify_ref(path, baseline_ref)
    return "initialized"
```

If the configured baseline is not `HEAD` for a newly created repository, `_verify_ref` must report it rather than silently rewriting the requested ref.

- [ ] **Step 4: Run the Git tests**

Run:

```bash
pytest tests/test_initialize.py -k git -q
```

Expected: all Git preparation tests pass.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/initialize.py tests/test_initialize.py
git commit -m "feat: prepare source git repository"
```

### Task 3: Build, reuse, and preflight the Apptainer image

**Files:**
- Modify: `simpleloop/initialize.py`
- Modify: `tests/test_initialize.py`

- [ ] **Step 1: Write failing image orchestration tests**

Use monkeypatches rather than building real SIFs:

```python
def test_initialize_builds_missing_image(monkeypatch, task_path: Path):
    cfg = config_mod.load(task_path, require_ready=False)
    calls = []

    monkeypatch.setattr(
        initialize_mod,
        "prepare_git",
        lambda repo, ref: "initialized",
    )
    monkeypatch.setattr(
        initialize_mod,
        "build_image",
        lambda definition, output, force=False: (
            calls.append((Path(definition), Path(output), force))
            or Path(output).write_bytes(b"sif")
            or Path(output)
        ),
    )
    monkeypatch.setattr(initialize_mod, "_preflight_image", lambda cfg: None)

    result = initialize(task_path)

    assert result.image_status == "built"
    assert calls == [
        (
            Path(cfg["runtime_definition"]),
            Path(cfg["runtime_image"]),
            False,
        )
    ]


def test_initialize_reuses_preflighted_image(monkeypatch, ready_task_path: Path):
    monkeypatch.setattr(initialize_mod, "prepare_git", lambda *args: "ready")
    monkeypatch.setattr(initialize_mod, "_preflight_image", lambda cfg: None)
    monkeypatch.setattr(
        initialize_mod,
        "build_image",
        lambda *args, **kwargs: pytest.fail("must not rebuild usable image"),
    )

    result = initialize(ready_task_path)

    assert result.image_status == "ready"


def test_initialize_reports_bad_existing_image_without_force(
    monkeypatch, ready_task_path: Path
):
    monkeypatch.setattr(initialize_mod, "prepare_git", lambda *args: "ready")
    monkeypatch.setattr(
        initialize_mod,
        "_preflight_image",
        lambda cfg: (_ for _ in ()).throw(
            RuntimePreflightError("bad image")
        ),
    )

    with pytest.raises(InitError, match="bad image.*--force"):
        initialize(ready_task_path)


def test_initialize_force_rebuilds_existing_image(
    monkeypatch, ready_task_path: Path
):
    calls = []
    monkeypatch.setattr(initialize_mod, "prepare_git", lambda *args: "ready")
    monkeypatch.setattr(
        initialize_mod,
        "build_image",
        lambda definition, output, force=False: calls.append(force) or Path(output),
    )
    monkeypatch.setattr(initialize_mod, "_preflight_image", lambda cfg: None)

    result = initialize(ready_task_path, force=True)

    assert calls == [True]
    assert result.image_status == "rebuilt"


def test_initialize_is_idempotent(monkeypatch, ready_task_path: Path):
    monkeypatch.setattr(initialize_mod, "_preflight_image", lambda cfg: None)
    monkeypatch.setattr(
        initialize_mod,
        "build_image",
        lambda *args, **kwargs: pytest.fail("unexpected rebuild"),
    )

    first = initialize(ready_task_path)
    second = initialize(ready_task_path)

    assert first.repo_status == second.repo_status == "ready"
    assert first.image_status == second.image_status == "ready"


def test_initialize_rejects_malformed_config_before_mutation(
    monkeypatch, task_path: Path
):
    raw = yaml.safe_load(task_path.read_text())
    raw["loop"]["max_rounds"] = 0
    task_path.write_text(yaml.safe_dump(raw))
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
        initialize(task_path)


def test_initialize_reports_missing_definition(
    monkeypatch, task_path: Path
):
    monkeypatch.setattr(
        initialize_mod,
        "prepare_git",
        lambda *args: "initialized",
    )

    with pytest.raises(InitError, match="runtime.definition.*runtime.def"):
        initialize(task_path)
```

- [ ] **Step 2: Run image orchestration tests and confirm they fail**

Run:

```bash
pytest tests/test_initialize.py -k 'image or idempotent or malformed' -v
```

Expected: failures because `initialize()` and `_preflight_image()` are absent.

- [ ] **Step 3: Implement image preflight and top-level orchestration**

Add:

```python
def _preflight_image(cfg: dict) -> None:
    with tempfile.TemporaryDirectory(prefix="simpleloop-init-") as run_dir:
        runtime = ApptainerRuntime(
            image=cfg["runtime_image"],
            binds=cfg["runtime_binds"],
            run_dir=run_dir,
        )
        runtime.preflight()


def initialize(
    config_path: str | Path,
    *,
    force: bool = False,
) -> InitResult:
    cfg = config_mod.load(config_path, require_ready=False)
    repo_status = prepare_git(cfg["repo_path"], cfg["baseline_ref"])
    image = Path(cfg["runtime_image"])
    definition = Path(cfg["runtime_definition"])

    if image.exists() and not force:
        try:
            _preflight_image(cfg)
        except RuntimePreflightError as exc:
            raise InitError(
                f"configured Apptainer image is not usable: {exc}; "
                "pass --force to rebuild it"
            ) from exc
        image_status = "ready"
    else:
        if not definition.is_file():
            raise InitError(
                f"runtime.definition does not exist or is not a file: "
                f"{definition}"
            )
        try:
            build_image(definition, image, force=force)
        except ImageBuildError as exc:
            raise InitError(str(exc)) from exc
        try:
            _preflight_image(cfg)
        except RuntimePreflightError as exc:
            raise InitError(
                f"built Apptainer image failed preflight: {exc}"
            ) from exc
        image_status = "rebuilt" if force else "built"

    # Re-run the ordinary strict loader so init cannot claim readiness under
    # weaker rules than validate/run.
    config_mod.load(config_path)
    return InitResult(
        repo_status=repo_status,
        image_status=image_status,
        repo_path=Path(cfg["repo_path"]),
        image_path=image,
    )
```

Preserve partial progress: if Git succeeds and image building fails, do not remove the new repository. A repeated call resumes idempotently.

- [ ] **Step 4: Run all initialization tests**

Run:

```bash
pytest tests/test_initialize.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/initialize.py tests/test_initialize.py
git commit -m "feat: initialize runnable task environment"
```

### Task 4: Expose `simpleloop init` in the CLI

**Files:**
- Modify: `simpleloop/cli.py`
- Modify: `tests/test_initialize.py`

- [ ] **Step 1: Write failing CLI tests**

Add:

```python
def test_init_cli_forwards_config_and_force(monkeypatch, capsys, tmp_path: Path):
    config = tmp_path / "task.yaml"
    seen = {}
    result = InitResult(
        repo_status="initialized",
        image_status="built",
        repo_path=tmp_path / "repo",
        image_path=tmp_path / "runtime.sif",
    )

    def fake_initialize(config_path, *, force):
        seen["args"] = (config_path, force)
        return result

    monkeypatch.setattr(cli_mod, "initialize", fake_initialize)
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
            InitError("source.path does not exist")
        ),
    )

    with pytest.raises(SystemExit) as exc:
        cli_mod.main(["init", "--config", "task.yaml"])

    assert exc.value.code == 1
    assert "Init error: source.path does not exist" in capsys.readouterr().err
```

- [ ] **Step 2: Run CLI tests and confirm they fail**

Run:

```bash
pytest tests/test_initialize.py -k cli -v
```

Expected: failures because the parser has no `init` command and CLI imports are absent.

- [ ] **Step 3: Add the parser and handler**

Import `InitError` and `initialize`, then register:

```python
init_parser = sub.add_parser(
    "init",
    help="Prepare a task's Git repository and Apptainer image.",
)
init_parser.add_argument(
    "--config",
    required=True,
    help="Task config (YAML/JSON).",
)
init_parser.add_argument(
    "--force",
    action="store_true",
    help="Rebuild the configured Apptainer image even when it exists.",
)
```

Handle it before `validate`/`run`:

```python
if args.command == "init":
    try:
        result = initialize(args.config, force=args.force)
    except (config_mod.ConfigError, InitError) as exc:
        print(f"Init error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Initializing: {args.config}")
    print(f"  Git source: {result.repo_status} ({result.repo_path})")
    print(
        f"  Apptainer image: {result.image_status} "
        f"({result.image_path})"
    )
    print("  Configuration: valid")
    print("Ready to run.")
    return
```

- [ ] **Step 4: Run CLI and adjacent image tests**

Run:

```bash
pytest tests/test_initialize.py tests/test_image.py -q
```

Expected: all tests pass and the existing `simpleloop image build` interface remains intact.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/cli.py tests/test_initialize.py
git commit -m "feat: add simpleloop init command"
```

### Task 5: Migrate examples and documentation to the init workflow

**Files:**
- Modify: `README.md`
- Modify: `examples/README.md`
- Modify: `examples/tiny_algo_opt/README.md`
- Modify: `examples/omilrec-opt/README.md`
- Modify: `examples/omilrec-post-v107-opt/README.md`
- Modify: `examples/omilrec-v100-opt/task.yaml`
- Modify: `examples/omilrec-v100-opt/task_hints.yaml`
- Modify: `examples/omilrec-v100-opt/task_nohints.yaml`
- Modify: `examples/omilrec-opt/task.yaml`
- Modify: `examples/omilrec-opt/omilrec-v1.11.0.yaml`
- Modify: `examples/omilrec-post-v107-opt/task.yaml`
- Modify: `examples/omilrec-post-v107-opt/task_hints.yaml`
- Modify: `examples/omilrec-post-v107-opt/task_nohints.yaml`
- Modify: `tests/test_example_apptainer.py`

- [ ] **Step 1: Update example tests first**

Replace adjacency-only assertions with exact definition/image expectations:

```python
LEAN_CONFIGS = [
    EXAMPLES / "task.yaml",
    EXAMPLES / "tiny_algo_opt" / "task.yaml",
]
JUNOSW_CONFIGS = [
    EXAMPLES / "omilrec-v100-opt" / "task.yaml",
    EXAMPLES / "omilrec-v100-opt" / "task_hints.yaml",
    EXAMPLES / "omilrec-v100-opt" / "task_nohints.yaml",
    EXAMPLES / "omilrec-opt" / "task.yaml",
    EXAMPLES / "omilrec-opt" / "omilrec-v1.11.0.yaml",
    EXAMPLES / "omilrec-post-v107-opt" / "task.yaml",
    EXAMPLES / "omilrec-post-v107-opt" / "task_hints.yaml",
    EXAMPLES / "omilrec-post-v107-opt" / "task_nohints.yaml",
]


def test_lean_configs_infer_same_name_definition():
    for path in LEAN_CONFIGS:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["runtime"]["image"] == "apptainer.sif"
        assert "definition" not in raw["runtime"]


def test_junosw_configs_name_shared_image_and_definition():
    for path in JUNOSW_CONFIGS:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["runtime"]["image"] == "../junosw-apptainer.sif"
        assert raw["runtime"]["definition"] == "../junosw-apptainer.def"
```

Update README assertions to require:

```python
assert "simpleloop init --config" in text
```

- [ ] **Step 2: Run example tests and confirm they fail**

Run:

```bash
pytest tests/test_example_apptainer.py -v
```

Expected: failures because docs still show manual setup/build commands and OMILREC configs use adjacent SIF paths.

- [ ] **Step 3: Update example runtime blocks**

Keep lean configs minimal:

```yaml
runtime:
  image: apptainer.sif
  binds: []
```

Keep existing non-empty `binds` unchanged, but point every OMILREC config directly at the shared artifacts:

```yaml
runtime:
  image: ../junosw-apptainer.sif
  definition: ../junosw-apptainer.def
  binds:
    - /cvmfs
    - /data/juno
    - /datafs/users/wujxy/agent-sci/omilrec_opt
```

Direct shared paths remove the need for ignored local `apptainer.sif` symlinks and let `simpleloop init` build the exact configured output.

- [ ] **Step 4: Update user-facing run instructions**

Make this the primary sequence in the root and tiny READMEs:

```bash
simpleloop init --config examples/tiny_algo_opt/task.yaml
simpleloop run \
  --config examples/tiny_algo_opt/task.yaml \
  --run-dir examples/tiny_algo_opt/runs/run-001
```

Document these semantics concisely:

- `source.path` must already be an existing directory.
- A missing/unborn Git repository receives `simpleloop baseline`.
- Existing repositories and valid images are reused.
- A missing image is built from optional `runtime.definition`, defaulting to the `.def` beside the `.sif`.
- `--force` rebuilds the configured image.
- `runtime.binds` remains optional and should be listed only when external directories are needed.
- `simpleloop image build` remains available for direct/manual image management.

Update OMILREC README commands to use their task config with `simpleloop init`; retain the explanation that their source repositories and absolute bind directories are external prerequisites.

- [ ] **Step 5: Run documentation/example tests**

Run:

```bash
pytest tests/test_example_apptainer.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add README.md examples tests/test_example_apptainer.py
git commit -m "docs: adopt simpleloop init in examples"
```

### Task 6: End-to-end verification

**Files:**
- No production changes expected

- [ ] **Step 1: Run focused suites**

Run:

```bash
pytest tests/test_initialize.py tests/test_runtime.py \
  tests/test_image.py tests/test_example_apptainer.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the complete test suite**

Run:

```bash
pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Exercise CLI help**

Run:

```bash
simpleloop init --help
simpleloop --help
```

Expected: `init --config CONFIG [--force]` is shown and all existing commands remain present.

- [ ] **Step 4: Exercise real idempotency on the tiny example when Apptainer is available**

Use a disposable copy so the tracked example and local SIF are not overwritten:

```bash
tmp_dir="$(mktemp -d)"
cp -a examples/tiny_algo_opt "$tmp_dir/tiny_algo_opt"
rm -rf "$tmp_dir/tiny_algo_opt/repo/.git"
rm -f "$tmp_dir/tiny_algo_opt/apptainer.sif"
simpleloop init --config "$tmp_dir/tiny_algo_opt/task.yaml"
simpleloop init --config "$tmp_dir/tiny_algo_opt/task.yaml"
simpleloop validate --config "$tmp_dir/tiny_algo_opt/task.yaml"
```

Expected:

- first init reports Git `initialized` and image `built`;
- second init reports both `ready` and does not add a commit;
- validate succeeds.

If a real image build is intentionally skipped because it requires network access, record that explicitly; do not substitute a fake SIF and claim real preflight success.

- [ ] **Step 5: Inspect the final diff**

Run:

```bash
git status --short
git diff --check
git diff --stat
```

Expected: only planned files are changed, no whitespace errors, and no generated `.sif`, run directories, or nested `.git` content is tracked.
