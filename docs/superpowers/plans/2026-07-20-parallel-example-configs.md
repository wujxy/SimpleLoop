# Parallel Example Configs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure the tiny algorithm and OMILREC v1.0.0 examples to run three objective-selected candidates concurrently per round.

**Architecture:** Keep parallelism entirely in existing task YAML fields. OMILREC reuses its structured speed objective and gates; tiny adds structured `ms_per_call`, `CORRECTNESS`, and `DRIFT` output so the selector can choose by measured speed rather than judger score.

**Tech Stack:** YAML, Python 3, pytest, SimpleLoop CLI validation.

## Global Constraints

- Both examples use `candidates_per_round: 3` and `max_workers: 3`.
- OMILREC keeps `SPEED_MS` as the lower-is-better objective and keeps both existing gates.
- tiny uses `ms_per_call` as the lower-is-better objective.
- tiny candidates are eligible only when `CORRECTNESS` and `DRIFT` both pass.
- `max_rounds` values remain unchanged.
- No core loop or agent code changes.
- Both READMEs document objective selection, shared `base_sha`, resource multiplication, and the `1/1` serial fallback.

---

### Task 1: Lock The Example Contracts With A Failing Test

**Files:**
- Modify: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: real YAML files under repository-root `examples/`.
- Produces: two tests asserting parallel values and objective/gate contracts.

- [ ] **Step 1: Add real-example YAML tests**

Add:

```python
EXAMPLES = Path(__file__).parents[2] / "examples"


def _example_yaml(relative_path: str) -> dict:
    return yaml.safe_load((EXAMPLES / relative_path).read_text(encoding="utf-8"))


def test_tiny_example_uses_parallel_objective_selection():
    raw = _example_yaml("tiny_algo_opt/task.yaml")

    assert raw["loop"]["candidates_per_round"] == 3
    assert raw["loop"]["max_workers"] == 3
    assert raw["eval"]["metrics"] == {
        "objective": {"key": "ms_per_call", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}, {"key": "DRIFT"}],
    }
    commands = "\n".join(raw["eval"]["commands"])
    assert "CORRECTNESS=PASS" in commands
    assert "CORRECTNESS=FAIL" in commands
    assert "DRIFT=PASS" in commands
    assert "DRIFT=FAIL" in commands


def test_omilrec_v100_example_uses_parallel_speed_selection():
    raw = _example_yaml("omilrec-v100.yaml")

    assert raw["loop"]["candidates_per_round"] == 3
    assert raw["loop"]["max_workers"] == 3
    assert raw["eval"]["metrics"] == {
        "objective": {"key": "SPEED_MS", "lower_is_better": True},
        "gates": [{"key": "CORRECTNESS"}, {"key": "EVAL_RESULT"}],
    }
```

- [ ] **Step 2: Run both tests and verify RED**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_parallel_candidates.py::test_tiny_example_uses_parallel_objective_selection \
  simpleloop/tests/test_parallel_candidates.py::test_omilrec_v100_example_uses_parallel_speed_selection \
  -v
```

Expected: both tests FAIL because the example loop blocks do not contain the
parallel fields; tiny also lacks a metrics block and gate output.

### Task 2: Update Both YAML Configs

**Files:**
- Modify: `examples/tiny_algo_opt/task.yaml`
- Modify: `examples/omilrec-v100.yaml`
- Test: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: existing `loop` and `eval` task schema.
- Produces: two valid task configs with three candidates and three workers.

- [ ] **Step 1: Add parallel fields to both loop blocks**

Use:

```yaml
loop:
  max_rounds: <existing value>
  candidates_per_round: 3
  max_workers: 3
```

- [ ] **Step 2: Make tiny gate output parseable**

Replace the first two tiny eval commands with:

```yaml
    - "PYTHONPATH=. python -m pytest tests/ -q && echo CORRECTNESS=PASS || echo CORRECTNESS=FAIL"
    - "PYTHONPATH=. python scripts/check_drift.py && echo DRIFT=PASS || echo DRIFT=FAIL"
```

Keep the benchmark command and add:

```yaml
  metrics:
    objective:
      key: ms_per_call
      lower_is_better: true
    gates:
      - key: CORRECTNESS
      - key: DRIFT
```

- [ ] **Step 3: Run the example contract tests and verify GREEN**

Run the Task 1 command again.

Expected: both tests PASS.

- [ ] **Step 4: Commit YAML behavior**

```bash
git add examples/tiny_algo_opt/task.yaml examples/omilrec-v100.yaml \
  simpleloop/tests/test_parallel_candidates.py
git commit -m "feat: enable parallel candidates in examples"
```

### Task 3: Update Example Documentation

**Files:**
- Modify: `examples/tiny_algo_opt/README.md`
- Modify: `examples/omilrec-v100-README.md`

**Interfaces:**
- Consumes: the YAML behavior from Task 2.
- Produces: user-facing run semantics and cost expectations matching the configs.

- [ ] **Step 1: Document tiny parallel behavior**

Add to `What the loop does on this task`:

```markdown
- Each round asks the proposer for three different candidate families. All
  candidates start from the same accepted `base_sha`, and up to three run
  concurrently.
- The harness selects the lowest `ms_per_call` candidate among candidates whose
  `CORRECTNESS` and `DRIFT` gates pass. Judger score is only a tie-breaker.
```

Add after the run command:

```markdown
The example defaults to `candidates_per_round: 3` and `max_workers: 3`. Set both
to `1` for serial-compatible execution.
```

- [ ] **Step 2: Document OMILREC parallel behavior and cost**

State that each round creates three candidates from the same accepted
`base_sha`, runs at most three concurrently, and selects the lowest eligible
`SPEED_MS`. Also state that 40 rounds permit up to 120 candidate executions and
three concurrent Claude/build/eval pipelines. Document `1/1` as the serial
fallback.

- [ ] **Step 3: Commit documentation**

```bash
git add examples/tiny_algo_opt/README.md examples/omilrec-v100-README.md
git commit -m "docs: explain parallel example execution"
```

### Task 4: Validate The Real Examples And Full Suite

**Files:**
- Verify all Task 1 through Task 3 files.

**Interfaces:**
- Consumes: complete example configuration.
- Produces: CLI and test evidence that both examples are valid.

- [ ] **Step 1: Initialize the tiny nested source repo**

```bash
bash examples/tiny_algo_opt/setup.sh
```

Expected: initializes the nested repo or reports it already exists.

- [ ] **Step 2: Validate tiny**

```bash
python -m simpleloop.cli validate --config examples/tiny_algo_opt/task.yaml
```

Expected: exit 0 and output `candidates_per_round: 3`, `max_workers: 3`, and
objective `ms_per_call`.

- [ ] **Step 3: Validate OMILREC**

```bash
python -m simpleloop.cli validate --config examples/omilrec-v100.yaml
```

Expected: exit 0 and output `candidates_per_round: 3`, `max_workers: 3`, and
objective `SPEED_MS`.

- [ ] **Step 4: Run all tests and static checks**

```bash
python -m pytest simpleloop/tests
python -m compileall simpleloop
git diff --check
git status --short
```

Expected: all tests pass, compileall and diff check exit 0, and the feature
worktree has no uncommitted tracked changes.
