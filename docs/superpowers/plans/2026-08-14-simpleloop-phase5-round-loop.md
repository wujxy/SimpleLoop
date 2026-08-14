# SimpleLoop Phase 5 Round + Loop Vertical Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `RunContext` and the transitional `WorkerBackend` with directly callable typed Round/Loop pipelines, explicit scheduled domain adapters, and one `app.py` Composition Root.

**Architecture:** `run_round` composes Proposer, CandidateRunner, Recorder, and Selector; `run_loop` advances Task/RSI state and owns history-before-checkpoint commit ordering. `app.py` interprets current configuration once and composes Workspace, World, Scheduler, Supervisor, scheduled adapters, persistence, reporting, and the transitional Phase-6 RSI runner.

**Tech Stack:** Python 3.9+, frozen dataclasses, Protocol, JSON/JSONL, fsync + atomic replace, existing World/Scheduling APIs, pytest.

## Global Constraints

- Follow `docs/superpowers/specs/2026-08-14-simpleloop-phase5-round-loop-design.md`.
- Preserve current config schema, CLI flags, Local/HEPJob behavior, static proposals, baseline, telemetry, plotting/export, crash resume, and landed RSI behavior.
- Add no dependencies or configuration fields.
- Do not import the standalone proposer package outside its existing lazy worker handler.
- Do not retain `RunContext`, `WorkerBackend`, `simpleloop/execution/`, or forwarding compatibility facades.
- Do not implement Phase-6 self event history or Phase-7 config migration.
- Every production behavior begins with a failing test and ends with focused plus full verification.

---

### Task 1: Durable Stage Transition and Idempotent History

**Files:**
- Modify: `simpleloop/persistence/journal.py`
- Modify: `simpleloop/harness/store.py`
- Test: `tests/test_job_journal.py`
- Test: `tests/test_history_store.py`

**Interfaces:**
- Produces: `JobJournal.transition(expected_stage, stage, round_id, context, jobs) -> JournalRecord`.
- Produces: `HistoryConflictError` and idempotent/fsynced `Store.append_round(result)`.

- [ ] **Step 1: Write failing persistence tests**

```python
def test_journal_transition_atomically_replaces_expected_stage(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("proposer", 2, {"proposal": "p"}, [])
    record = journal.transition(
        "proposer", "candidates", 2,
        {"proposals": [{"instruction": "p"}]},
        [{"request_id": "r2-c0"}],
    )
    assert record.stage == "candidates"
    assert journal.load() == record

def test_append_round_is_idempotent_and_conflicts_on_different_content(tmp_path):
    store = Store(tmp_path, SCHEMA)
    result = terminal_round(0)
    store.append_round(result)
    store.append_round(result)
    assert len(store.history()) == 1
    with pytest.raises(HistoryConflictError):
        store.append_round(replace(result, parent_sha="different"))
```

- [ ] **Step 2: Run tests and verify the missing APIs fail**

Run: `python -m pytest -q tests/test_job_journal.py tests/test_history_store.py`

Expected: FAIL because `transition` and `HistoryConflictError` do not exist.

- [ ] **Step 3: Implement the minimal durable operations**

`transition` must load exactly one active record, compare stage/round, and call
the existing atomic `_write` once. `append_round` must project the typed result
first, reject duplicate/conflicting round ids, append one JSON line, flush, and
`os.fsync` the file descriptor.

- [ ] **Step 4: Run persistence tests**

Run: `python -m pytest -q tests/test_job_journal.py tests/test_history_store.py tests/test_phase0_characterization.py tests/test_views_and_parse.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/persistence/journal.py simpleloop/harness/store.py \
  tests/test_job_journal.py tests/test_history_store.py
git commit -m "refactor: make round persistence crash idempotent"
```

---

### Task 2: Typed Round Pipeline

**Files:**
- Modify: `simpleloop/candidate.py`
- Modify: `simpleloop/round.py`
- Test: `tests/test_round_pipeline.py`
- Modify: `tests/test_round_contract.py`

**Interfaces:**
- Produces: `CandidateBatchResult`, `SelectionPolicy`, `RoundRequest`, `Proposer`, `CandidateRunner`, `RoundRecorder`, `run_round`.
- Consumes: current `ProposerRequest`, `CandidatePlan`, `CandidateBatchRequest`, `select_candidate`, and `RoundResult`.

- [ ] **Step 1: Write failing Round Pipeline tests**

```python
def test_round_fans_proposals_into_candidates_and_selects_winner():
    result = run_round(
        request(), proposer=FixedProposer((Proposal("a"), Proposal("b"))),
        candidates=FakeCandidates((candidate(0, 90), candidate(1, 80))),
        recorder=RecordingRecorder(),
    )
    assert result.selection.candidate_id == 1
    assert result.next_sha == "sha-1"

def test_round_abstention_records_and_skips_candidates():
    candidates = FakeCandidates(())
    result = run_round(
        request(), proposer=AbstainingProposer(), candidates=candidates,
        recorder=RecordingRecorder(),
    )
    assert result.candidates == ()
    assert candidates.calls == []
```

Also cover proposal recording before candidate execution, no improvement,
static `require_improvement=False`, evidence refs, and batch telemetry.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest -q tests/test_round_pipeline.py`

Expected: FAIL because `RoundRequest`/`run_round` do not exist.

- [ ] **Step 3: Implement only the typed data flow**

`run_round` calls proposer once, records once, skips candidate runner for
abstention, otherwise enumerates proposals into plans at the incumbent SHA,
calls candidates once, calls the existing selector, and returns `RoundResult`.
It performs no I/O, config lookup, printing, or exception normalization.

- [ ] **Step 4: Run Round tests**

Run: `python -m pytest -q tests/test_round_pipeline.py tests/test_round_contract.py tests/test_parallel_candidates.py -k 'selector or round'`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/candidate.py simpleloop/round.py \
  tests/test_round_pipeline.py tests/test_round_contract.py
git commit -m "refactor: add explicit round pipeline"
```

---

### Task 3: Typed Loop Pipeline

**Files:**
- Rewrite: `simpleloop/loop.py`
- Test: `tests/test_loop_pipeline.py`

**Interfaces:**
- Produces: `LoopState`, `LoopRequest`, `RsiResult`, `LoopResult`, `RoundRunner`, `RsiRunner`, `RoundHistory`, `CheckpointStore`, `LoopObserver`, `run_loop`.
- Consumes: `RoundRequest`, `RoundResult`, `SelectionPolicy`, and `InfrastructureError`.

- [ ] **Step 1: Write failing Loop Pipeline tests**

```python
def test_loop_commits_history_before_clearing_checkpoint():
    events = []
    result = run_loop(
        LoopRequest("goal", 1, LoopState(0, "base", {"OBJ": 100}), policy()),
        rounds=OneRound(events), rsi=NeverRsi(),
        history=History(events), checkpoint=Checkpoint(events),
        observer=Observer(events),
    )
    assert events == ["round", "history", "clear", "observe"]
    assert result.state.incumbent_sha == "winner"

def test_loop_infrastructure_failure_does_not_consume_or_clear_round():
    events = []
    checkpoint = Checkpoint([])
    result = run_loop(
        LoopRequest("goal", 1, LoopState(0, "base", {"OBJ": 100}), policy()),
        rounds=FailingRound(InfrastructureError("lost")), rsi=NeverRsi(),
        history=History(events), checkpoint=checkpoint,
        observer=Observer(events),
    )
    assert result.interrupted is True
    assert result.state.next_round == 0
    assert checkpoint.calls == 0
```

Also cover RSI due branch, KEEP/CHANGE tally, no-winner metrics retention,
observer-after-clear, and non-infrastructure exception propagation.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest -q tests/test_loop_pipeline.py`

Expected: FAIL because typed Loop APIs do not exist.

- [ ] **Step 3: Replace monolithic loop with the minimal state machine**

Keep only dataclasses, Protocols, no-op observer values if needed, and
`run_loop`. The module may import domain contracts and `InfrastructureError`;
it must not import config, YAML, OS, Workspace, Scheduler, reporting, telemetry,
the proposer package, or concrete providers.

- [ ] **Step 4: Run Loop tests**

Run: `python -m pytest -q tests/test_loop_pipeline.py tests/test_round_pipeline.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/loop.py tests/test_loop_pipeline.py
git commit -m "refactor: reduce loop to typed state progression"
```

---

### Task 4: WorkerJobs and Scheduled Task Adapters

**Files:**
- Create: `simpleloop/scheduling/jobs.py`
- Create: `simpleloop/scheduling/task.py`
- Test: `tests/test_worker_jobs.py`
- Test: `tests/test_scheduled_task.py`
- Delete after migration: `tests/test_worker_backend.py`

**Interfaces:**
- Produces: `WorkerJob`, `WorkerJobPolicy`, `WorkerJobs.run`.
- Produces: `BaselineRequest`, `BaselineResult`, `ScheduledProposer`, `ScheduledCandidates`, `ScheduledBaseline`.
- Consumes: Scheduler, JobSupervisor, JobJournal, WorkspaceProvider, Worker envelope/domain codecs, telemetry sink.

- [ ] **Step 1: Write failing generic WorkerJobs tests**

```python
def test_worker_jobs_builds_one_shell_free_worker_job(tmp_path):
    client, supervisor, journal = worker_jobs(tmp_path)
    client.run(stage="candidate", round_id=1, context={}, jobs=(
        WorkerJob("candidate", "r1-c0", {"candidate_id": 0}, tmp_path / "c0"),
    ), max_parallel=1)
    spec = supervisor.requests[0].jobs[0]
    assert spec.argv[-2:] == ("--manifest", str(spec.manifest_path))
    assert spec.request.kind == "candidate"

def test_worker_jobs_transitions_from_proposer_to_candidates(tmp_path):
    client, supervisor, journal = worker_jobs(tmp_path)
    journal.begin("proposer", 1, {}, [])
    client.run(
        stage="candidates", round_id=1, context={},
        jobs=(WorkerJob(
            "candidate", "r1-c0", {"candidate_id": 0}, tmp_path / "c0"
        ),),
        max_parallel=1, transition_from="proposer",
    )
    assert journal.load().stage == "candidates"
```

- [ ] **Step 2: Run WorkerJobs tests and verify failure**

Run: `python -m pytest -q tests/test_worker_jobs.py`

Expected: FAIL because `scheduling.jobs` does not exist.

- [ ] **Step 3: Implement WorkerJobs without domain logic**

Construct `WorkerRequest`/`JobSpec` from explicit values and delegate exactly
once to `JobSupervisor`. Stage transition occurs before supervisor reconciliation;
new stages use `journal.begin` through the supervisor.

- [ ] **Step 4: Write failing task-adapter tests**

Cover candidate workspace/payload/order, telemetry stamping, proposer replay
from candidate context, active proposer resume, proposer-to-candidate atomic
transition, abstention, baseline validation, and partial/all-infrastructure
classification.

- [ ] **Step 5: Implement scheduled task adapters and run tests**

Run: `python -m pytest -q tests/test_worker_jobs.py tests/test_scheduled_task.py tests/test_job_supervisor.py tests/test_scheduling_worker.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add simpleloop/scheduling/jobs.py simpleloop/scheduling/task.py \
  tests/test_worker_jobs.py tests/test_scheduled_task.py
git commit -m "refactor: expose scheduled task domain adapters"
```

---

### Task 5: Scheduled RSI Transport and Legacy RsiRunner

**Files:**
- Create: `simpleloop/scheduling/rsi.py`
- Modify: `simpleloop/self_repo.py`
- Test: `tests/test_scheduled_rsi.py`
- Modify: `tests/test_rsi_host.py`
- Modify: `tests/test_self_repo.py`

**Interfaces:**
- Produces: `ScheduledSelfReview.review(round_id) -> Mapping[str, object]`.
- Produces: `ScheduledViability.check(payload) -> Mapping[str, object] | None`.
- Produces: `LegacyRsiRunner.due(round_id)` and `.run(round_id) -> RsiResult`.

- [ ] **Step 1: Write failing RSI adapter tests**

Assert declared worker kinds, resume of matching inflight context, usage
ingestion, validity mapping, and no direct subprocess ownership.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest -q tests/test_scheduled_rsi.py tests/test_rsi_host.py`

Expected: FAIL because the scheduled RSI adapters and `LegacyRsiRunner` do not exist.

- [ ] **Step 3: Implement transport adapters and move current host transition**

Move `_DEFAULT_SELF_REVIEW_DEFER`, self-executor construction injection,
review ledger update, commitment update, viability invocation, and adoption
logging behind `LegacyRsiRunner`. Do not change `SelfRepo` formats or adoption.

- [ ] **Step 4: Run RSI tests**

Run: `python -m pytest -q tests/test_scheduled_rsi.py tests/test_rsi_host.py tests/test_self_repo.py tests/test_self_review.py`

Expected: PASS with the existing live-model viability skip only.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/scheduling/rsi.py simpleloop/self_repo.py \
  tests/test_scheduled_rsi.py tests/test_rsi_host.py tests/test_self_repo.py
git commit -m "refactor: adapt landed rsi to loop port"
```

---

### Task 6: Round Artifacts, Summary, and Composition Root

**Files:**
- Create: `simpleloop/persistence/round_artifacts.py`
- Create: `simpleloop/reporting/summary.py`
- Create: `simpleloop/app.py`
- Modify: `simpleloop/cli.py`
- Test: `tests/test_round_artifacts.py`
- Test: `tests/test_summary.py`
- Test: `tests/test_app.py`
- Modify: `tests/test_static_mode.py`
- Modify: `tests/test_provenance_export_lock.py`

**Interfaces:**
- Produces: `RoundArtifacts.record_proposals`.
- Produces: `build_summary(history, metrics_schema, baseline_metrics, baseline_sha, repo, run_dir) -> dict` and `write_summary(run_dir, summary) -> None`.
- Produces: `AppRequest` and `app.run(request)`.

- [ ] **Step 1: Write failing artifact and summary tests**

Assert proposal trace/handoff shapes and summary compatibility with current
`summary.json` fields using explicit arguments rather than `RunContext`.

- [ ] **Step 2: Implement focused artifact/summary modules**

Move only the current behavior. Do not import `loop.py` or concrete scheduler
providers in reporting/persistence modules.

- [ ] **Step 3: Write failing app composition tests**

Assert config/mode validation occurs before provider work, Local and HEPJob
compose the same Round/Loop runners with different Scheduler only, resume state
projection is unchanged, CLI passes an `AppRequest`, and public summary output
is unchanged.

- [ ] **Step 4: Implement app Composition Root**

Move lock/config snapshot, proposal loading, provider construction, preflight,
baseline/resume projection, static proposer, progress observer, and final
summary from old loop. Compose all services with explicit constructors; do not
create a context/service-locator object.

- [ ] **Step 5: Run app/reporting tests**

Run: `python -m pytest -q tests/test_app.py tests/test_static_mode.py tests/test_provenance_export_lock.py tests/test_round_artifacts.py tests/test_summary.py tests/test_cli.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add simpleloop/app.py simpleloop/cli.py \
  simpleloop/persistence/round_artifacts.py simpleloop/reporting/summary.py \
  tests/test_app.py tests/test_static_mode.py tests/test_provenance_export_lock.py \
  tests/test_round_artifacts.py tests/test_summary.py
git commit -m "refactor: compose application around typed pipelines"
```

---

### Task 7: Production Cutover and Delete Transitional Owners

**Files:**
- Delete: `simpleloop/execution/backend.py`
- Delete: `simpleloop/execution/__init__.py`
- Delete: `tests/test_worker_backend.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `tests/test_telemetry.py`
- Modify: `tests/test_architecture_boundaries.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: all Phase-5 replacements.
- Produces: one live app/loop/round/scheduling path with guards against old owners.

- [ ] **Step 1: Add failing architecture guards**

```python
def test_phase5_removes_context_and_execution_bridge():
    production = read_all_python(ROOT / "simpleloop")
    assert "RunContext" not in production
    assert "WorkerBackend" not in production
    assert not (ROOT / "simpleloop/execution").exists()

def test_loop_and_round_are_provider_free():
    for path in (ROOT / "simpleloop/loop.py", ROOT / "simpleloop/round.py"):
        imports = imported_modules(path)
        assert not imports.intersection({"config", "yaml", "os"})
        assert not any("scheduling.local" in item or "scheduling.hepjob" in item
                       for item in imports)
```

- [ ] **Step 2: Migrate remaining tests/imports and delete owners**

Use `rg` to prove no production consumers, delete the execution package, and
replace private helper tests with Round/Loop/App/adapter public behavior tests.

- [ ] **Step 3: Update README pipeline and extension boundaries**

Document `app → loop → round → candidate`, explicit ports, scheduling adapters,
and Phase-6 transitional RSI boundary.

- [ ] **Step 4: Run architecture and full tests**

Run:

```bash
python -m pytest -q tests/test_architecture_boundaries.py
python -m pytest -q
python -m compileall -q simpleloop proposer tests
git diff --check
```

Expected: all pass with only the existing live-model skip.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: cut over to phase five application pipeline"
```

---

### Task 8: Final Verification Record

**Files:**
- Modify: `docs/superpowers/plans/2026-08-14-simpleloop-phase5-round-loop.md`

**Interfaces:**
- Produces: completed implementation and verification record.

- [ ] **Step 1: Mark completed steps and add final evidence**

Add `**Status:** Implemented (2026-08-14)` and the fresh pytest count only
after all production commits and checks succeed.

- [ ] **Step 2: Run final verification**

```bash
python -m pytest -q
python -m compileall -q simpleloop proposer tests
rg -n "RunContext|WorkerBackend" simpleloop --glob '*.py'
test ! -d simpleloop/execution
rg -n "from .*config|import yaml|import os|scheduling\.(local|hepjob)" \
  simpleloop/loop.py simpleloop/round.py
git diff --check
git status --short
```

Expected: tests/compile pass, searches produce no output, execution directory
is absent, diff check passes, and status contains only this plan update.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-08-14-simpleloop-phase5-round-loop.md
git commit -m "docs: complete phase five migration plan"
```
