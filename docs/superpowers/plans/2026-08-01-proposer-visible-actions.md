# Proposer Visible Action Summaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show concise, bounded Proposer activity in the terminal and raise the default research budget from 20 to 50 steps.

**Architecture:** Keep `ProposerAgent.run()` as the sole owner of runtime output. Add private formatting helpers for action and observation metadata, print lifecycle lines synchronously with `flush=True`, and retain all existing model/tool behavior. Change only the central researcher default and the README's minimal configuration; explicit task budgets remain untouched.

**Tech Stack:** Python 3, pytest, existing `ChatModel` and `ResearchTools` abstractions.

## Global Constraints

- Do not print prompts, accumulated messages, raw model responses, tool-result bodies, Insight text, environment variables, or credentials.
- Normalize query and command previews to one line and cap them at 160 characters.
- Do not add a logger abstraction, trace file, configuration flag, dependency, or public API.
- Keep the outer Proposer -> Executor -> Harness/Gates topology and all authority boundaries unchanged.
- Set the default `researcher.max_steps` to 50; explicit YAML values remain explicit per-run budgets.
- Do not modify or stage `examples/tiny_algo_opt/task.yaml`.

---

### Task 1: Print safe Proposer action summaries

**Files:**
- Modify: `tests/test_proposer_agent.py`
- Modify: `simpleloop/roles/proposer.py`

**Interfaces:**
- Consumes: parsed action dictionaries from `_parse_action(...)` and observation dictionaries from `ResearchTools.execute(...)`.
- Produces: private `_action_summary(action: dict) -> str` and `_result_summary(action: dict, observation: dict) -> str`; no public API change.

- [ ] **Step 1: Write the failing behavior-output test**

Update `FakeTools.execute()` to return realistic bounded observation shapes, then add:

```python
def test_agent_prints_safe_action_summaries(tmp_path, monkeypatch, capsys):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    long_command = "git diff --stat\n" + ("x" * 180) + "HIDDEN_TAIL"
    model = FakeModel([
        _reply({
            "action": "run_research_command",
            "command": long_command,
            "cwd": "source",
        }),
        _reply({"action": "search_history", "query": "cache reuse"}),
        _reply({
            "action": "write_insight",
            "text": "PRIVATE INSIGHT BODY",
            "refs": ["r0c0"],
        }),
        _reply({
            "action": "submit_proposals",
            "proposals": ["Replace the cache layout."],
        }),
    ])

    _agent(model, max_steps=5).run(**_run_args(tmp_path))

    output = capsys.readouterr().out
    assert "[proposer] started max_steps=5" in output
    assert "[proposer step 1/5] thinking" in output
    assert "action=run_research_command cwd=source" in output
    assert "result=ok exit_code=0" in output
    assert "action=search_history" in output
    assert "matches=0" in output
    assert "action=write_insight refs=1" in output
    assert "action=submit_proposals count=1" in output
    assert "[proposer] finished steps=4 elapsed=" in output
    assert "TOOL_RESULT_BODY" not in output
    assert "PRIVATE INSIGHT BODY" not in output
    assert "HIDDEN_TAIL" not in output
```

Make the fake return command metadata with `output="TOOL_RESULT_BODY"`, a
history `result=[]`, and the existing pending Insight state.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python -m pytest -q tests/test_proposer_agent.py::test_agent_prints_safe_action_summaries
```

Expected: FAIL because `ProposerAgent.run()` currently prints no activity.

- [ ] **Step 3: Implement the minimal summaries**

In `simpleloop/roles/proposer.py`, add a 160-character one-line preview helper,
action-specific summary helper, and result helper. The loop implementation must
have this shape:

```python
started = time.monotonic()
deadline = started + self.timeout_seconds
print(f"[proposer] started max_steps={self.max_steps}", flush=True)

for _step in range(self.max_steps):
    step = _step + 1
    print(
        f"[proposer step {step}/{self.max_steps}] thinking",
        flush=True,
    )
    # Existing model call, usage handling, and parsing remain here.
    print(
        f"[proposer step {step}/{self.max_steps}] "
        f"{_action_summary(action)}",
        flush=True,
    )
    if action["action"] == "submit_proposals":
        print(
            f"[proposer] finished steps={step} "
            f"elapsed={time.monotonic() - started:.1f}s",
            flush=True,
        )
        return ProposerResult(...)
    observation = tools.execute(action, deadline=deadline)
    print(
        f"[proposer step {step}/{self.max_steps}] "
        f"{_result_summary(action, observation)}",
        flush=True,
    )
```

The helpers expose only:

```text
run_research_command: cwd, bounded command; ok/error, exit_code, output_chars, truncated, timed_out when true
search_history: bounded query; ok/error, match count
inspect_episode: ref; ok/error
write_insight: ref count; ok/error
submit_proposals: proposal count
```

- [ ] **Step 4: Run Proposer tests and verify GREEN**

Run:

```bash
python -m pytest -q tests/test_proposer_agent.py
```

Expected: all tests PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add simpleloop/roles/proposer.py tests/test_proposer_agent.py
git commit -m "feat: show proposer action summaries"
```

### Task 2: Raise the default research budget

**Files:**
- Modify: `tests/test_config_execution.py`
- Modify: `simpleloop/config.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `_RESEARCHER_DEFAULTS` through the existing config resolver.
- Produces: resolved `cfg["researcher"]["max_steps"] == 50` when the researcher block omits an explicit value.

- [ ] **Step 1: Change the default-value assertion to 50**

In `tests/test_config_execution.py::test_researcher_defaults`, change:

```python
"max_steps": 20,
```

to:

```python
"max_steps": 50,
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python -m pytest -q tests/test_config_execution.py::test_researcher_defaults
```

Expected: FAIL with actual value 20 versus expected value 50.

- [ ] **Step 3: Change the central default and README example**

In `simpleloop/config.py`, set:

```python
"max_steps": 50,
```

In the README minimal configuration, set:

```yaml
max_steps: 50
```

Do not bulk-edit explicit budgets in `examples/**/task*.yaml`.

- [ ] **Step 4: Run focused configuration tests and verify GREEN**

Run:

```bash
python -m pytest -q tests/test_config_execution.py
```

Expected: all tests PASS.

- [ ] **Step 5: Run the relevant combined suite**

Run:

```bash
python -m pytest -q tests/test_proposer_agent.py tests/test_config_execution.py tests/test_runtime.py tests/test_parallel_candidates.py
```

Expected: all tests PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add simpleloop/config.py tests/test_config_execution.py README.md
git commit -m "config: raise default proposer step budget"
```

### Task 3: Final verification and scope audit

**Files:**
- Verify only; no planned modifications.

**Interfaces:**
- Consumes: the two committed deliverables above.
- Produces: evidence that the branch is test-clean and the user's dirty task file was not included.

- [ ] **Step 1: Run the full test suite**

```bash
python -m pytest -q
```

Expected: all tests PASS.

- [ ] **Step 2: Audit branch scope**

```bash
git status --short
git diff HEAD~2..HEAD --stat
git diff HEAD~2..HEAD -- examples/tiny_algo_opt/task.yaml
```

Expected: the user's task YAML remains only as its pre-existing working-tree
modification, the last command prints no committed diff, and implementation
commits contain only the planned production, test, and README files.
