# Executor Sandbox Recovery and Single-Lane Proposer Design

## Goal

Restore the Executor's ability to use Bash, edit the declared workspace, verify
changes, and deliver a Harness-owned commit without exposing the host home or
protected evaluator assets. In the same phase, remove the current proposer
lane quota/thread-pool scheduler so one lane generates ten hypotheses and the
same cognitive element selects up to `candidates_per_round` proposals.

## Scope

This is the first recovery phase only.

Included:

- a private writable home for every Executor session;
- role-specific external mounts so evaluator binds never leak to Executor;
- an Executor-specific preflight using the production mount path;
- one proposer lane per round in both local and HEPJob execution;
- process-group cleanup when an Executor exits abnormally or the run receives
  `SIGTERM`;
- unit, Apptainer integration, and one real disposable Executor smoke test.

Deferred:

- first-class `BLOCKED`/`PARTIAL` history states;
- local in-flight round resume;
- benchmark repetitions, noise floors, or serialized performance evaluation;
- proposal ranking beyond the existing cognitive selection;
- tree evolution scheduling. Lane remains the future tree evolution unit, but
  this phase does not implement the tree scheduler.

## Confirmed Failure

The old and current runs use byte-identical SIF images. With the old Executor
arguments, `/home/wujxy` is writable. The current mount-world implementation
adds `--no-mount cwd,home,hostfs` without mounting a replacement home, making
the same path read-only. Claude's Bash tool therefore fails before commands
such as `true`, `pwd`, or `ls /work` execute.

The current implementation also applies every `runtime.binds` entry before it
constructs the Executor subset world. The OMILREC task binds the whole
`omilrec_opt` directory, so protected evaluator assets remain visible and
writable through their absolute paths even though they are absent from
`/work`.

## Executor Sandbox

### Session-owned filesystem

Every Executor call creates one temporary root with two children:

```text
simpleloop-exec-<random>/
├── work/    # empty /work scaffold
└── home/    # private writable container home
```

The runtime continues to disable the automatic host home mount. It explicitly
binds `home/` read-write to the container user's home path and injects the same
path as `HOME`. The real host home is never mounted. The temporary root exists
for the whole Claude subprocess lifetime and is removed afterward.

The declared worktree-relative mount map remains authoritative:

- `safety.editable_paths` appear below `/work` read-write;
- `safety.read_only_paths` appear below `/work` read-only;
- unlisted worktree paths remain absent.

### Role-specific external dependencies

`runtime.binds` remains the Harness/evaluator bind list. It is not inherited by
the Executor mount-world path.

A new optional field is added:

```yaml
runtime:
  executor_read_only_binds:
    - /cvmfs
    - /data/juno
```

Every entry must be an existing absolute directory and is always mounted
read-only at the same absolute path. No writable external Executor bind is
introduced. The OMILREC configs must not list the repository root, run root,
or evaluator root in this field.

`MountMap` carries these external read-only paths alongside its worktree
relative `rw` and `ro` paths. This keeps the capability declaration attached
to the Executor rather than to the shared runtime used by evaluation.

## Executor Preflight

The existing image preflight remains a lightweight check that Apptainer and
required binaries exist. A second Executor preflight runs after
`Workspace.setup()` and before baseline evaluation or proposal generation.

It creates a disposable worktree and the same private `work/` and `home/`
layout used by a real Executor. It calls `exec_argv()` with the production
`MountMap` and verifies:

1. the container working directory is `/work`;
2. `bash`, `git`, `node`, and `claude` resolve;
3. `$HOME` is the private mounted path and is writable;
4. `mkdir -p "$HOME/.claude"` and a temporary file operation succeed;
5. every declared read-write path is visible and writable;
6. every declared read-only path is visible and not writable;
7. a sentinel under an ordinary unlisted worktree path is absent;
8. each evaluator-only `runtime.binds` path is absent unless it is also
   explicitly declared in `executor_read_only_binds`.

Any failure raises `RuntimePreflightError` before the baseline, proposer, or
Executor consumes model time. The error identifies the failed capability.

The preflight does not make a model API request. Final acceptance includes one
separate real Claude smoke call because only that can prove the Bash tool and
delivery path together.

## Single-Lane Proposer

Lane remains a durable execution and telemetry boundary. Only the scheduler is
removed.

Each round performs this flow:

```text
lane-0 workspace
    -> sample 5 of 9 generative operations
    -> Generator emits 2 ideas per operation (10 hypotheses)
    -> one Cognitive batch audits the hypotheses
    -> selects and enriches up to candidates_per_round proposals
    -> candidate Executor fan-out remains unchanged
```

`candidates_per_round` means only the maximum number of proposals submitted to
Executor. It no longer controls proposer lane count.

Diversity remains a semantic preference. The cognitive prompt asks for
different mechanisms and code regions where useful. Existing signature
deduplication may reduce the batch, but the Generator is not asked to regenerate
merely to reach four unique proposals. Fewer than four proposals are valid when
the cognitive element selects fewer viable leads.

The following remain because a lane will later be a tree evolution unit:

- `LaneState` and `LaneResult`;
- `run_lane_episode()`;
- lane workspace lifecycle and `lane-0` directory;
- `ProposerLaneSpec` and the standalone lane worker;
- lane telemetry and HEPJob result format.

The following are removed:

- `_SELECT_PER_LANE`;
- `_MAX_LANE_WORKERS`;
- `lane_quotas()`;
- `_run_lanes()` and its proposer `ThreadPoolExecutor`;
- local and HEPJob loops that derive and submit multiple proposer lanes.

Local execution creates one `lane-0` workspace and calls the orchestrator once.
HEPJob execution submits one lane worker job with
`select_quota=candidates_per_round`.

## Cancellation and Cleanup

Every started agent subprocess group is registered in a small thread-safe
process-group registry. Normal completion unregisters it. Timeout and exceptions
terminate the group using the existing TERM-then-KILL behavior.

The run entry point installs a `SIGTERM` handler that marks cancellation and
terminates all registered local process groups before unwinding. Agent polling
detects cancellation and raises a distinct cancellation exception so a stopped
run is not recorded as an ordinary failed candidate. HEPJob keeps its existing
scheduler-specific orphan cleanup.

This phase guarantees cleanup, not local round resume.

## Acceptance Criteria

The change is accepted only when all of the following are demonstrated:

- unit tests prove Executor argv excludes ordinary `runtime.binds`, mounts only
  declared external read-only dependencies, and mounts a private writable home;
- the original EROFS condition is reproduced by a failing integration test and
  the fixed sandbox passes it with the configured SIF;
- the Executor preflight rejects a read-only/missing home and rejects a leaked
  protected sentinel;
- a real Claude smoke call can run Bash, read and edit a disposable worktree,
  return a self-report, and produce a Harness commit;
- the host home, source repository, and evaluator assets remain unchanged after
  the smoke call;
- local and HEPJob proposer tests each create exactly one lane/job and request
  up to four proposals from the same cognitive batch;
- sending `SIGTERM` to a test run leaves no registered Claude or Apptainer
  process group;
- the complete relevant pytest suite passes.

## Necessity Review

The private home, role-specific binds, and exact preflight directly address
observed failures and a proven isolation leak. Single-lane scheduling removes
the demonstrated duplicate-producing scheduler while retaining the lane
abstraction needed later. Status redesign, resume, and benchmark statistics do
not contribute to the immediate run/commit guarantee and are therefore omitted
from this phase.
