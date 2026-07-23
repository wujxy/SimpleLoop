# Mandatory Apptainer Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one configured Apptainer SIF the mandatory execution environment for all SimpleLoop agents and harness evaluations, fail before optimization when that environment is unusable, and provide a minimal SIF build shortcut plus maintained definition files for every example.

**Architecture:** Add one `ApptainerRuntime` boundary that owns command wrapping, clean-environment policy, binds, and preflight. Inject the same object into proposer, executor, judger, and evaluation paths while leaving Git/worktree orchestration on the host. Represent evaluation exit status explicitly so baseline acceptance can be fail-fast without changing how candidate output is shown to the judger.

**Tech Stack:** Python 3.9+, standard-library `argparse`/`dataclasses`/`pathlib`/`shutil`/`subprocess`, Apptainer, AlmaLinux 9 definition files, pytest, YAML.

## Global Constraints

- Apptainer is mandatory. Do not retain a host-execution fallback, compatibility flag, or environment escape hatch.
- Keep host-side Git clone/worktree/gating/history/plot logic unchanged; only Claude and harness evaluation execute in the SIF.
- Use one runtime object and one argv builder for all three agents and all evaluation commands.
- Always use `apptainer exec --cleanenv`, `shell=False`, same-path configured binds, an automatic read/write run-directory bind, and role-specific `--cwd`.
- Do not use `--containall` or `--no-home`; normal home and network behavior remain available.
- Prevent pre-existing `APPTAINERENV_*`, `SINGULARITYENV_*`, `APPTAINER_BIND`, and `APPTAINER_BINDPATH` variables from bypassing the fixed runtime policy.
- Only inject the approved Claude token/auth/endpoint, proxy, and certificate variables into the container. Never print their values.
- Keep the current executor prompt and debugging autonomy; do not add environment-investigation budgets or prohibitions.
- A configured baseline evaluation is an environment acceptance test and must pass before the first proposer. A task without eval commands stops after successful tool preflight.
- Config loading remains strict: the SIF and bind directories must already exist. Consequently, example docs must build the adjacent SIF before `simpleloop validate`; repository tests inspect unbuilt example YAML as data rather than loading it through `config.load`.
- Definition files are independent regular files, not symlinks, and do not copy repositories, JUNO, data, maps, or other large resources into the image.
- `simpleloop image build` is a thin synchronous wrapper; it does not generate definitions, retry, manage caches, or build implicitly during `run`.

---

## File Map

- Create `simpleloop/runtime.py`: runtime errors, argv/environment construction, and preflight.
- Create `simpleloop/image.py`: definition validation, output derivation, and `apptainer build --fakeroot`.
- Create `simpleloop/tests/test_runtime.py`: runtime config, argv, environment, and preflight tests.
- Create `simpleloop/tests/test_image.py`: image builder and CLI tests.
- Create `simpleloop/tests/test_example_apptainer.py`: definition/config/docs asset tests.
- Modify `simpleloop/config.py`: require and normalize `runtime.image` and `runtime.binds`.
- Modify `simpleloop/agent.py`: run literal `claude` through the injected runtime.
- Modify `simpleloop/judger.py`: run eval payloads through the runtime and return command statuses.
- Modify `simpleloop/loop.py`: preflight before workspace setup, inject the runtime everywhere, and enforce baseline acceptance.
- Modify `simpleloop/cli.py`: add `image build`, runtime validation output, and runtime error handling.
- Modify `simpleloop/tests/test_parallel_candidates.py`: runtime-aware helpers, evaluator doubles, candidate wiring, and baseline tests.
- Modify `simpleloop/tests/test_agent_usage.py`: provide a runtime double to direct `Agent` tests.
- Modify `simpleloop/tests/test_views_and_parse.py`: avoid strict-loading an unbuilt example image.
- Create four independent definition files under `examples/`.
- Modify all five example task YAML files, all example READMEs, root `README.md`, and `.gitignore`.

---

### Task 1: Require and Normalize Runtime Configuration

**Files:**
- Create: `simpleloop/tests/test_runtime.py`
- Modify: `simpleloop/config.py`
- Modify: `simpleloop/tests/test_parallel_candidates.py`
- Modify: `simpleloop/tests/test_views_and_parse.py`
- Modify: `examples/task.yaml`
- Modify: `examples/tiny_algo_opt/task.yaml`
- Modify: `examples/omilrec-opt/task.yaml`
- Modify: `examples/omilrec-opt/omilrec-v1.11.0.yaml`
- Modify: `examples/omilrec-post-v107-opt/task.yaml`

**Interfaces:**
- Adds normalized config keys `runtime_image: str` and `runtime_binds: list[str]`.
- Relative image paths resolve against the task-config directory.
- Bind paths must be absolute existing directories.

- [ ] **Step 1: Extend the temporary-config helper and write failing schema tests**

Change `_write_config` so every unrelated config test remains valid:

```python
def _write_config(tmp_path: Path, loop_block: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"SIF-test-double")
    cfg = {
        "kind": "task",
        "task": {"goal": "go faster"},
        "safety": {"editable_paths": ["src/**"], "frozen_paths": []},
        "loop": {"max_rounds": 3, **(loop_block or {})},
        "runtime": {"image": "runtime.sif"},
        "source": {"path": str(repo), "baseline_ref": "HEAD"},
    }
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path
```

Add focused tests to `test_runtime.py`:

```python
_MISSING = object()


def write_task(tmp_path, runtime=_MISSING):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    raw = {
        "kind": "task",
        "task": {"goal": "test"},
        "safety": {"editable_paths": ["src/**"]},
        "loop": {"max_rounds": 1},
        "source": {"path": str(repo)},
    }
    if runtime is not _MISSING:
        raw["runtime"] = runtime
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_runtime_is_required(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="runtime: required"):
        config_mod.load(write_task(tmp_path))


def test_relative_image_resolves_and_binds_default_empty(tmp_path):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    cfg = config_mod.load(write_task(tmp_path, {"image": "runtime.sif"}))
    assert cfg["runtime_image"] == str(image.resolve())
    assert cfg["runtime_binds"] == []


@pytest.mark.parametrize("runtime, message", [
    ({}, "runtime.image"),
    ({"image": "missing.sif"}, "does not exist"),
    ({"image": "runtime.sif", "binds": "not-a-list"}, "runtime.binds"),
    ({"image": "runtime.sif", "binds": ["relative"]}, "must be absolute"),
    ({"image": "runtime.sif", "unknown": True}, "unknown key"),
])
def test_runtime_rejects_invalid_shapes(tmp_path, runtime, message):
    (tmp_path / "runtime.sif").write_bytes(b"test")
    with pytest.raises(config_mod.ConfigError, match=message):
        config_mod.load(write_task(tmp_path, runtime))


def test_runtime_rejects_unreadable_image(monkeypatch, tmp_path):
    image = tmp_path / "runtime.sif"
    image.write_bytes(b"test")
    real_access = config_mod.os.access
    monkeypatch.setattr(
        config_mod.os,
        "access",
        lambda path, mode: False if Path(path) == image else real_access(path, mode),
    )
    with pytest.raises(config_mod.ConfigError, match="not readable"):
        config_mod.load(write_task(tmp_path, {"image": "runtime.sif"}))
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
python -m pytest simpleloop/tests/test_runtime.py simpleloop/tests/test_parallel_candidates.py::test_config_parallel_defaults -q
```

Expected: runtime tests fail because `runtime` is not recognized or required.

- [ ] **Step 3: Implement strict runtime parsing**

In `config.py`, import `os`, add `"runtime"` to `TASK_TOP_KEYS`, require the block, and normalize it:

```python
def _resolve_runtime(raw: object, config_path: Path) -> tuple[str, list[str]]:
    if not isinstance(raw, dict):
        raise ConfigError("runtime: required and must be an object")
    unknown = set(raw) - {"image", "binds"}
    if unknown:
        raise ConfigError(f"runtime: unknown key(s): {sorted(unknown)}")

    image_value = raw.get("image")
    if not isinstance(image_value, str) or not image_value.strip():
        raise ConfigError("runtime.image: required non-empty path")
    image = Path(_rel(image_value, config_path)).expanduser().resolve()
    if not image.is_file():
        raise ConfigError(f"runtime.image: does not exist or is not a file: {image}")
    if not os.access(image, os.R_OK):
        raise ConfigError(f"runtime.image: not readable: {image}")

    raw_binds = raw.get("binds", [])
    if not isinstance(raw_binds, list):
        raise ConfigError("runtime.binds: must be a list of absolute directories")
    binds: list[str] = []
    for index, value in enumerate(raw_binds):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"runtime.binds[{index}]: must be a non-empty path")
        bind = Path(value).expanduser()
        if not bind.is_absolute():
            raise ConfigError(f"runtime.binds[{index}]: must be absolute: {value}")
        bind = bind.resolve()
        if not bind.is_dir():
            raise ConfigError(f"runtime.binds[{index}]: not an existing directory: {bind}")
        binds.append(str(bind))
    return str(image), binds
```

Call it from `_resolve` and include the two normalized keys in the returned dict.
Update the module schema comment to document required `runtime.image` and
optional `runtime.binds`.

- [ ] **Step 4: Migrate every example YAML**

Add this block to the generic and tiny configs:

```yaml
runtime:
  image: apptainer.sif
  binds: []
```

Add this block to both `examples/omilrec-opt/*.yaml` task configs and the
post-v107 config:

```yaml
runtime:
  image: apptainer.sif
  binds:
    - /cvmfs
    - /data/juno
    - /datafs/users/wujxy/agent-sci/omilrec_opt
```

Do not call `config.load` on these checked-in configs in unit tests because the
generated `apptainer.sif` is intentionally absent. Change the existing
post-v107 assertion to use `_example_yaml`, and assert its raw `source.path`,
eval command, and metric gates.

- [ ] **Step 5: Run config tests and verify GREEN**

Run:

```bash
python -m pytest simpleloop/tests/test_runtime.py simpleloop/tests/test_parallel_candidates.py simpleloop/tests/test_views_and_parse.py -q
```

Expected: all pass without constructing a real SIF.

- [ ] **Step 6: Commit**

```bash
git add simpleloop/config.py simpleloop/tests/test_runtime.py simpleloop/tests/test_parallel_candidates.py simpleloop/tests/test_views_and_parse.py examples/task.yaml examples/tiny_algo_opt/task.yaml examples/omilrec-opt/task.yaml examples/omilrec-opt/omilrec-v1.11.0.yaml examples/omilrec-post-v107-opt/task.yaml
git commit -m "feat: require Apptainer runtime config"
```

---

### Task 2: Build the Shared Apptainer Runtime Boundary

**Files:**
- Create: `simpleloop/runtime.py`
- Modify: `simpleloop/tests/test_runtime.py`

**Interfaces:**
- Produces `RuntimePreflightError(RuntimeError)`.
- Produces `ApptainerRuntime(image: Path, binds: Sequence[Path], run_dir: Path)`.
- Produces `exec_argv(payload: Sequence[str], *, cwd: Path) -> list[str]`.
- Produces `subprocess_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]`.
- Produces `preflight() -> None`.

- [ ] **Step 1: Write failing argv and environment tests**

Add:

```python
def test_exec_argv_keeps_paths_and_payload_arguments_atomic(tmp_path):
    run_dir = tmp_path / "run dir"
    run_dir.mkdir()
    bind = tmp_path / "resource dir"
    bind.mkdir()
    image = tmp_path / "image file.sif"
    image.write_bytes(b"test")
    runtime = ApptainerRuntime(image, [bind], run_dir, executable="/usr/bin/apptainer")

    assert runtime.exec_argv(
        ["bash", "-lc", "printf '%s\n' \"$PWD\""],
        cwd=run_dir,
    ) == [
        "/usr/bin/apptainer", "exec", "--cleanenv",
        "--bind", f"{bind}:{bind}",
        "--bind", f"{run_dir}:{run_dir}:rw",
        "--cwd", str(run_dir),
        str(image),
        "bash", "-lc", "printf '%s\n' \"$PWD\"",
    ]


def test_subprocess_env_strips_container_injection_and_forwards_allowlist(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(runtime_mod.os, "environ", {
        "PATH": "/host/bin",
        "PYTHONPATH": "/bad/python",
        "LD_LIBRARY_PATH": "/bad/lib",
        "APPTAINERENV_PYTHONPATH": "/worse/python",
        "SINGULARITYENV_LD_LIBRARY_PATH": "/worse/lib",
        "APPTAINER_BIND": "/unexpected",
        "ANTHROPIC_BASE_URL": "https://endpoint.example",
        "UNRELATED_SECRET": "do-not-forward-as-payload",
    })
    runtime = make_runtime(tmp_path)
    env = runtime.subprocess_env({"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"})

    assert env["PATH"] == "/host/bin"
    assert env["PYTHONPATH"] == "/bad/python"  # host launcher only; --cleanenv removes it
    assert "APPTAINER_BIND" not in env
    assert "APPTAINERENV_PYTHONPATH" not in env
    assert "SINGULARITYENV_LD_LIBRARY_PATH" not in env
    assert env["APPTAINERENV_ANTHROPIC_BASE_URL"] == "https://endpoint.example"
    assert env["APPTAINERENV_CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"
    assert "APPTAINERENV_UNRELATED_SECRET" not in env
```

Also parameterize the documented upper/lower proxy variables, auth variables,
and certificate variables to ensure each becomes exactly one
`APPTAINERENV_<name>` entry.

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
python -m pytest simpleloop/tests/test_runtime.py -q
```

Expected: import fails because `simpleloop.runtime` does not exist.

- [ ] **Step 3: Implement argv and clean-environment policy**

Use constants and immutable resolved paths:

```python
_FORWARDED_ENV = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
}
_BLOCKED_HOST_KEYS = {"APPTAINER_BIND", "APPTAINER_BINDPATH"}
_BLOCKED_PREFIXES = ("APPTAINERENV_", "SINGULARITYENV_")


class ApptainerRuntime:
    def __init__(self, image, binds, run_dir, *, executable="apptainer"):
        self.image = Path(image).resolve()
        self.binds = tuple(Path(path).resolve() for path in binds)
        self.run_dir = Path(run_dir).resolve()
        self.executable = executable

    def exec_argv(self, payload, *, cwd):
        argv = [self.executable, "exec", "--cleanenv"]
        for bind in self.binds:
            if bind != self.run_dir:
                argv.extend(["--bind", f"{bind}:{bind}"])
        argv.extend(["--bind", f"{self.run_dir}:{self.run_dir}:rw"])
        argv.extend(["--cwd", str(Path(cwd).resolve()), str(self.image)])
        argv.extend(str(item) for item in payload)
        return argv

    def subprocess_env(self, overrides=None):
        env = {
            key: value for key, value in os.environ.items()
            if key not in _BLOCKED_HOST_KEYS
            and not key.startswith(_BLOCKED_PREFIXES)
        }
        payload = {
            key: os.environ[key]
            for key in _FORWARDED_ENV
            if key in os.environ
        }
        payload.update(overrides or {})
        for key, value in payload.items():
            if key == "CLAUDE_CODE_MAX_OUTPUT_TOKENS" or key in _FORWARDED_ENV:
                env[f"APPTAINERENV_{key}"] = str(value)
        return env
```

The host launcher still receives ordinary host variables so a site-installed
Apptainer can start. `--cleanenv` prevents `PYTHONPATH`, `LD_LIBRARY_PATH`, and
virtualenv state from entering the payload; only explicit `APPTAINERENV_*`
entries cross that boundary.

- [ ] **Step 4: Write failing preflight tests**

Mock `shutil.which` and `subprocess.run` to cover:

```python
def test_preflight_reports_missing_host_apptainer(monkeypatch, tmp_path):
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimePreflightError, match="apptainer.*host"):
        runtime.preflight()


def test_preflight_uses_one_container_probe(monkeypatch, tmp_path):
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda _name: "/usr/bin/apptainer")
    seen = {}
    monkeypatch.setattr(
        runtime_mod.subprocess,
        "run",
        lambda argv, **kwargs: seen.update(argv=argv, kwargs=kwargs)
        or subprocess.CompletedProcess(argv, 0, "preflight: PASS\n", ""),
    )
    runtime.preflight()
    assert seen["argv"][-3:-1] == ["bash", "-lc"]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["cwd"] == str(runtime.run_dir)


@pytest.mark.parametrize("stderr, expected", [
    ("missing tool: cmake", "cmake"),
    ("run directory is not writable", "not writable"),
])
def test_preflight_surfaces_container_probe_failure(
    monkeypatch, tmp_path, stderr, expected,
):
    runtime = make_runtime(tmp_path)
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda _name: "/usr/bin/apptainer")
    monkeypatch.setattr(
        runtime_mod.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 127, "", stderr),
    )
    with pytest.raises(RuntimePreflightError, match=expected):
        runtime.preflight()
```

Also test a missing image, missing bind directory, missing run directory, and
timeout. Assert no subprocess is started for static-path failures.

- [ ] **Step 5: Implement preflight and concise runtime summary**

Resolve the host executable with `shutil.which`, then run one fixed probe:

```python
_REQUIRED_TOOLS = ("bash", "git", "gcc", "g++", "make", "cmake", "node", "claude")
_PREFLIGHT_SCRIPT = """
for tool in bash git gcc g++ make cmake node claude; do
    command -v "$tool" >/dev/null 2>&1 || {
        printf 'missing tool: %s\\n' "$tool" >&2
        exit 127
    }
done
test -w "$1" || {
    printf 'run directory is not writable: %s\\n' "$1" >&2
    exit 126
}
printf 'preflight: PASS\\n'
""".strip()


def preflight(self):
    found = shutil.which(self.executable)
    if not found:
        raise RuntimePreflightError("apptainer executable not found on host")
    self.executable = found
    self._validate_paths()
    argv = self.exec_argv(
        ["bash", "-lc", _PREFLIGHT_SCRIPT, "simpleloop-preflight", str(self.run_dir)],
        cwd=self.run_dir,
    )
    try:
        completed = subprocess.run(
            argv, cwd=str(self.run_dir), env=self.subprocess_env(),
            shell=False, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=60, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimePreflightError("Apptainer preflight timed out after 60s") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[:4000]
        raise RuntimePreflightError(
            f"Apptainer preflight failed with exit {completed.returncode}: {detail}"
        )
```

Add `summary_lines()` returning image, comma-separated binds (including the
automatic run dir), and no secret-bearing environment values.

- [ ] **Step 6: Run tests and commit**

```bash
python -m pytest simpleloop/tests/test_runtime.py -q
git add simpleloop/runtime.py simpleloop/tests/test_runtime.py
git commit -m "feat: add shared Apptainer runtime boundary"
```

---

### Task 3: Route Claude Agents Through the Runtime

**Files:**
- Modify: `simpleloop/agent.py`
- Modify: `simpleloop/tests/test_runtime.py`
- Modify: `simpleloop/tests/test_agent_usage.py`
- Modify: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- `Agent(runtime: ApptainerRuntime, ...)` becomes mandatory.
- Host-side `claude` discovery is removed; preflight owns in-image discovery.

- [ ] **Step 1: Write failing Agent boundary tests**

Use a recording runtime:

```python
class RecordingRuntime:
    def __init__(self):
        self.calls = []

    def exec_argv(self, payload, *, cwd):
        self.calls.append((list(payload), Path(cwd)))
        return ["apptainer", "exec", "image.sif", *payload]

    def subprocess_env(self, overrides=None):
        self.overrides = dict(overrides or {})
        return {"APPTAINERENV_CLAUDE_CODE_MAX_OUTPUT_TOKENS": self.overrides[
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
        ]}
```

Mock `subprocess.Popen` with the existing test process double and assert:

```python
def test_agent_wraps_literal_claude_and_keeps_prompt_on_stdin(monkeypatch, tmp_path):
    runtime = RecordingRuntime()
    proc = FakePopen(stdout='{"result":"ok","usage":{}}')
    monkeypatch.setattr(agent_mod.subprocess, "Popen", proc.factory)
    agent = Agent(runtime=runtime, timeout_seconds=60)

    assert agent.run_text("large prompt", cwd=tmp_path, label="executor") == "ok"
    payload, cwd = runtime.calls[0]
    assert payload[0] == "claude"
    assert "large prompt" not in payload
    assert cwd == tmp_path
    assert proc.stdin_text == "large prompt"
    assert proc.kwargs["shell"] is not True
    assert runtime.overrides == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}
```

Update direct parser/usage tests to instantiate `Agent(runtime=RecordingRuntime())`.
Tests that monkeypatch `Agent._run` need no process behavior beyond that.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
python -m pytest simpleloop/tests/test_runtime.py simpleloop/tests/test_agent_usage.py simpleloop/tests/test_parallel_candidates.py -q
```

Expected: `Agent` does not accept or use `runtime`.

- [ ] **Step 3: Inject and use the runtime**

Change the constructor and the command-building portion of `_run`:

```python
class Agent:
    def __init__(
        self,
        runtime: ApptainerRuntime,
        command: str = "claude",
        timeout_seconds: int = 1800,
        extra_args: list[str] | None = None,
        model: str | None = None,
        allowed_tools: str = "Read,Edit,Write,Bash",
        max_output_tokens: int = 64000,
        usage_observer: Callable[[object], None] | None = None,
    ):
        self.runtime = runtime
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.extra_args = list(extra_args or [])
        self.model = model
        self.allowed_tools = allowed_tools
        self.max_output_tokens = max_output_tokens
        self.usage_observer = usage_observer

    def _run(
        self,
        prompt: str,
        *,
        cwd: Path,
        label: str,
        json_schema: dict | None = None,
    ) -> AgentResult:
        payload = [
            self.command, "-p",
            "--input-format", "text",
            "--output-format", "json",
            "--allowedTools", self.allowed_tools,
        ]
        if self.model:
            payload += ["--model", self.model]
        payload += self.extra_args
        if json_schema is not None:
            payload += [
                "--json-schema",
                json.dumps(json_schema, separators=(",", ":")),
            ]
        argv = self.runtime.exec_argv(payload, cwd=cwd)
        env = self.runtime.subprocess_env({
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.max_output_tokens),
        })
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
```

Delete `_resolve_command` and the unused `shutil` import. Preserve stdin
feeding, heartbeat, usage notification, timeout, process-group termination, and
result decoding exactly as the existing lines following `Popen` implement them.

- [ ] **Step 4: Verify and commit**

```bash
python -m pytest simpleloop/tests/test_runtime.py simpleloop/tests/test_agent_usage.py simpleloop/tests/test_parallel_candidates.py -q
git add simpleloop/agent.py simpleloop/tests/test_runtime.py simpleloop/tests/test_agent_usage.py simpleloop/tests/test_parallel_candidates.py
git commit -m "feat: run Claude agents inside Apptainer"
```

---

### Task 4: Route Evaluation Through the Runtime and Preserve Exit Status

**Files:**
- Modify: `simpleloop/judger.py`
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/tests/test_runtime.py`
- Modify: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Adds immutable `EvalResult(text: str, metrics: dict, returncodes: tuple[int, ...])`.
- Changes `run_eval(commands, cwd, runtime, metrics_schema=None) -> EvalResult`.
- Candidate evaluation presentation stays unchanged via `result.text` and `result.metrics`.

- [ ] **Step 1: Write failing evaluator tests**

Add:

```python
def test_run_eval_wraps_bash_lc_and_parses_metrics(monkeypatch, tmp_path):
    runtime = RecordingRuntime()
    seen = {}
    monkeypatch.setattr(
        judger_mod.subprocess,
        "run",
        lambda argv, **kwargs: seen.update(argv=argv, kwargs=kwargs)
        or subprocess.CompletedProcess(
            argv, 0, "SPEED_MS=12.5\nCORRECTNESS=PASS\n", ""
        ),
    )
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}],
    }

    result = judger_mod.run_eval(
        ["bash scripts/eval.sh --evtmax 10"], tmp_path, runtime, schema
    )

    assert runtime.calls == [
        (["bash", "-lc", "bash scripts/eval.sh --evtmax 10"], tmp_path)
    ]
    assert seen["kwargs"]["shell"] is False
    assert result.returncodes == (0,)
    assert result.metrics == {"SPEED_MS": 12.5, "CORRECTNESS": True}
    assert "[OK]" in result.text


def test_run_eval_records_each_nonzero_status(monkeypatch, tmp_path):
    runtime = RecordingRuntime()
    results = iter([
        subprocess.CompletedProcess([], 0, "first", ""),
        subprocess.CompletedProcess([], 9, "", "second failed"),
    ])
    monkeypatch.setattr(judger_mod.subprocess, "run", lambda *a, **k: next(results))
    result = judger_mod.run_eval(["first", "second"], tmp_path, runtime)
    assert result.returncodes == (0, 9)
    assert "[EXIT 9]" in result.text
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
python -m pytest simpleloop/tests/test_runtime.py -q
```

Expected: `run_eval` still takes the old signature and returns a tuple.

- [ ] **Step 3: Implement `EvalResult` and runtime execution**

```python
@dataclass(frozen=True)
class EvalResult:
    text: str
    metrics: dict
    returncodes: tuple[int, ...]

    @property
    def commands_ok(self) -> bool:
        return all(code == 0 for code in self.returncodes)


def run_eval(commands, cwd, runtime, metrics_schema=None):
    blocks, full_text, returncodes = [], [], []
    for cmd in commands:
        argv = runtime.exec_argv(["bash", "-lc", cmd], cwd=cwd)
        completed = subprocess.run(
            argv, shell=False, cwd=str(cwd), env=runtime.subprocess_env(),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=600, check=False,
        )
        out = completed.stdout.strip()
        err = completed.stderr.strip()
        status = "OK" if completed.returncode == 0 else f"EXIT {completed.returncode}"
        body = out if out else err
        blocks.append(f"$ {cmd}  [{status}]\n{body[:16000]}")
        full_text.append(body)
        returncodes.append(completed.returncode)
    text = "\n\n".join(blocks)
    combined = "\n".join(full_text)
    metrics = _parse_metrics(combined, metrics_schema) if metrics_schema else {}
    return EvalResult(text, metrics, tuple(returncodes))
```

Update all three loop call sites to pass `runtime` and read `.text`/`.metrics`.
Thread `runtime` through `_run_candidates` and `_run_one_candidate`; place it
after the agent arguments and pass it by keyword at call sites to avoid
positional ambiguity. Update fake evaluators to accept `runtime` and return
`EvalResult`.

- [ ] **Step 4: Verify candidate behavior and commit**

```bash
python -m pytest simpleloop/tests/test_runtime.py simpleloop/tests/test_parallel_candidates.py -q
git add simpleloop/judger.py simpleloop/loop.py simpleloop/tests/test_runtime.py simpleloop/tests/test_parallel_candidates.py
git commit -m "feat: evaluate candidates inside Apptainer"
```

---

### Task 5: Preflight the Run and Make Baseline Acceptance Fail Fast

**Files:**
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/cli.py`
- Modify: `simpleloop/tests/test_parallel_candidates.py`
- Modify: `simpleloop/tests/test_runtime.py`

**Interfaces:**
- Adds `BaselineAcceptanceError(RuntimeError)`.
- Changes `_eval_baseline(workspace, cfg, baseline_sha, runtime)`.
- `loop.run` constructs/preflights one runtime before agent creation and workspace setup.

- [ ] **Step 1: Write failing pure baseline-acceptance tests**

Extract a helper and test all rules:

```python
@pytest.mark.parametrize("result, schema, message", [
    (EvalResult("bad", {}, (3,)), None, "exit 3"),
    (
        EvalResult("missing objective", {"CORRECTNESS": True}, (0,)),
        {
            "objective": {"key": "SPEED_MS", "lower_is_better": True},
            "gates": [{"key": "CORRECTNESS"}],
        },
        "SPEED_MS",
    ),
    (
        EvalResult("failed gate", {"SPEED_MS": 10.0, "CORRECTNESS": False}, (0,)),
        {
            "objective": {"key": "SPEED_MS", "lower_is_better": True},
            "gates": [{"key": "CORRECTNESS"}],
        },
        "CORRECTNESS",
    ),
])
def test_require_baseline_acceptance_rejects_bad_results(result, schema, message):
    with pytest.raises(loop_mod.BaselineAcceptanceError, match=message):
        loop_mod._require_baseline_acceptance(result, schema)


def test_require_baseline_acceptance_accepts_commands_without_schema():
    loop_mod._require_baseline_acceptance(EvalResult("ok", {}, (0, 0)), None)


def test_require_baseline_acceptance_accepts_objective_and_all_gates():
    schema = {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "A"}, {"key": "B"}],
    }
    loop_mod._require_baseline_acceptance(
        EvalResult("ok", {"SPEED_MS": 10.0, "A": True, "B": True}, (0,)),
        schema,
    )
```

Explicitly reject a boolean or non-finite objective (`NaN`, `inf`) so an
unusable measurement cannot start optimization.

- [ ] **Step 2: Write failing run-order tests**

Extend the existing full-loop fakes in `test_parallel_candidates.py` with an
`events: list[str]`. The fake runtime appends `"preflight"`, the fake Agent
factory appends `"agent"`, and `FakeWorkspace.setup` appends
`"workspace.setup"`. Run with an empty static proposal list so no round agent is
invoked, then assert:

```python
assert events.count("preflight") == 1
assert events.index("preflight") < events.index("agent")
assert events.index("preflight") < events.index("workspace.setup")
```

Add separate tests showing that a baseline timeout/nonzero/gate failure produces
zero agent calls, while a no-eval config reaches the first proposer after
preflight. Reuse existing loop fakes rather than launching Apptainer.

- [ ] **Step 3: Implement baseline acceptance**

```python
class BaselineAcceptanceError(RuntimeError):
    pass


def _require_baseline_acceptance(result, schema):
    failed_codes = [code for code in result.returncodes if code != 0]
    if failed_codes:
        raise BaselineAcceptanceError(
            f"baseline evaluation command failed with exit {failed_codes[0]}:\n"
            f"{result.text[:8000]}"
        )
    if not schema:
        return
    objective = schema["objective"]["key"]
    value = result.metrics.get(objective)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise BaselineAcceptanceError(
            f"baseline objective {objective} is missing or not finite:\n"
            f"{result.text[:8000]}"
        )
    failed_gates = [
        gate["key"] for gate in schema.get("gates", [])
        if result.metrics.get(gate["key"]) is not True
    ]
    if failed_gates:
        raise BaselineAcceptanceError(
            f"baseline gate(s) did not pass: {', '.join(failed_gates)}:\n"
            f"{result.text[:8000]}"
        )
```

Remove the broad exception swallowing from `_eval_baseline`. Always remove its
worktree in `finally`, but propagate worktree, timeout, and acceptance errors.
On success return `(result.text, result.metrics)`. With no eval return
`(None, {})`.

- [ ] **Step 4: Construct and inject one runtime in `run`**

Immediately after creating `run_dir_path`:

```python
runtime = ApptainerRuntime(
    image=cfg["runtime_image"],
    binds=cfg["runtime_binds"],
    run_dir=run_dir_path,
)
for line in runtime.summary_lines():
    print(line, flush=True)
runtime.preflight()
print("preflight: PASS", flush=True)
```

This must precede all `Agent(...)` calls and `workspace.setup()`. Pass
`runtime=runtime` to all three Agents, candidate helpers, and both fresh/continue
baseline paths. Do not create separate role runtimes.

Catch `RuntimePreflightError` and `BaselineAcceptanceError` in CLI `run`, print
`Runtime error: ...` or `Baseline error: ...`, and exit 1 without a traceback.

- [ ] **Step 5: Show normalized runtime in `validate`**

After existing validation output:

```python
print(f"  runtime image: {cfg['runtime_image']}")
binds = ", ".join(cfg["runtime_binds"]) or "(none)"
print(f"  runtime binds: {binds}")
```

Validation does not start the container; `run` owns dynamic preflight.

- [ ] **Step 6: Verify and commit**

```bash
python -m pytest simpleloop/tests/test_runtime.py simpleloop/tests/test_parallel_candidates.py -q
git add simpleloop/loop.py simpleloop/cli.py simpleloop/tests/test_runtime.py simpleloop/tests/test_parallel_candidates.py
git commit -m "feat: fail fast on invalid runtime baseline"
```

---

### Task 6: Add the Minimal SIF Build Command

**Files:**
- Create: `simpleloop/image.py`
- Create: `simpleloop/tests/test_image.py`
- Modify: `simpleloop/cli.py`

**Interfaces:**
- Produces `ImageBuildError(RuntimeError)`.
- Produces `default_output(definition: Path) -> Path`.
- Produces `build_image(definition: Path, output: Path | None = None, *, force: bool = False) -> Path`.
- Adds `simpleloop image build DEF [--output SIF] [--force]`.

- [ ] **Step 1: Write failing builder tests**

```python
def test_default_output_replaces_definition_suffix(tmp_path):
    assert default_output(tmp_path / "juno.def") == tmp_path / "juno.sif"


def test_build_image_uses_fakeroot_and_explicit_output(monkeypatch, tmp_path):
    definition = tmp_path / "juno.def"
    definition.write_text("Bootstrap: docker\nFrom: almalinux:9\n")
    output = tmp_path / "images" / "custom.sif"
    output.parent.mkdir()
    seen = {}
    monkeypatch.setattr(image_mod.shutil, "which", lambda _name: "/usr/bin/apptainer")
    monkeypatch.setattr(
        image_mod.subprocess,
        "run",
        lambda argv, **kwargs: seen.update(argv=argv, kwargs=kwargs)
        or subprocess.CompletedProcess(argv, 0),
    )
    assert build_image(definition, output) == output.resolve()
    assert seen["argv"] == [
        "/usr/bin/apptainer", "build", "--fakeroot",
        str(output.resolve()), str(definition.resolve()),
    ]
    assert seen["kwargs"]["check"] is False


def test_existing_output_requires_force(monkeypatch, tmp_path):
    definition = tmp_path / "juno.def"
    definition.write_text("Bootstrap: docker\nFrom: almalinux:9\n")
    output = tmp_path / "juno.sif"
    output.write_bytes(b"existing")
    with pytest.raises(ImageBuildError, match="already exists.*--force"):
        build_image(definition, output)
```

Also test missing definition, missing output parent, missing host Apptainer,
nonzero build exit propagation, and that `force=True` permits the builder to
invoke Apptainer.

- [ ] **Step 2: Run tests and verify RED**

```bash
python -m pytest simpleloop/tests/test_image.py -q
```

Expected: import fails because `simpleloop.image` does not exist.

- [ ] **Step 3: Implement the thin builder**

```python
def default_output(definition):
    return Path(definition).expanduser().with_suffix(".sif")


def build_image(definition, output=None, *, force=False):
    definition = Path(definition).expanduser().resolve()
    if not definition.is_file():
        raise ImageBuildError(f"definition file does not exist: {definition}")
    output = Path(output).expanduser().resolve() if output else default_output(definition)
    if not output.parent.is_dir():
        raise ImageBuildError(f"output directory does not exist: {output.parent}")
    if output.exists() and not force:
        raise ImageBuildError(f"output already exists: {output}; pass --force to overwrite")
    apptainer = shutil.which("apptainer")
    if not apptainer:
        raise ImageBuildError("apptainer executable not found on host")
    completed = subprocess.run(
        [apptainer, "build", "--fakeroot", str(output), str(definition)],
        check=False,
    )
    if completed.returncode:
        raise ImageBuildError(f"apptainer build failed with exit {completed.returncode}")
    return output
```

Do not capture stdout/stderr; inherited streams give live build output.

- [ ] **Step 4: Add nested CLI parsing and tests**

Add parsers:

```python
image_parser = sub.add_parser("image", help="Build Apptainer images.")
image_sub = image_parser.add_subparsers(dest="image_command", required=True)
image_build = image_sub.add_parser("build", help="Build a SIF from a definition.")
image_build.add_argument("definition")
image_build.add_argument("--output")
image_build.add_argument("--force", action="store_true")
```

Dispatch before config commands, call `build_image`, print
`Built image: <resolved path>`, and map `ImageBuildError` to stderr plus exit 1.
CLI tests monkeypatch `simpleloop.cli.build_image` and verify default, override,
force, and error paths.

- [ ] **Step 5: Verify and commit**

```bash
python -m pytest simpleloop/tests/test_image.py -q
git add simpleloop/image.py simpleloop/cli.py simpleloop/tests/test_image.py
git commit -m "feat: add Apptainer image build shortcut"
```

---

### Task 7: Add Independent Example Definitions and Documentation

**Files:**
- Create: `examples/apptainer.def`
- Create: `examples/tiny_algo_opt/apptainer.def`
- Create: `examples/omilrec-opt/apptainer.def`
- Create: `examples/omilrec-post-v107-opt/apptainer.def`
- Create: `simpleloop/tests/test_example_apptainer.py`
- Modify: `examples/README.md`
- Modify: `examples/tiny_algo_opt/README.md`
- Modify: `examples/omilrec-opt/README.md`
- Modify: `examples/omilrec-post-v107-opt/README.md`
- Modify: `README.md`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing example-asset tests**

```python
EXAMPLE_DIRS = [
    EXAMPLES,
    EXAMPLES / "tiny_algo_opt",
    EXAMPLES / "omilrec-opt",
    EXAMPLES / "omilrec-post-v107-opt",
]
TASK_CONFIGS = [
    EXAMPLES / "task.yaml",
    EXAMPLES / "tiny_algo_opt" / "task.yaml",
    EXAMPLES / "omilrec-opt" / "task.yaml",
    EXAMPLES / "omilrec-opt" / "omilrec-v1.11.0.yaml",
    EXAMPLES / "omilrec-post-v107-opt" / "task.yaml",
]


def test_each_example_has_independent_regular_definition():
    definitions = [directory / "apptainer.def" for directory in EXAMPLE_DIRS]
    assert all(path.is_file() and not path.is_symlink() for path in definitions)
    assert len({path.resolve() for path in definitions}) == 4


def test_definitions_contain_pinned_runtime_tools():
    for directory in EXAMPLE_DIRS:
        text = (directory / "apptainer.def").read_text(encoding="utf-8")
        assert "From: almalinux:9" in text
        assert "node-v22.19.0-linux-x64.tar.xz" in text
        assert "@anthropic-ai/claude-code@2.1.216" in text
        for command in ("bash", "git", "gcc", "g++", "make", "cmake", "node", "claude"):
            assert f"command -v {command}" in text


def test_all_example_configs_reference_adjacent_image():
    for path in TASK_CONFIGS:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["runtime"]["image"] == "apptainer.sif"
        assert isinstance(raw["runtime"].get("binds", []), list)


def test_omilrec_configs_bind_large_external_roots():
    for path in TASK_CONFIGS[2:]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert raw["runtime"]["binds"] == [
            "/cvmfs",
            "/data/juno",
            "/datafs/users/wujxy/agent-sci/omilrec_opt",
        ]


def test_generated_sifs_are_ignored():
    assert "*.sif" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
```

Also assert every README contains its exact local `simpleloop image build`
command and OMILREC READMEs name all three external binds.

- [ ] **Step 2: Run tests and verify RED**

```bash
python -m pytest simpleloop/tests/test_example_apptainer.py -q
```

Expected: definition files and documentation are absent.

- [ ] **Step 3: Create the generic and tiny definitions as independent files**

Both files use this complete build body, with comments tailored to the example
and no `%files` section:

```text
Bootstrap: docker
From: almalinux:9

%post
    set -eux
    dnf install -y \
        bash ca-certificates curl diffutils findutils git \
        gcc gcc-c++ make cmake patch procps-ng which \
        python3 python3-pip tar xz
    curl -fsSLO https://nodejs.org/dist/v22.19.0/node-v22.19.0-linux-x64.tar.xz
    tar -xJf node-v22.19.0-linux-x64.tar.xz -C /usr/local --strip-components=1
    rm -f node-v22.19.0-linux-x64.tar.xz
    python3 -m pip install --no-cache-dir pytest
    npm install -g @anthropic-ai/claude-code@2.1.216
    dnf clean all
    rm -rf /var/cache/dnf

%environment
    export LANG=C.UTF-8
    export LC_ALL=C.UTF-8

%test
    command -v bash
    command -v git
    command -v gcc
    command -v g++
    command -v make
    command -v cmake
    command -v node
    command -v claude
    command -v python3
    command -v pytest
```

The root comments say large resources belong in `runtime.binds`. The tiny
comments state that the repository arrives through the run-directory bind and
is deliberately not baked into the image.

- [ ] **Step 4: Create both OMILREC definitions as independent files**

Use the same pinned AlmaLinux/Node/Claude base shown above in each regular file.
Retain Python/pytest as harmless diagnostic basics, but add comments stating
that `/cvmfs`, `/data/juno`, source packages, reconstruction maps, and inputs
remain external and must not be copied into the SIF. Do not source a JUNO setup
script in `%environment`; each task/eval command controls the required release.

- [ ] **Step 5: Update docs and ignore generated images**

Add `*.sif` as its own `.gitignore` line.

In each example README, put the exact local build before validation/run:

```bash
simpleloop image build examples/apptainer.def
simpleloop image build examples/tiny_algo_opt/apptainer.def
simpleloop image build examples/omilrec-opt/apptainer.def
simpleloop image build examples/omilrec-post-v107-opt/apptainer.def
```

Use only the command belonging to that README. State that the default output is
the adjacent `apptainer.sif`, `--output` may point at a shared prebuilt SIF, and
the YAML `runtime.image` must be updated in that case. OMILREC docs list
`/cvmfs`, `/data/juno`, and project storage as required same-path binds.

Update root `README.md`:

- require host Apptainer instead of host Claude/compilers;
- show `simpleloop image build examples/apptainer.def` before validate/run;
- include the required `runtime` YAML block;
- explain that Claude/eval run in one clean SIF while Git orchestration remains
  host-side;
- add `runtime.py` and `image.py` to the design table;
- call out the breaking removal of host execution.

- [ ] **Step 6: Verify and commit**

```bash
python -m pytest simpleloop/tests/test_example_apptainer.py -q
git add .gitignore README.md examples simpleloop/tests/test_example_apptainer.py
git commit -m "docs: add Apptainer definitions for all examples"
```

---

### Task 8: Full Regression and Acceptance Verification

**Files:**
- Modify only files needed to fix failures directly caused by this feature.

- [ ] **Step 1: Run the complete unit suite**

```bash
python -m pytest simpleloop/tests -q
```

Expected: all tests pass. If a legacy test creates a task config, add a fake
readable SIF and required runtime block. If it constructs `Agent` directly,
inject a recording runtime. Do not weaken mandatory production behavior for a
test convenience.

- [ ] **Step 2: Exercise CLI help and config errors without Apptainer**

```bash
python -m simpleloop.cli --help
python -m simpleloop.cli image build --help
python -m simpleloop.cli validate --config examples/task.yaml
```

Expected:

- top-level help lists `image`;
- nested help lists `definition`, `--output`, and `--force`;
- validation fails clearly if `examples/apptainer.sif` has not been built,
  proving strict config behavior.

- [ ] **Step 3: Run an argv-only acceptance test without a real container**

```bash
python -m pytest \
  simpleloop/tests/test_runtime.py \
  simpleloop/tests/test_image.py \
  simpleloop/tests/test_example_apptainer.py -q
```

Expected: all pass with subprocesses mocked; no host Apptainer or network is
required for CI.

- [ ] **Step 4: Optional real-host smoke test when Apptainer is available**

```bash
command -v apptainer
simpleloop image build examples/tiny_algo_opt/apptainer.def
simpleloop validate --config examples/tiny_algo_opt/task.yaml
simpleloop run \
  --config examples/tiny_algo_opt/task.yaml \
  --run-dir /tmp/simpleloop-apptainer-smoke \
  --proposals examples/omilrec-opt/omilrec-paper-proposals-test.yaml
```

Only perform the build with explicit user approval because it downloads a base
image and dependencies. For the run, use a one-entry proposal file matching the
tiny task if the OMILREC proposal fixture is not shape-compatible. Acceptance is
successful preflight, successful baseline, and start of the first executor
inside the SIF; stop before consuming unnecessary model tokens if this is only
a runtime smoke test.

- [ ] **Step 5: Inspect secrets and host-fallback absence**

```bash
rg -n "os\\.environ|APPTAINERENV_|SINGULARITYENV_|shell=True|shutil\\.which\\(self\\.command" simpleloop
rg -n "host fallback|use_host|no_apptainer|disable.*apptainer" simpleloop README.md examples
```

Expected:

- `shell=True` is absent from agent/evaluation host boundaries;
- only the runtime module creates container environment entries;
- no host-agent fallback switch exists;
- no log statement prints auth values.

- [ ] **Step 6: Review the final diff against the approved design**

```bash
git diff --check
git status --short
git log --oneline -8
```

Verify all three agents and both evaluation paths share the same runtime object, every
example definition is a regular file, all five task YAMLs require the adjacent
SIF, and no unrelated user changes are included.

- [ ] **Step 7: Commit any verification-only fixes**

If verification changed files, stage each exact file named by `git status`
individually and commit with:

```bash
git commit -m "test: complete Apptainer runtime coverage"
```

Skip this step when verification required no changes.

---

## Completion Criteria

- Old configs without `runtime` fail with a direct config error.
- An activated host virtualenv/JUNO environment does not become the agent or
  evaluator payload environment.
- Proposer, executor, judger, baseline eval, and candidate eval all use one SIF,
  bind set, run-directory mount, and cwd policy.
- Missing Apptainer/image/bind/tool/write access fails before agent creation.
- Nonzero, timeout, missing-objective, or failed-gate baseline prevents every
  optimization agent call.
- Tasks without eval proceed after tool preflight.
- `simpleloop image build` produces correct `apptainer build --fakeroot` argv
  and never silently overwrites an image.
- Four independent example definitions and all required runtime config/docs
  changes are present.
- The complete unit suite passes without requiring Apptainer or network access.
