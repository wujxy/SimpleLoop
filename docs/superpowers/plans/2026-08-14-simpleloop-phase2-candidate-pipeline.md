# SimpleLoop Phase 2 Candidate Pipeline Implementation Plan

**Status:** Planned on `refactor/phase0-phase1-typed-contracts`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the candidate business logic embedded in `candidate_worker.py` and `loop.py` with one typed `run_candidate()` pipeline used by Local and HEPJob execution.

**Architecture:** `CandidateBatchRequest` carries independently parented `CandidatePlan` values into an execution backend. Each backend opens a worktree and constructs a `CandidateRequest`; Local and the standalone worker then call the same guarded pipeline through narrow Executor, ArtifactWorkspace, Evaluator, Gate, and Trace ports. Existing Apptainer, Git, Condor, worker persistence, history, selection, telemetry, and RSI semantics remain unchanged.

**Tech Stack:** Python 3.9+, frozen dataclasses, `typing.Protocol`, Git worktrees, Apptainer, HTCondor adapters, pytest.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-14-simpleloop-phase2-candidate-pipeline-design.md`.
- Use TDD: every production behavior starts with a focused failing test.
- Keep Phase 0 candidate worker, history, inflight, proposer lane, and RSI fixtures unchanged.
- Do not implement World, Sandbox, Scheduler, worker envelope, LoopSpec, Composition Root, tree search, or RSI restructuring.
- Candidate Pipeline must not receive or read `cfg` or `RunContext`.
- Candidate worktree creation and cleanup remain backend responsibilities.
- HEPJob retry, poll, resume, and collect remain structurally unchanged.
- Preserve all candidate statuses, gate details, `eligible`, handoff files, usage sidecar, and `_FINISHED` semantics.
- Delete migrated implementations; do not retain parallel Executor, Evaluator, Gate, or candidate pipeline code.
- Do not edit `.env`, credentials, production configuration, or ignored runtime assets.
- Make one focused commit per task.

---

### Task 1: Add candidate plans and typed stage ports

**Files:**

- Modify: `simpleloop/candidate.py`
- Create: `simpleloop/stages/executor.py`
- Create: `simpleloop/stages/evaluator.py`
- Create: `simpleloop/stages/gate.py`
- Modify: `simpleloop/stages/__init__.py`
- Create: `tests/test_candidate_contracts.py`

**Interfaces:**

- Consumes: existing `Proposal`, `CandidateResult`, `ExecutionResult`, `CandidateArtifact`, `EvaluationResult`, and `GateDecision`.
- Produces: `CandidatePlan`, `CandidateBatchRequest`, `CandidateRequest`, `Executor`, `ArtifactWorkspace`, `Evaluator`, `CandidateTrace`, request/config dataclasses, `GateSpec`, and `apply_gates()`.

- [x] **Step 1: Write failing frozen-contract and gate tests**

```python
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from simpleloop.candidate import (
    CandidateBatchRequest,
    CandidatePlan,
    CandidateRequest,
    EvaluationResult,
)
from simpleloop.stages.gate import GateSpec, apply_gates
from simpleloop.stages.proposer import Proposal


def test_candidate_batch_allows_independent_parents(tmp_path: Path):
    plans = (
        CandidatePlan(0, "parent-a", Proposal("a")),
        CandidatePlan(1, "parent-b", Proposal("b")),
    )
    batch = CandidateBatchRequest(4, plans)
    request = CandidateRequest(4, 1, "parent-b", plans[1].proposal, tmp_path)
    assert batch.candidates[1].parent_sha == request.parent_sha
    with pytest.raises(FrozenInstanceError):
        plans[0].parent_sha = "mutated"


def test_apply_gates_accepts_only_finite_objective():
    spec = GateSpec("SPEED_MS", ("CORRECTNESS",))
    good = apply_gates(
        EvaluationResult("ok", {"SPEED_MS": 90.0, "CORRECTNESS": True}, (0,)),
        spec,
    )
    bad = apply_gates(
        EvaluationResult("nan", {"SPEED_MS": float("nan"), "CORRECTNESS": True}, (0,)),
        spec,
    )
    assert good.passed is True and good.eligible is True
    assert bad.passed is True and bad.eligible is False


def test_apply_gates_preserves_skip_details():
    decision = apply_gates(
        None,
        GateSpec("SPEED_MS", ("CORRECTNESS",)),
        skip_reason="not run because Executor produced no change",
    )
    assert decision.results["PATHS"].passed is True
    assert decision.results["EVAL_COMMANDS"].passed is None
    assert decision.results["EVAL_COMMANDS"].detail == (
        "not run because Executor produced no change"
    )
```

- [x] **Step 2: Run the tests and verify the new API is missing**

Run:

```bash
python -m pytest -q tests/test_candidate_contracts.py
```

Expected: collection fails because `CandidateBatchRequest` and `simpleloop.stages.gate` do not exist.

- [x] **Step 3: Add the minimal frozen contracts and pure gate**

Add to `simpleloop/candidate.py`:

```python
from pathlib import Path, PurePosixPath
from typing import Protocol


@dataclass(frozen=True)
class CandidatePlan:
    candidate_id: int
    parent_sha: str
    proposal: Proposal


@dataclass(frozen=True)
class CandidateBatchRequest:
    round_id: int
    candidates: tuple[CandidatePlan, ...]


@dataclass(frozen=True)
class CandidateRequest:
    round_id: int
    candidate_id: int
    parent_sha: str
    proposal: Proposal
    worktree: Path


@dataclass(frozen=True)
class CommitRequest:
    round_id: int
    candidate_id: int
    parent_sha: str
    worktree: Path
    changed_paths: tuple[PurePosixPath, ...]


class ArtifactWorkspace(Protocol):
    def inspect(self, worktree: Path) -> tuple[PurePosixPath, ...]: ...
    def commit(self, request: CommitRequest) -> CandidateArtifact: ...


class CandidateTrace(Protocol):
    def record_execution(self, request: CandidateRequest,
                         execution: ExecutionResult,
                         artifact: CandidateArtifact | None) -> None: ...
    def record_evaluation(self, request: CandidateRequest,
                          evaluation: EvaluationResult | None,
                          gate: GateDecision,
                          status: CandidateStatus) -> None: ...
```

Create `simpleloop/stages/executor.py` with the port types used by Task 2:

```python
@dataclass(frozen=True)
class ExecutorConfig:
    goal: str
    gate_block: str = ""
    prompt_dir: Path | None = None


@dataclass(frozen=True)
class ExecutionRequest:
    round_id: int
    candidate_id: int
    proposal: Proposal
    worktree: Path


class Executor(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionResult: ...
```

Create `simpleloop/stages/evaluator.py` with the port types used by Task 2:

```python
@dataclass(frozen=True)
class EvaluationConfig:
    commands: tuple[str, ...]
    objective_key: str
    gate_keys: tuple[str, ...]
    timeout_seconds: int = 600
    output_cap_chars: int = 16000


@dataclass(frozen=True)
class EvaluationRequest:
    worktree: Path


class Evaluator(Protocol):
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult: ...
```

Create `simpleloop/stages/gate.py`; reuse the current reserved key validation and details, but return typed rows:

```python
PATHS = "PATHS"
EVAL_COMMANDS = "EVAL_COMMANDS"


@dataclass(frozen=True)
class GateSpec:
    objective_key: str
    gate_keys: tuple[str, ...]


def apply_gates(evaluation: EvaluationResult | None, spec: GateSpec,
                *, skip_reason: str | None = None) -> GateDecision:
    if spec.objective_key in {PATHS, EVAL_COMMANDS}:
        raise ValueError(f"objective key {spec.objective_key} is reserved by the harness")
    seen = {PATHS, EVAL_COMMANDS, spec.objective_key}
    for key in spec.gate_keys:
        if key in seen:
            raise ValueError(f"gate key {key} is reserved or duplicated")
        seen.add(key)
    if evaluation is None:
        reason = skip_reason or "not run"
        rows = {PATHS: GateResult(True, ""), EVAL_COMMANDS: GateResult(None, reason)}
        rows.update({key: GateResult(None, reason) for key in spec.gate_keys})
        return GateDecision(rows, False, False)
    commands_ok = not evaluation.error and all(code == 0 for code in evaluation.returncodes)
    detail = evaluation.error or ("" if commands_ok else f"exit codes: {list(evaluation.returncodes)}")
    rows = {PATHS: GateResult(True, ""), EVAL_COMMANDS: GateResult(commands_ok, detail)}
    for key in spec.gate_keys:
        value = evaluation.metrics.get(key)
        row_detail = "" if value is True else "evaluator reported FAIL" if value is False else "metric missing or unknown"
        rows[key] = GateResult(value if isinstance(value, bool) else None, row_detail)
    passed = all(row.passed is True for row in rows.values())
    objective = evaluation.metrics.get(spec.objective_key)
    eligible = bool(
        passed
        and isinstance(objective, (int, float))
        and not isinstance(objective, bool)
        and math.isfinite(objective)
    )
    return GateDecision(rows, passed, eligible)
```

- [x] **Step 4: Verify and commit typed ports**

```bash
python -m pytest -q tests/test_candidate_contracts.py
git add simpleloop/candidate.py simpleloop/stages tests/test_candidate_contracts.py
git commit -m "refactor: define candidate stage ports"
```

Expected: candidate contract tests pass.

---

### Task 2: Extract Executor, ArtifactWorkspace, Evaluator, Trace, and baseline adapters

**Files:**

- Modify: `simpleloop/stages/executor.py`
- Modify: `simpleloop/stages/evaluator.py`
- Create: `simpleloop/stages/artifacts.py`
- Create: `simpleloop/persistence/candidate_trace.py`
- Modify: `simpleloop/roles/executor.py`
- Modify: `tests/test_candidate_worker.py`
- Create: `tests/test_candidate_stages.py`

**Interfaces:**

- Consumes: Task 1 ports and the existing `Agent`, `Workspace`, `ApptainerRuntime`, eval parser, prompt loader, and handoff writer.
- Produces: `AgentExecutor`, `GitArtifactWorkspace`, `HarnessEvaluator`, `HandoffCandidateTrace`, `BaselineAcceptanceError`, and `validate_baseline()`.

- [x] **Step 1: Write failing adapter tests**

```python
def test_agent_executor_only_returns_agent_facts(tmp_path: Path):
    agent = FakeAgent('done\n```json\n{"outcome":"completed","summary":"ok"}\n```')
    result = AgentExecutor(agent, ExecutorConfig("goal")).execute(
        ExecutionRequest(2, 3, Proposal("cache it"), tmp_path)
    )
    assert result.status == "EXECUTED"
    assert result.self_report["summary"] == "ok"
    assert agent.cwd == tmp_path


def test_git_artifact_workspace_maps_commit_request(tmp_path: Path):
    workspace = FakeWorkspace(changed=["src/a.cc"], sha="child")
    adapter = GitArtifactWorkspace(workspace)
    paths = adapter.inspect(tmp_path)
    artifact = adapter.commit(CommitRequest(2, 3, "parent", tmp_path, paths))
    assert artifact.parent_sha == "parent"
    assert artifact.sha == "child"
    assert workspace.commit_args == (tmp_path, "2-c3", ["src/a.cc"])


def test_harness_evaluator_normalizes_runtime_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(evaluator_mod.evals, "run_eval",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    result = HarnessEvaluator(object(), EvaluationConfig(("eval",), "SPEED_MS", ())).evaluate(
        EvaluationRequest(tmp_path)
    )
    assert result.error == "offline"


def test_validate_baseline_rejects_missing_objective():
    with pytest.raises(BaselineAcceptanceError, match="missing or not finite"):
        validate_baseline(
            EvaluationResult("bad", {"CORRECTNESS": True}, (0,)),
            GateSpec("SPEED_MS", ("CORRECTNESS",)),
        )
```

- [x] **Step 2: Run tests and confirm adapters are missing**

```bash
python -m pytest -q tests/test_candidate_stages.py
```

Expected: import failures for `AgentExecutor`, `GitArtifactWorkspace`, and `HarnessEvaluator`.

- [x] **Step 3: Move the existing implementations behind the ports**

Implement `AgentExecutor.execute()` in `stages/executor.py` by moving prompt construction and `parse_self_report()` from `roles/executor.py`. It returns only:

```python
ExecutionResult(
    status="EXECUTED",
    output=agent_output,
    self_report=parse_self_report(agent_output),
)
```

and catches `(AgentError, ValueError)` as:

```python
ExecutionResult(status="EXECUTOR_FAILED", reason=str(exc))
```

Implement `GitArtifactWorkspace` in `stages/artifacts.py`:

```python
class GitArtifactWorkspace:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def inspect(self, worktree: Path) -> tuple[PurePosixPath, ...]:
        return tuple(PurePosixPath(path) for path in self.workspace.changed_paths(worktree))

    def commit(self, request: CommitRequest) -> CandidateArtifact:
        paths = [path.as_posix() for path in request.changed_paths]
        sha = self.workspace.commit(
            request.worktree,
            f"{request.round_id}-c{request.candidate_id}",
            paths,
        )
        return CandidateArtifact(request.parent_sha, sha, request.changed_paths)
```

Implement `HarnessEvaluator` and baseline validation in `stages/evaluator.py`:

```python
class BaselineAcceptanceError(RuntimeError):
    pass


class HarnessEvaluator:
    def __init__(self, runtime: ApptainerRuntime, config: EvaluationConfig):
        self.runtime = runtime
        self.config = config

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        try:
            result = evals.run_eval(
                list(self.config.commands), cwd=request.worktree,
                runtime=self.runtime,
                metrics_schema={
                    "objective": {"key": self.config.objective_key},
                    "gates": [{"key": key} for key in self.config.gate_keys],
                },
                timeout_seconds=self.config.timeout_seconds,
                output_cap=self.config.output_cap_chars,
            )
        except Exception as exc:
            return EvaluationResult(f"(eval failed to run: {exc})", error=str(exc))
        return EvaluationResult(result.text, result.metrics, result.returncodes)


def validate_baseline(evaluation: EvaluationResult, spec: GateSpec) -> None:
    failed = [code for code in evaluation.returncodes if code != 0]
    if failed:
        raise BaselineAcceptanceError(f"baseline evaluation command failed with exit {failed[0]}:\n{evaluation.text[:8000]}")
    objective = evaluation.metrics.get(spec.objective_key)
    if isinstance(objective, bool) or not isinstance(objective, (int, float)) or not math.isfinite(objective):
        raise BaselineAcceptanceError(f"baseline objective {spec.objective_key} is missing or not finite:\n{evaluation.text[:8000]}")
    failed_gates = [key for key in spec.gate_keys if evaluation.metrics.get(key) is not True]
    if failed_gates:
        raise BaselineAcceptanceError(f"baseline gate(s) did not pass: {', '.join(failed_gates)}:\n{evaluation.text[:8000]}")
```

Implement `HandoffCandidateTrace` by projecting typed values into the exact existing executor/eval handoff dictionaries. Move imports/tests from `roles.executor` to `stages.executor`, then delete the old implementation or reduce `roles/executor.py` to no production consumers before deleting it in Task 6.

- [x] **Step 4: Verify adapter behavior and existing executor tests**

```bash
python -m pytest -q tests/test_candidate_stages.py tests/test_candidate_worker.py -k "self_report or execute or baseline"
```

Expected: all selected tests pass.

- [x] **Step 5: Commit stage adapters**

```bash
git add simpleloop/stages simpleloop/persistence/candidate_trace.py simpleloop/roles/executor.py tests/test_candidate_stages.py tests/test_candidate_worker.py
git commit -m "refactor: extract candidate stage adapters"
```

---

### Task 3: Implement the shared Candidate Pipeline

**Files:**

- Modify: `simpleloop/candidate.py`
- Create: `tests/test_candidate_pipeline.py`

**Interfaces:**

- Consumes: Task 1 ports and Task 2 adapters.
- Produces: `run_candidate()` and `run_candidate_guarded()` as the sole candidate business implementation.

- [x] **Step 1: Write failing fake-port pipeline tests**

Create deterministic fakes that append `"execute"`, `"inspect"`, `"commit"`, `"evaluate"`, `"gate-trace"` to a shared call list. Cover these assertions in separate tests:

```python
result = run_candidate(request, executor=executor, artifacts=artifacts,
                       evaluator=evaluator, gate_spec=spec, trace=trace)
assert result.status is CandidateStatus.COMPLETED
assert result.sha == "child"
assert calls == ["execute", "inspect", "commit", "execution-trace",
                 "evaluate", "evaluation-trace"]
```

```python
result = run_candidate(
    no_change_request,
    executor=no_change_executor,
    artifacts=artifacts,
    evaluator=evaluator,
    gate_spec=spec,
    trace=trace,
)
assert result.status is CandidateStatus.NO_CHANGE
assert "evaluate" not in calls
```

```python
result = run_candidate_guarded(
    request,
    executor=ExplodingExecutor(),
    artifacts=artifacts,
    evaluator=evaluator,
    gate_spec=spec,
    trace=trace,
)
assert result.status is CandidateStatus.WORKER_FAILED
assert result.parent_sha == request.parent_sha
```

Also assert executor failure, eval error retaining artifact, nonzero command rejection, and hard-gate rejection.

- [x] **Step 2: Run tests and confirm pipeline functions are missing**

```bash
python -m pytest -q tests/test_candidate_pipeline.py
```

Expected: import failure for `run_candidate`.

- [x] **Step 3: Implement the minimal pipeline state machine**

Implement the exact order from the spec. Construct results through one private helper so every branch fills the existing fields:

```python
def _result(request, *, status, execution, gate, artifact=None, evaluation=None):
    return CandidateResult(
        candidate_id=request.candidate_id,
        experiment_id=f"r{request.round_id}c{request.candidate_id}",
        proposal=request.proposal,
        parent_sha=request.parent_sha,
        status=status,
        execution=execution,
        artifact=artifact,
        evaluation=evaluation,
        gate=gate,
    )
```

`run_candidate_guarded()` catches `Exception`, creates the existing unknown gate rows through `apply_gates(None, gate_spec)`, and returns `WORKER_FAILED` with `ExecutionResult("WORKER_FAILED", reason=f"candidate worker failed: {exc}")`. Put that projection in `candidate_failure_from_request(request, reason)` so the worker catch-all and Local backend never rebuild failure rows themselves.

- [x] **Step 4: Verify pipeline and candidate codec tests**

```bash
python -m pytest -q tests/test_candidate_pipeline.py tests/test_candidate_contracts.py tests/test_candidate_result_codec.py
```

Expected: all pass.

- [x] **Step 5: Commit the shared pipeline**

```bash
git add simpleloop/candidate.py tests/test_candidate_pipeline.py
git commit -m "refactor: add shared candidate pipeline"
```

---

### Task 4: Reduce candidate worker to a transport adapter

**Files:**

- Modify: `simpleloop/candidate_worker.py`
- Modify: `tests/test_candidate_worker.py`
- Modify: `tests/test_phase0_characterization.py`
- Modify: `tests/test_telemetry.py`

**Interfaces:**

- Consumes: `CandidateSpec`, Task 2 configured adapters, `run_candidate_guarded()`, and existing candidate result codec.
- Produces: unchanged worker CLI and durable result shape without embedded business stages.

- [ ] **Step 1: Redirect worker tests to the shared pipeline and assert thin ownership**

Add:

```python
def test_worker_delegates_business_to_shared_pipeline(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(worker_mod, "run_candidate_guarded",
                        lambda request, **ports: seen.append(request) or candidate_failure_from_request(request, "test"))
    assert main(["--manifest", str(valid_manifest(tmp_path))]) == 0
    assert len(seen) == 1
    assert seen[0].proposal.instruction == "do the thing"
```

Update existing candidate behavior tests to import and exercise `simpleloop.candidate.run_candidate` with fake ports; leave CLI/result tests in `test_candidate_worker.py`.

- [ ] **Step 2: Run the delegation test and verify it fails**

```bash
python -m pytest -q tests/test_candidate_worker.py::test_worker_delegates_business_to_shared_pipeline
```

Expected: failure because worker still owns and calls its local `run_candidate` implementation.

- [ ] **Step 3: Delete worker business logic and build explicit adapters**

Keep in `candidate_worker.py` only:

- `CandidateSpec` JSON transport;
- resolved-config adapter construction;
- baseline-only adapter call;
- `CandidateRequest` conversion;
- `run_candidate_guarded()` call;
- catch-all for pre-pipeline worker failures;
- atomic `write_result()` and CLI `main()`.

Replace `CandidateDeps` with a configured bundle whose fields are named ports, not raw business state:

```python
@dataclass(frozen=True)
class CandidatePorts:
    executor: Executor
    artifacts: ArtifactWorkspace
    evaluator: Evaluator
    gate_spec: GateSpec
    trace: CandidateTrace
```

`build_ports(cfg, run_dir, usage_observer, prompt_dir)` may read resolved config because it is the worker composition boundary. It must return configured ports; `run_candidate()` never sees `cfg`.

- [ ] **Step 4: Verify worker and all Phase 0 boundaries**

```bash
python -m pytest -q tests/test_candidate_worker.py tests/test_phase0_characterization.py tests/test_telemetry.py
```

Expected: all pass and `tests/fixtures/phase0/candidate-result.json` remains unmodified.

- [ ] **Step 5: Commit the thin worker**

```bash
git add simpleloop/candidate_worker.py tests/test_candidate_worker.py tests/test_phase0_characterization.py tests/test_telemetry.py
git commit -m "refactor: make candidate worker a transport adapter"
```

---

### Task 5: Move Local candidate batching out of the loop

**Files:**

- Modify: `simpleloop/execution/base.py`
- Modify: `simpleloop/execution/local.py`
- Modify: `simpleloop/execution/hepjob.py`
- Modify: `simpleloop/loop.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `tests/test_hepjob_backend.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_telemetry.py`

**Interfaces:**

- Consumes: `CandidateBatchRequest`, configured candidate ports, and existing RoundJournal.
- Produces: typed backend batch interface; Local serial/parallel execution with no `loop.py` import; HEPJob manifests projected from independently parented plans.

- [ ] **Step 1: Write failing Local mixed-parent and cleanup tests**

```python
def test_local_batch_uses_each_plan_parent_and_preserves_order(tmp_path, monkeypatch):
    plans = (
        CandidatePlan(0, "parent-a", Proposal("a")),
        CandidatePlan(1, "parent-b", Proposal("b")),
    )
    results = backend.run_candidates(CandidateBatchRequest(7, plans))
    assert workspace.added == [("7-c0", "parent-a"), ("7-c1", "parent-b")]
    assert [result.candidate_id for result in results] == [0, 1]
    assert workspace.removed == ["7-c0", "7-c1"]


def test_local_batch_cleans_worktree_after_guarded_failure(tmp_path):
    workspace = FakeWorkspace(root=tmp_path)
    ctx = FakeContext(workspace=workspace, max_workers=1)
    backend = LocalBackend(
        ctx,
        candidate_runner=lambda request, **ports: candidate_failure_from_request(
            request, "pipeline exploded"
        ),
    )
    request = CandidateBatchRequest(
        7,
        (CandidatePlan(0, "parent", Proposal("p0")),),
    )
    result = backend.run_candidates(request)[0]
    assert result.status is CandidateStatus.WORKER_FAILED
    assert workspace.removed == ["7-c0"]
```

Define `FakeWorkspace` and `FakeContext` beside these tests as minimal records: `FakeWorkspace.add_worktree()` appends `(worktree_id, parent_sha)` and returns `root / worktree_id`; `remove_worktree()` appends the id. `FakeContext` supplies that workspace plus `cfg={"max_workers": max_workers}` and the existing candidate adapter fields used by `LocalBackend`.

Update fake backends to accept `run_candidates(request, *, journal=None)`.

- [ ] **Step 2: Run focused backend tests and verify signature failures**

```bash
python -m pytest -q tests/test_parallel_candidates.py tests/test_hepjob_backend.py
```

Expected: failures because current backends require `proposals`, `round_id`, and `parent_sha` keyword arguments.

- [ ] **Step 3: Migrate all backend calls together**

Change `ExecutionBackend.run_candidates()` to the typed request. In `loop.py` construct:

```python
candidate_request = CandidateBatchRequest(
    round_id,
    tuple(
        CandidatePlan(index, parent_sha, proposal)
        for index, proposal in enumerate(proposal_batch.proposals)
    ),
)
candidates = ctx.execution_backend.run_candidates(
    candidate_request,
    journal=journal,
)
```

Move the serial/ThreadPool logic into `LocalBackend.run_candidates()`. It opens `f"{round_id}-c{candidate_id}"`, creates a `CandidateRequest`, calls `run_candidate_guarded()`, and removes the worktree in `finally`. It stores futures by input index and returns results in plan order.

Update HEPJob `_prepare()` and `_write_manifest()` to consume `CandidatePlan`; use each plan's own `parent_sha` and `proposal.instruction`. Keep resume jobs and supervision unchanged.

Delete from `loop.py`:

```text
_run_candidates
_run_candidate_guarded
_deps_from_ctx
_run_one_candidate
_candidate_failure
```

- [ ] **Step 4: Verify Local, HEPJob, runtime, and telemetry behavior**

```bash
python -m pytest -q tests/test_parallel_candidates.py tests/test_hepjob_backend.py tests/test_runtime.py tests/test_telemetry.py
```

Expected: all pass.

- [ ] **Step 5: Commit backend migration**

```bash
git add simpleloop/execution simpleloop/loop.py tests/test_parallel_candidates.py tests/test_hepjob_backend.py tests/test_runtime.py tests/test_telemetry.py
git commit -m "refactor: move candidate batching into backends"
```

---

### Task 6: Decouple baseline, delete old owners, and enforce Phase 2 architecture

**Files:**

- Modify: `simpleloop/execution/local.py`
- Modify: `simpleloop/execution/hepjob.py`
- Modify: `simpleloop/loop.py`
- Delete: `simpleloop/roles/executor.py`
- Modify or delete migrated portions: `simpleloop/harness/evals.py`
- Modify or delete migrated portions: `simpleloop/harness/gate.py`
- Modify: `tests/test_architecture_boundaries.py`
- Modify: `tests/test_candidate_stages.py`
- Modify imports in all affected tests.
- Modify: `docs/superpowers/plans/2026-08-14-simpleloop-phase2-candidate-pipeline.md`

**Interfaces:**

- Consumes: shared `validate_baseline()`, Phase 2 pipeline, and compatibility fixtures.
- Produces: zero Local/HEPJob imports of `loop.py`, one owner per candidate stage, executable architecture guards, and a completed plan record.

- [ ] **Step 1: Add failing architecture guards**

```python
def test_candidate_execution_modules_do_not_import_loop():
    for relative in (
        "simpleloop/candidate.py",
        "simpleloop/candidate_worker.py",
        "simpleloop/execution/local.py",
        "simpleloop/execution/hepjob.py",
        "simpleloop/stages/executor.py",
        "simpleloop/stages/evaluator.py",
        "simpleloop/stages/gate.py",
    ):
        assert "simpleloop.loop" not in normalized_imports(ROOT / relative)


def test_loop_does_not_define_candidate_execution_helpers():
    names = top_level_function_names(ROOT / "simpleloop/loop.py")
    assert not names.intersection({
        "_run_candidates", "_run_candidate_guarded", "_deps_from_ctx",
        "_run_one_candidate", "_candidate_failure",
    })


def test_candidate_pipeline_does_not_read_context_or_config_dicts():
    source = (ROOT / "simpleloop/candidate.py").read_text(encoding="utf-8")
    assert "RunContext" not in source
    assert "CandidateDeps" not in source
    assert ".cfg" not in source
```

- [ ] **Step 2: Run architecture tests and confirm remaining imports fail**

```bash
python -m pytest -q tests/test_architecture_boundaries.py
```

Expected: fails on current baseline error/timestamp imports until cleanup completes.

- [ ] **Step 3: Use the shared baseline validator and remove old imports**

Convert Local `EvalResult` to typed `EvaluationResult`, then call:

```python
validate_baseline(
    EvaluationResult(result.text, result.metrics, result.returncodes),
    GateSpec(objective_key, gate_keys),
)
```

HEPJob calls the same function on the baseline `CandidateResult.evaluation`. Move the timestamp helper to a neutral module already imported by backends, or define a private backend-local helper; neither backend may import `loop.py`.

Delete `BaselineAcceptanceError` from `loop.py`, import it from `stages.evaluator` where CLI-facing handling needs it, and remove `roles/executor.py` after all imports use `stages.executor`. Remove migrated eval/gate implementations only when `rg` proves no production consumer remains; retain `objective_delta()` and metric parsing in the smallest module still serving reporting and evaluator.

- [ ] **Step 4: Run complete verification**

```bash
python -m pytest -q
python -m compileall -q simpleloop tests
rg -n "from \.\.loop|from \..loop|import simpleloop\.loop" simpleloop/execution simpleloop/candidate.py simpleloop/candidate_worker.py simpleloop/stages
rg -n "def _run_candidates|def _run_candidate_guarded|def _deps_from_ctx|def _run_one_candidate|def _candidate_failure" simpleloop/loop.py
git diff --check
```

Expected: pytest passes with the established environment skip only; compileall exits 0; both `rg` commands produce no output; diff check exits 0.

- [ ] **Step 5: Mark this plan implemented and commit Phase 2 completion**

Change the plan header to:

```markdown
**Status:** Implemented on `refactor/phase0-phase1-typed-contracts`.
```

Mark every checkbox `[x]`, then:

```bash
git add -A
git diff --cached --check
git commit -m "test: enforce phase two candidate boundaries"
```

Expected: worktree is clean and Phase 2 architecture guards pass.
