# SimpleLoop Phase 6 RSI Vertical Migration Design

**Status:** Approved for implementation (2026-08-14)

**Upstream specification:**
`2026-08-14-simpleloop-typed-pipeline-refactor-design.md`

## 1. Objective

Phase 6 replaces the landed, monolithic RSI host implementation with explicit
body, history, and pipeline modules while preserving RSI semantics and keeping
the proposer an independent package:

```text
app.run
→ run_loop
→ RsiPipeline.due / RsiPipeline.run
→ reviewer → editor → viability
→ append terminal self event
```

The migration is vertical and real. It deletes `simpleloop/self_repo.py`, sends
all model work through the Phase-4 Scheduler/Worker path, and makes an append-only
self event stream the sole authority for active revision and review commitment.

## 2. Current Problem

`simpleloop/self_repo.py` is 597 lines and owns source snapshotting, direct Git
commands, mutable state, review history, self editing, viability preparation,
adoption, prompt construction, and RSI orchestration. This creates four concrete
problems:

- `state.json` and `reviews.jsonl` can disagree after a crash;
- adoption mutates the checkout before the review record is durable;
- self editing bypasses `WorkerJobs`, so Local and HEPJob do not share one
  retry/resume path;
- the proposer worker imports `SelfRepo` and discovers body/history inputs
  implicitly instead of receiving them in its manifest.

The current behavior is characterized by the RSI test suite and remains the
semantic baseline: KEEP, CHANGE, viable adoption, non-viable rejection,
commitment scheduling, independent proposer runtime, and run-local self life.

## 3. Scope and Non-Goals

Phase 6 includes:

- typed RSI request, result, decision, revision, candidate, event, and state;
- `SelfBodyStore`, `SelfHistoryStore`, `SelfReviewer`, `SelfEditor`, and
  `ViabilityChecker` ports;
- a Git body adapter that composes the existing `GitWorkspaceProvider`;
- one canonical, idempotent self event stream;
- a derived `reviews.jsonl` view for the unchanged independent proposer;
- a pure `run_rsi` pipeline and a small loop-facing runner;
- scheduled self-review, self-edit, and viability adapters;
- crash-resume across review, edit, commit, viability, terminal event, and
  checkpoint cleanup;
- removal of `SelfRepo`, `LegacyRsiRunner`, direct RSI subprocess/Git/result
  parsing, and the mutable `state.json` authority.

Phase 6 excludes:

- changes to RSI epistemology, prompts, KEEP/CHANGE semantics, or model roles;
- moving proposer code into `simpleloop` or importing proposer in a pipeline;
- self trees, lineage archives, multiple self candidates, or selection policy;
- Phase-7 config/schema/legacy-directory cleanup;
- automatic migration of run directories created by the pre-Phase-6 kernel;
- new dependencies or configuration fields.

## 4. Package and Dependency Structure

```text
simpleloop/rsi/
  __init__.py       narrow exports
  models.py         immutable domain values and protocols
  history.py        event codec, idempotent writer, state/review projections
  body.py           proposer seed and Git-backed revision workspaces
  pipeline.py       run_rsi and loop-facing RsiPipeline

simpleloop/scheduling/rsi.py
                    WorkerJobs adapters only
simpleloop/scheduling/handlers/rsi.py
                    self-edit worker adapter only
```

Dependency direction:

```text
rsi.models
    ↑
rsi.history   rsi.body   scheduling adapters
    ↑             ↑              ↑
             rsi.pipeline
                    ↑
                  app.py
```

`rsi.pipeline` may depend only on domain values and protocols. It must not
import config, JSON, filesystem providers, Git, subprocess, Apptainer, worker
envelopes, reporting, or the proposer package.

## 5. Domain Contracts

`simpleloop/rsi/models.py` defines frozen values:

```python
class SelfDecisionKind(str, Enum):
    KEEP = "KEEP"
    CHANGE = "CHANGE"

@dataclass(frozen=True)
class SelfRevision:
    sha: str

@dataclass(frozen=True)
class SelfChange:
    target: str
    intent: str
    instruction: str
    evidence_refs: tuple[str, ...] = ()

@dataclass(frozen=True)
class SelfDecision:
    kind: SelfDecisionKind
    diagnosis: str
    keep_reason: str | None = None
    change: SelfChange | None = None
    next_review_after_rounds: int | None = None

@dataclass(frozen=True)
class SelfState:
    active_sha: str
    next_review_round: int | None
    last_review_round: int | None

@dataclass(frozen=True)
class RsiRequest:
    round_id: int
    goal: str

@dataclass(frozen=True)
class RsiResult:
    round_id: int
    decision: SelfDecisionKind
    candidate_sha: str | None = None
    adopted: bool | None = None
    detail: str = ""
```

Ports are explicit:

```python
class SelfReviewer(Protocol):
    def review(self, request: SelfReviewRequest) -> SelfDecision: ...

class SelfEditor(Protocol):
    def edit(self, request: SelfEditRequest) -> SelfEditResult: ...

class ViabilityChecker(Protocol):
    def check(self, request: ViabilityRequest) -> ViabilityResult: ...

class SelfBodyStore(Protocol):
    def initialize(self, seed: Path) -> SelfRevision: ...
    def prepare_candidate(self, round_id: int, parent_sha: str) -> SourceWorkspace: ...
    def commit_candidate(self, workspace: SourceWorkspace,
                         request: SelfCommitRequest) -> SelfCandidate: ...
    def discard_candidate(self, workspace: SourceWorkspace) -> None: ...
    def materialize(self, sha: str) -> Path: ...

class SelfHistoryStore(Protocol):
    def state(self) -> SelfState: ...
    def events(self) -> tuple[SelfEvent, ...]: ...
    def append(self, event: SelfEvent) -> None: ...
    @property
    def review_view_path(self) -> Path: ...
```

The persistent candidate API is intentional: a context manager would remove a
worktree when a scheduled job is interrupted, destroying resumable filesystem
state. Candidate cleanup occurs only after a terminal self event is durable.

## 6. Canonical Self History

Run layout becomes:

```text
self/history.jsonl    canonical Host-owned event stream
self/reviews.jsonl    atomic, rebuildable proposer-facing projection
self/repo              body Git object database plus active runtime checkout
self/worktrees         resumable candidate workspaces
```

There is no `state.json`. RSI histories are small, so projecting state on load
is simpler and safer than maintaining a second authority.

Event kinds are:

```text
INITIALIZED
REVIEWED_KEEP
REVIEWED_CHANGE
CANDIDATE_CREATED
CANDIDATE_REJECTED
CANDIDATE_ADOPTED
```

Every event has a stable `event_id`, normally `r<round>:<kind>`. Append behavior
matches round history: absent id appends and fsyncs; identical id is a no-op;
different content for the same id raises `SelfHistoryConflictError`.

Projection rules:

- `INITIALIZED` establishes the first active SHA and optional first review;
- `REVIEWED_KEEP` is terminal and advances the commitment;
- `REVIEWED_CHANGE` records diagnosis and requested change but is non-terminal;
- `CANDIDATE_ADOPTED` is terminal, changes active SHA, and advances commitment;
- `CANDIDATE_REJECTED` is terminal, retains active SHA, and advances commitment;
- `CANDIDATE_CREATED` records immutable implementation facts only.

`reviews.jsonl` folds each terminal self round into the existing review record
shape. This preserves proposer behavior without making the independent proposer
understand Host recovery events. The file is replaced atomically after event
append and is rebuilt from `history.jsonl` whenever missing or inconsistent.

## 7. Git Body Semantics

`GitSelfBodyStore` owns only self-specific semantics:

- snapshot the `proposer/` seed into `self/repo` and create S0;
- compose `GitWorkspaceProvider` for candidate create/inspect/commit/remove;
- treat all candidate commits as immutable revisions addressable by SHA;
- materialize the event-projected active SHA into `self/repo` for worker import;
- never merge a candidate merely to adopt it.

The active checkout is a rebuildable runtime cache. `history.jsonl` remains the
authority. Startup always materializes the projected active SHA, so a crash
between ADOPT event append and checkout update is harmless.

Candidate creation accepts any parent SHA even though Phase 6 policy always
uses the current active SHA. This is the only tree-evolution preparation in
scope; no tree policy or archive is added.

## 8. RSI Pipeline and Recovery

`run_rsi` follows one fixed data flow:

```text
load state
→ review
→ KEEP: append REVIEWED_KEEP
→ CHANGE: append REVIEWED_CHANGE
  → prepare/resume candidate workspace
  → edit
  → executor failure or no change: append CANDIDATE_REJECTED
  → commit candidate idempotently
  → append CANDIDATE_CREATED
  → viability
  → append CANDIDATE_ADOPTED or CANDIDATE_REJECTED
→ terminal checkpoint cleanup
```

Business failures are typed terminal results: KEEP, missing/invalid change,
self-editor failure, no change, and non-viable candidate. Scheduler loss,
manifest/result corruption, inaccessible shared storage, and provider failure
remain infrastructure exceptions and do not consume the round.

Commit is idempotent: if the candidate worktree is clean and HEAD already
descends from the requested parent, that HEAD is the previously completed
candidate. This closes the crash window between Git commit and
`CANDIDATE_CREATED` append.

The loop-facing `RsiPipeline` supplies:

```python
RsiPipeline.prepare() -> None
RsiPipeline.due(round_id) -> bool
RsiPipeline.run(round_id) -> loop.RsiResult
```

`prepare` initializes or resumes body/history, materializes active SHA, and
clears an old RSI journal only when the corresponding terminal event is already
durable. It never clears an unfinished stage.

## 9. Scheduler and Worker Flow

One journal carries RSI through:

```text
self_review → self_edit → viability
```

- Self-review and viability continue to invoke the independent proposer through
  the existing lazy proposer handler.
- Self-edit gains worker kind `self_edit`; its handler builds the Phase-3 World,
  runs the existing Agent in the candidate body workspace, and returns a typed
  edit result. It does not inspect or commit Git.
- Scheduled adapters decode their domain result exactly once and do not clear
  the journal. The pipeline clears only after a terminal event.
- Manifest payloads explicitly carry active SHA, self body path, review view
  path, candidate workspace path, and result directory. The proposer handler no
  longer imports a Host body/history class.

This flow is identical under LocalScheduler and HEPJobScheduler because both
continue to use `WorkerJobs` and the shared filesystem contract.

## 10. Application Composition

`app.py` constructs:

```text
GitSelfBodyStore
JsonlSelfHistoryStore
ScheduledSelfReviewer
ScheduledSelfEditor
ScheduledViabilityChecker
RsiPipeline
```

It calls `rsi.prepare()` before reconstructing loop state. `_starting_state`
uses `SelfState.last_review_round`; no direct self file parsing remains in the
Composition Root. The loop continues to consume only its small `RsiRunner`
port and decision string.

Task proposer runs continue to import `proposer` from the materialized active
body. The proposer remains a top-level independent package and never imports
`simpleloop`.

## 11. Testing and Structural Gates

Tests cover:

- model validation and decision decoding;
- event codec, idempotent append, conflict detection, projection, and review
  view rebuilding;
- body initialization, arbitrary-parent candidate creation, idempotent commit,
  rejection cleanup, and active materialization;
- pure pipeline KEEP, adopt, editor rejection, viability rejection, and resume
  from every non-terminal event;
- stale-terminal journal reconciliation without clearing unfinished work;
- scheduled review/edit/viability stage transitions and result decoding;
- self-edit worker world boundaries;
- Local/HEPJob-neutral manifests;
- application composition and loop resume after RSI rounds.

Structural completion gates:

```text
simpleloop/self_repo.py absent
SelfRepo and LegacyRsiRunner absent
rsi/pipeline.py has no provider/config/subprocess/JSON imports
proposer package has zero simpleloop imports
self worker stages all pass through WorkerJobs
full pytest and compileall pass
```

## 12. Completion Condition

Phase 6 is complete when new runs and resumes use only the typed RSI modules,
the self event stream is the only authority, all three model stages use the
unified scheduler/worker path, the old host module is deleted, and the full
suite verifies unchanged user-visible RSI semantics.
