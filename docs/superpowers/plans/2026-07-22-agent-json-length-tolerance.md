# Agent JSON Length Tolerance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep agent generation lengths strict in JSON Schema while truncating overlong free text at N+300 and exposing candidate-specific parallel failures.

**Architecture:** Make local changes in the proposer and judger parsers rather than introducing a shared policy layer. Keep all structural validation hard, add the missing judger Schema, and pass candidate-specific labels through the existing judge call.

**Tech Stack:** Python 3, pytest, Claude Code JSON Schema structured output.

## Global Constraints

- N+300 applies only to `reflection`, `proposal`, and `feedback`.
- Schema generation targets remain 600, 800, and 500 characters respectively.
- Runtime caps are 900, 1100, and 800 characters respectively.
- Structural violations remain hard failures.
- Executor remains text/edit based and has no JSON Schema.
- Do not add retries or a shared policy abstraction.

---

### Task 1: Proposer free-text tolerance

**Files:**
- Modify: `simpleloop/proposer.py`
- Test: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: `_proposer_schema(candidates_per_round: int) -> dict`
- Produces: `_parse_batch(...)` results whose free text is capped at N+300.

- [ ] Add tests asserting `reflection` is capped at 900, `proposal` at 1100, Schema generation limits remain 600/800, and non-blank/unique-family requirements are visible.
- [ ] Run the focused tests and confirm they fail because `_parse_batch` currently preserves overlong text.
- [ ] Add direct string slicing after type/non-empty validation and update the proposer prompt/Schema patterns without changing structural validation.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Judger Schema and tolerant feedback

**Files:**
- Modify: `simpleloop/judger.py`
- Test: `simpleloop/tests/test_views_and_parse.py`

**Interfaces:**
- Produces: `_judger_schema() -> dict` with exact keys, score range, risk enum, and `feedback.maxLength = 500`.
- Produces: `_parse(data: dict) -> Judgment` that caps feedback at 800.
- Changes: `judge(..., label: str = "judger") -> Judgment` forwards Schema and label to `Agent.run_json`.

- [ ] Add tests for Schema shape, Schema delivery, feedback accepted through 800, feedback over 800 truncated, and invalid structure still rejected.
- [ ] Run focused tests and confirm failure because no judger Schema exists and overlong feedback is rejected.
- [ ] Add `_judger_schema`, pass it to `run_json`, accept an optional label, and replace the >600 rejection with `feedback[:800]`.
- [ ] Re-run focused tests and confirm they pass.

### Task 3: Candidate-specific logging

**Files:**
- Modify: `simpleloop/loop.py`
- Test: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: `judger.judge(..., label=...)` from Task 2.
- Produces: labels such as `judger r1-c0` and a visible candidate failure line.

- [ ] Add a focused candidate-worker test capturing the judger label and failure output.
- [ ] Run it and confirm failure because the current label is generic and the exception branch is silent.
- [ ] Pass `label=f"judger r{round_id}-c{candidate_id}"` and print the caught candidate-local exception before recording `_candidate_failure`.
- [ ] Re-run the focused test and confirm it passes.

### Task 4: Verification

**Files:**
- Verify all files above.

- [ ] Run `python -m pytest simpleloop/tests/test_views_and_parse.py simpleloop/tests/test_parallel_candidates.py -q` and confirm zero failures.
- [ ] Run `python -m pytest simpleloop/tests/ -q` and confirm zero failures.
- [ ] Run `git diff --check` and inspect `git diff --stat` and `git status --short`.
