# SimpleLoop Phase 4 Scheduler + Worker Design

**Status:** Approved for implementation on `refactor/phase0-phase1-typed-contracts`.

## 1. Goal

Phase 4 replaces the duplicated Local/HEPJob lifecycle and candidate/proposer
worker protocols with one scheduling boundary:

```text
typed WorkerRequest
→ Scheduler
→ JobSupervisor
→ unified Worker
→ typed WorkerResult
```

Local and HEPJob differ only in where a process is launched. Manifest writes,
polling, timeout, retry, resume, result validation, collection, and asynchronous
workspace release have one owner. This is a real migration: every production
path moves to the new boundary and the previous owners are deleted.

The standalone `proposer` package remains independent. It never imports
`simpleloop`, receives a live Host object, or moves back under Host ownership.

## 2. Scope

Phase 4 migrates:

- local and HEPJob candidate batches;
- local and HEPJob proposer lanes;
- baseline evaluation;
- RSI self-review transport;
- RSI viability-smoke transport;
- worker manifests, results, usage, retry, resume, and workspace cleanup;
- the two existing inflight files into one journal.

Phase 4 does not create `run_round`, `run_loop`, or the final Composition Root,
remove `RunContext`, redesign selection/history, restructure SelfRepo Git state
or adoption, change optimization semantics, change YAML, add a general workflow
framework, or add a second remote scheduler.

RSI self-review and viability use the common transport because otherwise the
old proposer worker and a second subprocess/result collector could not be
deleted. RSI body/history/adoption remain Phase 6 responsibilities.

## 3. Selected Approach

Use a small standard-library scheduling kernel with two adapters:

- `LocalScheduler` owns local process creation, observation, and process-group
  cancellation.
- `HEPJobScheduler` owns Condor submit/query/remove and resource encoding.
- `JobSupervisor` owns all location-neutral lifecycle policy.
- one worker CLI dispatches typed envelopes to lazily imported handlers.

Do not retain separate candidate/proposer supervisors. A staged candidate-only
migration would preserve the duplicated system that Phase 4 exists to remove.

Do not add Parsl. Its executor, worker-manager, block-provisioning, and
distributed workflow model are larger than this loop's one-process-per-job
needs. Do not switch to HTCondor Python bindings in this phase: bindings can
replace CLI mechanics but cannot replace SimpleLoop's durable business result,
retry classification, or Host crash recovery, while the deployed configuration
already supports explicit Condor CLI selection.

## 4. Package Ownership

```text
simpleloop/
  scheduling/
    __init__.py
    contracts.py       # frozen scheduler values and narrow Scheduler protocol
    envelope.py        # worker v1 request/result codec and atomic result write
    supervisor.py      # sole poll/retry/resume/collect owner
    local.py           # local process adapter
    hepjob.py          # Condor adapter and resource encoding
    worker.py          # sole worker CLI and lazy handler dispatch
    handlers/
      __init__.py
      candidate.py     # candidate/baseline Host handler
      proposer.py      # proposer/self-review/viability Host adapter

  persistence/
    journal.py         # sole inflight journal owner

  execution/
    backend.py         # temporary Phase-5 bridge from domain requests to jobs
```

`execution/backend.py` may consume the current `RunContext` only as a temporary
composition bridge. It may prepare typed payloads and decode domain results,
but it may not submit, poll, retry, resume, write worker results, or implement a
provider-specific branch. Phase 5 deletes this bridge when it creates the final
Composition Root.

Delete after migration:

```text
simpleloop/candidate_worker.py
simpleloop/proposer_lane_worker.py
simpleloop/execution/local.py
simpleloop/execution/hepjob.py
simpleloop/execution/proposer_lanes.py
simpleloop/execution/base.py
```

## 5. Scheduling Contracts

```python
class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LOST = "lost"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    timeout_seconds: int
    disappearance_grace_seconds: int


@dataclass(frozen=True)
class ResourceSpec:
    cpus: int = 1
    memory_mb: int = 0
    requirements: str | None = None


@dataclass(frozen=True)
class JobSpec:
    request: WorkerRequest
    manifest_path: Path
    result_path: Path
    stdout_path: Path
    stderr_path: Path
    argv: tuple[str, ...]
    retry: RetryPolicy
    resources: ResourceSpec
    workspace: SourceWorkspace | None = None


@dataclass(frozen=True)
class JobHandle:
    scheduler: str
    value: str


@dataclass(frozen=True)
class JobObservation:
    handle: JobHandle
    state: JobState
    detail: str = ""


class Scheduler(Protocol):
    def submit(self, job: JobSpec) -> JobHandle: ...
    def inspect(
        self,
        handles: tuple[JobHandle, ...],
    ) -> tuple[JobObservation, ...]: ...
    def cancel(self, handle: JobHandle) -> None: ...
```

The concrete names may acquire narrowly necessary timestamp or log fields
during TDD, but no config dictionary, proposal, metric, World, or RSI value may
enter the Scheduler protocol.

`UNKNOWN` is distinct from `LOST`: a failed scheduler query does not advance a
disappearance timer and must never cause a retry.

## 6. JobSupervisor

```python
class JobSupervisor:
    def run_batch(
        self,
        request: JobBatchRequest,
        *,
        scheduler: Scheduler,
        journal: JobJournal,
    ) -> JobBatchResult: ...
```

The supervisor is the sole owner of:

1. atomic manifest writes;
2. maximum parallelism;
3. submission and persisted handles;
4. polling and state reconciliation;
5. running timeout and cancellation;
6. held/lost/no-result classification;
7. infrastructure-only retry;
8. worker-envelope validation and collection;
9. workspace release through `WorkspaceProvider`;
10. resume from the persisted job table.

Rules:

- A valid expected result envelope is business-terminal even if the scheduler
  still reports the process as present.
- A scheduler-terminal or disappeared process without a valid result is an
  infrastructure failure after the configured grace period.
- A malformed or mismatched envelope raises `ProtocolError` and is not retried.
- A handler's typed business failure is a valid result and is not retried.
- Timeouts, held jobs, lost jobs, and launch failures may retry up to
  `max_attempts`.
- Partial candidate/proposer success is returned; only an all-infrastructure
  failure raises `InfrastructureError` for the enclosing operation.
- A restored Local handle is not reattached to an untrusted PID. Its recorded
  process group is cancelled if still present, then the persisted manifest is
  retried. HEPJob handles are re-inspected by scheduler id.

## 7. Worker Protocol

The only executable entry is:

```bash
python -m simpleloop.scheduling.worker --manifest manifest.json
```

Request envelope:

```json
{
  "protocol": "simpleloop.worker.v1",
  "kind": "candidate",
  "request_id": "r3-c1",
  "payload": {},
  "result_path": "/shared/run/rounds/r3/candidates/c1/result.json"
}
```

Result envelope:

```json
{
  "protocol": "simpleloop.worker.v1",
  "kind": "candidate",
  "request_id": "r3-c1",
  "status": "completed",
  "result": {},
  "usage": [],
  "error": null,
  "execution": {}
}
```

Supported kinds are `candidate`, `proposer`, `self_review`, and `viability`.
Baseline evaluation is candidate handler mode `baseline`; it is not a fifth
protocol. Handlers accept/return JSON-domain mappings and report usage to the
worker. Only the worker writes the outer envelope.

The worker writes a temporary file, flushes and fsyncs it, calls `os.replace`,
then fsyncs the containing directory. The valid envelope is the completion
proof. `_FINISHED`, `usage.json`, and all worker-meta sidecars are deleted.

The worker dispatch table contains module/function strings and imports only the
selected handler. The proposer handler performs the run-local or candidate-self
`sys.path` redirect before its first `import proposer`. Generic scheduling code
never imports proposer.

## 8. Journal and Resume

The only durable active-stage file is:

```text
run_dir/inflight.json
```

It uses schema `simpleloop.inflight.v1` and contains `round_id`, `stage`, typed
stage context, and the complete persisted job table. Stages are `proposer`,
`candidates`, `self_review`, and `viability`. Only one stage is active; RSI moves
from self-review to viability sequentially rather than nesting journals.

Candidate context carries the parent SHA and serialized proposals needed to
resume without proposing again. History commit ordering remains:

```text
complete RoundResult
→ HistoryStore.append_round + fsync
→ JobJournal.clear
```

Baseline uses the same supervisor with a non-durable journal because rerunning
the baseline is idempotent and no task state precedes it.

Old history remains readable by reporting. New code does not continue the old
`inflight_round.json` or `inflight_proposer.json` formats, consistent with the
approved global rule that old run directories do not guarantee new-Kernel
`--continue` compatibility.

## 9. Production Data Flows

### 9.1 Candidate

```text
CandidateBatchRequest
→ create SourceWorkspaces
→ candidate WorkerRequests
→ JobSupervisor(LocalScheduler | HEPJobScheduler)
→ unified worker
→ existing run_candidate_guarded
→ WorkerResults
→ CandidateResults
→ release SourceWorkspaces
```

Local candidates move out of frontend threads and into subprocess workers.
`max_workers` becomes supervisor batch parallelism. The small process startup
cost buys identical isolation, timeout, retry, and result semantics locally and
on HPC.

### 9.2 Proposer

```text
ProposerRequest
→ create lane SourceWorkspace
→ proposer WorkerRequest
→ common supervisor
→ lazy proposer Host handler
→ ProposalBatch
```

The proposer package remains an independently importable and executable domain
package. The Host adapter is the only layer translating its output.

### 9.3 Baseline, Self-review, Viability

Baseline uses candidate mode `baseline`. Self-review and viability use their
own envelope kinds and the proposer handler. Viability receives the candidate
self path explicitly and evaluates the same observable behavior as today:
COMPLETED submit or abstention is viable; boot/import failure or lane failure is
not viable.

## 10. Error Contract

- `InfrastructureError(retryable=True)`: launch failure, held/lost process,
  timeout, missing result, temporary scheduler failure after policy exhaustion.
- `ProtocolError`: malformed JSON, wrong protocol, request id/kind mismatch,
  impossible persisted state. Never retried.
- Candidate executor/no-change/eval/gate outcomes remain `CandidateResult`.
- Proposer abstention/lane failure remains a proposer-domain terminal result.
- RSI KEEP/non-viable/reject remain RSI-domain terminal results.
- Config errors remain outside scheduling and handlers consume resolved config.

## 11. Compatibility and Boundaries

- No YAML schema change.
- No optimization, gate, selection, history, telemetry, or RSI adoption semantic
  change.
- Existing result domain codecs remain the source of candidate serialization.
- Telemetry usage moves inside the worker envelope and is ingested exactly once
  by the Host bridge.
- Condor command strings occur only in `scheduling/hepjob.py` and config.
- Process-group primitives occur only in `scheduling/local.py` and the existing
  sandbox process adapter.
- Pipeline/domain modules do not import scheduler implementations.
- `proposer/` has zero `simpleloop` imports.

## 12. Testing and Acceptance

TDD coverage includes:

- envelope round-trip, mismatch rejection, and atomic result replacement;
- journal round-trip, atomic updates, resume, and conflicting-stage rejection;
- supervisor success, partial success, concurrency, query UNKNOWN, held, lost,
  timeout, retry exhaustion, malformed result, and workspace cleanup;
- Local submit/inspect/cancel, process groups, and restored-handle behavior;
- HEPJob resource rendering, submit/query/remove, held/lost/query failure, and
  resume under fake Condor commands;
- all four worker dispatch kinds and lazy proposer imports;
- Local and HEPJob candidate/proposer/baseline integration through the same
  supervisor;
- RSI self-review and viability transport behavior;
- architecture guards for deleted legacy owners and single ownership.

Completion requires:

```text
worker CLI                         1
worker envelope                    1
JobSupervisor                      1
inflight journal                   1
Local/HPC retry/poll/resume        1 implementation
_FINISHED                          0 production occurrences
usage.json worker sidecar          0 production occurrences
old inflight names                 0 production occurrences
Condor outside HEPJob adapter      0 occurrences
proposer importing simpleloop      0 occurrences
legacy execution/worker owners     deleted
full pytest                        pass
compileall                         pass
git diff --check                   pass
```

## 13. Migration Rule

Every task follows:

```text
write a failing behavior/architecture test
→ add one live boundary
→ migrate production callers
→ delete the displaced responsibility
→ run focused tests
→ commit
```

No empty facade, duplicate supervisor, compatibility worker, optional second
protocol, generic plugin system, or speculative scheduler feature is allowed.
