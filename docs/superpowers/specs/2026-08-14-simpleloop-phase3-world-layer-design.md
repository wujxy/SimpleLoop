# SimpleLoop Phase 3 World Layer Design

**Status:** Approved for implementation on `refactor/phase0-phase1-typed-contracts`.

## 1. Goal

Phase 3 extracts one real World boundary from the existing Git worktree and
Apptainer implementations:

```text
add SourceWorkspace, ExecutionSandbox, and World boundaries
→ migrate their existing consumers
→ delete the old SimpleLoop Workspace and Runtime owners
```

After this phase, Git workspace state has one owner, Apptainer is one sandbox
adapter rather than a domain dependency, and agents can access only a
Host-prepared World while retaining required network access.

Every refactor phase follows the same rule: introduce one boundary that is used
in production, migrate the responsibility, delete its previous owner, and run
the relevant behavior and architecture tests. Empty facades and simultaneous
cross-phase rewrites are not acceptable.

## 2. Scope

Phase 3 covers the SimpleLoop Host and candidate execution path. The standalone
`proposer` package remains independent: it does not import `simpleloop.world`,
share live sandbox objects, or move back under Host ownership. Its existing
runtime adapter remains private to that package. Phase 4 may pass a serialized
World specification through the worker protocol, but will not share package
implementation.

Phase 3 does not unify Local and HEPJob scheduling, redesign the worker
envelope, remove `RunContext`, build the final Composition Root, restructure
RSI, add tree evolution, introduce a second sandbox provider, or change the
public YAML format.

## 3. Package Ownership

Create one focused package:

```text
simpleloop/world/
├── __init__.py
├── contracts.py    # frozen values and narrow protocols
├── git.py          # GitWorkspaceProvider
├── apptainer.py    # ApptainerSandbox
└── builder.py      # World, WorldSpec, and WorldBuilder
```

`simpleloop/world/contracts.py` owns mechanism-neutral inputs and outputs.
`git.py` is the only module that understands Git clone/worktree/ref commands.
`apptainer.py` is the only SimpleLoop module that constructs Apptainer argv,
binds, launcher environments, or preflight processes. `builder.py` binds one
source workspace to one sandbox policy without interpreting proposals,
evaluation metrics, gates, scheduling, or RSI.

The existing `simpleloop/harness/workspace.py` and
`simpleloop/container/runtime.py` are deleted after all consumers migrate.
The proposer package's separate `proposer/runtime.py` is explicitly excluded
from this deletion.

## 4. Contracts

### 4.1 Source workspace

```python
@dataclass(frozen=True)
class WorkspaceSpec:
    workspace_id: str
    revision: str


@dataclass(frozen=True)
class SourceWorkspace:
    workspace_id: str
    path: Path
    base_sha: str


@dataclass(frozen=True)
class ChangeSet:
    paths: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class CommitRequest:
    round_id: int
    candidate_id: int
    parent_sha: str
    changed_paths: tuple[PurePosixPath, ...]


class WorkspaceProvider(Protocol):
    def initialize(self) -> str: ...
    def create(self, spec: WorkspaceSpec) -> SourceWorkspace: ...
    def remove(self, workspace: SourceWorkspace) -> None: ...

    @contextmanager
    def open(self, spec: WorkspaceSpec) -> Iterator[SourceWorkspace]: ...

    def inspect(self, workspace: SourceWorkspace) -> ChangeSet: ...
    def commit(
        self,
        workspace: SourceWorkspace,
        request: CommitRequest,
    ) -> CandidateArtifact: ...
    def diff(self, parent_sha: str, child_sha: str) -> str: ...
```

`GitWorkspaceProvider.initialize()` idempotently prepares the per-run clone and
returns the resolved baseline SHA. The provider retains candidate commit
reachability, concurrent worktree isolation, stale-worktree recovery, detached
lane behavior, diff, and Harness-owned commit semantics. Concrete directory and
ref layout do not enter the public workspace contract; persisted worker
manifests continue to carry the resolved path they already use.

`open()` is the safe synchronous `create()`/`remove()` composition. Local and
baseline execution use it. HEPJob temporarily uses explicit `create()` and
`remove()` because a workspace outlives one frontend stack frame and must
survive scheduler polling and frontend restart. Phase 4 moves that asynchronous
resource lifecycle into `JobSupervisor`.

The Phase 2 `CommitRequest.worktree` field is deleted: the workspace is the
explicit first argument to `WorkspaceProvider.commit()`. `ArtifactWorkspace`
similarly changes from naked `Path` arguments to `SourceWorkspace` values.

### 4.2 Sandbox process

```python
class MountMode(str, Enum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


@dataclass(frozen=True)
class MountSpec:
    source: Path
    target: PurePosixPath
    mode: MountMode = MountMode.READ_ONLY


@dataclass(frozen=True)
class SandboxSpec:
    image: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    network: bool = True


@dataclass(frozen=True)
class ProcessRequest:
    argv: tuple[str, ...]
    cwd: PurePosixPath
    timeout_seconds: int
    stdin: str | None = None
    label: str = ""


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


class ExecutionSandbox(Protocol):
    def run(self, request: ProcessRequest) -> ProcessResult: ...


class SandboxProvider(Protocol):
    def bind(
        self,
        spec: SandboxSpec,
        mounts: tuple[MountSpec, ...],
    ) -> ExecutionSandbox: ...
```

`ProcessRequest.argv` is always executed shell-free. A caller that needs shell
syntax requests it explicitly with an argv such as
`("bash", "-lc", command)`. A timed-out process is terminated as a process
group and returns `timed_out=True`; `exit_code` records the final negative
signal code. Failure to locate or launch the sandbox/process raises a sandbox
infrastructure error instead of pretending that a payload exited normally.

### 4.3 World

```python
@dataclass(frozen=True)
class WorldSpec:
    workspace_mode: MountMode
    writable_paths: tuple[PurePosixPath, ...] = ()
    external_mounts: tuple[MountSpec, ...] = ()


@dataclass(frozen=True)
class World:
    workspace: SourceWorkspace
    sandbox: ExecutionSandbox

    def run(self, request: ProcessRequest) -> ProcessResult:
        return self.sandbox.run(request)


class WorldBuilder:
    def __init__(self, provider: SandboxProvider): ...

    def build(
        self,
        workspace: SourceWorkspace,
        sandbox: SandboxSpec,
        world: WorldSpec,
    ) -> World: ...
```

Separating `SandboxSpec` from `WorldSpec` is necessary because the same image
and provider execute two different views of one source workspace. WorldBuilder
validates and resolves the workspace base mount, writable overlays, and
external mounts, then asks its `SandboxProvider` for one bound
`ExecutionSandbox`. This keeps filesystem policy out of Agent and Evaluator
code. `WorldBuilder` is a small composition service, not an alternative
scheduler or business pipeline.

## 5. Candidate Data Flow

### 5.1 Local

```text
CandidatePlan
→ GitWorkspaceProvider.open(WorkspaceSpec)
→ SourceWorkspace
→ WorldBuilder.build(executor policy)
→ WorldBuilder.build(evaluator policy)
→ run_candidate
→ commit, evaluate, gate
→ context-managed cleanup
```

`CandidateRequest.worktree: Path` becomes
`CandidateRequest.workspace: SourceWorkspace`. Artifact, Executor, and
Evaluator adapters receive the descriptor or a prepared World rather than a
naked host path. The typed Candidate Pipeline retains its Phase 2 ordering and
statuses.

### 5.2 HEPJob

```text
frontend GitWorkspaceProvider.create
→ existing manifest records workspace id/path/base SHA
→ worker reconstructs SourceWorkspace
→ worker builds Executor and Evaluator Worlds
→ shared run_candidate
→ frontend GitWorkspaceProvider.remove
```

Condor manifest, retry, resume, poll, collect, `_FINISHED`, result codec, and
inflight journal semantics do not change in this phase. The worker composes the
new World interfaces from the same resolved configuration; it does not receive
a new Phase 4 worker envelope early.

### 5.3 Baseline

Baseline evaluation uses an Evaluator World over a baseline
`SourceWorkspace`. It no longer reaches the baseline through the legacy mode
that implicitly mounts the entire run directory read-write.

## 6. World Policies

### 6.1 Executor World

- The complete worktree is visible at `/work` read-only.
- Each configured `editable_paths` entry is overlaid read-write.
- Configured `read_only_binds` are explicit read-only external mounts.
- HOME is an isolated temporary directory owned by the World lifecycle.
- Network access is enabled because the agent response requires it.
- Only model credentials, proxy/SSL settings, and explicitly approved process
  variables enter the clean environment.

### 6.2 Evaluator World

- It references the same `SourceWorkspace` as the Executor World.
- The workspace is read-write to preserve current build, cache, and evaluation
  command behavior.
- Existing `runtime.binds` are represented as explicit compatibility mounts.
- Network access remains enabled.
- Anthropic/model credentials are not injected into evaluator processes;
  proxy and certificate settings may be forwarded.
- The enclosing run directory and host filesystem are not implicitly exposed.

Evaluator commands are trusted Harness operations, not untrusted agent work,
but they still execute inside an explicit World. The two Worlds share source
state and do not share filesystem or environment policy.

## 7. Apptainer Adapter

`ApptainerSandbox` exclusively owns:

- shell-free Apptainer argv construction;
- `--cleanenv`, containment, cwd/home/hostfs suppression, and user namespace
  selection;
- typed mount conversion and network policy;
- environment allowlisting and Apptainer environment injection;
- subprocess timeout, process-group cleanup, lifecycle heartbeat, duration,
  stdout, and stderr;
- provider and World preflight;
- secret-free startup summaries.

Business and stage modules no longer call or mention `exec_argv()`,
`subprocess_env()`, `world_mount_map()`, `MountMap`, raw bind strings, or
Apptainer flags.

Preflight has two levels. Provider preflight validates the executable, image,
and basic sandbox startup. World preflight executes the current sentinel-based
checks against the exact HOME and RO/RW mount policy. It proves that `/work` is
visible, the base is not writable, declared overlays are writable, and external
mounts exist. Baseline and evaluator Worlds receive the corresponding policy
check rather than borrowing the executor check.

## 8. Mount Safety

- Mount mode defaults to read-only.
- Container targets are normalized absolute `PurePosixPath` values.
- Empty targets, `..` traversal, and duplicate targets are rejected.
- A workspace writable source must remain beneath the resolved current
  `SourceWorkspace.path`.
- Writable overlays reject symlink traversal out of the workspace.
- External sources must be absolute and explicitly supplied by resolved
  configuration.
- Host HOME, cwd, and hostfs are not mounted implicitly.
- Credentials never appear in argv, manifests, summaries, or errors.

This is defense against an agent changing or reading anything outside the
prepared World. It is not a promise to resist kernel/container escape or to
prevent exfiltration of data that the Host deliberately mounted or injected.

## 9. Consumer Migration

### 9.1 Agent and Executor

`roles.agent.Agent` receives a World instead of `ApptainerRuntime` and
`MountMap`. Agent remains responsible for Claude CLI arguments, semantic
process labels, response envelope decoding, and usage telemetry. Sandbox uses
the label for lifecycle heartbeat and is responsible for process startup,
environment, timeout, and cleanup.
`AgentExecutor` remains a business adapter and never imports a concrete
sandbox.

### 9.2 Evaluator

`HarnessEvaluator` receives an Evaluator World. Evaluation command order,
per-command timeout, output cap, metric parsing, baseline validation, and gate
semantics remain unchanged. `harness/evals.py` is reduced to pure evaluation
command orchestration and metric parsing or its remaining pure helpers are
moved to the smallest existing owner; it does not know Apptainer.

### 9.3 Artifacts

`GitArtifactWorkspace` uses `SourceWorkspace` and `WorkspaceProvider` to
inspect and commit. The Candidate Pipeline does not import the concrete Git
provider.

### 9.4 Composition and backends

During Phase 3, `loop.py`, `candidate_worker.py`, and `initialize.py` may still
instantiate `GitWorkspaceProvider`, `ApptainerSandbox`, and World policies as
temporary composition sites. They do not construct argv or bind strings.
Final assembly moves to `app.py` in Phase 5. Local and HEPJob backends manage
location/lifecycle only and do not interpret sandbox policy.

## 10. Errors

The mechanism layer exposes only the distinctions consumers can act on:

```python
class WorkspaceError(RuntimeError): ...
class SandboxError(RuntimeError): ...
class SandboxLaunchError(SandboxError): ...
```

No larger exception hierarchy is added. Stage adapters map mechanism outcomes
into the existing business results:

- agent nonzero exit or timeout becomes `EXECUTOR_FAILED`;
- evaluation nonzero exit becomes an `EVAL_COMMANDS` gate failure;
- evaluation launch failure becomes `EVAL_FAILED`;
- unexpected workspace creation, inspection, or commit failure is normalized
  by `run_candidate_guarded`;
- baseline failures remain `BaselineAcceptanceError`.

## 11. Incremental Migration

1. Add frozen contracts and fake-based contract tests.
2. Move the real Git implementation into `GitWorkspaceProvider` and migrate
   run setup, candidate, baseline, and lane consumers.
3. Extract the real Apptainer process implementation into
   `ApptainerSandbox`, preserving current argv and environment behavior where
   policy has not explicitly changed.
4. Add `WorldBuilder` and the Executor/Evaluator World policies.
5. Migrate Agent, Evaluator, and Artifact adapters.
6. Migrate Local, candidate worker, and baseline composition.
7. Migrate HEPJob workspace usage without changing scheduler protocols.
8. Delete the old SimpleLoop workspace/runtime owners and compatibility
   symbols.
9. Add executable architecture guards and run complete verification.

Each step begins with a failing focused test, ends with a passing relevant
suite, and is committed independently.

## 12. Verification

Unit and integration coverage includes:

- frozen contract behavior;
- Git setup, baseline, create/open/remove, commit/diff, concurrent isolation,
  stale cleanup, lane independence, and candidate commit reachability;
- mount RO/RW conversion, normalized targets, duplicate rejection, symlink
  escape rejection, external mounts, and network on/off;
- clean environment, credential separation, and secret-free summaries;
- timeout, process-group cleanup, and launch failures;
- WorldBuilder composition and lifecycle with fake providers/sandboxes;
- Agent and Evaluator behavior with fake Worlds;
- Local and HEPJob candidate characterization;
- baseline, worker result, history, resume, and RSI golden fixtures;
- full pytest, compileall, and diff checks.

Architecture tests enforce:

```text
candidate.py and candidate stages do not import Apptainer or concrete Git
roles/agent.py does not import Apptainer
execution/local.py and execution/hepjob.py do not construct argv or binds
old simpleloop/container/runtime.py does not exist
old simpleloop/harness/workspace.py does not exist
proposer does not import simpleloop.world
```

Real Apptainer and Condor checks remain environment-dependent; deterministic
argv/process/provider tests use fakes when those executables are unavailable.

## 13. Completion Criteria

Phase 3 is complete only when:

- production Local and HEPJob paths use `SourceWorkspace` and World adapters;
- Executor and Evaluator run in their explicit, independently configured
  Worlds;
- all SimpleLoop Apptainer argv, bind, environment, and preflight logic has one
  concrete owner;
- all SimpleLoop Git clone/worktree/commit/diff logic has one concrete owner;
- the two old owner files and their compatibility symbols are deleted;
- no candidate business module depends on Apptainer;
- proposer remains independent;
- existing selection, history, worker, resume, baseline, and RSI behavior tests
  pass.
