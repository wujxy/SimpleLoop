# Self-Improvement MVP Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the prompt-specific outer-loop contract with a minimal, run-local `self_improvement` MVP while keeping prompt evolution as its only internal implementation.

**Architecture:** The CLI selects a generic outer supervisor from the presence of `self_improvement`. The supervisor creates a private prompt lineage under each run directory and injects its active prompt path into the otherwise independent artifact loop. Prompt history, rollback, and recovery remain prompt-specific internals; no generic strategy framework is added.

**Tech Stack:** Python 3.9+, PyYAML, pytest, existing SimpleLoop CLI and Claude `Agent`.

## Global Constraints

- Preserve the user's unrelated worktree changes; only overlap with `examples/task.yaml` where the approved config migration requires it.
- Do not modify `.env`, secrets, runtime images, or production configuration.
- Use strict config validation and do not retain a `prompt_self_improvement` compatibility alias.
- Every new run starts from package v000; only `--continue` resumes a run-local lineage.
- Keep snapshot, rollback, events, and inflight recovery.
- Do not introduce `mode`, `target`, backend, or strategy options.
- Follow red-green-refactor for each behavior change.

---

### Task 1: Minimal public configuration and package name

**Files:**
- Modify: `tests/test_prompt_self_improvement.py`
- Modify: `tests/test_runtime.py`
- Modify: `simpleloop/config.py`
- Move: `simpleloop/prompt_self_improvement/` to `simpleloop/self_improvement/`
- Modify: `simpleloop/cli.py`

**Interfaces:**
- Consumes: top-level task YAML.
- Produces: resolved `cfg["self_improvement"]` equal to `None` when absent or `{"interval_rounds": int}` when present.

- [ ] **Step 1: Write failing config and import tests**

Change the task helper to write `self_improvement`, import the implementation
from `simpleloop.self_improvement`, and assert:

```python
assert config.load(_task_file(tmp_path))["self_improvement"] is None
assert config.load(
    _task_file(tmp_path, {"interval_rounds": 2})
)["self_improvement"] == {"interval_rounds": 2}
```

Add strict failures for zero interval, removed fields, and the old top-level
name.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest -q tests/test_prompt_self_improvement.py -k "config or defaults or resolves"
```

Expected: collection or assertion failure because `simpleloop.self_improvement`
and the new config contract do not exist.

- [ ] **Step 3: Implement the minimal config and rename**

Rename `_resolve_prompt_self_improvement` to `_resolve_self_improvement`.
Accept only `interval_rounds`, default it to 10 when the block is present, and
return `None` when absent. Move the package and update CLI imports and
dispatch. Do not accept the old top-level key.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run:

```bash
pytest -q tests/test_prompt_self_improvement.py -k "config or defaults or resolves"
pytest -q tests/test_runtime.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/config.py simpleloop/cli.py simpleloop/self_improvement \
  simpleloop/prompt_self_improvement tests/test_prompt_self_improvement.py \
  tests/test_runtime.py
git commit -m "refactor: minimize self-improvement configuration"
```

### Task 2: Run-local prompt lineage and final-trigger semantics

**Files:**
- Modify: `tests/test_prompt_self_improvement.py`
- Modify: `simpleloop/self_improvement/supervisor.py`

**Interfaces:**
- Consumes: `run_dir` and resolved `cfg["self_improvement"]["interval_rounds"]`.
- Produces: `run_dir/self_improvement/prompts`,
  `run_dir/self_improvement/prompt_history`, and summary
  `{"self_improvement": {"active_version": str, "events": list}}`.

- [ ] **Step 1: Write failing ownership and trigger tests**

Add tests showing that two run directories initialize separate v000 histories,
an accepted version in run A is absent from run B, and an interval boundary at
`max_rounds` does not call the optimizer. Update path assertions to:

```python
root = run_dir / "self_improvement"
history = PromptHistory(root / "prompts", root / "prompt_history")
```

Assert the returned summary exposes `active_version` and events.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest -q tests/test_prompt_self_improvement.py -k "supervisor or run_local"
```

Expected: FAIL because the supervisor still reads configured task-level paths,
triggers at the last boundary, and omits self-improvement summary state.

- [ ] **Step 3: Implement run-local state**

In the supervisor derive both directories from
`run_dir / "self_improvement"`, rename the lock to
`.self-improvement.lock`, construct `PromptGate()` and `MetaOptimizer()` from
internal defaults, and trigger only when `completed < total`.

Wrap every return with:

```python
summary["self_improvement"] = {
    "active_version": history.state["active_version"],
    "events": history.events(),
}
```

- [ ] **Step 4: Run focused tests to verify GREEN**

Run:

```bash
pytest -q tests/test_prompt_self_improvement.py -k "supervisor or run_local"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/self_improvement/supervisor.py \
  tests/test_prompt_self_improvement.py
git commit -m "refactor: scope self-improvement state to each run"
```

### Task 3: Inject active prompts without outer-config coupling

**Files:**
- Modify: `tests/test_prompt_self_improvement.py`
- Modify: `tests/test_candidate_worker.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/candidate_worker.py`
- Modify: `simpleloop/execution/hepjob.py`
- Modify: `simpleloop/self_improvement/supervisor.py`

**Interfaces:**
- Produces: optional internal `prompt_dir` argument on `loop.run`;
  `RunContext.prompt_dir`; serializable `CandidateSpec.prompt_dir`; and
  `CandidateDeps.prompt_dir`.
- Consumes: the supervisor's run-local active prompt directory.

- [ ] **Step 1: Write failing injection tests**

Assert the supervisor's artifact runner receives the active prompt directory.
Add local and serialized-worker tests proving proposer, executor, and judger
receive `deps.prompt_dir`, while a normal run uses `None`. Assert no production
read of `cfg["self_improvement"]` is required by the inner loop.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest -q tests/test_prompt_self_improvement.py tests/test_candidate_worker.py \
  tests/test_parallel_candidates.py -k "prompt"
```

Expected: FAIL because prompt selection still comes from the old resolved
configuration.

- [ ] **Step 3: Implement explicit propagation**

Add `prompt_dir: str | Path | None = None` to `loop.run` and propagate it into
`RunContext`. Add a string `prompt_dir` field to `CandidateSpec` for HEPJob
manifests and an optional path to `CandidateDeps`. Parse the spec before
building standalone worker dependencies. Replace all reads of the outer config
with these injected values.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run:

```bash
pytest -q tests/test_prompt_self_improvement.py tests/test_candidate_worker.py \
  tests/test_parallel_candidates.py -k "prompt"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/loop.py simpleloop/candidate_worker.py \
  simpleloop/execution/hepjob.py simpleloop/self_improvement/supervisor.py \
  tests/test_prompt_self_improvement.py tests/test_candidate_worker.py \
  tests/test_parallel_candidates.py
git commit -m "refactor: inject active prompts into artifact runs"
```

### Task 4: Reduce the optimizer protocol

**Files:**
- Modify: `tests/test_prompt_self_improvement.py`
- Modify: `simpleloop/self_improvement/gate.py`
- Modify: `simpleloop/self_improvement/history.py`
- Modify: `simpleloop/self_improvement/optimizer.py`
- Modify: `simpleloop/self_improvement/supervisor.py`

**Interfaces:**
- Produces: `OptimizerReport(diagnosis: str, evidence: list[str])`;
  `PromptGate()` with internal 30,000-character limit.
- Consumes: exact YAML report keys `diagnosis` and `evidence`.

- [ ] **Step 1: Write failing report tests**

Change report fixtures to:

```yaml
diagnosis: role drift
evidence:
  - r0c0
```

Assert unknown keys fail, empty diagnosis fails, and changed/no-change behavior
comes only from `PromptHistory.has_changes`.

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
pytest -q tests/test_prompt_self_improvement.py -k "report or gate or history"
```

Expected: FAIL because the parser still requires `status` and `intent`.

- [ ] **Step 3: Implement the reduced protocol**

Remove `status` and `intent` from the dataclass, parser, gate checks, manifests,
and optimizer instructions. Move `MAX_PROMPT_CHARS = 30000` into `gate.py` and
make `PromptGate` parameterless.

- [ ] **Step 4: Run focused tests to verify GREEN**

Run:

```bash
pytest -q tests/test_prompt_self_improvement.py -k "report or gate or history"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/self_improvement tests/test_prompt_self_improvement.py
git commit -m "refactor: reduce prompt optimizer protocol"
```

### Task 5: Public documentation and complete verification

**Files:**
- Modify: `README.md`
- Modify: `examples/task.yaml`
- Modify: `examples/tiny_algo_opt/task.yaml`
- Modify: `docs/simpleloop_prompt_self_improvement_mvp.md`

**Interfaces:**
- Produces: one documented public YAML contract matching strict validation.

- [ ] **Step 1: Update public examples**

Replace every active example block with:

```yaml
self_improvement:
  interval_rounds: 2
```

Document run-local v000 initialization and the lack of a final-round optimizer
call. Mark the earlier prompt-specific design document as superseded by the
approved convergence spec where historical details remain.

- [ ] **Step 2: Check stale public references**

Run:

```bash
rg -n "prompt_self_improvement|optimizer_command|prompt_dir|history_dir|max_prompt_chars" \
  README.md examples simpleloop tests
```

Expected: only prompt-specific internal type/argument names and intentional
strict-rejection tests remain.

- [ ] **Step 3: Run targeted tests**

Run:

```bash
pytest -q tests/test_prompt_self_improvement.py tests/test_prompt_templates.py \
  tests/test_candidate_worker.py tests/test_parallel_candidates.py \
  tests/test_runtime.py
```

Expected: PASS.

- [ ] **Step 4: Run full project verification**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 5: Check formatting and worktree scope**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; the user's pre-existing `examples/task.yaml`
change is now intentionally incorporated only for the approved config
migration.

- [ ] **Step 6: Commit**

```bash
git add README.md examples/task.yaml examples/tiny_algo_opt/task.yaml \
  docs/simpleloop_prompt_self_improvement_mvp.md
git commit -m "docs: publish minimal self-improvement workflow"
```

- [ ] **Step 7: Present the architecture**

Show the user the final configuration, component boundaries, run-local
filesystem tree, segment/trigger/recovery flow, test evidence, and the explicit
boundary where future harness-code evolution will require worktree and
evaluation-gate design.
