# SimpleLoop Phase 5 Round + Loop Vertical Migration Design

**Status:** Approved for implementation (2026-08-14)

**Upstream specification:**
`2026-08-14-simpleloop-typed-pipeline-refactor-design.md`

## 1. Objective

Phase 5 replaces the remaining context-driven orchestration with two explicit
business pipelines and one Composition Root:

```text
app.run(AppRequest)
→ run_loop(LoopRequest)
→ run_round(RoundRequest) | RsiRunner.run(round_id)
→ proposer.propose
→ candidates.run
→ select
→ persist
```

The migration is vertical and real. It does not retain `RunContext`, wrap
`WorkerBackend` in a new facade, or leave a second orchestration path behind.

## 2. Current Problem

`simpleloop/loop.py` is 890 lines and currently owns configuration, run locking,
provider construction, preflight, baseline, resume, task/RSI branching,
proposing, candidate fan-out, selection, telemetry finalization, persistence,
plotting, summary generation, and CLI-facing error behavior.

`simpleloop/execution/backend.py` is a 409-line Phase-4 bridge. It consumes the
mutable `RunContext`, reads configuration dictionaries, prepares every domain
payload, calls the scheduling kernel, decodes all domain results, and handles
telemetry. Its module docstring already declares that Phase 5 must remove it.

`simpleloop/round.py` currently contains only `RoundResult`; the Round Pipeline
does not yet exist. `HistoryStore.append_round` also appends blindly, leaving a
crash window between history append and inflight clear.

## 3. Scope

Phase 5 includes:

- pure `run_round` and `run_loop` functions;
- frozen Round/Loop request and result values;
- explicit Proposer, CandidateRunner, RsiRunner, history, checkpoint, recorder,
  and observer ports;
- `app.py` as the only Composition Root;
- explicit scheduled task and RSI transport adapters;
- idempotent round history and atomic stage transition;
- removal of `RunContext`, `WorkerBackend`, and `simpleloop/execution/`;
- migration of CLI, reporting, telemetry, static proposals, resume, and tests.

Phase 5 explicitly excludes:

- the Phase-6 RSI body/history/event-stream rewrite;
- the Phase-7 new config schema and `legacy_config.py`;
- wholesale deletion of `harness/`, `roles/`, or other Phase-7 compatibility
  modules;
- changes to candidate, gate, selection, proposal, evaluation, or RSI adoption
  semantics;
- new dependencies or new user configuration fields.

## 4. Round Pipeline

### 4.1 Contracts

`simpleloop/round.py` owns:

```python
@dataclass(frozen=True)
class SelectionPolicy:
    objective_key: str
    lower_is_better: bool
    require_improvement: bool = True

@dataclass(frozen=True)
class RoundRequest:
    round_id: int
    goal: str
    incumbent_sha: str
    incumbent_metrics: Mapping[str, object]
    selection: SelectionPolicy

@dataclass(frozen=True)
class CandidateBatchResult:
    candidates: tuple[CandidateResult, ...]
    telemetry: Mapping[str, object] = field(default_factory=dict)

class Proposer(Protocol):
    def propose(self, request: ProposerRequest) -> ProposalBatch: ...

class CandidateRunner(Protocol):
    def run(self, request: CandidateBatchRequest) -> CandidateBatchResult: ...

class RoundRecorder(Protocol):
    def record_proposals(
        self, request: RoundRequest, proposals: ProposalBatch
    ) -> None: ...
```

`RoundResult` remains the terminal domain fact and retains `next_sha`.

### 4.2 Function

```python
def run_round(
    request: RoundRequest,
    *,
    proposer: Proposer,
    candidates: CandidateRunner,
    recorder: RoundRecorder,
) -> RoundResult:
    ...
```

Data flow is fixed:

```text
ProposerRequest(round_id, goal, incumbent_sha)
→ ProposalBatch
→ record_proposals before candidate launch
→ abstention: empty CandidateBatchResult
  or proposals: CandidatePlan[] → CandidateBatchRequest → CandidateBatchResult
→ select_candidate
→ RoundResult
```

Static mode is implemented by `StaticProposer`; `run_round` never branches on
CLI/config/static mode. Infrastructure and protocol exceptions propagate to
`run_loop`. Business abstention, failure, no-change, gate rejection, and no
improvement remain typed results.

## 5. Loop Pipeline

### 5.1 Contracts

`simpleloop/loop.py` is reduced to pipeline contracts and state advancement:

```python
@dataclass(frozen=True)
class LoopState:
    next_round: int
    incumbent_sha: str
    incumbent_metrics: Mapping[str, object]

@dataclass(frozen=True)
class LoopRequest:
    goal: str
    stop_round: int
    state: LoopState

@dataclass(frozen=True)
class RsiResult:
    round_id: int
    decision: str

@dataclass(frozen=True)
class LoopResult:
    state: LoopState
    task_rounds: int
    rsi_rounds: int
    rsi_tally: Mapping[str, int]
    interrupted: bool = False
    interruption: str | None = None

class RoundRunner(Protocol):
    def run(self, request: RoundRequest) -> RoundResult: ...

class RsiRunner(Protocol):
    def due(self, round_id: int) -> bool: ...
    def run(self, round_id: int) -> RsiResult: ...

class RoundHistory(Protocol):
    def append_round(self, result: RoundResult) -> None: ...

class CheckpointStore(Protocol):
    def clear(self) -> None: ...

class LoopObserver(Protocol):
    def round_committed(self, result: RoundResult) -> None: ...
```

### 5.2 Function

```python
def run_loop(
    request: LoopRequest,
    *,
    rounds: RoundRunner,
    rsi: RsiRunner,
    history: RoundHistory,
    checkpoint: CheckpointStore,
    observer: LoopObserver,
) -> LoopResult:
    ...
```

For each round id:

```text
rsi.due?
  yes → rsi.run → tally → next id
  no  → rounds.run
        → history.append_round + fsync
        → checkpoint.clear
        → observer.round_committed
        → advance incumbent and incumbent metrics
```

`InfrastructureError` returns an interrupted result without consuming the
round or clearing inflight. `ProtocolError`, configuration errors, and
programmer errors propagate. The pipeline does not print, read config, inspect
environment variables, construct providers, generate plots, or write summary.

## 6. Scheduling Domain Adapters

### 6.1 Generic WorkerJobs

`simpleloop/scheduling/jobs.py` provides the one domain-neutral client above
`JobSupervisor`:

```python
@dataclass(frozen=True)
class WorkerJob:
    kind: str
    request_id: str
    payload: Mapping[str, object]
    result_dir: Path
    workspace: SourceWorkspace | None = None

@dataclass(frozen=True)
class WorkerJobPolicy:
    python: str
    retry: RetryPolicy
    resources: ResourceSpec

class WorkerJobs:
    def run(
        self,
        *,
        stage: str,
        round_id: int,
        context: Mapping[str, object],
        jobs: tuple[WorkerJob, ...],
        max_parallel: int,
        transition_from: str | None = None,
    ) -> JobBatchResult: ...
```

It owns `JobSpec`/manifest/log argv construction only. It receives Scheduler,
Supervisor, JobJournal, and policy explicitly. It does not read config or know
candidate/proposer/RSI types.

### 6.2 Task Adapters

`simpleloop/scheduling/task.py` provides:

```python
ScheduledProposer.propose(ProposerRequest) -> ProposalBatch
ScheduledCandidates.run(CandidateBatchRequest) -> CandidateBatchResult
ScheduledBaseline.evaluate(BaselineRequest) -> BaselineResult
```

They receive explicit run directory, workspace provider, limits, prompt path,
worker client, telemetry sink, and metrics spec. They prepare only their own
payloads and decode only their own domain results.

`ScheduledProposer` is idempotent by round:

- active `proposer` stage resumes its worker result;
- active `candidates` stage reconstructs the already accepted proposals from
  candidate context without invoking proposer again;
- a new round creates one lane workspace and starts `proposer`.

`ScheduledCandidates` atomically transitions the journal from `proposer` to
`candidates`; it never clears terminal candidate inflight. Candidate envelope
usage is ingested and each returned candidate receives its telemetry snapshot.

### 6.3 RSI Transport

`simpleloop/scheduling/rsi.py` provides explicit `ScheduledSelfReview` and
`ScheduledViability` adapters over the same `WorkerJobs`. They contain only
payload/result conversion. `SelfRepo`, self-execution, review ledger, and
adoption remain in the Phase-6 transitional `LegacyRsiRunner`.

## 7. Composition Root

`simpleloop/app.py` owns the public run entry:

```python
@dataclass(frozen=True)
class AppRequest:
    config_path: Path
    run_dir: Path
    proposals: Path | tuple[str, ...] | None = None
    resume: bool = False
    target_rounds: int | None = None
    prompt_dir: Path | None = None

def run(request: AppRequest) -> Mapping[str, object]: ...
```

It alone performs:

```text
load current config
→ validate CLI mode combinations
→ resolve/create run_dir and lock it
→ snapshot resolved/original config
→ build Workspace, Sandbox/World, Scheduler, Supervisor, WorkerJobs
→ build ScheduledProposer/Candidates/Baseline/RSI
→ initialize workspace and SelfRepo
→ preflight
→ baseline + resume-state projection
→ construct RoundRunner and LegacyRsiRunner
→ run_loop
→ summary/reporting
```

`simpleloop/cli.py` calls only `app.run(AppRequest(...))` for the run command.
The existing external CLI flags and current configuration schema remain
unchanged.

## 8. Persistence and Crash Semantics

### 8.1 JobJournal Transition

```python
JobJournal.transition(
    expected_stage: str,
    stage: str,
    round_id: int,
    context: Mapping[str, object],
    jobs: Sequence[Mapping[str, object]],
) -> JournalRecord
```

It performs one atomic replacement and rejects a missing/mismatched active
stage. Proposer completion remains journaled until candidate transition or
terminal abstention history commit.

### 8.2 Idempotent History

`Store.append_round` reads existing rows before writing:

- no row with round id: append, flush, and fsync;
- one structurally equal row: no-op;
- same round id with different content: raise `HistoryConflictError`;
- duplicate ids already on disk: raise `HistoryConflictError`.

This makes the order safe:

```text
append round
→ process crash is possible
→ resume sees identical history and no-ops
→ clear inflight
```

The current legacy history projection remains byte-compatible apart from the
new fsync/idempotency behavior.

### 8.3 Round Artifacts

`simpleloop/persistence/round_artifacts.py` implements `RoundRecorder` and
writes proposer trace plus `proposals.json` immediately after proposal output,
before candidate execution. These are non-authoritative artifacts; journal and
history remain authoritative.

## 9. Reporting

`simpleloop/reporting/summary.py` builds and atomically writes `summary.json`
from explicit `HistoryStore`, baseline metrics, baseline SHA, and workspace
facts. Progress plotting is invoked by an app-owned `LoopObserver` only after
history commit and inflight clear.

No reporting module imports `loop.py` or provider implementations.

## 10. RSI Boundary

Phase 5 adds a `LegacyRsiRunner` implementing the new loop port using current
`SelfRepo` behavior. It preserves:

- review scheduling and default deferral;
- KEEP/CHANGE decisions;
- self-executor behavior;
- viability call and adoption;
- `reviews.jsonl` and `state.json` formats.

The class is explicitly transitional and deleted in Phase 6. Phase 5 does not
invent partial self-event history or alter adoption authority.

## 11. Deletions

After consumers migrate, delete:

```text
simpleloop/execution/backend.py
simpleloop/execution/__init__.py
```

Remove from production:

```text
RunContext
_build_context
_starting_state
_next_proposals
_finalize_candidates
_run_self_review_round
_summary
the old monolithic _run_locked round loop
```

Compatibility aliases or forwarding facades for these owners are not retained.
Tests migrate to public pipeline/adapters rather than old private functions.

## 12. Testing

### Unit

- `run_round`: normal fan-out, abstention, selection, static proposer,
  telemetry, infrastructure propagation.
- `run_loop`: Task/RSI branch, incumbent advance, no-winner retention,
  history-before-clear ordering, infrastructure interruption, protocol
  propagation, observer ordering.
- Journal transition and history idempotency/conflict.
- WorkerJobs and every scheduled domain adapter with fake supervisor/workspace.
- summary and round-artifact writers.

### Integration

- current fake-port loop integration migrated away from `RunContext`;
- CLI constructs `AppRequest` and calls app;
- Local/HEPJob composition produces identical domain adapters;
- static mode, baseline, resume, telemetry, export, plotting, RSI KEEP/CHANGE,
  viable adoption, and non-viable rejection remain covered;
- three crash windows: proposer→candidate, candidate→history, history→clear.

### Architecture

- zero `RunContext` and `WorkerBackend` occurrences in production;
- `simpleloop/execution/` absent;
- `loop.py` imports no config, YAML, OS, Workspace, Scheduler, proposer package,
  reporting, or provider implementation;
- `round.py` imports no config, Journal, Scheduler, provider, or proposer
  package implementation;
- only `app.py` selects Local versus HEPJob;
- proposer package still has zero SimpleLoop imports.

## 13. Acceptance

Phase 5 is complete when:

```text
run_round and run_loop are directly callable typed functions
RunContext = 0
WorkerBackend = 0
simpleloop/execution = absent
one scheduler/supervisor/worker/journal path remains
history append is idempotent and fsynced
Local and HEPJob share the same Round and Loop pipelines
current config and CLI remain compatible
RSI behavior is unchanged
all tests and compileall pass
architecture scans and git diff --check pass
```
