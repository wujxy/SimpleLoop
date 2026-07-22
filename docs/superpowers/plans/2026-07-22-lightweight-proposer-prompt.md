# Lightweight Proposer Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the proposer prompt with the approved lightweight, batch-oriented hypothesis-generation contract without changing its JSON interface.

**Architecture:** Keep proposal parsing and schemas unchanged. Add a rendered-prompt regression test around the existing `propose()` boundary, then replace only the prompt text so responsibility boundaries, batch reflection semantics, and the natural handoff to the executor are explicit.

**Tech Stack:** Python, pytest, existing `simpleloop.proposer` API.

## Global Constraints

- Preserve the exact configured candidate count and current JSON schema.
- Keep visible limits of 600 characters for `reflection`, 64 for `family`, and 800 for `proposal`.
- Do not add tool-count, time, phase, or source-line budgets.
- Do not alter parsing, truncation, executor, or judger behavior.
- Preserve unrelated working-tree changes.

---

### Task 1: Render the lightweight proposer role prompt

**Files:**
- Modify: `simpleloop/proposer.py`
- Test: `simpleloop/tests/test_parallel_candidates.py`

**Interfaces:**
- Consumes: existing `propose(agent, goal, editable, frozen, history, base_sha, cwd, candidates_per_round)` inputs and `agent.run_json(prompt, ..., json_schema=...)` boundary.
- Produces: the same `ProposalBatch` and schema, with revised prompt semantics only.

- [x] **Step 1: Write the failing rendered-prompt test** with assertions for role boundaries, reasoning chain, batch-level reflection, natural handoff, source grounding, limits, and removal of old over-exploration triggers.
- [x] **Step 2: Run the focused test and verify it fails because the current prompt lacks the new semantics.**
- [x] **Step 3: Replace only the prompt text in `propose()`, leaving schema and parser behavior unchanged.**
- [x] **Step 4: Run the focused test and verify it passes.**
- [x] **Step 5: Run the proposer test module and full `simpleloop/tests` suite.**
- [x] **Step 6: Review the scoped diff and commit only the four planned files.**
