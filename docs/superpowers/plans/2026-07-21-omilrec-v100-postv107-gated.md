# OMILREC v1.0.0 Post-v1.0.7 Gate Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independent `omilrec-v100-postv107-gated` package that starts from v1.0.0 source and is wired to post-v1.0.7 FCN plus relaxed reconstruction gates.

**Architecture:** Clone the committed v1.0.0 source state into a new top-level git repo, then transplant only the gate infrastructure needed for post-v1.0.7 validation. Add a new SimpleLoop example that references the new package with a package-relative eval command.

**Tech Stack:** Git, Bash, CMake, pytest/ROOT, SimpleLoop YAML config.

## Global Constraints

- Do not modify the existing `omilrec-v100` working tree.
- Do not use `omilrec` as the SimpleLoop source package for this experiment.
- New package path is `/datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated`.
- Algorithm starting point is commit `b51f3b8`, tag `v1.0.0` / `v1.0.0-base`.
- SimpleLoop eval command is `bash scripts/sl_eval_post_v107.sh --evtmax 10`.
- SimpleLoop hard gates are `FCN`, `CONSISTENCY`, and `EVAL_RESULT`.

---

### Task 1: Create Independent Package

**Files:**
- Create directory/repo: `/datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated`

**Interfaces:**
- Consumes: `omilrec-v100` committed git objects.
- Produces: a standalone git worktree at v1.0.0 source state.

- [ ] **Step 1: Verify source tag exists**

Run:

```bash
git -C /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100 rev-parse v1.0.0
```

Expected: prints `b51f3b8...`.

- [ ] **Step 2: Clone committed source to new package**

Run:

```bash
git clone --local /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100 /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated
```

Expected: new directory exists and is a git repo.

- [ ] **Step 3: Reset package to v1.0.0 branch**

Run:

```bash
git -C /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated checkout -b postv107-gated-v100 v1.0.0
```

Expected: branch `postv107-gated-v100` points at `b51f3b8`.

- [ ] **Step 4: Confirm no dirty inherited state**

Run:

```bash
git -C /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated status --short
```

Expected: no output.

### Task 2: Add Post-v1.0.7 Gate Harness

**Files:**
- Modify: `omilrec-v100-postv107-gated/CMakeLists.txt`
- Modify: `omilrec-v100-postv107-gated/OMILRECV2/CMakeLists.txt`
- Create/copy: `omilrec-v100-postv107-gated/tests/unit/test_fcn.cc`
- Create/copy: `omilrec-v100-postv107-gated/tests/unit/CMakeLists.txt`
- Create/copy: `omilrec-v100-postv107-gated/tests/fixtures/v107_rev1/**`
- Create/copy as needed: FCN/serialization support files under `OMILRECV2/src/`

**Interfaces:**
- Consumes: post-v1.0.7 FCN test fixtures and helper code from `omilrec`.
- Produces: `build/bin/test_fcn.exe` when configured with `-DBUILD_UNIT_TESTS=ON`.

- [ ] **Step 1: Write package-local smoke test for FCN target wiring**

Create or update a lightweight test that asserts `BUILD_UNIT_TESTS=ON` exposes a
`test_fcn` target from the new package CMake graph.

- [ ] **Step 2: Run smoke test and observe failure**

Run the selected test command from the new package.

Expected: fails because the v1.0.0 package has no `test_fcn` target yet.

- [ ] **Step 3: Transplant minimal FCN harness**

Copy the smallest needed FCN replay source, fixture, and CMake support from
`omilrec` into the new package while keeping v1.0.0 algorithm code as the base.

- [ ] **Step 4: Run smoke test and CMake target check**

Run:

```bash
cmake -S /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated -B /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated/build -DCMAKE_BUILD_TYPE=Release -DBUILD_UNIT_TESTS=ON
cmake --build /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated/build --target test_fcn --parallel
```

Expected: config and target build succeed, or fail with a concrete missing
symbol/header that must be fixed inside the new package.

### Task 3: Add Eval Wrapper and SimpleLoop Config

**Files:**
- Create: `omilrec-v100-postv107-gated/scripts/sl_eval_post_v107.sh`
- Create: `SimpleLoop/examples/omilrec-v100-postv107-gated.yaml`
- Create or update: `SimpleLoop/simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: `test_fcn.exe`, reconstruction reference files, `quick_bench.sh`.
- Produces: package-local eval output with `FCN`, `CONSISTENCY`, `SPEED_MS`, and `EVAL_RESULT`.

- [ ] **Step 1: Write failing config test**

Add a test that loads `examples/omilrec-v100-postv107-gated.yaml` and asserts:

```python
assert cfg["repo_path"].endswith("/omilrec-v100-postv107-gated")
assert cfg["eval_commands"] == ["bash scripts/sl_eval_post_v107.sh --evtmax 10"]
assert {"key": "FCN"} in cfg["metrics"]["gates"]
assert {"key": "CONSISTENCY"} in cfg["metrics"]["gates"]
assert {"key": "EVAL_RESULT"} in cfg["metrics"]["gates"]
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py -k postv107_gated -q
```

Expected: fails because the config does not exist yet.

- [ ] **Step 3: Add wrapper and config**

Create the package-local wrapper and SimpleLoop config. The wrapper must not
reference absolute paths to `omilrec` or `omilrec-v100`.

- [ ] **Step 4: Run config test**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py -k postv107_gated -q
python -m simpleloop.cli validate --config examples/omilrec-v100-postv107-gated.yaml
```

Expected: both pass.

### Task 4: Manual Edit-Code Plus Test Chain

**Files:**
- Temporarily modify: one admitted `OMILRECV2/src/*.cc` or `OMILRECV2/src/*.h` file in the new package.

**Interfaces:**
- Consumes: completed new package and SimpleLoop config.
- Produces: proof that the new package wrapper runs from the new package after a source edit.

- [ ] **Step 1: Make a harmless source edit**

Add a short comment in an admitted source file in the new package.

- [ ] **Step 2: Run eval wrapper**

Run from the new package root:

```bash
bash scripts/sl_eval_post_v107.sh --evtmax 10
```

Expected: wrapper prints the package root, build/gate stages, and machine-readable metrics.

- [ ] **Step 3: Revert harmless source edit**

Use `git checkout -- <file>` or apply an inverse patch only inside the new package.

- [ ] **Step 4: Confirm final package state**

Run:

```bash
git -C /datafs/users/wujxy/agent-sci/omilrec_opt/v1.0/omilrec-v100-postv107-gated status --short
python -m simpleloop.cli validate --config examples/omilrec-v100-postv107-gated.yaml
```

Expected: only intentional package/config/harness files remain dirty or committed; config validation passes.

## Self-Review

- Spec coverage: package isolation, v1.0.0 start, post-v1.0.7 gates, SimpleLoop config, and manual chain validation are covered.
- Placeholder scan: no TBD/TODO/fill-in placeholders remain.
- Type consistency: config keys match `simpleloop.config.load` output keys and metrics schema shape.
