# Proposer Protocol Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover from at most two malformed Proposer action responses within the same step while asking HEPAI to return one JSON object.

**Architecture:** Keep `ChatModel` and the one-action Runtime contract unchanged. The HEPAI adapter adds JSON object mode; `ProposerAgent.run()` wraps only local action parsing in a bounded correction loop and continues to fail closed after repeated violations.

**Tech Stack:** Python 3.11+, HEPAI/OpenAI-compatible Chat Completions, pytest.

## Global Constraints

- Keep exactly one action per valid model response.
- Do not extract partial JSON, strip prose, infer an action, or add compatibility parsing.
- Allow two correction calls per step; only valid actions consume `max_steps`.
- Correction calls consume the existing deadline and report usage normally.
- Never print or persist rejected model-controlled text.
- Add no configuration field, generic retry framework, provider registry, or batch action protocol.

---

### Task 1: Request HEPAI JSON Object Mode

**Files:**
- Modify: `tests/test_model.py`
- Modify: `simpleloop/roles/model.py`

**Interfaces:**
- Consumes: `HepAIChatModel.complete(system, messages, timeout_seconds)`.
- Produces: the same `ModelReply`; the provider request additionally contains `response_format={"type": "json_object"}`.

- [ ] **Step 1: Strengthen the existing provider-request test**

Add the required field to the exact expected kwargs in `test_hepai_uses_nonstreaming_chat_completion_and_timeout`:

```python
"response_format": {"type": "json_object"},
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest -q tests/test_model.py::test_hepai_uses_nonstreaming_chat_completion_and_timeout`

Expected: FAIL because the actual call has no `response_format`.

- [ ] **Step 3: Add the minimal provider constraint**

In `HepAIChatModel.complete()`, add:

```python
response_format={"type": "json_object"},
```

to `client.chat.completions.create(...)` without changing its public interface.

- [ ] **Step 4: Run model tests and verify GREEN**

Run: `python -m pytest -q tests/test_model.py`

Expected: all model tests PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/roles/model.py tests/test_model.py
git commit -m "fix: request structured proposer responses"
```

### Task 2: Repair Malformed Actions Within One Step

**Files:**
- Modify: `tests/test_proposer_agent.py`
- Modify: `simpleloop/roles/proposer.py`

**Interfaces:**
- Consumes: `_parse_action(text, candidates_per_round)` and `ModelReply`.
- Produces: the existing `ProposerResult`; malformed local action responses receive at most two correction calls before `ProposerError`.

- [ ] **Step 1: Add a failing recovery test**

Add a test whose fake model returns an object plus trailing prose, then a valid research action, then a valid terminal action:

```python
def test_agent_repairs_protocol_without_consuming_a_step(
    tmp_path, monkeypatch, capsys,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    secret = '{"action":"search_history","query":"cache"} PRIVATE_TAIL'
    model = FakeModel([
        ModelReply(secret, usage={"t": 1}),
        _reply({"action": "search_history", "query": "cache"}, {"t": 2}),
        _reply({"action": "submit_proposals", "proposals": ["Try A"]}, {"t": 3}),
    ])

    result = _agent(model, max_steps=2).run(**_run_args(tmp_path))

    assert result.proposals == ["Try A"]
    assert result.usage == [{"t": 1}, {"t": 2}, {"t": 3}]
    assert [item[0]["action"] for item in FakeTools.instances[0].actions] == [
        "search_history",
    ]
    output = capsys.readouterr().out
    assert "protocol repair 1/2 reason=invalid_json" in output
    assert "PRIVATE_TAIL" not in output
```

- [ ] **Step 2: Add a failing fail-closed test**

Add a test with three malformed replies:

```python
def test_agent_fails_closed_after_two_protocol_repairs(
    tmp_path, monkeypatch,
):
    FakeTools.instances.clear()
    monkeypatch.setattr(proposer_mod, "ResearchTools", FakeTools)
    model = FakeModel([ModelReply("{} trailing") for _ in range(3)])

    with pytest.raises(ProposerError, match="after 2 repairs"):
        _agent(model).run(**_run_args(tmp_path))

    assert len(model.calls) == 3
    assert FakeTools.instances[0].actions == []
```

- [ ] **Step 3: Add a transport-boundary regression test**

Import `ModelError` and add a model that raises it from `complete()`. Assert
`ProposerAgent.run()` raises `ModelError` after exactly one call, proving the
protocol loop does not retry provider failures.

- [ ] **Step 4: Run the new tests and verify RED**

Run the three new tests explicitly with `python -m pytest -q`.

Expected: recovery and fail-closed tests FAIL because no correction loop exists;
the transport test already passes and records the unchanged boundary.

- [ ] **Step 5: Implement the bounded correction loop**

Add one module constant:

```python
_MAX_PROTOCOL_REPAIRS = 2
```

Within each existing outer step, attempt the model call and `_parse_action()` up
to three times. On `ProposerError` from parsing:

```python
reason = (
    "invalid_json"
    if isinstance(exc.__cause__, (TypeError, json.JSONDecodeError))
    else "invalid_action"
)
```

If repairs remain, print the fixed-category safe summary, append the rejected
assistant message and this compact user correction, and retry:

```python
{"role": "user", "content": (
    "Protocol correction required. Return exactly one JSON action object "
    "matching the Runtime contract, with no prose or additional JSON."
)}
```

Recompute the remaining deadline before every model call. If the third response
is invalid, raise `ProposerError("proposer action protocol failed after 2 repairs")`
from the validation error. Leave successful action execution unchanged.

- [ ] **Step 6: Run focused Proposer tests and verify GREEN**

Run: `python -m pytest -q tests/test_proposer_agent.py`

Expected: all Proposer tests PASS.

- [ ] **Step 7: Commit**

```bash
git add simpleloop/roles/proposer.py tests/test_proposer_agent.py
git commit -m "fix: repair malformed proposer actions"
```

### Task 3: Verify the Provider and Regression Suite

**Files:**
- No production changes expected.

**Interfaces:**
- Consumes: configured `HEPAI_API_KEY`, base URL, and `gpt-5.5`.
- Produces: verification evidence only.

- [ ] **Step 1: Run one minimal live JSON-object completion**

Use the configured HEPAI client to request a tiny response with
`response_format={"type":"json_object"}` and verify that the returned content
parses as exactly one dictionary. Do not print the key or full response.

- [ ] **Step 2: Run the relevant suite**

Run:

```bash
python -m pytest -q tests/test_model.py tests/test_proposer_agent.py tests/test_research_tools.py tests/test_runtime.py tests/test_parallel_candidates.py tests/test_static_mode.py
```

Expected: all selected tests PASS except any independently confirmed assertion
caused by the user's uncommitted `examples/tiny_algo_opt/task.yaml` values.

- [ ] **Step 3: Run the top-level project suite**

Run: `python -m pytest -q tests`

Expected: all tests PASS after excluding only a separately demonstrated failure
caused by the user's uncommitted task configuration, if it remains present.

- [ ] **Step 4: Review the diff for MVP scope**

Confirm there is no new config, batch action format, permissive parser, raw
response logging, generic retry abstraction, or unrelated change. Confirm
`examples/tiny_algo_opt/task.yaml` remains uncommitted.
