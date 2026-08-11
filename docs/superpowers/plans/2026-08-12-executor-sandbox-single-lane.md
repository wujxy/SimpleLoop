# Executor Sandbox Recovery and Single-Lane Proposer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Executor session able to use Bash and deliver a Harness commit inside a private, correctly isolated Apptainer workspace, while replacing proposer multi-lane scheduling with one retained lane that generates ten hypotheses and selects up to four proposals.

**Architecture:** The Executor receives a session-owned writable home, the existing `/work` mount map, and only explicitly declared read-only external dependencies. A production-equivalent Executor preflight runs before model work. Proposer lane data structures and the standalone lane worker remain, but local and HEPJob backends always execute exactly one `lane-0`; candidate execution fan-out remains unchanged.

**Tech Stack:** Python 3.9+, Apptainer, Claude Code CLI, Git worktrees, pytest, YAML.

## Global Constraints

- Do not mount the real host home into Executor.
- Do not expose `runtime.binds` to Executor; they remain Harness/evaluator capabilities.
- Executor external dependencies are read-only and explicitly configured.
- Keep the existing worktree-relative `editable_paths`/`read_only_paths` world.
- Preserve `LaneState`, `LaneResult`, `run_lane_episode()`, lane workspace paths, lane worker manifests, and lane telemetry for future tree evolution.
- Remove only lane quota and proposer ThreadPool scheduling; candidate parallelism is unchanged.
- `candidates_per_round` becomes the maximum proposals selected by the one cognitive batch.
- Diversity is a prompt preference, not a regeneration or exact-count gate.
- Do not implement status redesign, local resume, benchmark repetitions, or tree scheduling in this phase.
- Use test-driven development and keep each implementation to the minimum needed by its failing tests.

---

## File Map

- `simpleloop/config.py`: validate and resolve `runtime.executor_read_only_binds`.
- `simpleloop/container/runtime.py`: construct the private-home Executor argv, keep evaluator binds out, and run Executor preflight.
- `simpleloop/roles/agent.py`: own the per-call sandbox lifetime and register the Claude process group.
- `simpleloop/processes.py`: minimal shared process-group registry and SIGTERM handler.
- `simpleloop/roles/research_tools.py`: register research process groups so run cancellation cleans them too.
- `simpleloop/loop.py`: build the enriched `MountMap`, run Executor preflight after workspace setup, and install run-scoped signal handling.
- `simpleloop/candidate_worker.py`: construct the same Executor mount map on worker nodes.
- `simpleloop/roles/orchestrator.py`: invoke one retained lane directly and remove quota/thread-pool scheduling.
- `simpleloop/execution/local.py`: create exactly one proposer lane workspace.
- `simpleloop/execution/hepjob.py`: submit exactly one proposer lane job.
- `examples/*/task*.yaml`: declare only required Executor read-only dependencies.
- Focused tests listed in each task below.

---

### Task 1: Add strict Executor read-only dependency config

**Necessity:** Executor needs `/cvmfs` and `/data/juno` for the OMILREC toolchain, but inheriting the evaluator's broad `runtime.binds` defeats isolation. One read-only list is the smallest role-specific capability declaration.

**Files:**

- Modify: `simpleloop/config.py:1-30,160-175,265-285,410-460`
- Modify: `tests/test_config_execution.py`

**Interfaces:**

- Produces: resolved key `executor_read_only_binds: list[str]`.
- Consumed by: Tasks 2, 4, and worker context construction.

- [ ] **Step 1: Write failing config tests**

Add to `tests/test_config_execution.py`:

```python
def test_executor_read_only_binds_resolve_absolute_paths(tmp_path: Path):
    raw = _base_task(tmp_path)
    dep = tmp_path / "dep"
    dep.mkdir()
    raw["runtime"]["executor_read_only_binds"] = [str(dep)]
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    cfg = config_mod.load(path)

    assert cfg["executor_read_only_binds"] == [str(dep.resolve())]


@pytest.mark.parametrize("value", ["relative", "", 7])
def test_executor_read_only_binds_reject_invalid_entries(tmp_path: Path, value):
    raw = _base_task(tmp_path)
    raw["runtime"]["executor_read_only_binds"] = [value]
    path = tmp_path / "task.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(config_mod.ConfigError,
                       match="runtime.executor_read_only_binds"):
        config_mod.load(path)
```

Also test that an omitted field resolves to `[]` and a nonexistent absolute
directory is rejected.

- [ ] **Step 2: Run the tests and verify red**

Run:

```bash
python -m pytest -q tests/test_config_execution.py -k executor_read_only_binds
```

Expected: FAIL because the runtime block rejects the unknown key or the
resolved config lacks `executor_read_only_binds`.

- [ ] **Step 3: Implement one reusable absolute-directory resolver**

Refactor the existing bind validation without changing `runtime.binds`
semantics:

```python
def _resolve_bind_dirs(raw: object, field: str) -> list[str]:
    if not isinstance(raw, list):
        raise ConfigError(f"{field}: must be a list of absolute directories")
    result = []
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{field}[{index}]: must be a non-empty path")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ConfigError(f"{field}[{index}]: must be absolute: {value}")
        path = path.resolve()
        if not path.is_dir():
            raise ConfigError(f"{field}[{index}]: not an existing directory: {path}")
        if ":" in str(path) or "," in str(path):
            raise ConfigError(f"{field}[{index}]: contains an unsupported bind separator")
        result.append(str(path))
    return result
```

Allow exactly `image`, `definition`, `binds`, and
`executor_read_only_binds` in the runtime block. Return the new list from
`_resolve_runtime()` and store it in the resolved config as
`executor_read_only_binds`.

- [ ] **Step 4: Run focused and full config tests**

Run:

```bash
python -m pytest -q tests/test_config_execution.py tests/test_runtime.py
```

Expected: PASS.

- [ ] **Step 5: Commit the config boundary**

```bash
git add simpleloop/config.py tests/test_config_execution.py
git commit -m "fix: separate executor dependency binds"
```

---

### Task 2: Construct an Executor world with a private writable home

**Necessity:** This is the direct EROFS fix. It must also stop shared
`runtime.binds` from leaking into the mount-world path.

**Files:**

- Modify: `simpleloop/container/runtime.py:55-180`
- Modify: `tests/test_executor_mount.py`
- Modify: `tests/test_runtime.py`

**Interfaces:**

- Consumes: `executor_read_only_binds` from Task 1.
- Produces: `MountMap.external_ro`, and
  `ApptainerRuntime.exec_argv(..., scaffold=Path, home=Path)`.

- [ ] **Step 1: Replace the old mount argv assertion with failing security assertions**

Update `test_exec_argv_with_mounts_builds_subset_world` to create `home` and
declare one external dependency:

```python
home = tmp_path / "home"
home.mkdir()
external = tmp_path / "external"
external.mkdir()
argv = rt.exec_argv(
    ["claude", "-p"], cwd=wt,
    mounts=MountMap(
        rw=("src", "build"),
        ro=("tests",),
        external_ro=(str(external),),
    ),
    scaffold=scaffold,
    home=home,
)

assert f"{home}:{rt.executor_home}:rw" in argv
assert f"{external}:{external}:ro" in argv
assert f"{bind_dep}:{bind_dep}" not in argv
assert argv[argv.index("--cwd") + 1] == "/work"
```

Add tests that mount-world mode rejects a missing `scaffold` or `home`, while
legacy/evaluator mode continues to include `runtime.binds` and does not require
either argument.

- [ ] **Step 2: Run the tests and verify red**

Run:

```bash
python -m pytest -q tests/test_executor_mount.py
```

Expected: FAIL because `MountMap` has no `external_ro`, `exec_argv()` has no
`home` parameter, and global binds still leak.

- [ ] **Step 3: Implement the minimal mount-world split**

Use the current account's passwd home as the container destination, but never
mount that host directory. The source remains the private temporary directory:

```python
import pwd


def _account_home() -> Path:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if not home.is_absolute():
        raise RuntimePreflightError(f"account home is not absolute: {home}")
    return home


@dataclass(frozen=True)
class MountMap:
    rw: tuple[str, ...] = ()
    ro: tuple[str, ...] = ()
    external_ro: tuple[str, ...] = ()
```

Change the signature:

```python
def exec_argv(self, payload, *, cwd, mounts=None, scaffold=None, home=None):
```

Set `self.executor_home = _account_home()` in `ApptainerRuntime.__init__()`.

Move the `for bind in self.binds` loop inside `if mounts is None`. In the
mount-world branch validate `scaffold` and `home`, then add:

```python
argv += ["--containall", "--no-mount", "cwd,home,hostfs"]
argv += ["--bind", f"{home_dir}:{self.executor_home}:rw"]
argv += ["--bind", f"{scaffold_dir}:/work:rw"]
for path in mounts.external_ro:
    external = Path(path).expanduser().resolve()
    argv += ["--bind", f"{external}:{external}:ro"]
```

Keep the existing worktree-relative rw/ro loop and `/work` cwd. Do not add an
external writable mount mode.

- [ ] **Step 4: Permit only the private HOME override**

Add `HOME` to `_OVERRIDE_ENV` but not `_FORWARDED_ENV`, so callers must provide
it explicitly and the ambient host HOME is never injected:

```python
_OVERRIDE_ENV = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS", "HOME"}
```

Assert in `tests/test_runtime.py`:

```python
env = runtime.subprocess_env({"HOME": str(runtime.executor_home)})
assert env["APPTAINERENV_HOME"] == str(runtime.executor_home)
```

- [ ] **Step 5: Run runtime tests**

Run:

```bash
python -m pytest -q tests/test_executor_mount.py tests/test_runtime.py
```

Expected: PASS.

- [ ] **Step 6: Commit the filesystem boundary**

```bash
git add simpleloop/container/runtime.py tests/test_executor_mount.py tests/test_runtime.py
git commit -m "fix: give executor a private writable home"
```

---

### Task 3: Make Agent own and clean the complete sandbox lifetime

**Necessity:** `home` and `work` must be created before argv construction and
must outlive Claude. A single temporary root prevents mismatched cleanup.

**Files:**

- Modify: `simpleloop/roles/agent.py:1-20,140-245`
- Modify: `tests/test_agent_usage.py`

**Interfaces:**

- Consumes: `runtime.executor_home` and the new `exec_argv()` signature from Task 2.
- Produces: one per-call temporary root containing `work/` and `home/`.

- [ ] **Step 1: Extend the fake runtime and write a failing lifecycle test**

Change the fake signature in `tests/test_agent_usage.py`:

```python
def __init__(self):
    self.calls = []
    self.overrides = None
    self.executor_home = Path("/home/tester")


def exec_argv(self, payload, *, cwd, mounts=None, scaffold=None, home=None):
    self.sandbox = (Path(scaffold), Path(home)) if mounts is not None else None
    self.calls.append((list(payload), Path(cwd)))
    return ["apptainer", "exec", "image.sif", *payload]
```

Add:

```python
def test_executor_agent_owns_private_work_and_home(monkeypatch, tmp_path):
    runtime = RecordingRuntime()
    monkeypatch.setattr(agent_mod.subprocess, "Popen", lambda *a, **k: FinishedProcess())
    agent = Agent(runtime=runtime, mounts=agent_mod.MountMap(rw=("src",)))

    assert agent.run_text("edit", cwd=tmp_path, label="executor") == "ok"

    work, home = runtime.sandbox
    assert work.name == "work"
    assert home.name == "home"
    assert not work.parent.exists()
    assert runtime.overrides["HOME"] == str(runtime.executor_home)
```

- [ ] **Step 2: Run the focused test and verify red**

Run:

```bash
python -m pytest -q tests/test_agent_usage.py -k private_work_and_home
```

Expected: FAIL because only one scaffold directory is currently created and
HOME is not passed.

- [ ] **Step 3: Replace `mkdtemp`/`rmtree` with one temporary-root context**

In `Agent._run()` use:

```python
with tempfile.TemporaryDirectory(prefix="simpleloop-exec-") as root:
    sandbox_root = Path(root)
    scaffold = sandbox_root / "work"
    home = sandbox_root / "home"
    scaffold.mkdir()
    home.mkdir(mode=0o700)
    argv = self.runtime.exec_argv(
        payload, cwd=cwd, mounts=self.mounts,
        scaffold=scaffold, home=home,
    )
    env = self.runtime.subprocess_env({
        "HOME": str(self.runtime.executor_home),
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.max_output_tokens),
        **({"ANTHROPIC_BASE_URL": self.base_url} if self.base_url else {}),
    })
    return self._run_process(argv, prompt, cwd, label, env)
```

Extract only enough of the existing Popen/poll code into `_run_process()` to
keep the context readable. Do not change timeout, JSON decoding, or output
semantics.

- [ ] **Step 4: Ensure live processes are killed in `finally`**

Inside `_run_process()` initialize `proc = None` and add:

```python
finally:
    if proc is not None and proc.poll() is None:
        _kill_group(proc)
```

Add a fake process test where stdin writing raises `OSError`; assert the kill
helper is called before the temporary directory disappears.

- [ ] **Step 5: Run Agent and candidate tests**

Run:

```bash
python -m pytest -q tests/test_agent_usage.py tests/test_candidate_worker.py
```

Expected: PASS.

- [ ] **Step 6: Commit the sandbox lifetime**

```bash
git add simpleloop/roles/agent.py tests/test_agent_usage.py tests/test_candidate_worker.py
git commit -m "fix: own executor sandbox for full agent call"
```

---

### Task 4: Add production-equivalent Executor preflight

**Necessity:** The old preflight passed because it exercised the legacy whole
run-dir world. The run must fail before proposer/model work if the actual
Executor world cannot initialize HOME, Bash, rw/ro paths, or isolation.

**Files:**

- Modify: `simpleloop/container/runtime.py:230-285`
- Modify: `simpleloop/loop.py:230-250,455-525`
- Modify: `simpleloop/candidate_worker.py:120-150`
- Modify: `tests/test_executor_mount.py`
- Modify: `tests/test_parallel_candidates.py`

**Interfaces:**

- Produces: `ApptainerRuntime.executor_preflight(worktree, mounts) -> None`.
- Called once after `Workspace.setup()` and before `_starting_state()`.

- [ ] **Step 1: Write failing preflight argv and ordering tests**

In `tests/test_executor_mount.py`, monkeypatch `subprocess.run` and assert the
captured argv contains private HOME, `/work`, `--no-mount cwd,home,hostfs`, and
only `MountMap.external_ro`.

In `tests/test_parallel_candidates.py`, use the existing fake context fixtures
and record events:

```python
events = []
ctx.workspace.setup = lambda: events.append("workspace.setup")
ctx.runtime.executor_preflight = lambda **kwargs: events.append("executor.preflight")
ctx.execution_backend.eval_baseline = lambda **kwargs: (
    events.append("baseline") or ("", baseline_metrics)
)

assert events[:3] == ["workspace.setup", "executor.preflight", "baseline"]
```

- [ ] **Step 2: Run focused tests and verify red**

Run:

```bash
python -m pytest -q tests/test_executor_mount.py tests/test_parallel_candidates.py -k preflight
```

Expected: FAIL because `executor_preflight()` and its call site do not exist.

- [ ] **Step 3: Implement the exact preflight script**

Add a script that receives the expected HOME plus rw, ro, and forbidden paths:

```python
_EXECUTOR_PREFLIGHT_SCRIPT = r'''
set -eu
[ "$PWD" = /work ]
[ "$HOME" = "$1" ]
for tool in bash git node claude; do command -v "$tool" >/dev/null; done
mkdir -p "$HOME/.claude"
: > "$HOME/.claude/simpleloop-preflight"
rm "$HOME/.claude/simpleloop-preflight"
for path in "$@"; do test -e "$path"; done
'''.strip()
```

Pass `str(self.executor_home)` as script argument `$1`. Build its argv through
`exec_argv()` using a temporary root with `work/` and
`home/`. Supplement the script with generated `test -w` checks for every rw
mount, `test ! -w` for every ro mount, and `test ! -e` for one unlisted sentinel
created in the disposable worktree. Use `subprocess.run(..., timeout=60)` and
raise `RuntimePreflightError` with bounded stdout/stderr on failure.

Do not call a model API in startup preflight.

- [ ] **Step 4: Wire the same MountMap everywhere**

Add one helper in `loop.py`:

```python
def _executor_mount_map(cfg: dict) -> MountMap:
    return MountMap(
        rw=tuple(cfg["editable_paths"]),
        ro=tuple(cfg.get("read_only_paths") or ()),
        external_ro=tuple(cfg.get("executor_read_only_binds") or ()),
    )
```

Use it when constructing the frontend Executor Agent. Mirror the same fields
in `candidate_worker.build_deps()` because HEPJob workers load the resolved
config independently.

After `ctx.workspace.setup()`, create a disposable `executor-preflight`
worktree at `ctx.workspace.baseline_sha()`, call
`ctx.runtime.executor_preflight(worktree=wt, mounts=ctx.executor_agent.mounts)`,
and remove the worktree in `finally`.

- [ ] **Step 5: Run focused startup tests**

Run:

```bash
python -m pytest -q tests/test_executor_mount.py tests/test_parallel_candidates.py tests/test_candidate_worker.py
```

Expected: PASS.

- [ ] **Step 6: Commit fail-fast preflight**

```bash
git add simpleloop/container/runtime.py simpleloop/loop.py simpleloop/candidate_worker.py tests/test_executor_mount.py tests/test_parallel_candidates.py tests/test_candidate_worker.py
git commit -m "fix: preflight the real executor world"
```

---

### Task 5: Remove proposer lane scheduling while preserving lane identity

**Necessity:** One cognitive context can compare all ten hypotheses and avoid
the cross-lane duplicate blind spot. Lane remains the future tree node unit;
only the present fan-out scheduler is removed.

**Files:**

- Modify: `simpleloop/roles/orchestrator.py:1-320,480-565`
- Modify: `simpleloop/execution/local.py:20-65`
- Modify: `simpleloop/execution/hepjob.py:740-825`
- Modify: `tests/test_orchestrator.py`
- Modify: `tests/test_hepjob_backend.py`
- Modify: `tests/test_parallel_candidates.py`

**Interfaces:**

- Preserves: `run_lane_episode(..., select_quota: int)` and lane worker result
  schema.
- Changes: orchestrator `run()` requires exactly one workspace and directly
  returns the one lane result.

- [ ] **Step 1: Replace quota scheduler tests with one-lane contract tests**

Delete `TestLaneQuotas` and multi-lane concurrency assertions. Add:

```python
def test_run_uses_one_lane_and_full_candidate_quota(tmp_path, monkeypatch):
    args = _run_args(tmp_path, candidates_per_round=4)
    args["workspaces"] = [tmp_path / "lane-0"]
    orch = _orchestrator(_gen_reply([_card()]))
    seen = {}

    def fake_one(lane, **kwargs):
        seen["lane_id"] = lane.lane_id
        seen["quota"] = kwargs["select_quota"]
        return LaneResult(
            lane_id=0, assigned_ops=lane.assigned_ops, outcome="submit",
            proposals=(SimpleNamespace(instruction="a"),),
        )

    monkeypatch.setattr(orch, "_run_one_lane", fake_one)
    orch.run(**args)

    assert seen == {"lane_id": 0, "quota": 4}
```

Add a test that one Generator invocation returns ten cards to the same
`research_batch`, and a test that submitting fewer than four remains valid.

- [ ] **Step 2: Run orchestrator tests and verify red**

Run:

```bash
python -m pytest -q tests/test_orchestrator.py
```

Expected: FAIL because current code derives two lanes and calls the proposer
ThreadPool.

- [ ] **Step 3: Remove only the scheduler**

Delete `_SELECT_PER_LANE`, `_MAX_LANE_WORKERS`, `lane_quotas()`, and
`_run_lanes()`. Keep `_HYPOTHESES_PER_LANE == 10` and
`_sample_generative_ops()`.

Replace the scheduling body in `run()` with:

```python
if len(workspaces) != 1:
    raise ValueError(f"single-lane proposer requires 1 workspace, got {len(workspaces)}")
lane = LaneState(lane_id=0, assigned_ops=_sample_generative_ops())
mode = _Mode(n_lanes=1, max_branch_steps=cognitive_steps, label="single-lane")
lane_result = self._run_one_lane(
    lane,
    select_quota=candidates_per_round,
    source_path=workspaces[0],
    gen_context=gen_context,
    goal=goal, editable=editable, frozen=frozen,
    memory_service=memory_service, base_sha=base_sha,
    repo_path=repo_path, run_dir=run_dir,
    current_round=current_round, gate_block=gate_block,
    prompt_dir=prompt_dir, hints=hints, explore=explore,
    mode=mode, gen_steps=gen_steps, cognitive_steps=cognitive_steps,
)
lane_results = [lane_result]
```

Keep existing result collection, abstention, trace, and telemetry behavior.
Change the cognitive prompt wording from a fixed expectation to “select up to
`select_quota`, preferring materially different mechanisms and code regions;
do not regenerate merely to fill the quota.” Do not add a uniqueness gate.

- [ ] **Step 4: Simplify LocalBackend to one workspace**

Replace lane-count derivation with:

```python
workspace = ctx.workspace.add_lane_workspace(0, base_sha)
try:
    return ctx.proposer_agent.run(
        # existing arguments unchanged
        workspaces=[workspace],
        candidates_per_round=cfg.get("candidates_per_round", 1),
    )
finally:
    ctx.workspace.remove_lane_workspace(0)
```

Update the log to `proposer round N: lane-0 workspace ...`.

- [ ] **Step 5: Simplify HEPJobBackend to one lane job**

Replace the quota loop with exactly one job:

```python
job = self._prepare_lane(
    0, round_id, base_sha,
    select_quota=self.ctx.cfg.get("candidates_per_round", 1),
    assigned_ops=list(_sample_generative_ops()),
)
self._submit_lane(job)
jobs = [job]
```

Keep `ProposerLaneSpec`, manifest fields, supervision, result collection, and
orphan marker behavior unchanged.

- [ ] **Step 6: Run local and HEPJob scheduler tests**

Run:

```bash
python -m pytest -q tests/test_orchestrator.py tests/test_hepjob_backend.py tests/test_parallel_candidates.py tests/test_proposer_lane_worker.py tests/test_lane_workspace.py
```

Expected: PASS with assertions that local and HEPJob each create one proposer
lane while four candidate Executors may still run concurrently.

- [ ] **Step 7: Commit single-lane scheduling**

```bash
git add simpleloop/roles/orchestrator.py simpleloop/execution/local.py simpleloop/execution/hepjob.py tests/test_orchestrator.py tests/test_hepjob_backend.py tests/test_parallel_candidates.py
git commit -m "refactor: run one proposer lane per round"
```

---

### Task 6: Reap process groups on exceptions and SIGTERM

**Necessity:** Stopping the observed run left four detached Apptainer groups.
Cleanup is part of a trustworthy Executor lifecycle, but resume behavior remains
out of scope.

**Files:**

- Create: `simpleloop/processes.py`
- Modify: `simpleloop/roles/agent.py`
- Modify: `simpleloop/roles/research_tools.py`
- Modify: `simpleloop/loop.py:180-250`
- Create: `tests/test_processes.py`
- Modify: `tests/test_agent_usage.py`
- Modify: `tests/test_research_tools.py`

**Interfaces:**

- Produces: module singleton `CHILD_PROCESSES` with `register(pid)`,
  `unregister(pid)`, and `terminate_all()`.
- Produces: `run_signal_handlers()` context manager for the main thread.

- [ ] **Step 1: Write failing registry tests**

Create `tests/test_processes.py`:

```python
import signal

from simpleloop import processes


def test_registry_terminates_each_registered_process_group(monkeypatch):
    sent = []
    registry = processes.ChildProcessRegistry()
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    registry.register(11)
    registry.register(22)

    registry.terminate_all()

    assert set(sent) == {(11, signal.SIGTERM), (22, signal.SIGTERM)}


def test_unregister_prevents_termination(monkeypatch):
    sent = []
    registry = processes.ChildProcessRegistry()
    monkeypatch.setattr(processes.os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    registry.register(11)
    registry.unregister(11)
    registry.terminate_all()
    assert sent == []
```

Add a handler test that invokes the installed SIGTERM callback, asserts all
groups receive TERM, and asserts `KeyboardInterrupt` is raised so the run does
not append a completed round.

- [ ] **Step 2: Run and verify red**

Run:

```bash
python -m pytest -q tests/test_processes.py
```

Expected: FAIL because `simpleloop.processes` does not exist.

- [ ] **Step 3: Implement the minimal thread-safe registry**

```python
class ChildProcessRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._pgids = set()

    def register(self, pgid: int) -> None:
        with self._lock:
            self._pgids.add(pgid)

    def unregister(self, pgid: int) -> None:
        with self._lock:
            self._pgids.discard(pgid)

    def terminate_all(self) -> None:
        with self._lock:
            pgids = tuple(self._pgids)
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                self.unregister(pgid)


CHILD_PROCESSES = ChildProcessRegistry()
```

Implement `run_signal_handlers()` as a context manager that installs handlers
only on the main thread, restores the prior handlers in `finally`, calls
`CHILD_PROCESSES.terminate_all()`, then raises `KeyboardInterrupt`.

- [ ] **Step 4: Register both Claude and research subprocesses**

Immediately after each `Popen(..., start_new_session=True)`:

```python
CHILD_PROCESSES.register(proc.pid)
try:
    # existing wait/poll logic
finally:
    CHILD_PROCESSES.unregister(proc.pid)
```

Keep each local TERM-then-KILL timeout helper. The registry handles frontend
termination; the owner handles escalation and normal cleanup.

- [ ] **Step 5: Scope signal handling around the locked run**

In `loop.run()`, wrap `_run_locked(...)` with `run_signal_handlers()` inside the
existing lock `try/finally`, so lock release still executes after cancellation.
Do not convert the interruption to a candidate failure or append history.

- [ ] **Step 6: Run process tests**

Run:

```bash
python -m pytest -q tests/test_processes.py tests/test_agent_usage.py tests/test_research_tools.py tests/test_parallel_candidates.py
```

Expected: PASS.

- [ ] **Step 7: Commit cancellation cleanup**

```bash
git add simpleloop/processes.py simpleloop/roles/agent.py simpleloop/roles/research_tools.py simpleloop/loop.py tests/test_processes.py tests/test_agent_usage.py tests/test_research_tools.py tests/test_parallel_candidates.py
git commit -m "fix: reap agent process groups on run stop"
```

---

### Task 7: Update task configs and prove the end-to-end contract

**Necessity:** Code-level argv tests do not prove the real SIF, Claude Bash
tool, workspace edit, and Harness commit work together. One disposable smoke
run closes the original failure loop.

**Files:**

- Modify: `examples/omilrec-v100-opt/task.yaml`
- Modify: `examples/omilrec-v100-opt/task_hints_local.yaml`
- Modify: other runnable example task configs that need external Executor dependencies
- Modify: `tests/test_example_apptainer.py`
- Modify: `tests/test_prompt_templates.py`
- Create: `scripts/executor_smoke.py`
- Test: focused and full pytest suites

**Interfaces:**

- Consumes: all previous tasks.
- Produces: a repeatable disposable real-Executor acceptance command.

- [ ] **Step 1: Update OMILREC role-specific binds**

Keep evaluator `runtime.binds` unchanged because its scripts need the broad
asset paths. Add only:

```yaml
runtime:
  executor_read_only_binds:
    - /cvmfs
    - /data/juno
```

Never add `/datafs/users/wujxy/agent-sci/omilrec_opt` to this field. Apply the
same rule to other examples only when their Executor build genuinely needs an
external directory.

- [ ] **Step 2: Add static example assertions**

In `tests/test_example_apptainer.py`, load the OMILREC configs and assert:

```python
assert cfg["executor_read_only_binds"] == ["/cvmfs", "/data/juno"]
assert not any("omilrec_opt" in p for p in cfg["executor_read_only_binds"])
```

Resolve expected paths using the config loader instead of comparing unresolved
YAML text.

- [ ] **Step 3: Add a real Apptainer regression test**

Mark the test to skip when the configured SIF or Apptainer is unavailable. It
must construct the same Executor argv and run:

```bash
set -eu
test "$PWD" = /work
mkdir -p "$HOME/.claude"
printf ok > "$HOME/.claude/write-check"
printf changed > /work/src/smoke.txt
test ! -e /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated
```

Use only a temporary worktree fixture. Assert the host source and evaluator
trees have no Git changes afterward.

- [ ] **Step 4: Implement the disposable Claude smoke driver**

`scripts/executor_smoke.py` must:

1. load a resolved task config;
2. create a temporary run directory and a tiny temporary Git repository;
3. build the production `MountMap` and `Agent`;
4. issue one proposal instructing Claude to run `pwd` and `true`, change one
   tracked text file, and emit the required self-report;
5. call the existing Executor/Harness commit path;
6. assert a commit SHA exists and exactly the expected file changed;
7. assert the temporary repo is the only host path modified;
8. print `EXECUTOR_SMOKE=PASS` and the commit SHA.

Do not embed tokens, base URLs, or task-specific evaluator logic in the script.
Read credentials from the existing environment and executor model/base URL from
the supplied config.

- [ ] **Step 5: Run all automated tests**

Run:

```bash
python -m pytest -q tests/test_config_execution.py tests/test_runtime.py tests/test_executor_mount.py tests/test_agent_usage.py tests/test_candidate_worker.py tests/test_orchestrator.py tests/test_cognitive_element.py tests/test_hepjob_backend.py tests/test_parallel_candidates.py tests/test_proposer_lane_worker.py tests/test_lane_workspace.py tests/test_processes.py tests/test_research_tools.py tests/test_example_apptainer.py tests/test_prompt_templates.py
```

Expected: all selected tests PASS with zero failures.

- [ ] **Step 6: Run the real Executor smoke once**

Run from the repository root with the existing authenticated environment:

```bash
python scripts/executor_smoke.py --config examples/omilrec-v100-opt/task_hints_local.yaml
```

Expected output includes:

```text
EXECUTOR_PREFLIGHT=PASS
EXECUTOR_SMOKE=PASS
```

After it exits, run:

```bash
pgrep -af '[c]laude|[a]pptainer.*simpleloop-exec-'
```

Expected: exit code 1 and no output.

- [ ] **Step 7: Verify repository scope and commit**

Run:

```bash
git diff --check
git status --short
```

Inspect every changed path; do not include pre-existing user changes. Then:

```bash
git add examples/omilrec-v100-opt/task.yaml examples/omilrec-v100-opt/task_hints_local.yaml tests/test_example_apptainer.py tests/test_prompt_templates.py scripts/executor_smoke.py
git commit -m "test: prove executor sandbox delivery"
```

---

## Final Verification Gate

- [ ] Run the complete project test suite:

```bash
python -m pytest -q
```

Expected: zero failed tests.

- [ ] Run the real smoke a second time after the full suite to prove no test
cleanup or ordering changed the environment:

```bash
python scripts/executor_smoke.py --config examples/omilrec-v100-opt/task_hints_local.yaml
```

Expected: `EXECUTOR_PREFLIGHT=PASS` and `EXECUTOR_SMOKE=PASS`.

- [ ] Verify the original failure is impossible under the accepted argv:

```bash
rg -n -- '--no-mount|EXECUTOR_HOME|executor_read_only_binds' simpleloop tests examples/omilrec-v100-opt
```

Expected: host home remains disabled, a private Executor home is mounted, and
the broad evaluator path appears only in evaluator `runtime.binds`.

- [ ] Verify single-lane semantics from a focused dry run or test log:

```text
proposer round 1: lane-0 workspace
orchestrator: lanes=1, hypotheses=10, select_up_to=4
```

- [ ] Check the final diff and commits:

```bash
git diff --check HEAD~7..HEAD
git log --oneline -7
```

Expected: only the seven scoped, independently tested changes above; no `.env`,
credential, production asset, evaluator, or unrelated user-file modifications.
