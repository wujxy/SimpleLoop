# Move Proposer Harness to Scripts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the standalone Proposer harness a repository-local script with no harness module or command registration in the `simpleloop` package.

**Architecture:** Move the complete runner unchanged to `scripts/proposer_harness.py` and colocate its small argparse entry point there. The script continues importing the production `ProposerOrchestrator` and support classes, while `simpleloop/cli.py` returns to containing only product commands.

**Tech Stack:** Python 3.11, argparse, pytest

## Global Constraints

- Preserve complete Generator + Cognitive behavior and all result artifacts.
- Do not keep a `simpleloop propose` compatibility alias.
- Do not alter Executor, evaluation, Gate, or loop behavior.
- Use `python scripts/proposer_harness.py ...` as the only CLI.

---

### Task 1: Require the repository-local script boundary

**Files:**
- Modify: `tests/test_proposer_harness.py`
- Modify: `tests/test_proposer_harness_runner.py`
- Modify: `tests/test_proposer_cli.py`

**Interfaces:**
- Consumes: Existing `run_proposer(config_path, output_dir, *, from_run=None, seed=None)`.
- Produces: Tests that import `scripts.proposer_harness`, call `main(argv)`, and reject `simpleloop propose`.

- [ ] **Step 1: Write the failing tests**

Change all harness imports to:

```python
from scripts import proposer_harness as harness
```

Change CLI tests to call:

```python
harness.main([
    "--config", "task.yaml",
    "--output-dir", "trial",
    "--from-run", "old-run",
    "--seed", "42",
])
```

Add:

```python
def test_simpleloop_cli_does_not_register_propose(capsys):
    with pytest.raises(SystemExit) as raised:
        cli_mod.main(["propose"])
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify RED**

Run: `PYTHONPATH=. pytest -q tests/test_proposer_harness.py tests/test_proposer_harness_runner.py tests/test_proposer_cli.py`

Expected: FAIL because `scripts.proposer_harness` does not exist or because `simpleloop propose` is still registered.

### Task 2: Move implementation and CLI

**Files:**
- Move: `simpleloop/proposer_harness.py` to `scripts/proposer_harness.py`
- Modify: `simpleloop/cli.py`

**Interfaces:**
- Consumes: Production imports under `simpleloop.*`.
- Produces: `scripts.proposer_harness.run_proposer(...)` and `scripts.proposer_harness.main(argv=None)`.

- [ ] **Step 1: Move the runner and make imports absolute**

Retain all runner functions and replace package-relative imports with `simpleloop.*` imports.

- [ ] **Step 2: Add the script entry point**

```python
def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the complete SimpleLoop Proposer without Executor or evaluation.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--from-run")
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(argv)
    try:
        summary = run_proposer(args.config, args.output_dir, from_run=args.from_run, seed=args.seed)
    except (config_mod.ConfigError, RuntimePreflightError, model_mod.ModelError,
            proposer_mod.ProposerError, WorkspaceError, ProposerHarnessError,
            ValueError) as exc:
        print(f"Proposer error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Generated {summary.proposal_count} proposal(s) from {summary.base_sha}.")
    print(f"  result: {summary.result_path}")
    print(f"  report: {summary.report_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Delete the `propose` parser and dispatch branch from `simpleloop/cli.py`**

- [ ] **Step 4: Run tests to verify GREEN**

Run: `PYTHONPATH=. pytest -q tests/test_proposer_harness.py tests/test_proposer_harness_runner.py tests/test_proposer_cli.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the script migration and tests**

Run: `git add scripts/proposer_harness.py simpleloop/cli.py tests/test_proposer_harness.py tests/test_proposer_harness_runner.py tests/test_proposer_cli.py && git commit -m "refactor: isolate proposer harness script"`


### Task 3: Update user documentation and verify

**Files:**
- Modify: `docs/README.md`

- [ ] **Step 1: Replace `simpleloop propose` examples with `python scripts/proposer_harness.py` and state that the harness is repository-local**

- [ ] **Step 2: Run focused and related regression tests**

Run: `PYTHONPATH=. pytest -q tests/test_proposer_harness.py tests/test_proposer_harness_runner.py tests/test_proposer_cli.py tests/test_generator.py tests/test_orchestrator.py tests/test_cognitive_element.py`

Expected: all tests pass.

- [ ] **Step 3: Check stale references and patch quality**

Run: `rg -n "simpleloop propose|simpleloop[./]proposer_harness" simpleloop scripts tests docs/README.md`

Expected: no output.

Run: `git diff --check`

Expected: no output.

- [ ] **Step 4: Commit documentation**

Run: `git add docs/README.md && git commit -m "docs: update proposer harness command"`
