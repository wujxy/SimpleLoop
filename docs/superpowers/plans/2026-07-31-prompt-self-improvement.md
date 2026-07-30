# Prompt Self-Improvement MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an unattended outer supervisor that runs SimpleLoop in fixed round segments, lets a Meta Optimizer evolve role semantics between segments, validates and versions accepted prompt changes, and recovers the parent prompt set after failures.

**Architecture:** Role prompts become package-shipped v000 semantic files; Python continues to own runtime facts, safety rules, JSON schemas, and parser contracts. A `prompt_self_improvement` package owns snapshots/state, static validation, Meta Optimizer invocation, and segment orchestration; the existing artifact loop gains only a target-round override so the supervisor remains outside it.

**Tech Stack:** Python 3.9+, pathlib, dataclasses, json/yaml, pytest, existing Claude `Agent` adapter.

## Global Constraints

- Semantic role contracts are editable; machine fields, enums, parser labels, Goal, Gate, history, and harness source are fixed.
- v000 uses the identity-internalized prompts from `docs/simpleloop_prompt_self_improvement_mvp.md`, not the current embedded prompts.
- Non-boundary role guidance avoids unnecessary absolute modal language.
- The Meta Optimizer runs only after an artifact segment has completely stopped.
- No replay, canary, A/B testing, automated improvement judgment, prompt candidates, memory evolution, Gate evolution, or harness evolution.
- Every production-code change follows a failing test.

---

### Task 1: External v000 semantic prompts and fixed protocol assembly

**Files:**
- Create: `simpleloop/prompts/__init__.py`
- Create: `simpleloop/prompts/proposer.md`
- Create: `simpleloop/prompts/executor.md`
- Create: `simpleloop/prompts/judger.md`
- Create: `simpleloop/prompts/meta_optimizer.md`
- Modify: `simpleloop/roles/proposer.py`
- Modify: `simpleloop/roles/executor.py`
- Modify: `simpleloop/roles/judger.py`
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/candidate_worker.py`
- Modify: `pyproject.toml`
- Test: `tests/test_prompt_templates.py`
- Test: `tests/test_parallel_candidates.py`
- Test: `tests/test_views_and_parse.py`

**Interfaces:**
- Produces: `load_semantic(role: str, prompt_dir: str | Path | None = None) -> str`.
- Role entry points gain `prompt_dir: str | Path | None = None` and assemble `semantic + context + fixed protocol`.
- Existing Proposer and Judger JSON schemas and parsers remain unchanged.

- [ ] **Step 1: Write failing prompt-loader and assembly tests**

```python
def test_load_semantic_uses_v000_package_prompt():
    text = load_semantic("proposer")
    assert text.startswith("You are the PROPOSER")
    assert "one connected process" in text
    assert "Do not retry" not in text

def test_proposer_assembles_semantics_context_and_fixed_schema(tmp_path):
    agent = CapturingAgent(valid_proposer_response())
    propose(agent, goal="faster", editable=["src/**"], frozen=[],
            history=[], insights=[], base_sha="abc", cwd=tmp_path)
    assert "You are the PROPOSER" in agent.prompt
    assert "Task goal:\nfaster" in agent.prompt
    assert "Source access is read-only" in agent.prompt
    assert agent.schema["required"] == ["reflection", "insight", "insight_refs", "proposals"]
```

- [ ] **Step 2: Run the focused tests and confirm missing loader/assets fail**

Run: `pytest -q tests/test_prompt_templates.py tests/test_parallel_candidates.py tests/test_views_and_parse.py`

Expected: FAIL because `simpleloop.prompts` and external semantic files do not exist.

- [ ] **Step 3: Add the four exact v000 semantic files and loader**

```python
PROMPT_NAMES = ("proposer", "executor", "judger", "meta_optimizer")

def load_semantic(role: str, prompt_dir: str | Path | None = None) -> str:
    if role not in PROMPT_NAMES:
        raise ValueError(f"unknown prompt role: {role}")
    path = (Path(prompt_dir) / f"{role}.md" if prompt_dir
            else Path(__file__).with_name(f"{role}.md"))
    return path.read_text(encoding="utf-8").strip()
```

Copy the four approved prompt bodies verbatim from design sections 5 and 6. Add `prompts/*.md` to setuptools package data.

- [ ] **Step 4: Replace embedded role identity prose with semantic loading while retaining facts and protocols in Python**

The fixed Proposer protocol continues to provide Goal, Gates, accepted SHA, insights, projected history, read-only source boundary, editable/frozen paths, exact candidate count, and JSON-only delivery. Executor retains worktree/Git/file safety. Judger retains authoritative metrics, `LANDED_STATE`, fixed score/risk/feedback fields, and JSON-only delivery.

- [ ] **Step 5: Run focused prompt and role tests**

Run: `pytest -q tests/test_prompt_templates.py tests/test_parallel_candidates.py tests/test_views_and_parse.py tests/test_candidate_worker.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add simpleloop/prompts simpleloop/roles simpleloop/loop.py simpleloop/candidate_worker.py pyproject.toml tests/test_prompt_templates.py tests/test_parallel_candidates.py tests/test_views_and_parse.py
git commit -m "refactor: externalize role semantic prompts"
```

### Task 2: Strict prompt self-improvement configuration

**Files:**
- Modify: `simpleloop/config.py`
- Test: `tests/test_prompt_self_improvement.py`

**Interfaces:**
- Produces resolved `cfg["prompt_self_improvement"]` with `enabled`, `interval_rounds`, `optimizer_command`, `prompt_dir`, `history_dir`, and `max_prompt_chars`.
- Paths resolve relative to the task config.

- [ ] **Step 1: Write failing config tests**

```python
def test_prompt_self_improvement_defaults_disabled(task_file):
    cfg = config.load(task_file)
    assert cfg["prompt_self_improvement"] == {"enabled": False}

def test_prompt_self_improvement_resolves_enabled_block(task_file):
    update_yaml(task_file, prompt_self_improvement={
        "enabled": True, "interval_rounds": 10,
        "optimizer_command": "claude", "prompt_dir": "prompts",
        "history_dir": "prompt_history", "max_prompt_chars": 30000,
    })
    cfg = config.load(task_file)
    assert cfg["prompt_self_improvement"]["prompt_dir"] == str((task_file.parent / "prompts").resolve())
```

Also cover unknown keys, non-boolean `enabled`, interval below 1, missing enabled paths, and max chars below 1000.

- [ ] **Step 2: Run tests and confirm unknown top-level block fails**

Run: `pytest -q tests/test_prompt_self_improvement.py -k config`

Expected: FAIL with `unknown top-level key`.

- [ ] **Step 3: Implement `_resolve_prompt_self_improvement(raw, config_path)`**

Return `{"enabled": False}` when absent/disabled. Require explicit paths only when enabled; normalize command and integers; reject unknown fields.

- [ ] **Step 4: Run config tests**

Run: `pytest -q tests/test_prompt_self_improvement.py -k config`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/config.py tests/test_prompt_self_improvement.py
git commit -m "feat: validate prompt self-improvement config"
```

### Task 3: Prompt snapshots, state, reports, and static gate

**Files:**
- Create: `simpleloop/prompt_self_improvement/__init__.py`
- Create: `simpleloop/prompt_self_improvement/history.py`
- Create: `simpleloop/prompt_self_improvement/gate.py`
- Test: `tests/test_prompt_self_improvement.py`

**Interfaces:**
- Produces `PromptHistory(prompt_dir, history_dir)` with `initialize()`, `restore(version)`, `has_changes(version)`, `snapshot(trigger_round, report)`, `mark_inflight(run_id, trigger_round)`, `finish_trigger(...)`, and `recover_inflight()`.
- Produces `PromptGate(max_prompt_chars).check(prompt_dir, report_path) -> list[str]`.
- `OptimizerReport.load(path) -> OptimizerReport` accepts exactly `status`, `diagnosis`, `evidence`, `intent`.

- [ ] **Step 1: Write failing history tests**

```python
def test_initialize_creates_new_v000_from_package_prompts(tmp_path):
    history = PromptHistory(tmp_path / "prompts", tmp_path / "history")
    assert history.initialize() == "v000"
    assert (tmp_path / "history/v000/proposer.md").read_text() == load_semantic("proposer") + "\n"

def test_restore_discards_partial_optimizer_edit(tmp_path):
    history = initialized_history(tmp_path)
    (history.prompt_dir / "proposer.md").write_text("partial")
    history.restore("v000")
    assert "You are the PROPOSER" in (history.prompt_dir / "proposer.md").read_text()
```

Cover snapshot numbering, no-change detection, atomic state, event `(run_id, trigger_round)` idempotence, and interrupted recovery.

- [ ] **Step 2: Run history tests and confirm imports fail**

Run: `pytest -q tests/test_prompt_self_improvement.py -k 'history or snapshot or restore or recovery'`

Expected: FAIL because package classes do not exist.

- [ ] **Step 3: Implement minimal atomic history storage**

Use temporary sibling files plus `os.replace` for state/events writes. Snapshot exactly the four Markdown prompts and generated manifest; never snapshot `optimizer_report.yaml`.

- [ ] **Step 4: Write failing gate/report tests**

```python
def test_gate_accepts_total_role_rewrite_but_rejects_changed_meta_core(tmp_path):
    prompt_dir = initialized_prompt_dir(tmp_path)
    (prompt_dir / "proposer.md").write_text("A wholly new semantic contract")
    assert PromptGate(30000).check(prompt_dir, valid_report(prompt_dir)) == []
    (prompt_dir / "meta_optimizer.md").write_text("changed core")
    assert any("META_IDENTITY_CORE" in e for e in PromptGate(30000).check(prompt_dir, valid_report(prompt_dir)))
```

Also cover missing files, extra files, oversize files, invalid UTF-8, malformed report, and report/diff status mismatch.

- [ ] **Step 5: Implement report parser and gate**

The immutable Meta core is extracted from the packaged v000 prompt, including marker lines. Gate semantics do not inspect role keywords.

- [ ] **Step 6: Run subsystem tests**

Run: `pytest -q tests/test_prompt_self_improvement.py -k 'history or snapshot or restore or recovery or gate or report'`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add simpleloop/prompt_self_improvement tests/test_prompt_self_improvement.py
git commit -m "feat: add prompt history and static gate"
```

### Task 4: Meta Optimizer invocation

**Files:**
- Create: `simpleloop/prompt_self_improvement/optimizer.py`
- Modify: `simpleloop/roles/agent.py`
- Test: `tests/test_prompt_self_improvement.py`

**Interfaces:**
- Produces `LocalRuntime.exec_argv(payload, cwd)` and `subprocess_env(extra)` for the existing `Agent` adapter.
- Produces `MetaOptimizer(command, timeout_seconds, max_output_tokens, agent_factory=Agent).run(...) -> None`.

- [ ] **Step 1: Write a failing invocation test with a capturing fake Agent**

```python
def test_optimizer_receives_absolute_read_write_boundaries(tmp_path):
    fake = CapturingMetaAgent()
    optimizer = MetaOptimizer("claude", 60, 8000, agent_factory=lambda **_: fake)
    optimizer.run(run_dir=tmp_path / "run", prompt_dir=tmp_path / "prompts",
                  history_dir=tmp_path / "history", source_dir=PROJECT_ROOT,
                  goal="faster", gate_block="CORRECTNESS")
    assert str((tmp_path / "run").resolve()) in fake.prompt
    assert "Fixed artifacts:" in fake.prompt
    assert fake.cwd == (tmp_path / "prompts").resolve()
```

- [ ] **Step 2: Run and confirm the optimizer class is missing**

Run: `pytest -q tests/test_prompt_self_improvement.py -k optimizer`

Expected: FAIL on import.

- [ ] **Step 3: Implement host-local Meta invocation through existing Agent**

Load `meta_optimizer.md`, append absolute read/write scope plus Goal/Gates, run with `Read,Edit,Write,Bash`, and require the agent to create `optimizer_report.yaml`. Do not add a diagnostic checklist.

- [ ] **Step 4: Run optimizer tests**

Run: `pytest -q tests/test_prompt_self_improvement.py -k optimizer`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/prompt_self_improvement/optimizer.py simpleloop/roles/agent.py tests/test_prompt_self_improvement.py
git commit -m "feat: invoke meta prompt optimizer"
```

### Task 5: Segmented outer supervisor and recovery

**Files:**
- Create: `simpleloop/prompt_self_improvement/supervisor.py`
- Modify: `simpleloop/loop.py`
- Test: `tests/test_prompt_self_improvement.py`

**Interfaces:**
- `loop.run(..., target_rounds: int | None = None)` caps only the current invocation target while preserving config `max_rounds` as the overall target.
- `supervisor.run(config_path, run_dir, continue_run=False, artifact_runner=loop.run, optimizer_factory=MetaOptimizer) -> dict`.

- [ ] **Step 1: Write failing target-round and supervisor tests**

```python
def test_supervisor_stops_inner_loop_before_optimizer(tmp_path):
    calls = []
    def artifact_runner(*_, target_rounds, **__):
        calls.append(("artifact", target_rounds))
        write_rounds(tmp_path / "run", target_rounds)
        return {"rounds": target_rounds}
    optimizer = FakeOptimizer(lambda: calls.append(("optimizer", count_rounds(tmp_path / "run"))))
    summary = supervisor.run(task, tmp_path / "run", artifact_runner=artifact_runner,
                             optimizer_factory=lambda **_: optimizer)
    assert calls == [("artifact", 2), ("optimizer", 2),
                     ("artifact", 4), ("optimizer", 4),
                     ("artifact", 5)]
    assert summary["rounds"] == 5
```

Also cover accepted snapshot activation, `no_change`, rejected gate restoration, optimizer exception restoration, resume idempotence, and no trigger for a partial final segment.

- [ ] **Step 2: Run and confirm supervisor/target-round tests fail**

Run: `pytest -q tests/test_prompt_self_improvement.py -k 'supervisor or segment or target_rounds'`

Expected: FAIL because no target override or supervisor exists.

- [ ] **Step 3: Add the target-round override without embedding Meta logic in `loop.py`**

Resolve `n_rounds = min(cfg["max_rounds"], target_rounds)` in normal proposer mode. Reject targets below one or above the overall configured target. Static-proposal mode remains incompatible with the supervisor.

- [ ] **Step 4: Implement the supervisor transaction**

Initialize/recover history, determine completed rounds from `history.jsonl`, run each next interval target, mark inflight, invoke Meta, parse report, gate, then snapshot/no-change/restore. Catch agent/report/gate failures, record them, and continue with the parent prompt set.

- [ ] **Step 5: Run supervisor tests**

Run: `pytest -q tests/test_prompt_self_improvement.py -k 'supervisor or segment or target_rounds'`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add simpleloop/prompt_self_improvement/supervisor.py simpleloop/loop.py tests/test_prompt_self_improvement.py
git commit -m "feat: add prompt self-improvement supervisor"
```

### Task 6: CLI integration and end-to-end verification

**Files:**
- Modify: `simpleloop/cli.py`
- Modify: `README.md`
- Test: `tests/test_prompt_self_improvement.py`
- Test: `tests/test_config_execution.py`

**Interfaces:**
- Existing `simpleloop run` dispatches to the outer supervisor when resolved config has `prompt_self_improvement.enabled: true`; normal and static runs retain existing behavior.

- [ ] **Step 1: Write failing CLI dispatch tests**

```python
def test_run_dispatches_enabled_config_to_supervisor(monkeypatch, task_file, tmp_path):
    called = []
    monkeypatch.setattr(supervisor, "run", lambda *a, **k: called.append(k) or summary())
    cli.main(["run", "--config", str(task_file), "--run-dir", str(tmp_path / "run")])
    assert called
```

Cover disabled config dispatching to `loop.run`, and rejection of `--proposals` with enabled self-improvement.

- [ ] **Step 2: Run CLI tests and confirm enabled dispatch fails**

Run: `pytest -q tests/test_prompt_self_improvement.py -k cli`

Expected: FAIL because CLI always calls `loop.run`.

- [ ] **Step 3: Implement minimal CLI dispatch and README usage**

Load config once for dispatch, call supervisor only when enabled, preserve existing error formatting, and document the six-key YAML block plus generated directories.

- [ ] **Step 4: Run focused feature suite**

Run: `pytest -q tests/test_prompt_self_improvement.py tests/test_prompt_templates.py tests/test_parallel_candidates.py tests/test_views_and_parse.py tests/test_candidate_worker.py tests/test_config_execution.py`

Expected: PASS.

- [ ] **Step 5: Run full verification**

Run: `pytest -q`

Expected: all tests pass.

Run: `python -m compileall -q simpleloop`

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add simpleloop/cli.py README.md tests/test_prompt_self_improvement.py tests/test_config_execution.py
git commit -m "feat: enable unattended prompt self-improvement"
```
