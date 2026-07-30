# Current-Version Test Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove tests that do not protect the latest SimpleLoop version, verify the retained suite, then integrate the verified `ihep_scale` state into `v0.1.1` and `rsi-prompt`.

**Architecture:** Make deletion-only edits to the test suite according to the approved audit. Keep production code and configuration unchanged, verify collected test identities and the full root suite, then perform Git integration only after tests pass.

**Tech Stack:** Python 3.11, pytest, Git

## Global Constraints

- Do not modify `.env`, secrets, or production configuration.
- Do not modify production Python code.
- Retain focused tests for distinct current-version behavior and failure modes.
- Remove only the tests listed in the approved design.
- Run `python -m pytest -q tests/` before Git integration.

---

### Task 1: Remove obsolete and invalid tests

**Files:**
- Delete: `tests/test_accidental_overwrite_protection.py`
- Modify: `tests/test_agent_usage.py`
- Modify: `tests/test_example_apptainer.py`
- Modify: `tests/test_plot.py`
- Modify: `tests/test_static_mode.py`
- Modify: `tests/test_telemetry.py`
- Modify: `tests/test_views_and_parse.py`

**Interfaces:**
- Consumes: the exact deletion list in `docs/superpowers/specs/2026-07-31-current-version-test-audit-design.md`
- Produces: a test suite containing only retained current-version contracts

- [ ] **Step 1: Delete the invalid overwrite test file**

Delete `tests/test_accidental_overwrite_protection.py`; do not replace it or
change `simpleloop/loop.py`.

- [ ] **Step 2: Delete explicitly obsolete test functions**

Remove the exact compatibility, site-specific, obsolete-wiring, and duplicate
functions listed in the approved design. Remove imports or constants only when
they become unused because of those deletions.

- [ ] **Step 3: Check the deletion diff**

Run:

```bash
git diff --check
git diff --stat
git diff -- tests/
```

Expected: no whitespace errors; no production or configuration changes; only
the approved test file/functions are deleted.

### Task 2: Verify the retained suite

**Files:**
- Test: `tests/`

**Interfaces:**
- Consumes: cleaned test suite from Task 1
- Produces: fresh collection and passing-suite evidence

- [ ] **Step 1: Collect tests**

Run:

```bash
python -m pytest --collect-only -q tests/
```

Expected: collection succeeds, none of the deleted test names appear, and
unrelated test modules remain collected.

- [ ] **Step 2: Run the complete root suite**

Run:

```bash
python -m pytest -q tests/
```

Expected: all retained tests pass.

- [ ] **Step 3: Commit the audit**

Run:

```bash
git add docs/superpowers/plans/2026-07-31-current-version-test-audit.md tests
git commit -m "test: remove obsolete version-specific coverage"
```

Expected: one commit containing the implementation plan and approved test
deletions.

### Task 3: Update the tag and merge the verified branch

**Files:**
- Modify Git ref: `refs/tags/v0.1.1`
- Modify Git branch: `refs/heads/rsi-prompt`

**Interfaces:**
- Consumes: verified `ihep_scale` tip
- Produces: `v0.1.1` pointing at that tip and `rsi-prompt` containing it

- [ ] **Step 1: Move the annotated tag**

Resolve the verified `ihep_scale` tip, recreate annotated tag `v0.1.1` at that
exact commit, and verify `v0.1.1^{}` resolves to it.

- [ ] **Step 2: Merge into `rsi-prompt`**

Switch to `rsi-prompt`, preserve its existing local commit, and merge
`ihep_scale` without rebasing or rewriting branch history.

- [ ] **Step 3: Verify the merged result**

Run:

```bash
python -m pytest -q tests/
git status --short --branch
git log --oneline --decorate --graph -12
git merge-base --is-ancestor ihep_scale rsi-prompt
```

Expected: tests pass, worktree is clean, and `ihep_scale` is an ancestor of
`rsi-prompt`.

- [ ] **Step 4: Push when authentication permits**

Push `ihep_scale`, `rsi-prompt`, and the moved `v0.1.1` tag. Force is permitted
only for the explicitly requested tag replacement; branch pushes must be
fast-forward or normal merge pushes.

Expected: remote refs resolve to the locally verified commits. If GitHub
authentication is unavailable, report the exact local state and the remaining
push commands without claiming remote completion.
