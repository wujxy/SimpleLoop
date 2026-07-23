# Structured Free-Text Truncation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Proposer and Judger free-text overlength recoverable through visible deterministic `N+500` truncation while keeping structured-output validation strict.

**Architecture:** Provider-facing JSON Schemas validate shape but do not impose free-text `maxLength`. A shared helper in `agent.py` strips, measures, warns, and truncates role text; Proposer and Judger parsers call it after their existing type/nonblank checks.

**Tech Stack:** Python 3.9+, pytest, Claude Code JSON Schema structured output

## Global Constraints

- Prompt generation targets remain unchanged.
- The shared tolerance margin is exactly 500 characters.
- Text at or below `N+500` is returned unchanged without a warning.
- Text above `N+500` is warned once and truncated to `N+500`.
- Required keys, exact keys, types, nonblank strings, enums, score range, exact candidate count, and normalized family uniqueness remain hard constraints.
- No model repair call, additional retry layer, scheduling change, persistence change, or prompt semantic change.

---

### Task 1: Shared Free-Text Normalizer

**Files:**
- Modify: `simpleloop/agent.py`
- Test: `simpleloop/tests/test_views_and_parse.py`

**Interfaces:**
- Consumes: a validated string, integer limit, role label, and field path.
- Produces: `normalize_free_text(value: str, *, limit: int, label: str, field: str) -> str`.

- [ ] **Step 1: Write focused failing helper tests**

Add the import and tests:

```python
from simpleloop.agent import normalize_free_text


def test_normalize_free_text_accepts_limit_without_warning(capsys):
    value = "x" * 1100
    assert normalize_free_text(
        value, limit=1100, label="proposer", field="reflection"
    ) == value
    assert capsys.readouterr().out == ""


def test_normalize_free_text_warns_and_truncates_after_strip(capsys):
    value = "  " + ("x" * 1101) + "  "
    assert normalize_free_text(
        value, limit=1100, label="proposer", field="reflection"
    ) == "x" * 1100
    assert capsys.readouterr().out == (
        "[proposer] warning: reflection length 1101 exceeds 1100; "
        "truncated to 1100\n"
    )
```

- [ ] **Step 2: Run the helper tests and verify failure**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py::test_normalize_free_text_accepts_limit_without_warning \
  simpleloop/tests/test_views_and_parse.py::test_normalize_free_text_warns_and_truncates_after_strip \
  -v
```

Expected: collection fails because `normalize_free_text` is not importable.

- [ ] **Step 3: Implement the minimal shared helper**

Add to `simpleloop/agent.py` above `Agent`:

```python
def normalize_free_text(
    value: str,
    *,
    limit: int,
    label: str,
    field: str,
) -> str:
    """Strip free text and visibly truncate it at the local tolerance limit."""
    normalized = value.strip()
    length = len(normalized)
    if length > limit:
        print(
            f"[{label}] warning: {field} length {length} exceeds {limit}; "
            f"truncated to {limit}",
            flush=True,
        )
        return normalized[:limit]
    return normalized
```

- [ ] **Step 4: Run the helper tests and verify success**

Run the Step 2 command.

Expected: 2 tests pass.

- [ ] **Step 5: Commit the helper**

```bash
git add simpleloop/agent.py simpleloop/tests/test_views_and_parse.py
git commit -m "feat: add visible free-text truncation"
```

### Task 2: Proposer Structural Schema and N+500 Parsing

**Files:**
- Modify: `simpleloop/proposer.py`
- Test: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: `normalize_free_text` from Task 1.
- Produces: `_proposer_schema(k)` without free-text `maxLength`; `_parse_batch(...)` with N+500 normalization and the fixed `proposer` log label.

- [ ] **Step 1: Replace proposer length tests with the new contract**

Update the schema test to assert that `maxLength` is absent while structural
constraints remain. Add boundary, failed-run regression, all-field, and
post-truncation family-collision tests:

```python
def test_proposer_schema_keeps_structure_without_text_max_lengths():
    schema = _proposer_schema(3)
    proposals = schema["properties"]["proposals"]
    assert proposals["minItems"] == proposals["maxItems"] == 3
    assert schema["required"] == [
        "reflection", "insight", "insight_refs", "proposals",
    ]
    assert schema["additionalProperties"] is False
    assert "maxLength" not in schema["properties"]["reflection"]
    assert "maxLength" not in schema["properties"]["insight"]
    assert "maxLength" not in schema["properties"]["insight_refs"]["items"]
    item_properties = proposals["items"]["properties"]
    assert "maxLength" not in item_properties["family"]
    assert "maxLength" not in item_properties["proposal"]
    assert item_properties["proposal"]["pattern"] == r"\S"
    assert item_properties["decision"]["enum"] == ["continue", "switch"]


def test_parse_batch_accepts_n_plus_500_without_warning(capsys):
    data = {
        "reflection": "r" * 1100,
        "insight": "i" * 1000,
        "insight_refs": ["x" * 532],
        "proposals": [{
            "family": "f" * 564,
            "decision": "switch",
            "proposal": "p" * 1300,
        }],
    }
    batch = _parse_batch(data, candidates_per_round=1)
    assert len(batch.reflection) == 1100
    assert len(batch.insight) == 1000
    assert len(batch.insight_refs[0]) == 532
    assert len(batch.proposals[0].family) == 564
    assert len(batch.proposals[0].proposal) == 1300
    assert capsys.readouterr().out == ""


def test_parse_batch_warns_and_truncates_every_free_text_field(capsys):
    data = {
        "reflection": "r" * 1131,
        "insight": "i" * 1001,
        "insight_refs": ["x" * 533],
        "proposals": [{
            "family": "f" * 565,
            "decision": "continue",
            "proposal": "p" * 1301,
        }],
    }
    batch = _parse_batch(data, candidates_per_round=1)
    assert len(batch.reflection) == 1100
    assert len(batch.insight) == 1000
    assert len(batch.insight_refs[0]) == 532
    assert len(batch.proposals[0].family) == 564
    assert len(batch.proposals[0].proposal) == 1300
    output = capsys.readouterr().out
    assert "reflection length 1131 exceeds 1100" in output
    assert "insight length 1001 exceeds 1000" in output
    assert "insight_refs[0] length 533 exceeds 532" in output
    assert "proposals[0].family length 565 exceeds 564" in output
    assert "proposals[0].proposal length 1301 exceeds 1300" in output


def test_parse_batch_rejects_families_equal_after_truncation():
    prefix = "x" * 564
    with pytest.raises(ValueError, match="duplicate family"):
        _parse_batch({
            "reflection": "r",
            "insight": "",
            "insight_refs": [],
            "proposals": [
                {"family": prefix + "a", "decision": "switch", "proposal": "p0"},
                {"family": prefix + "b", "decision": "continue", "proposal": "p1"},
            ],
        }, candidates_per_round=2)
```

- [ ] **Step 2: Run the proposer tests and verify failure**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py -q
```

Expected: failures show old `maxLength` declarations, old N+300 boundaries, and
missing warnings.

- [ ] **Step 3: Implement proposer schema and parser changes**

In `simpleloop/proposer.py`:

```python
from .agent import Agent, normalize_free_text

_STRUCTURED_TEXT_MARGIN = 500
```

Remove every free-text `maxLength` member from `_proposer_schema`, retaining
types, `minLength`, `pattern`, required keys, enums, and exact item count.

Replace every string slice in `_parse_batch` with:

```python
reflection = normalize_free_text(
    reflection,
    limit=_REFLECTION_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
    label="proposer",
    field="reflection",
)
insight = normalize_free_text(
    insight,
    limit=_INSIGHT_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
    label="proposer",
    field="insight",
)
insight_refs = [
    normalize_free_text(
        ref,
        limit=_INSIGHT_REF_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
        label="proposer",
        field=f"insight_refs[{i}]",
    )
    for i, ref in enumerate(raw_insight_refs)
]
```

Within the proposal loop normalize `family` and `proposal` using field paths
`proposals[{i}].family` and `proposals[{i}].proposal` and limits 564 and 1300
respectively. Keep family case-folding and duplicate detection after
normalization.

- [ ] **Step 4: Run proposer tests and verify success**

Run:

```bash
python -m pytest simpleloop/tests/test_parallel_candidates.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit proposer behavior**

```bash
git add simpleloop/proposer.py simpleloop/tests/test_parallel_candidates.py
git commit -m "fix: truncate overlong proposer text locally"
```

### Task 3: Judger Structural Schema and Candidate-Labeled N+500 Parsing

**Files:**
- Modify: `simpleloop/judger.py`
- Test: `simpleloop/tests/test_views_and_parse.py`

**Interfaces:**
- Consumes: `normalize_free_text` from Task 1 and the existing `judge(..., label=...)` argument.
- Produces: `_judger_schema()` without free-text `maxLength`; `_parse(data, *, label="judger")` with labeled N+500 normalization.

- [ ] **Step 1: Write the new Judger schema and parser tests**

Replace the old margin assertions and truncation test with:

```python
def test_judger_schema_keeps_structure_without_text_max_lengths():
    schema = _judger_schema()
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "score", "risk", "feedback", "feedback_for_proposer",
    ]
    assert schema["properties"]["score"] == {
        "type": "number", "minimum": 0.0, "maximum": 1.0,
    }
    assert schema["properties"]["risk"]["enum"] == ["low", "medium", "high"]
    assert "maxLength" not in schema["properties"]["feedback"]
    assert "maxLength" not in schema["properties"]["feedback_for_proposer"]
    assert schema["properties"]["feedback"]["pattern"] == r"\S"
    assert schema["properties"]["feedback_for_proposer"]["pattern"] == r"\S"


def test_parse_accepts_judger_n_plus_500_without_warning(capsys):
    judgment = _parse({
        "score": 0.5,
        "risk": "low",
        "feedback": "f" * 1000,
        "feedback_for_proposer": "p" * 800,
    }, label="judger r1-c0")
    assert len(judgment.feedback) == 1000
    assert len(judgment.feedback_for_proposer) == 800
    assert capsys.readouterr().out == ""


def test_parse_warns_and_truncates_judger_text_with_candidate_label(capsys):
    judgment = _parse({
        "score": 0.5,
        "risk": "low",
        "feedback": "f" * 1001,
        "feedback_for_proposer": "p" * 801,
    }, label="judger r14-c2")
    assert len(judgment.feedback) == 1000
    assert len(judgment.feedback_for_proposer) == 800
    assert capsys.readouterr().out == (
        "[judger r14-c2] warning: feedback length 1001 exceeds 1000; "
        "truncated to 1000\n"
        "[judger r14-c2] warning: feedback_for_proposer length 801 exceeds 800; "
        "truncated to 800\n"
    )
```

Update the capturing-agent test to return `feedback = "x" * 1001`, then assert
the result is 1000 characters and the warning uses `judger r1-c0`.

- [ ] **Step 2: Run Judger-focused tests and verify failure**

Run:

```bash
python -m pytest \
  simpleloop/tests/test_views_and_parse.py::test_judger_schema_keeps_structure_without_text_max_lengths \
  simpleloop/tests/test_views_and_parse.py::test_parse_accepts_judger_n_plus_500_without_warning \
  simpleloop/tests/test_views_and_parse.py::test_parse_warns_and_truncates_judger_text_with_candidate_label \
  simpleloop/tests/test_views_and_parse.py::test_judge_passes_schema_and_custom_label \
  -v
```

Expected: failures show old schema limits, old truncation thresholds, and an
unsupported parser label.

- [ ] **Step 3: Implement Judger schema and parser changes**

In `simpleloop/judger.py`:

```python
from .agent import Agent, normalize_free_text

_STRUCTURED_TEXT_MARGIN = 500
```

Remove `maxLength` from `feedback` and `feedback_for_proposer` schema
properties. Change the parser signature and normalization:

```python
def _parse(data: dict, *, label: str = "judger") -> Judgment:
    # retain all existing structure/type/range/nonblank validation
    return Judgment(
        score=score,
        risk=risk,
        feedback=normalize_free_text(
            feedback,
            limit=_FEEDBACK_GENERATION_LIMIT + _STRUCTURED_TEXT_MARGIN,
            label=label,
            field="feedback",
        ),
        feedback_for_proposer=normalize_free_text(
            feedback_for_proposer,
            limit=(
                _FEEDBACK_FOR_PROPOSER_GENERATION_LIMIT
                + _STRUCTURED_TEXT_MARGIN
            ),
            label=label,
            field="feedback_for_proposer",
        ),
    )
```

Change `judge()` to return `_parse(data, label=label)` so parallel candidates
retain their qualified warning label.

- [ ] **Step 4: Run Judger and full unit tests**

Run:

```bash
python -m pytest simpleloop/tests/test_views_and_parse.py -q
python -m pytest simpleloop/tests/ -q
```

Expected: both commands pass with no failures.

- [ ] **Step 5: Commit Judger behavior**

```bash
git add simpleloop/judger.py simpleloop/tests/test_views_and_parse.py
git commit -m "fix: truncate overlong judger text locally"
```

### Task 4: Final Regression and Documentation Consistency

**Files:**
- Modify only if required by verification: `README.md`
- Verify: `docs/superpowers/specs/2026-07-23-structured-free-text-truncation-design.md`

**Interfaces:**
- Consumes: completed Tasks 1–3.
- Produces: verified SimpleLoop behavior with no stale N+300 implementation assertions.

- [ ] **Step 1: Search for stale implementation expectations**

Run:

```bash
rg -n '_STRUCTURED_TEXT_MARGIN = 300|maxLength.*feedback|maxLength.*proposal|N\\+300' \
  simpleloop README.md
```

Expected: no active implementation or test assertion still describes the old
N+300 policy. Historical design documents may retain historical N+300 text.

- [ ] **Step 2: Run the complete unit suite**

Run:

```bash
python -m pytest simpleloop/tests/ -q
```

Expected: all tests pass.

- [ ] **Step 3: Check the final diff**

Run:

```bash
git diff --check
git status --short
```

Expected: `git diff --check` prints nothing; status contains only intentional
implementation and test changes, or is clean if Task 3 committed everything.

- [ ] **Step 4: Commit any verification-only correction**

Only if Step 1 or Step 3 required a correction:

```bash
git add README.md simpleloop
git commit -m "docs: align structured text tolerance"
```

If no correction was required, do not create an empty commit.
