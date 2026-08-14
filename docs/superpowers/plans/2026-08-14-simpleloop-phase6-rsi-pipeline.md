# SimpleLoop Phase 6 RSI Vertical Migration Implementation Plan

**Status:** Approved for implementation (2026-08-14)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the monolithic RSI host with typed body/history/pipeline modules, an event-sourced active revision, and one Local/HEPJob worker path for review, edit, and viability.

**Architecture:** `rsi.pipeline.run_rsi` is a provider-free transaction over explicit reviewer, editor, body, viability, history, and checkpoint ports. `JsonlSelfHistoryStore` owns the canonical event stream and proposer-facing review projection; `GitSelfBodyStore` composes the existing Git workspace provider; scheduled adapters carry all model work through `WorkerJobs`.

**Tech Stack:** Python 3.9+, frozen dataclasses, Enum, Protocol, JSONL with fsync and atomic projections, GitWorkspaceProvider, WorkerJobs, Local/HEPJob schedulers, pytest.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-14-simpleloop-phase6-rsi-pipeline-design.md`.
- Preserve current RSI semantics: KEEP, CHANGE, default defer 8, viability by a minimal real proposer episode, adopt only when viable, and run-local proposer continuity.
- Keep proposer as an independent top-level package with zero `simpleloop` imports.
- Add no dependencies or user configuration fields.
- Do not implement tree search, lineage selection, old run-directory migration, or Phase-7 config cleanup.
- All self model work must use WorkerJobs and therefore work identically with LocalScheduler and HEPJobScheduler.
- The canonical active SHA and review commitment come only from `self/history.jsonl`; `reviews.jsonl` is a rebuildable read view and `state.json` is removed.
- Every production behavior begins with a failing test and ends with focused plus full verification.

---

### Task 1: Typed RSI Domain Contracts

**Files:**
- Create: `simpleloop/rsi/__init__.py`
- Create: `simpleloop/rsi/models.py`
- Create: `tests/test_rsi_models.py`

**Interfaces:**
- Produces: `SelfDecisionKind`, `SelfRevision`, `SelfChange`, `SelfDecision`, `SelfState`, `SelfEventKind`, `SelfEvent`, `SelfCandidate`, `SelfReviewRequest`, `SelfEditRequest`, `SelfEditResult`, `SelfCommitRequest`, `ViabilityRequest`, `ViabilityResult`, `RsiRequest`, and `RsiResult`.
- Produces: `SelfReviewer`, `SelfEditor`, `SelfBodyStore`, `SelfHistoryStore`, `ViabilityChecker`, and `RsiCheckpoint` protocols.

- [ ] **Step 1: Write failing model tests**

```python
def test_change_decision_requires_a_change():
    with pytest.raises(ValueError, match="change"):
        SelfDecision(SelfDecisionKind.CHANGE, "diagnosis")

def test_keep_decision_rejects_a_change():
    with pytest.raises(ValueError, match="KEEP"):
        SelfDecision(
            SelfDecisionKind.KEEP, "diagnosis",
            change=SelfChange("prompt", "intent", "instruction"),
        )

def test_rsi_result_exposes_loop_decision_string():
    result = RsiResult(4, SelfDecisionKind.KEEP)
    assert result.decision.value == "KEEP"
```

- [ ] **Step 2: Run the tests and verify missing imports fail**

Run: `python -m pytest -q tests/test_rsi_models.py`

Expected: FAIL because `simpleloop.rsi.models` does not exist.

- [ ] **Step 3: Implement the minimal immutable domain model**

Use frozen dataclasses and the six event kinds from the design. Validate only
invariants required by the pipeline: non-empty SHAs/event ids, CHANGE has a
change, KEEP has no change, positive explicit defer, and adopted events have a
candidate SHA. Keep serialization out of this module.

- [ ] **Step 4: Run model tests**

Run: `python -m pytest -q tests/test_rsi_models.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/rsi/__init__.py simpleloop/rsi/models.py tests/test_rsi_models.py
git commit -m "refactor: define typed rsi contracts"
```

---

### Task 2: Canonical Self Event History

**Files:**
- Create: `simpleloop/rsi/history.py`
- Create: `tests/test_rsi_history.py`

**Interfaces:**
- Consumes: Task 1 event/state values.
- Produces: `SelfHistoryConflictError`.
- Produces: `JsonlSelfHistoryStore(root: Path)` implementing `state`, `events`, `append`, `initialize`, `last_terminal_round`, and `review_view_path`.

- [ ] **Step 1: Write failing event-store tests**

```python
def test_events_are_idempotent_and_conflicting_ids_fail(tmp_path):
    store = JsonlSelfHistoryStore(tmp_path / "self")
    event = initialized("seed", first_review=4)
    store.append(event)
    store.append(event)
    assert store.events() == (event,)
    with pytest.raises(SelfHistoryConflictError):
        store.append(replace(event, active_sha="other"))

def test_projection_adopts_only_from_terminal_event(tmp_path):
    store = initialized_store(tmp_path, "s0")
    store.append(reviewed_change(3, "s0"))
    store.append(candidate_created(3, "s0", "s1"))
    assert store.state().active_sha == "s0"
    store.append(candidate_adopted(3, "s0", "s1", next_round=11))
    assert store.state() == SelfState("s1", 11, 3)

def test_review_view_is_rebuilt_from_terminal_events(tmp_path):
    store = initialized_store(tmp_path, "s0")
    store.append(reviewed_keep(4, "s0", next_round=9))
    store.review_view_path.unlink()
    assert json.loads(store.ensure_review_view().read_text().splitlines()[0])[
        "decision"
    ] == "KEEP"
```

Also cover rejection retaining active SHA, malformed/unknown events,
out-of-order parent conflicts, fsync, and an interrupted projection replace.

- [ ] **Step 2: Run the tests and verify failure**

Run: `python -m pytest -q tests/test_rsi_history.py`

Expected: FAIL because `rsi.history` does not exist.

- [ ] **Step 3: Implement codec, append, and projections**

Encode each event to a stable JSON object with `schema_version`, `event_id`,
`kind`, and typed payload fields. `append` compares existing event ids, appends
one line, flushes, calls `os.fsync`, and atomically rewrites `reviews.jsonl`.
`state` folds events and raises on impossible transitions rather than silently
repairing authority.

- [ ] **Step 4: Run history tests**

Run: `python -m pytest -q tests/test_rsi_history.py tests/test_rsi_models.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/rsi/history.py tests/test_rsi_history.py
git commit -m "refactor: make self history event sourced"
```

---

### Task 3: Git Self Body Store over WorkspaceProvider

**Files:**
- Create: `simpleloop/rsi/body.py`
- Create: `tests/test_rsi_body.py`
- Modify: `simpleloop/world/git.py`
- Modify: `tests/test_git_workspace_provider.py`

**Interfaces:**
- Consumes: `GitWorkspaceProvider`, `WorkspaceSpec`, `CommitRequest`, and Task 1 body values.
- Produces: `GitSelfBodyStore(root: Path, seed: Path)` with idempotent `initialize`, `prepare_candidate`, `commit_candidate`, `discard_candidate`, and `materialize`.
- Produces only the smallest generic workspace change needed for idempotent reuse of an existing deterministic worktree.

- [ ] **Step 1: Write failing body contract tests**

```python
def test_initialize_snapshots_only_proposer_and_materializes_s0(tmp_path, seed):
    bodies = GitSelfBodyStore(tmp_path / "self", seed)
    revision = bodies.initialize()
    assert (bodies.runtime_path / "proposer" / "__init__.py").is_file()
    assert bodies.materialize(revision.sha) == bodies.runtime_path

def test_candidate_commit_is_idempotent_after_crash(tmp_path, seed):
    bodies = initialized_bodies(tmp_path, seed)
    workspace = bodies.prepare_candidate(5, bodies.initial_revision.sha)
    edit_prompt(workspace.path)
    first = bodies.commit_candidate(workspace, commit_request(5))
    second = bodies.commit_candidate(workspace, commit_request(5))
    assert second == first

def test_candidate_can_start_from_non_active_parent(tmp_path, seed):
    bodies = initialized_bodies(tmp_path, seed)
    first = make_candidate(bodies, round_id=1, parent=bodies.initial_revision.sha)
    workspace = bodies.prepare_candidate(2, first.sha)
    assert workspace.base_sha == first.sha
```

Also cover no-change, wrong parent, stale deterministic worktree reuse,
discard safety, and materializing an unknown SHA.

- [ ] **Step 2: Run body tests and verify failure**

Run: `python -m pytest -q tests/test_rsi_body.py tests/test_git_workspace_provider.py`

Expected: FAIL because the body store and required idempotent workspace API do
not exist.

- [ ] **Step 3: Implement the minimum body adapter**

Initialization copies only the proposer tree, initializes local Git identity,
commits S0, and constructs `GitWorkspaceProvider` over `self/repo`. Candidate
workspaces use deterministic ids `self-<round>`. Commit maps workspace changes
through the provider and treats a clean workspace whose HEAD differs from the
parent as the already committed result. `materialize` checks out the requested
SHA as the runtime cache; it does not write history or decide adoption.

- [ ] **Step 4: Run provider/body tests**

Run: `python -m pytest -q tests/test_rsi_body.py tests/test_git_workspace_provider.py tests/test_world_contracts.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/rsi/body.py simpleloop/world/git.py \
  tests/test_rsi_body.py tests/test_git_workspace_provider.py
git commit -m "refactor: adapt self bodies to git workspaces"
```

---

### Task 4: Pure RSI Pipeline and Recovery Policy

**Files:**
- Create: `simpleloop/rsi/pipeline.py`
- Create: `tests/test_rsi_pipeline.py`
- Modify: `simpleloop/loop.py`
- Modify: `tests/test_loop_pipeline.py`

**Interfaces:**
- Consumes: Task 1 ports, Task 2 history behavior, Task 3 body behavior.
- Produces: `run_rsi(request, *, reviewer, editor, bodies, viability, history) -> RsiResult`.
- Produces: `RsiPipeline.prepare`, `RsiPipeline.due`, and `RsiPipeline.run` implementing the loop `RsiRunner` port.

- [ ] **Step 1: Write failing pure pipeline tests**

```python
def test_keep_appends_one_terminal_event_and_advances_commitment():
    result, history = execute(review=keep(defer=5), round_id=4)
    assert result.decision is SelfDecisionKind.KEEP
    assert history.state().next_review_round == 9

def test_change_edits_commits_checks_and_adopts():
    result, history = execute(review=change(), edit=edited(), viable=True)
    assert result.adopted is True
    assert [event.kind for event in history.events()][-3:] == [
        SelfEventKind.REVIEWED_CHANGE,
        SelfEventKind.CANDIDATE_CREATED,
        SelfEventKind.CANDIDATE_ADOPTED,
    ]

def test_resume_after_candidate_created_skips_review_edit_and_commit():
    ports = interrupted_after_candidate_created()
    run_rsi(request(8), **ports)
    assert ports.reviewer.calls == 0
    assert ports.editor.calls == 0
    assert ports.viability.calls == 1
```

Also cover default defer 8, invalid CHANGE rejection, editor error, no-change,
non-viable rejection, repeated terminal call idempotence, infrastructure
propagation, and terminal-vs-unfinished checkpoint reconciliation.

- [ ] **Step 2: Run pipeline tests and verify failure**

Run: `python -m pytest -q tests/test_rsi_pipeline.py`

Expected: FAIL because `rsi.pipeline` does not exist.

- [ ] **Step 3: Implement the minimal transaction**

Resume is driven only by events: no event calls reviewer; REVIEWED_CHANGE calls
or resumes editor; CANDIDATE_CREATED calls or resumes viability; a terminal
event returns its projection without repeating work. Append the adoption event
before materializing the active runtime cache. `RsiPipeline.prepare` initializes
body/history, materializes projected active SHA, rebuilds the review view, and
clears only a journal proven terminal by history.

- [ ] **Step 4: Run pipeline and loop tests**

Run: `python -m pytest -q tests/test_rsi_pipeline.py tests/test_loop_pipeline.py`

Expected: PASS.

- [ ] **Step 5: Run a provider import guard**

Run: `rg -n "subprocess|shutil|json|config|Apptainer|WorkerJobs|SelfRepo" simpleloop/rsi/pipeline.py`

Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add simpleloop/rsi/pipeline.py simpleloop/loop.py \
  tests/test_rsi_pipeline.py tests/test_loop_pipeline.py
git commit -m "refactor: add event driven rsi pipeline"
```

---

### Task 5: Scheduled RSI Ports and Self-Edit Worker

**Files:**
- Rewrite: `simpleloop/scheduling/rsi.py`
- Create: `simpleloop/scheduling/handlers/rsi.py`
- Modify: `simpleloop/scheduling/worker.py`
- Modify: `simpleloop/scheduling/handlers/proposer.py`
- Modify: `tests/test_scheduled_rsi.py`
- Create: `tests/test_rsi_worker.py`
- Modify: `tests/test_scheduling_worker.py`

**Interfaces:**
- Consumes: Task 1 typed requests/results and existing `WorkerJobs`.
- Produces: `ScheduledSelfReviewer.review(SelfReviewRequest) -> SelfDecision`.
- Produces: `ScheduledSelfEditor.edit(SelfEditRequest) -> SelfEditResult`.
- Produces: `ScheduledViabilityChecker.check(ViabilityRequest) -> ViabilityResult`.
- Produces: worker kind `self_edit` and explicit self-review/viability payloads.

- [ ] **Step 1: Write failing scheduled adapter tests**

```python
def test_review_starts_rsi_journal_with_explicit_body_and_history(tmp_path):
    reviewer.review(SelfReviewRequest(5, "goal", "s0", body, reviews))
    job = jobs.calls[0]["jobs"][0]
    assert job.kind == "self_review"
    assert job.payload["self_repo"] == str(body)
    assert job.payload["reviews_path"] == str(reviews)

def test_edit_transitions_review_to_self_edit_and_does_not_clear(tmp_path):
    result = editor.edit(SelfEditRequest(5, change(), workspace))
    assert jobs.calls[0]["transition_from"] == "self_review"
    assert jobs.calls[0]["jobs"][0].kind == "self_edit"
    assert jobs.cleared == 0

def test_viability_transitions_from_self_edit(tmp_path):
    checker.check(viability_request())
    assert jobs.calls[0]["transition_from"] == "self_edit"
```

Also cover replay of persisted payloads, usage telemetry, infrastructure
failure, typed decode, and direct `CANDIDATE_CREATED` resume into viability.

- [ ] **Step 2: Write failing self-edit handler tests**

Verify that the handler builds an Apptainer world with the candidate body root
read-only and only `proposer/` read-write, feeds the explicit instruction to the
Agent, reports `EDITED`/`EDITOR_FAILED`, and never runs Git.

- [ ] **Step 3: Run tests and verify failure**

Run: `python -m pytest -q tests/test_scheduled_rsi.py tests/test_rsi_worker.py tests/test_scheduling_worker.py`

Expected: FAIL because typed adapters and `self_edit` do not exist.

- [ ] **Step 4: Implement scheduled adapters and worker handler**

Remove all `finally: jobs.clear()` calls. Decode proposer self-review into
`SelfDecision`; classify proposer lane COMPLETED/LANE_FAILED for viability;
return typed edit status from the new handler. The handler may construct World
and Agent but may not inspect, commit, or adopt Git.

- [ ] **Step 5: Make proposer inputs explicit**

Replace the `SelfRepo` import in `run_self_review_lane` with values from
`ProposerLaneSpec`: `self_repo`, `reviews_path`, and `incumbent_self_sha`.
Task and viability redirects use the explicit `self_repo` manifest field.

- [ ] **Step 6: Run scheduled/worker tests**

Run: `python -m pytest -q tests/test_scheduled_rsi.py tests/test_rsi_worker.py tests/test_scheduling_worker.py tests/test_self_review.py tests/test_host_proposer_contract.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add simpleloop/scheduling/rsi.py simpleloop/scheduling/handlers/rsi.py \
  simpleloop/scheduling/handlers/proposer.py simpleloop/scheduling/worker.py \
  tests/test_scheduled_rsi.py tests/test_rsi_worker.py \
  tests/test_scheduling_worker.py tests/test_self_review.py
git commit -m "refactor: schedule every rsi model stage"
```

---

### Task 6: Application Cutover and Old Host Removal

**Files:**
- Modify: `simpleloop/app.py`
- Modify: `simpleloop/scheduling/task.py`
- Modify: `simpleloop/scheduling/handlers/proposer.py`
- Modify: `tests/test_app_composition.py`
- Modify: `tests/test_scheduled_task.py`
- Rewrite: `tests/test_rsi_host.py`
- Rewrite: `tests/test_self_repo.py` into package-level migration guards
- Delete: `simpleloop/self_repo.py`

**Interfaces:**
- Consumes: Tasks 2–5 concrete stores and adapters.
- Produces: one `RsiPipeline` composed in `app.py`; task proposer always loads the materialized active body.
- Removes: `SelfRepo`, `LegacyRsiRunner`, `check_viability`, mutable commitment seeding, and direct self-executor factory.

- [ ] **Step 1: Write failing composition and boundary tests**

```python
def test_app_composes_typed_rsi_pipeline(monkeypatch, config):
    build = capture_composition(monkeypatch)
    run(config)
    assert build.rsi.__class__.__name__ == "RsiPipeline"

def test_task_proposer_payload_uses_materialized_active_body(tmp_path):
    proposer.propose(request())
    assert jobs.calls[0]["jobs"][0].payload["self_repo"] == str(active_body)

def test_old_self_repo_module_is_absent():
    assert importlib.util.find_spec("simpleloop.self_repo") is None
```

Also cover fresh initialization with configured first review, resume after a
self-review-only round, terminal stale journal cleanup, and summary tally.

- [ ] **Step 2: Run cutover tests and verify failure**

Run: `python -m pytest -q tests/test_app_composition.py tests/test_scheduled_task.py tests/test_rsi_host.py tests/test_self_repo.py`

Expected: FAIL because app still composes `LegacyRsiRunner`.

- [ ] **Step 3: Cut over the Composition Root**

Construct body/history first, pass typed scheduled ports into `RsiPipeline`,
call `prepare`, and use projected `last_review_round` for loop resume. Pass the
body runtime path explicitly into scheduled task proposer payloads. Remove
`_seed_rsi_commitment`, `_self_executor_factory`, and all old imports.

- [ ] **Step 4: Delete the old host module and migrate tests**

Delete `simpleloop/self_repo.py`. Retain behavioral tests under the new modules;
do not add forwarding imports, aliases, or a compatibility facade.

- [ ] **Step 5: Run all RSI and composition tests**

Run: `python -m pytest -q tests/test_rsi_models.py tests/test_rsi_history.py tests/test_rsi_body.py tests/test_rsi_pipeline.py tests/test_scheduled_rsi.py tests/test_rsi_worker.py tests/test_rsi_host.py tests/test_self_repo.py tests/test_app_composition.py tests/test_loop_pipeline.py tests/test_self_review.py`

Expected: PASS.

- [ ] **Step 6: Run structural guards**

Run: `test ! -f simpleloop/self_repo.py && ! rg -n "SelfRepo|LegacyRsiRunner|from \.\.\.self_repo|from \.self_repo" simpleloop tests --glob '*.py'`

Expected: exit 0 and no matches.

- [ ] **Step 7: Commit**

```bash
git add -A simpleloop tests
git commit -m "refactor: cut over to typed rsi application"
```

---

### Task 7: Documentation, Full Verification, and Phase Completion

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-08-14-simpleloop-phase6-rsi-pipeline.md`

**Interfaces:**
- Documents the stable trust boundary and run-directory authority after cutover.

- [ ] **Step 1: Update architecture documentation**

Document:

```text
app → loop → rsi.pipeline → typed ports
self/history.jsonl = authority
self/reviews.jsonl = proposer read projection
self/repo = run-local body/runtime cache
review/edit/viability = WorkerJobs under Local or HEPJob
```

State explicitly that proposer remains independent and that Phase 6 prepares
body interfaces for future branching without implementing tree evolution.

- [ ] **Step 2: Run focused architecture suite**

Run: `python -m pytest -q tests/test_rsi_models.py tests/test_rsi_history.py tests/test_rsi_body.py tests/test_rsi_pipeline.py tests/test_scheduled_rsi.py tests/test_rsi_worker.py tests/test_app_composition.py`

Expected: PASS.

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q`

Expected: all tests pass; the one real-model viability test may remain skipped.

- [ ] **Step 4: Run compile and boundary verification**

```bash
python -m compileall -q simpleloop proposer tests
test ! -f simpleloop/self_repo.py
! rg -n "SelfRepo|LegacyRsiRunner" simpleloop tests --glob '*.py'
! rg -n "subprocess|shutil|json|config|Apptainer|WorkerJobs" simpleloop/rsi/pipeline.py
! rg -n "from simpleloop|import simpleloop" proposer --glob '*.py'
git diff --check
git status --short
```

Expected: compile succeeds; guards emit no matches; diff check succeeds; only
the intended documentation completion edit remains before the final commit.

- [ ] **Step 5: Mark this plan implemented and record exact verification**

Set `Status` to `Implemented`, check every completed box, and add the exact
pytest count plus structural guard results at the top of this document.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/superpowers/plans/2026-08-14-simpleloop-phase6-rsi-pipeline.md
git commit -m "docs: complete phase six rsi migration"
```
