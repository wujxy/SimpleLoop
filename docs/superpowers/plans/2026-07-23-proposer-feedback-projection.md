# Proposer Feedback Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve complete Judger diagnostics in history and reports while exposing only concise `feedback_for_proposer` prose through recent-history and Search Memory paths.

**Architecture:** Extend the existing `Judgment` value with one required string and persist both feedback forms in serial and parallel history. Keep complete `feedback` as the audit/report source; project only `feedback_for_proposer` through `views.for_proposer()`, `proposer.propose()`, and `memory.resolve_episode()`. Legacy records expose an empty short field and never fall back to complete feedback.

**Tech Stack:** Python 3.9+, dataclasses, JSON Schema, append-only JSONL, pytest.

## Global Constraints

- Work on branch `v0.0.4`.
- Keep `feedback_for_proposer` as free prose; do not add mechanism states, confidence, retry actions, or a new model call.
- New Judger responses require a nonblank `feedback_for_proposer`.
- Complete `feedback` remains unchanged and continues to drive `LANDING_STATE` extraction and final reports.
- No Proposer-visible path may expose complete `feedback`.
- Old history never falls back to complete feedback; its short field is `""`.
- Do not change Insight schema, CLI command shape, selection, metrics, gates, or the six-round recent window.
- Preserve unrelated working-tree changes.

---

### Task 1: Extend the Judger Contract

**Files:**
- Modify: `simpleloop/judger.py`
- Test: `simpleloop/tests/test_views_and_parse.py`

**Interfaces:**
- Consumes: the existing `judge(...) -> Judgment` call boundary.
- Produces: `Judgment(score: float, risk: str, feedback: str, feedback_for_proposer: str)` and a four-key structured-output contract.

- [ ] **Step 1: Add failing schema, parser, and prompt tests**

Update Judgment fixtures and add focused assertions equivalent to:

```python
def test_parse_preserves_both_feedback_fields():
    judgment = _parse({
        "score": 0.5,
        "risk": "low",
        "feedback": "LANDED_STATE: not-implemented\nImplemented: x\nResult: y\nAnalysis: z",
        "feedback_for_proposer": "The mechanism remains inconclusive.",
    })
    assert judgment.feedback_for_proposer == "The mechanism remains inconclusive."


@pytest.mark.parametrize("value", [None, "", "   ", 7])
def test_parse_rejects_invalid_feedback_for_proposer(value):
    data = {
        "score": 0.5,
        "risk": "low",
        "feedback": "full",
        "feedback_for_proposer": value,
    }
    with pytest.raises(ValueError):
        _parse(data)


def test_judger_schema_requires_feedback_for_proposer():
    schema = _judger_schema()
    assert schema["required"] == [
        "score", "risk", "feedback", "feedback_for_proposer"
    ]
    assert schema["properties"]["feedback_for_proposer"]["pattern"] == r"\S"
```

Extend the rendered-prompt test to assert the approved role-oriented wording
and the four-key JSON example.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```bash
python -m pytest simpleloop/tests/test_views_and_parse.py -k \
  "feedback_for_proposer or judger_schema or parse_preserves_both" -q
```

Expected: failures because `Judgment`, `_judger_schema()`, `_parse()`, and the
prompt still know only `feedback`.

- [ ] **Step 3: Implement the minimal Judger extension**

In `simpleloop/judger.py`:

```python
_FEEDBACK_FOR_PROPOSER_GENERATION_LIMIT = 300


@dataclass
class Judgment:
    score: float
    risk: str
    feedback: str
    feedback_for_proposer: str
```

Add a required nonblank Schema property with the existing structured-text
headroom:

```python
"feedback_for_proposer": {
    "type": "string",
    "minLength": 1,
    "maxLength": (
        _FEEDBACK_FOR_PROPOSER_GENERATION_LIMIT
        + _STRUCTURED_TEXT_MARGIN
    ),
    "pattern": r"\S",
},
```

Add the approved prompt guidance:

```text
`feedback_for_proposer` is a short search-context note for later proposal
generation. In one or two concise sentences, capture the smallest reusable
lesson about the attempted mechanism. When the evidence permits, distinguish
what the result says about the mechanism from what may be specific to this
implementation. Keep implementation diagnosis in `feedback`; this note does
not need to repeat metrics already provided by the harness.
```

Require exactly four parsed keys, validate the short field as a nonblank
string, and retain it independently with the same `N+300` tolerance convention.

- [ ] **Step 4: Run the Judger-focused tests**

Run:

```bash
python -m pytest simpleloop/tests/test_views_and_parse.py -k \
  "parse or schema or feedback or judger" -q
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add simpleloop/judger.py simpleloop/tests/test_views_and_parse.py
git commit -m "feat: add proposer-facing judger feedback"
```

---

### Task 2: Persist Both Feedback Forms

**Files:**
- Modify: `simpleloop/store.py`
- Modify: `simpleloop/loop.py`
- Test: `simpleloop/tests/test_views_and_parse.py`
- Test: `simpleloop/tests/test_parallel_candidates.py`
- Test: `simpleloop/tests/test_plot.py`

**Interfaces:**
- Consumes: `Judgment.feedback_for_proposer` from Task 1.
- Produces: serial and parallel history records containing both `feedback` and `feedback_for_proposer`.

- [ ] **Step 1: Add failing serial, parallel, and failure-record tests**

Extend `Store.append(...)` tests to pass:

```python
feedback_for_proposer="mechanism-level lesson"
```

and assert both persisted fields.

Extend parallel fake judgments:

```python
return Judgment(
    score=0.5,
    risk="low",
    feedback="full technical diagnosis",
    feedback_for_proposer="short search lesson",
)
```

Assert each normalized candidate contains both strings. Add assertions that
`_record_failure()` and `_candidate_failure()` produce a nonempty
`feedback_for_proposer`.

- [ ] **Step 2: Run focused persistence tests and verify failure**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py \
  simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_plot.py \
  -k "feedback or record_failure or candidate_failure" -q
```

Expected: failures because store and loop records do not carry the short field.

- [ ] **Step 3: Implement serial persistence**

Extend the store signature:

```python
def append(
    self,
    round_id: int,
    proposal: str,
    sha: str | None,
    score: float | None,
    feedback: str,
    feedback_for_proposer: str = "",
    ...
) -> None:
```

Persist `"feedback_for_proposer": feedback_for_proposer` without altering the
existing `"feedback"` field. Pass `judgment.feedback_for_proposer` from the
serial loop call.

- [ ] **Step 4: Implement parallel and failure persistence**

In `Store.append_generation()`, copy each candidate's short field and expose
the selected candidate's short field at the generation top level.

In `_run_one_candidate()`, include:

```python
"feedback_for_proposer": judgment.feedback_for_proposer,
```

In `_record_failure()` and `_candidate_failure()`, use the existing concise
loop-failure message for both fields because no complete Judger diagnosis
exists.

- [ ] **Step 5: Run persistence-focused tests**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py \
  simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_plot.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```bash
git add simpleloop/store.py simpleloop/loop.py \
  simpleloop/tests/test_views_and_parse.py \
  simpleloop/tests/test_parallel_candidates.py \
  simpleloop/tests/test_plot.py
git commit -m "feat: persist proposer feedback separately"
```

---

### Task 3: Isolate the Recent Proposer View

**Files:**
- Modify: `simpleloop/views.py`
- Modify: `simpleloop/proposer.py`
- Test: `simpleloop/tests/test_views_and_parse.py`
- Test: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: history records containing both feedback fields.
- Produces: a six-round Proposer projection and prompt containing only `feedback_for_proposer`.

- [ ] **Step 1: Add failing projection and rendered-prompt tests**

Use distinct sentinels:

```python
candidate = {
    "feedback": "FULL_TECHNICAL_SENTINEL",
    "feedback_for_proposer": "SHORT_SEARCH_SENTINEL",
    ...
}
```

Assert:

```python
projected = views.for_proposer([record])
visible = projected[0]["candidates"][0]
assert visible["feedback_for_proposer"] == "SHORT_SEARCH_SENTINEL"
assert "feedback" not in visible
```

Render a Proposer prompt and assert:

```python
assert 'feedback_for_proposer="SHORT_SEARCH_SENTINEL"' in agent.prompt
assert "FULL_TECHNICAL_SENTINEL" not in agent.prompt
```

Add a legacy record without the short field and assert the projected value is
`""`, not complete feedback.

- [ ] **Step 2: Run focused projection tests and verify failure**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py \
  simpleloop/tests/test_parallel_candidates.py \
  -k "proposer and feedback" -q
```

Expected: failures because the view and prompt still render complete feedback.

- [ ] **Step 3: Implement the projection boundary**

In serial and parallel branches of `views.for_proposer()`:

```python
"feedback_for_proposer": c.get("feedback_for_proposer") or "",
```

Do not project a `feedback` key. Continue deriving `landing_state` from the
stored complete feedback internally.

Update module comments and docstrings to distinguish complete audit feedback
from concise Proposer feedback.

- [ ] **Step 4: Render only the short field**

In `proposer.propose()`, change history rendering to:

```python
f'feedback_for_proposer="{c.get("feedback_for_proposer", "")}" | '
```

Apply the equivalent serial-record change. Update comments that still describe
complete feedback as Proposer context.

- [ ] **Step 5: Run recent-view tests**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py \
  simpleloop/tests/test_parallel_candidates.py -q
```

Expected: PASS and no full-feedback sentinel in the rendered prompt.

- [ ] **Step 6: Commit Task 3**

```bash
git add simpleloop/views.py simpleloop/proposer.py \
  simpleloop/tests/test_views_and_parse.py \
  simpleloop/tests/test_parallel_candidates.py
git commit -m "feat: isolate proposer feedback context"
```

---

### Task 4: Isolate Search Memory Lookup and Verify the System

**Files:**
- Modify: `simpleloop/memory.py`
- Test: `simpleloop/tests/test_memory.py`
- Test: `simpleloop/tests/test_views_and_parse.py`
- Test: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: old or new candidate records resolved by `resolve_episode(history, ref)`.
- Produces: episode JSON with `feedback_for_proposer` and without complete `feedback`.

- [ ] **Step 1: Add failing episode and CLI tests**

Update `_parallel_history()` so each candidate has distinct feedback sentinels.
Assert:

```python
episode = memory.resolve_episode(_parallel_history(), "r2c1")
assert episode["feedback_for_proposer"] == "short layout lesson"
assert "feedback" not in episode
```

Add a legacy episode containing only complete feedback:

```python
episode = memory.resolve_episode(legacy_history, "r7c0")
assert episode["feedback_for_proposer"] == ""
assert "feedback" not in episode
```

Apply the same assertions to `simpleloop memory show`.

- [ ] **Step 2: Run memory tests and verify failure**

Run:

```bash
python -m pytest simpleloop/tests/test_memory.py -q
```

Expected: failures because `resolve_episode()` still returns complete feedback.

- [ ] **Step 3: Implement the compact episode projection**

Replace the episode feedback field with:

```python
"feedback_for_proposer":
    candidate.get("feedback_for_proposer") or "",
```

Do not return complete `feedback`. Update module and function docstrings to
state that historical lookup follows the same Proposer-facing boundary as
recent history.

- [ ] **Step 4: Run memory and cross-component tests**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_memory.py \
  simpleloop/tests/test_views_and_parse.py \
  simpleloop/tests/test_parallel_candidates.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the full SimpleLoop test suite**

Run:

```bash
python -m pytest simpleloop/tests -q
```

Expected: all tests pass.

- [ ] **Step 6: Review the scoped diff**

Run:

```bash
git diff --check
git status --short
git diff --stat HEAD~3..HEAD
```

Expected: no whitespace errors and only the planned implementation, tests, and
plan/spec files changed.

- [ ] **Step 7: Commit Task 4**

```bash
git add simpleloop/memory.py simpleloop/tests/test_memory.py
git commit -m "feat: limit memory lookup to proposer feedback"
```

