# SimpleLoop Phase 3 World Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace SimpleLoop's concrete Workspace/Apptainer dependencies with one production SourceWorkspace, ExecutionSandbox, and World layer while preserving candidate, baseline, HEPJob, history, resume, and RSI behavior.

**Architecture:** `GitWorkspaceProvider` owns all Host Git state; `ApptainerSandbox` owns all SimpleLoop container/process mechanics; `WorldBuilder` binds a `SourceWorkspace` to Executor or Evaluator policy. Candidate business code consumes typed workspaces and Worlds, while Local/HEPJob retain only location-specific lifecycle until Phase 4.

**Tech Stack:** Python 3.9+, frozen dataclasses, `typing.Protocol`, Git worktrees, Apptainer, subprocess process groups, pytest.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-14-simpleloop-phase3-world-layer-design.md`.
- Use TDD: write and observe a focused failure before each production change.
- Keep the standalone `proposer` package independent; do not import `simpleloop.world` from it.
- Preserve Local/HEPJob candidate results, history, resume, `_FINISHED`, telemetry, baseline, selection, and RSI semantics.
- Do not redesign Scheduler, worker envelope, `RunContext`, Composition Root, RSI, tree search, or YAML.
- Do not add a second sandbox provider.
- Keep network enabled for Executor and Evaluator Worlds.
- Executor gets model credentials; Evaluator does not.
- Delete `simpleloop/harness/workspace.py` and `simpleloop/container/runtime.py` only after all SimpleLoop consumers migrate.
- Make one focused commit per task.

---

### Task 1: Add mechanism-neutral World contracts

**Files:**
- Create: `simpleloop/world/__init__.py`
- Create: `simpleloop/world/contracts.py`
- Test: `tests/test_world_contracts.py`

**Interfaces:**
- Consumes: `CandidateArtifact` and the Phase 2 candidate identity fields.
- Produces: `WorkspaceSpec`, `SourceWorkspace`, `ChangeSet`, `CommitRequest`, `WorkspaceProvider`, `MountMode`, `MountSpec`, `SandboxSpec`, `ProcessRequest`, `ProcessResult`, `ExecutionSandbox`, and `SandboxProvider`.

- [ ] **Step 1: Write failing frozen-contract tests**

```python
def test_world_values_are_frozen(tmp_path):
    workspace = SourceWorkspace("r0-c0", tmp_path, "base")
    with pytest.raises(FrozenInstanceError):
        workspace.base_sha = "other"


def test_mount_defaults_read_only(tmp_path):
    mount = MountSpec(tmp_path, PurePosixPath("/data"))
    assert mount.mode is MountMode.READ_ONLY


def test_process_result_records_timeout_without_exception():
    result = ProcessResult(("sleep", "2"), -9, "", "", 1.0, True)
    assert result.timed_out is True
```

- [ ] **Step 2: Run the contract tests and observe the missing module failure**

Run: `python -m pytest -q tests/test_world_contracts.py`

Expected: collection fails because `simpleloop.world` does not exist.

- [ ] **Step 3: Implement only the frozen values and protocols from the spec**

```python
@dataclass(frozen=True)
class WorkspaceSpec:
    workspace_id: str
    revision: str


@dataclass(frozen=True)
class SourceWorkspace:
    workspace_id: str
    path: Path
    base_sha: str


@dataclass(frozen=True)
class ChangeSet:
    paths: tuple[PurePosixPath, ...]
```

Implement the remaining signatures exactly as section 4 of the design, with `CommitRequest` carrying no worktree path.

- [ ] **Step 4: Run focused and existing candidate contract tests**

Run: `python -m pytest -q tests/test_world_contracts.py tests/test_candidate_contracts.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/world tests/test_world_contracts.py
git commit -m "refactor: define world layer contracts"
```

---

### Task 2: Move Git ownership into GitWorkspaceProvider

**Files:**
- Create: `simpleloop/world/git.py`
- Modify: `tests/test_lane_workspace.py`
- Create: `tests/test_git_workspace_provider.py`
- Retain temporarily: `simpleloop/harness/workspace.py`

**Interfaces:**
- Consumes: Task 1 workspace contracts.
- Produces: `GitWorkspaceProvider.initialize/create/remove/open/inspect/commit/diff`, plus temporary lane layout methods used only until backend migration.

- [ ] **Step 1: Write failing provider lifecycle tests**

```python
def test_open_creates_and_always_removes_workspace(tmp_path):
    provider, base = make_provider(tmp_path)
    with pytest.raises(RuntimeError, match="stop"):
        with provider.open(WorkspaceSpec("r0-c0", base)) as workspace:
            assert workspace.path.exists()
            raise RuntimeError("stop")
    assert not workspace.path.exists()


def test_commit_returns_typed_artifact_and_keeps_sha_reachable(tmp_path):
    provider, base = make_provider(tmp_path)
    with provider.open(WorkspaceSpec("r0-c0", base)) as workspace:
        (workspace.path / "src/a.py").write_text("changed\n")
        changes = provider.inspect(workspace)
        artifact = provider.commit(workspace, CommitRequest(0, 0, base, changes.paths))
    assert provider.diff(base, artifact.sha)
    assert artifact.parent_sha == base
```

- [ ] **Step 2: Run provider tests and observe import failure**

Run: `python -m pytest -q tests/test_git_workspace_provider.py`

Expected: fails because `GitWorkspaceProvider` is absent.

- [ ] **Step 3: Move the real Git implementation without adding a wrapper layer**

Implement the clone fallback, baseline resolution, stale cleanup, worktree creation, status parsing, commit, ref reachability, diff, and cleanup in `world/git.py`. `open()` must use `try/finally`:

```python
@contextmanager
def open(self, spec: WorkspaceSpec):
    workspace = self.create(spec)
    try:
        yield workspace
    finally:
        self.remove(workspace)
```

Keep current lane filesystem behavior through provider-owned temporary `create_lane/remove_lane` methods. During migration, `harness/workspace.py` may expose the old method names only as a delegating compatibility class; it contains no Git command or lifecycle implementation.

- [ ] **Step 4: Run Git/lane/candidate artifact tests**

Run: `python -m pytest -q tests/test_git_workspace_provider.py tests/test_lane_workspace.py tests/test_candidate_stages.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/world/git.py tests/test_git_workspace_provider.py tests/test_lane_workspace.py
git commit -m "refactor: add git workspace provider"
```

---

### Task 3: Extract ApptainerSandbox process ownership

**Files:**
- Create: `simpleloop/world/apptainer.py`
- Create: `tests/test_apptainer_sandbox.py`
- Modify: `tests/test_executor_mount.py`
- Retain temporarily: `simpleloop/container/runtime.py`

**Interfaces:**
- Consumes: Task 1 sandbox contracts.
- Produces: `ApptainerSandbox.preflight/bind/summary_lines` and a bound `ExecutionSandbox.run(ProcessRequest) -> ProcessResult`.

- [ ] **Step 1: Write failing argv, environment, and process tests**

```python
def test_bound_sandbox_builds_shell_free_contained_argv(tmp_path):
    provider = ApptainerSandbox(executable="apptainer")
    sandbox = provider.bind(
        SandboxSpec(tmp_path / "runtime.sif", {"TOKEN": "value"}, True),
        (MountSpec(tmp_path / "repo", PurePosixPath("/work")),),
    )
    argv = sandbox.argv(ProcessRequest(("python", "-V"), PurePosixPath("/work"), 10))
    assert argv[-2:] == ["python", "-V"]
    assert "--containall" in argv
    assert "shell=True" not in argv


def test_run_returns_timeout_result_and_kills_process_group(monkeypatch, tmp_path):
    # Fake Popen/clock so timeout is deterministic.
    result = bound_sandbox(tmp_path, monkeypatch).run(
        ProcessRequest(("sleep", "9"), PurePosixPath("/work"), 1, label="probe")
    )
    assert result.timed_out is True
    assert result.exit_code < 0
```

Also pin model credential inclusion for Executor specs, exclusion for Evaluator specs, proxy forwarding, network on/off flags, and secret-free summaries.

- [ ] **Step 2: Run tests and observe missing adapter failure**

Run: `python -m pytest -q tests/test_apptainer_sandbox.py`

Expected: fails because `simpleloop.world.apptainer` is absent.

- [ ] **Step 3: Move current runtime mechanics into the concrete adapter**

Use the current `--cleanenv`, `--no-eval`, userns, containall, mount, preflight, timeout, and process-group behavior. Convert typed mounts to `source:target:mode` only inside this file. Launch with `shell=False`, `start_new_session=True`, and return `ProcessResult`; only executable/image/process launch failures raise `SandboxLaunchError`.

During migration, `container/runtime.py` may re-export a compatibility class implemented in `world/apptainer.py`; it must not retain a second argv, environment, preflight, or subprocess implementation. Task 7 deletes both the facade and legacy methods after consumers migrate.

- [ ] **Step 4: Run sandbox and historical runtime characterization tests**

Run: `python -m pytest -q tests/test_apptainer_sandbox.py tests/test_executor_mount.py tests/test_runtime.py -k 'exec_argv or preflight or subprocess_env or summary'`

Expected: all selected tests pass after migrating assertions to the new adapter API.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/world/apptainer.py tests/test_apptainer_sandbox.py tests/test_executor_mount.py tests/test_runtime.py
git commit -m "refactor: extract apptainer sandbox"
```

---

### Task 4: Build validated Executor and Evaluator Worlds

**Files:**
- Create: `simpleloop/world/builder.py`
- Create: `tests/test_world_builder.py`
- Modify: `simpleloop/world/__init__.py`

**Interfaces:**
- Consumes: `SandboxProvider`, `SourceWorkspace`, `SandboxSpec`, `WorldSpec`, and typed mounts.
- Produces: `WorldBuilder.build(...) -> World`, `executor_world_spec(...)`, and `evaluator_world_spec(...)`.

- [ ] **Step 1: Write failing WorldBuilder safety tests**

```python
def test_executor_world_mounts_base_ro_and_editable_overlay_rw(tmp_path):
    provider = CapturingSandboxProvider()
    workspace = SourceWorkspace("c0", tmp_path / "repo", "base")
    (workspace.path / "src").mkdir(parents=True)
    world = WorldBuilder(provider).build(
        workspace,
        SandboxSpec(tmp_path / "runtime.sif"),
        WorldSpec(MountMode.READ_ONLY, (PurePosixPath("src"),)),
    )
    assert provider.mounts[0].target == PurePosixPath("/work")
    assert provider.mounts[0].mode is MountMode.READ_ONLY
    assert provider.mounts[1].target == PurePosixPath("/work/src")
    assert provider.mounts[1].mode is MountMode.READ_WRITE
    assert world.workspace is workspace


def test_builder_rejects_writable_symlink_escape(tmp_path):
    # repo/link points outside repo
    with pytest.raises(WorldError, match="escapes workspace"):
        build_executor_world(tmp_path, writable=(PurePosixPath("link"),))
```

Also test `..`, duplicate targets, evaluator workspace RW, external mounts, and missing sources.

- [ ] **Step 2: Run tests and observe missing builder failure**

Run: `python -m pytest -q tests/test_world_builder.py`

Expected: fails because `WorldBuilder` is absent.

- [ ] **Step 3: Implement validation and mount resolution**

`WorldBuilder` must resolve the workspace base to `/work`, validate every relative writable path, create missing declared build directories beneath the workspace, reject symlink escapes, append external mounts, reject duplicate targets, and call `provider.bind()` once. Policy helpers accept resolved values, not a config dict.

- [ ] **Step 4: Run builder and sandbox tests**

Run: `python -m pytest -q tests/test_world_builder.py tests/test_apptainer_sandbox.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/world tests/test_world_builder.py
git commit -m "refactor: add explicit world builder"
```

---

### Task 5: Migrate candidate stages to SourceWorkspace and World

**Files:**
- Modify: `simpleloop/candidate.py`
- Modify: `simpleloop/stages/artifacts.py`
- Modify: `simpleloop/stages/executor.py`
- Modify: `simpleloop/stages/evaluator.py`
- Modify: `simpleloop/roles/agent.py`
- Modify: `simpleloop/harness/evals.py`
- Modify: `tests/test_candidate_contracts.py`
- Modify: `tests/test_candidate_stages.py`
- Modify: `tests/test_agent_usage.py`
- Modify: `tests/test_prompt_templates.py`
- Modify: `tests/test_gate_pipeline.py`

**Interfaces:**
- Consumes: Task 1–4 contracts and concrete Worlds supplied at composition.
- Produces: Candidate/Execution/Evaluation requests using `SourceWorkspace`; Agent and HarnessEvaluator run only through `World.run()`.

- [ ] **Step 1: Write failing stage boundary tests**

```python
def test_candidate_request_carries_source_workspace(tmp_path):
    workspace = SourceWorkspace("c0", tmp_path, "base")
    request = CandidateRequest(0, 0, "base", Proposal("change"), workspace)
    assert request.workspace is workspace


def test_agent_runs_claude_through_world(tmp_path):
    world = FakeWorld(ProcessResult(("claude",), 0, '{"result":"ok"}', "", 0.1))
    result = Agent(world=world).run_text("prompt", cwd=tmp_path, label="executor")
    assert result == "ok"
    assert world.requests[0].cwd == PurePosixPath("/work")


def test_evaluator_runs_commands_through_world():
    world = FakeWorld(ProcessResult(("bash",), 0, "SPEED=1\nOK=PASS", "", 0.1))
    result = HarnessEvaluator(world, config).evaluate(EvaluationRequest(workspace))
    assert result.metrics == {"SPEED": 1.0, "OK": True}
```

- [ ] **Step 2: Run focused tests and observe old Path/Runtime API failures**

Run: `python -m pytest -q tests/test_candidate_contracts.py tests/test_candidate_stages.py tests/test_agent_usage.py tests/test_prompt_templates.py`

Expected: new tests fail because requests and adapters still use `Path`/`ApptainerRuntime`.

- [ ] **Step 3: Migrate the typed pipeline and adapters minimally**

Remove `worktree` from the candidate `CommitRequest`; make `ArtifactWorkspace.inspect/commit` accept `SourceWorkspace`; make `ExecutionRequest` and `EvaluationRequest` carry it. Agent builds only Claude argv and decodes output; HarnessEvaluator loops commands through its World and uses pure metric parsing. Preserve all Phase 2 statuses, trace projections, output caps, and baseline validation.

- [ ] **Step 4: Run all candidate, agent, eval, gate, and prompt tests**

Run: `python -m pytest -q tests/test_candidate_contracts.py tests/test_candidate_stages.py tests/test_candidate_worker.py tests/test_agent_usage.py tests/test_prompt_templates.py tests/test_gate_pipeline.py tests/test_runtime.py -k 'candidate or eval or metric or baseline'`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/candidate.py simpleloop/stages simpleloop/roles/agent.py simpleloop/harness/evals.py tests
git commit -m "refactor: run candidate stages through worlds"
```

---

### Task 6: Migrate Local, worker, baseline, and HEPJob composition

**Files:**
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/candidate_worker.py`
- Modify: `simpleloop/execution/local.py`
- Modify: `simpleloop/execution/hepjob.py`
- Modify: `simpleloop/initialize.py`
- Modify: `simpleloop/execution/base.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `tests/test_candidate_worker.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_hepjob_backend.py`
- Modify: `tests/test_initialize.py`
- Modify: `tests/test_static_mode.py`
- Modify: `tests/test_plot.py`
- Modify: `tests/test_telemetry.py`

**Interfaces:**
- Consumes: `GitWorkspaceProvider`, `ApptainerSandbox`, `WorldBuilder`, World policy helpers, and migrated candidate ports.
- Produces: production Local and HEPJob paths using typed SourceWorkspace/World without changing scheduler protocols.

- [ ] **Step 1: Write failing production composition tests**

```python
def test_local_backend_opens_typed_workspace_and_cleans_it():
    result = backend.run_candidates(batch)
    assert provider.opened == [WorkspaceSpec("0-c0", "parent")]
    assert provider.closed == ["0-c0"]
    assert result[0].parent_sha == "parent"


def test_worker_reconstructs_source_workspace_from_existing_manifest(tmp_path):
    ports = build_ports(config, tmp_path)
    assert ports.workspace.workspace_id == "0-c0"
    assert ports.executor.world.workspace is ports.workspace
    assert ports.evaluator.world.workspace is ports.workspace


def test_hepjob_keeps_manifest_and_retry_shape_unchanged():
    job = backend._prepare(plan, round_id=3)
    manifest = json.loads((job.result_dir / "manifest.json").read_text())
    assert manifest["parent_sha"] == plan.parent_sha
    assert manifest["worktree_path"]
```

- [ ] **Step 2: Run backend/worker tests and observe concrete API failures**

Run: `python -m pytest -q tests/test_parallel_candidates.py tests/test_candidate_worker.py tests/test_hepjob_backend.py tests/test_runtime.py tests/test_initialize.py`

Expected: new tests fail because composition still constructs legacy Runtime/Workspace values.

- [ ] **Step 3: Migrate composition without touching scheduling behavior**

Loop/worker/initialize instantiate the concrete provider, sandbox provider, and builder. Local uses provider `open()`; HEPJob uses `create/remove` and reconstructs `SourceWorkspace` from the existing manifest path in workers. Baseline gets an Evaluator World. Preserve Condor commands, job records, retries, journal payload, proposer subprocesses, result codecs, and RSI paths exactly.

- [ ] **Step 4: Run all runtime/backend/worker/resume/RSI tests**

Run: `python -m pytest -q tests/test_parallel_candidates.py tests/test_candidate_worker.py tests/test_hepjob_backend.py tests/test_runtime.py tests/test_initialize.py tests/test_round_contract.py tests/test_rsi_host.py tests/test_self_repo.py tests/test_self_review.py tests/test_static_mode.py tests/test_plot.py tests/test_telemetry.py`

Expected: all pass with only established environment skips.

- [ ] **Step 5: Commit**

```bash
git add simpleloop tests
git commit -m "refactor: compose candidates from explicit worlds"
```

---

### Task 7: Delete old owners and enforce World boundaries

**Files:**
- Delete: `simpleloop/harness/workspace.py`
- Delete: `simpleloop/container/runtime.py`
- Modify: `simpleloop/container/__init__.py`
- Modify: `simpleloop/harness/__init__.py`
- Modify: `README.md`
- Modify: `tests/test_architecture_boundaries.py`
- Modify: `docs/superpowers/plans/2026-08-14-simpleloop-phase3-world-layer.md`

**Interfaces:**
- Consumes: completed production migration.
- Produces: one Git owner, one SimpleLoop Apptainer owner, executable dependency guards, and the Phase 3 completion record.

- [ ] **Step 1: Add failing architecture guards before deletion**

```python
def test_business_modules_do_not_import_apptainer_or_concrete_git():
    for relative in BUSINESS_WORLD_CONSUMERS:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "Apptainer" not in source
        assert "container.runtime" not in source
        assert "world.git" not in source


def test_old_world_owners_are_removed():
    assert not (ROOT / "simpleloop/container/runtime.py").exists()
    assert not (ROOT / "simpleloop/harness/workspace.py").exists()


def test_proposer_remains_independent_of_simpleloop_world():
    for path in (ROOT / "proposer").rglob("*.py"):
        assert "simpleloop.world" not in path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run architecture tests and observe old owner failures**

Run: `python -m pytest -q tests/test_architecture_boundaries.py`

Expected: fails while old files/imports remain.

- [ ] **Step 3: Remove compatibility imports, delete old files, and update current docs**

Delete old owner files only after `rg` proves there are no SimpleLoop consumers. Update README's trust boundary to list `world/git.py`, `world/apptainer.py`, and `world/builder.py`. Historical dated design documents remain historical records.

- [ ] **Step 4: Run complete verification**

```bash
python -m pytest -q
python -m compileall -q simpleloop proposer tests
rg -n "container\.runtime|harness\.workspace|MountMap|world_mount_map|exec_argv|subprocess_env" simpleloop
rg -n "simpleloop\.world" proposer
git diff --check
```

Expected: pytest passes with the established environment skip only; compileall exits 0; both `rg` commands produce no output except allowed imports inside `world/apptainer.py` when the query is narrowed for architecture checking; diff check exits 0.

- [ ] **Step 5: Mark the plan implemented and commit**

Change the header status to `Implemented`, mark every checkbox `[x]`, then:

```bash
git add -A
git diff --cached --check
git commit -m "test: enforce phase three world boundaries"
```

Expected: the dedicated refactor worktree is clean and all Phase 3 completion criteria are executable.
