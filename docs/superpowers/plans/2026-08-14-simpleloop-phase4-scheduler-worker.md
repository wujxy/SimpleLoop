# SimpleLoop Phase 4 Scheduler + Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace duplicated Local/HEPJob and candidate/proposer lifecycle code with one typed scheduler, supervisor, worker envelope, and inflight journal.

**Architecture:** Domain-facing execution code prepares typed worker requests. A single `JobSupervisor` persists and supervises those requests through either `LocalScheduler` or `HEPJobScheduler`; one worker CLI lazily dispatches them to candidate or proposer Host handlers and atomically writes one result envelope. The temporary execution bridge preserves current loop calls until Phase 5 while all displaced lifecycle owners are deleted now.

**Tech Stack:** Python 3.9+, frozen dataclasses, Protocol, JSON, subprocess/process groups, HTCondor CLI adapter, pytest.

## Global Constraints

- Preserve Local and HEPJob execution, networking, baseline, parallel candidates, gate/selection semantics, crash resume, telemetry, and fully landed RSI behavior.
- `proposer/` must not import `simpleloop` or receive live Host objects.
- Only infrastructure failures retry; valid business results never retry.
- No YAML schema change and no new dependency.
- New runs use `simpleloop.worker.v1` and `simpleloop.inflight.v1`; old inflight files are not continued.
- Local and HEPJob execute the same worker entry and use the same supervisor.
- Use the minimum fields and abstractions required by live Local/HEPJob paths.

---

### Task 1: Worker Envelope and Inflight Journal

**Files:**
- Create: `simpleloop/scheduling/__init__.py`
- Create: `simpleloop/scheduling/envelope.py`
- Create: `simpleloop/persistence/journal.py`
- Test: `tests/test_worker_envelope.py`
- Test: `tests/test_job_journal.py`

**Interfaces:**
- Produces: `WorkerRequest`, `WorkerResult`, `WorkerStatus`, `ProtocolError`, `write_request`, `read_request`, `write_result`, `read_result`.
- Produces: `JobJournal.begin(stage, round_id, context, jobs)`, `load()`, `save_jobs(jobs)`, and `clear()`.

- [ ] **Step 1: Write failing envelope tests**

```python
def test_worker_result_round_trip_is_atomic(tmp_path):
    request = WorkerRequest("candidate", "r2-c1", {"candidate_id": 1},
                            tmp_path / "result.json")
    write_request(tmp_path / "manifest.json", request)
    assert read_request(tmp_path / "manifest.json") == request
    result = WorkerResult("candidate", "r2-c1", WorkerStatus.COMPLETED,
                          {"status": "NO_CHANGE"}, ({"model": "m"},))
    write_result(request.result_path, result)
    assert read_result(request.result_path, expected=request) == result
    assert not list(tmp_path.glob("*.tmp"))


def test_worker_result_rejects_request_identity_mismatch(tmp_path):
    request = WorkerRequest("candidate", "r2-c1", {}, tmp_path / "result.json")
    write_result(request.result_path, WorkerResult(
        "proposer", "r2-c1", WorkerStatus.COMPLETED, {}, (), None, {}))
    with pytest.raises(ProtocolError, match="kind"):
        read_result(request.result_path, expected=request)
```

- [ ] **Step 2: Run envelope tests and verify failure**

Run: `python -m pytest -q tests/test_worker_envelope.py`

Expected: FAIL because `simpleloop.scheduling.envelope` does not exist.

- [ ] **Step 3: Implement the minimal strict envelope codec**

Use frozen dataclasses, require protocol exactly `simpleloop.worker.v1`, require
object payload/result/execution and list usage, reject bool-as-string coercions,
and write JSON through one `_atomic_json(path, payload)` helper that flushes,
fsyncs, replaces, and fsyncs the parent directory.

- [ ] **Step 4: Write failing journal tests**

```python
def test_journal_preserves_stage_context_and_jobs(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("candidates", 3, {"parent_sha": "abc"}, [])
    journal.save_jobs([{"request_id": "r3-c0", "attempt": 1}])
    record = journal.load()
    assert record.stage == "candidates"
    assert record.context == {"parent_sha": "abc"}
    assert record.jobs[0]["request_id"] == "r3-c0"
    journal.clear()
    assert journal.load() is None


def test_journal_refuses_to_replace_a_different_active_stage(tmp_path):
    journal = JobJournal(tmp_path / "inflight.json")
    journal.begin("proposer", 1, {}, [])
    with pytest.raises(ProtocolError, match="active stage"):
        journal.begin("candidates", 1, {}, [])
```

- [ ] **Step 5: Implement the journal and run focused tests**

Run: `python -m pytest -q tests/test_worker_envelope.py tests/test_job_journal.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add simpleloop/scheduling simpleloop/persistence/journal.py \
  tests/test_worker_envelope.py tests/test_job_journal.py
git commit -m "refactor: define worker and journal protocols"
```

---

### Task 2: Scheduler Contracts and Sole JobSupervisor

**Files:**
- Create: `simpleloop/scheduling/contracts.py`
- Create: `simpleloop/scheduling/supervisor.py`
- Test: `tests/test_job_supervisor.py`

**Interfaces:**
- Produces: `JobState`, `RetryPolicy`, `ResourceSpec`, `JobSpec`, `JobHandle`, `JobObservation`, `Scheduler`, `JobBatchRequest`, `JobOutcome`, `JobBatchResult`, `InfrastructureError`.
- Consumes: envelope read/write and `JobJournal` from Task 1.

- [ ] **Step 1: Write failing state-machine tests**

Cover these exact cases with a deterministic fake Scheduler and fake clock:

| Test | Required assertion |
|---|---|
| `test_supervisor_returns_valid_result_without_waiting_for_queue_exit` | A matching envelope returns `SUCCEEDED` before another `inspect` call. |
| `test_supervisor_limits_parallel_submissions` | Peak live handles equals `max_parallel`. |
| `test_unknown_query_does_not_mark_job_lost` | UNKNOWN advances neither attempt nor `gone_since`. |
| `test_held_job_retries_then_collects` | First handle is cancelled; attempt two result is returned. |
| `test_disappeared_job_waits_for_grace_then_retries` | No retry before grace; one retry immediately after it. |
| `test_running_timeout_cancels_and_retries` | Timed-out handle is cancelled and the next attempt starts. |
| `test_malformed_result_is_protocol_error_not_retry` | `ProtocolError` is raised and submit count stays one. |
| `test_resume_uses_persisted_remote_handle` | Existing handle is inspected without a new submit. |
| `test_terminal_job_releases_workspace_once` | Fake WorkspaceProvider records one matching removal. |
| `test_partial_success_is_returned_with_infra_failures` | Result contains one success and one exhausted failure. |

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `python -m pytest -q tests/test_job_supervisor.py`

Expected: FAIL because scheduling contracts/supervisor do not exist.

- [ ] **Step 3: Implement frozen contracts**

`JobSpec` carries request/paths/argv/retry/resources and optional
`SourceWorkspace`. Persisted runtime records carry attempt, state, scheduler
handle, submitted/running/gone wall-clock timestamps, and note. Do not put raw
config or business models into scheduling contracts.

- [ ] **Step 4: Implement one reconciliation loop**

The loop must check result files before scheduler observations, preserve
`UNKNOWN`, retry only `FAILED`/`LOST`/timeout/missing-result states, persist
after every transition, enforce `max_parallel`, and release the workspace on a
final outcome. Inject `clock` and `sleep` callables for deterministic tests.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest -q tests/test_worker_envelope.py tests/test_job_journal.py tests/test_job_supervisor.py`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add simpleloop/scheduling/contracts.py simpleloop/scheduling/supervisor.py \
  tests/test_job_supervisor.py
git commit -m "refactor: add unified job supervisor"
```

---

### Task 3: Local Scheduler

**Files:**
- Create: `simpleloop/scheduling/local.py`
- Test: `tests/test_local_scheduler.py`

**Interfaces:**
- Produces: `LocalScheduler.submit/inspect/cancel` implementing `Scheduler`.
- Consumes: scheduling contracts from Task 2.

- [ ] **Step 1: Write failing LocalScheduler tests**

Write five tests with these concrete assertions: an argv containing a literal
semicolon is not shell-evaluated and reaches `SUCCEEDED`; a sleeping child is
`RUNNING`; cancelling it makes its process group disappear; a handle absent
from the scheduler's live `Popen` map is `LOST`; and a child writing one line to
each stream produces exactly those lines in `stdout_path` and `stderr_path`.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest -q tests/test_local_scheduler.py`

Expected: FAIL because `LocalScheduler` does not exist.

- [ ] **Step 3: Implement LocalScheduler**

Launch `JobSpec.argv` with `shell=False`, `start_new_session=True`, inherited
explicit environment, and job log files. Keep live `Popen` objects in a private
map keyed by handle value. An unknown restored pid is `LOST`; cancellation uses
SIGTERM then SIGKILL on its process group without accepting broad targets.

- [ ] **Step 4: Run scheduler and supervisor tests**

Run: `python -m pytest -q tests/test_local_scheduler.py tests/test_job_supervisor.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/scheduling/local.py tests/test_local_scheduler.py
git commit -m "refactor: add local scheduler adapter"
```

---

### Task 4: HEPJob Scheduler

**Files:**
- Create: `simpleloop/scheduling/hepjob.py`
- Test: `tests/test_hepjob_scheduler.py`
- Modify: `tests/test_config_execution.py`

**Interfaces:**
- Produces: `HEPJobConfig`, `HEPJobScheduler.submit/inspect/cancel`.
- Consumes: generic `JobSpec`, `ResourceSpec`, `JobHandle`, and `JobObservation`.

- [ ] **Step 1: Write failing adapter tests**

Port behavior—not private methods—from `tests/test_hepjob_backend.py`:

Write adapter tests whose fake command runner captures argv and file contents.
Assert the rendered submit file contains the exact worker argv, CPU, memory,
Requirements, accounting group, and IHEP group; submit/query/remove argv carry
the configured collector and schedd; status codes 1/2/5 map to
PENDING/RUNNING/FAILED; a nonzero query maps every requested handle to UNKNOWN;
an absent id maps to LOST; and cancellation invokes the configured remove
command for exactly the requested scheduler id.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest -q tests/test_hepjob_scheduler.py`

Expected: FAIL because the adapter does not exist.

- [ ] **Step 3: Implement the thin Condor adapter**

Move `_requirements_expr`, target flags, submit-file rendering, cluster-id
parsing, query parsing, hold-reason diagnostics, and remove invocation from the
old backend. Do not copy polling, retry, result, workspace, proposer, candidate,
or baseline logic.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest -q tests/test_hepjob_scheduler.py tests/test_config_execution.py tests/test_job_supervisor.py`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add simpleloop/scheduling/hepjob.py tests/test_hepjob_scheduler.py \
  tests/test_config_execution.py
git commit -m "refactor: reduce hepjob to scheduler adapter"
```

---

### Task 5: Unified Worker and Lazy Handlers

**Files:**
- Create: `simpleloop/scheduling/handlers/__init__.py`
- Create: `simpleloop/scheduling/handlers/candidate.py`
- Create: `simpleloop/scheduling/handlers/proposer.py`
- Create: `simpleloop/scheduling/worker.py`
- Test: `tests/test_scheduling_worker.py`
- Modify: `tests/test_candidate_worker.py`
- Modify: `tests/test_proposer_lane_worker.py`
- Modify: `tests/test_self_review.py`

**Interfaces:**
- Produces: `worker.main(argv)`, candidate `handle(payload, usage)`, proposer
  `handle_proposer`, `handle_self_review`, and `handle_viability`.
- Consumes: existing Candidate Pipeline, World layer, standalone proposer
  package, candidate codec, and SelfRepo viability classification.

- [ ] **Step 1: Write failing worker-dispatch tests**

Use monkeypatched import targets for each of the four kinds. Assert each target
receives the original payload and its return mapping appears inside exactly one
matching result envelope. Before proposer dispatch, assert `proposer` is absent
from `sys.modules`; for viability assert the candidate-self path is first on
`sys.path` at import. Make a handler raise and assert a FAILED envelope with the
same kind/request id. An unknown kind must also write a FAILED envelope whose
error identifies the unsupported kind.

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest -q tests/test_scheduling_worker.py`

Expected: FAIL because unified worker/handlers do not exist.

- [ ] **Step 3: Move candidate composition into candidate handler**

Port `CandidateSpec`, `build_ports`, baseline evaluation, fallback candidate
failure, and shared `run_candidate_guarded` invocation. The handler returns the
inner encoded candidate mapping and never writes files.

- [ ] **Step 4: Move proposer composition into lazy proposer handler**

Port lane spec/deps, proposal conversion, self-review behavior, and viability
behavior. Perform self-repo redirect from payload before importing any proposer
module. Return plain mappings and collect usage through the supplied callback.

- [ ] **Step 5: Implement the only worker CLI**

Read one request, import only the selected handler via `importlib`, run it,
capture a framework error as `WorkerStatus.FAILED`, add host/attempt/provider
execution facts, and call the sole envelope writer.

- [ ] **Step 6: Run handler/worker tests**

Run: `python -m pytest -q tests/test_scheduling_worker.py tests/test_candidate_worker.py tests/test_proposer_lane_worker.py tests/test_self_review.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add simpleloop/scheduling/handlers simpleloop/scheduling/worker.py \
  tests/test_scheduling_worker.py tests/test_candidate_worker.py \
  tests/test_proposer_lane_worker.py tests/test_self_review.py
git commit -m "refactor: unify worker dispatch and results"
```

---

### Task 6: Domain Execution Bridge and Production Migration

**Files:**
- Create: `simpleloop/execution/backend.py`
- Modify: `simpleloop/execution/__init__.py`
- Modify: `simpleloop/loop.py`
- Modify: `simpleloop/self_repo.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `tests/test_hepjob_backend.py`
- Modify: `tests/test_rsi_host.py`
- Modify: `tests/test_self_repo.py`
- Modify: `tests/test_telemetry.py`
- Modify: `tests/test_phase0_characterization.py`

**Interfaces:**
- Produces: transitional `WorkerBackend` with `eval_baseline`,
  `run_candidates`, `run_proposer_lanes`, `run_self_review`, and
  `run_viability`.
- Consumes: WorkspaceProvider, schedulers, supervisor, journal, worker
  envelopes, and current domain codecs.

- [ ] **Step 1: Write failing production-composition tests**

Write production-composition tests with a recording Supervisor. Assert Local
and HEP candidate batches submit the same candidate payload shape and preserve
candidate-id order; proposer and self-review use their declared kinds;
candidate resume loads the persisted proposal context and does not call the
proposer; two usage records in one envelope generate exactly two telemetry
calls; a workspace is still present while its result is decoded and absent
after return; and viable/non-viable proposer smoke mappings retain the current
`ViabilityResult` decisions.

- [ ] **Step 2: Run focused production tests and verify failure**

Run: `python -m pytest -q tests/test_parallel_candidates.py tests/test_hepjob_backend.py tests/test_rsi_host.py tests/test_self_repo.py`

Expected: FAIL until `build_backend` composes the common scheduling kernel.

- [ ] **Step 3: Implement WorkerBackend as a transport bridge**

Create typed payloads/result directories/workspaces, call one supervisor, decode
candidate/proposer/self results, ingest envelope usage, and apply the enclosing
operation's all-infrastructure-failure policy. It must contain no subprocess,
Condor command, polling loop, retry loop, or result writer.

- [ ] **Step 4: Move loop to `inflight.json` and common resume**

Replace `_InflightJournal` and `_load_inflight` with `JobJournal`; preserve
parent/proposal context and clear candidates only after history append. Remove
`resume_round` and `cleanup_proposer_orphans` branches: `run_candidates` and
`run_proposer_lanes` resume matching persisted stages themselves.

- [ ] **Step 5: Route viability through WorkerBackend**

Inject the backend viability callable into `SelfRepo.transition` without
restructuring SelfRepo Git/adoption. Remove direct worker subprocess, old result
reading, and timeout ownership from `check_viability`; retain the pure
`_classify_smoke` verdict.

- [ ] **Step 6: Run all migrated behavior tests**

Run: `python -m pytest -q tests/test_parallel_candidates.py tests/test_hepjob_backend.py tests/test_rsi_host.py tests/test_self_repo.py tests/test_telemetry.py tests/test_phase0_characterization.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add simpleloop/execution simpleloop/loop.py simpleloop/self_repo.py \
  tests/test_parallel_candidates.py tests/test_hepjob_backend.py \
  tests/test_rsi_host.py tests/test_self_repo.py tests/test_telemetry.py \
  tests/test_phase0_characterization.py
git commit -m "refactor: run all jobs through scheduling kernel"
```

---

### Task 7: Delete Displaced Owners and Enforce Boundaries

**Files:**
- Delete: `simpleloop/candidate_worker.py`
- Delete: `simpleloop/proposer_lane_worker.py`
- Delete: `simpleloop/execution/local.py`
- Delete: `simpleloop/execution/hepjob.py`
- Delete: `simpleloop/execution/proposer_lanes.py`
- Delete: `simpleloop/execution/base.py`
- Modify: `tests/test_architecture_boundaries.py`
- Delete or replace: obsolete private-owner tests superseded by Tasks 1–6
- Modify: `README.md`

**Interfaces:**
- Consumes: all live Phase 4 replacements.
- Produces: architecture guards preventing legacy scheduling ownership from
  returning.

- [ ] **Step 1: Add failing architecture guards**

```python
def test_phase4_has_one_worker_and_no_legacy_owners():
    assert not (ROOT / "simpleloop/candidate_worker.py").exists()
    assert not (ROOT / "simpleloop/proposer_lane_worker.py").exists()
    for old in ("base.py", "local.py", "hepjob.py", "proposer_lanes.py"):
        assert not (ROOT / "simpleloop/execution" / old).exists()


def test_phase4_completion_markers_and_old_journals_are_absent():
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "simpleloop").rglob("*.py")
    )
    assert "_FINISHED" not in production
    assert "usage.json" not in production
    assert "inflight_round.json" not in production
    assert "inflight_proposer.json" not in production


def test_condor_commands_are_owned_only_by_hepjob_scheduler():
    matches = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "simpleloop").rglob("*.py")
        if "condor_submit" in path.read_text(encoding="utf-8")
    }
    assert matches <= {"simpleloop/config.py", "simpleloop/scheduling/hepjob.py"}


def test_proposer_package_does_not_import_simpleloop():
    for path in (ROOT / "proposer").rglob("*.py"):
        assert "simpleloop" not in path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Delete old owners and migrate remaining imports**

Use `rg` to prove there are no production consumers before each deletion. Keep
only tests of public behavior and the new provider/supervisor contracts; remove
tests pinned to old private `_Job` or backend methods.

- [ ] **Step 3: Update README trust and execution boundaries**

Document World as filesystem/process isolation, Scheduler as placement,
Supervisor as lifecycle policy, unified worker envelopes, Local/HPC parity,
network-enabled agents, and the independent proposer boundary.

- [ ] **Step 4: Run architecture and full tests**

Run:

```bash
python -m pytest -q tests/test_architecture_boundaries.py
python -m pytest -q
python -m compileall -q simpleloop proposer tests
rg -n "_FINISHED|usage\.json|inflight_round|inflight_proposer|candidate_worker|proposer_lane_worker" simpleloop
rg -n "condor_submit|condor_q|condor_rm" simpleloop --glob '*.py'
rg -n "simpleloop" proposer --glob '*.py'
git diff --check
```

Expected:

- pytest passes with only the existing live-model viability skip;
- compileall exits zero;
- first and third searches produce no output;
- Condor search matches only config and `simpleloop/scheduling/hepjob.py`;
- diff check exits zero.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "test: enforce phase four scheduling boundaries"
```

---

### Task 8: Final Plan/Verification Record

**Files:**
- Modify: `docs/superpowers/plans/2026-08-14-simpleloop-phase4-scheduler-worker.md`

**Interfaces:**
- Produces: an implementation-status record and final verification evidence.

- [ ] **Step 1: Mark every completed task and add implementation status**

Set `**Status:** Implemented (2026-08-14)` below the title and check all steps
only after their commits and tests exist.

- [ ] **Step 2: Run final clean-tree verification**

Run:

```bash
python -m pytest -q
python -m compileall -q simpleloop proposer tests
git diff --check
git status --short
```

Expected: tests and compileall pass; diff check has no output; status contains
only this plan update before its commit.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/plans/2026-08-14-simpleloop-phase4-scheduler-worker.md
git commit -m "docs: complete phase four scheduling plan"
```
