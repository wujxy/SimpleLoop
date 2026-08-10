# Standalone Proposer Harness Design

## Goal

Provide a standalone, repeatable test entry point for the complete SimpleLoop
Proposer pipeline. A user supplies the same task configuration used by the
normal loop; the harness runs Generator survey, lever-map synthesis and
hypothesis generation followed by Cognitive sieve/enrichment, then returns the
final proposals for human review. It does not run the Executor, candidate
evaluation, Gates, or the optimization loop.

The harness exists so the Proposer can be refactored and compared in isolation
without paying for candidate implementation or conflating proposal quality
with Executor and evaluator behavior.

## Scope

The MVP includes:

- one repository-local script, `scripts/proposer_harness.py`;
- the complete existing `ProposerOrchestrator` pipeline;
- fresh-history and existing-run-history modes;
- an isolated read-only source snapshot at one Git revision;
- deterministic seeding of SimpleLoop's Generator lens scheduler;
- one authoritative machine-readable result and one human-readable proposal
  report;
- failure recording and guaranteed temporary-worktree cleanup;
- automated unit and CLI tests.

The MVP excludes proposal scoring, an interactive UI, Executor invocation,
candidate implementation, benchmark execution, Gate execution, history
advancement, automatic comparison of multiple runs, and control of upstream
model sampling randomness.

## Command Interface

Fresh-history mode:

```bash
python scripts/proposer_harness.py \
  --config examples/omilrec-v100-opt/task.yaml \
  --output-dir proposer-tests/test-001
```

Existing-run-history mode:

```bash
python scripts/proposer_harness.py \
  --config examples/omilrec-v100-opt/task.yaml \
  --from-run runs/omilrec-v100-generator-proposer-008 \
  --output-dir proposer-tests/test-002
```

Reproducible SimpleLoop scheduling:

```bash
python scripts/proposer_harness.py \
  --config examples/omilrec-v100-opt/task.yaml \
  --output-dir proposer-tests/test-003 \
  --seed 42
```

Arguments:

- `--config` is required and is loaded through the existing config loader.
- `--output-dir` is required and must not already contain a completed result.
- `--from-run` is optional. When present, the Cognitive side reads that run's
  existing history and Search Memory, and the default base revision is the
  run's current accepted SHA. The source run is never modified.
- `--seed` is optional. It fixes Python-side Generator lens scheduling. It does
  not claim to make remote model responses deterministic.

The task config remains the source of the goal, editable and frozen paths,
Gates, proposer model, runtime image, runtime binds, candidate width, and
Generator/Cognitive step budgets. Executor credentials are not checked and no
Executor object is constructed.

## Architecture

The implementation is a thin adapter around the existing
`ProposerOrchestrator.run()` interface:

```text
scripts/proposer_harness.py
  -> standalone proposer runner
      -> load task config
      -> resolve history source and base SHA
      -> create isolated Workspace and source worktree
      -> construct ApptainerRuntime, model and MemoryService
      -> call ProposerOrchestrator.run()
      -> serialize result.json and proposals.md
      -> remove temporary worktree
```

No Generator, Cognitive, or orchestration logic is copied. Refactoring the
production Proposer therefore changes both normal-loop and standalone behavior
through the same implementation.

The runner is separate from `loop._build_context()` because that function also
constructs an Executor, validates Executor credentials, creates evaluation
state, and initializes execution backends. Reusing it would violate the
standalone command's boundary.

## Components

### Script entry point

`scripts/proposer_harness.py` parses arguments, runs the harness, prints the
proposal count and artifact paths, and maps expected setup/model/proposer
errors to a non-zero exit. `simpleloop/cli.py` does not register or import this
testing entry point.

### Standalone runner

The same focused script owns input resolution, runtime and workspace setup,
exact invocation of `ProposerOrchestrator`, artifact serialization, and
cleanup. It exposes a Python function so tests and future repository-local
comparison tools do not need to spawn a subprocess.

The function returns a small summary containing the result path, report path,
proposal count, abstention state, and base SHA.

### Existing pipeline

The runner instantiates the existing model transport, runtime,
`ProposerOrchestrator`, `Workspace`, and `MemoryService`. It passes the same
goal, paths, Gates, width, hints and step budgets that `loop._next_proposals()`
passes today.

## Input and History Semantics

### Fresh-history mode

The output directory acts as an empty proposer run directory. Its
`MemoryService` sees no prior experiments, findings, frontier, or abstentions.
The Generator remains history-free, and the Cognitive side receives the same
empty-memory condition that a new loop would provide.

The base SHA resolves from the configured repository and `baseline_ref`.

### Existing-run mode

The Generator remains history-free. The Cognitive side and its research tools
read history, findings, frontier and related artifacts from `--from-run`.

The base SHA is derived from the last authoritative history record's selected
SHA. If the history is empty, the runner falls back to the configured baseline
revision. The isolated Workspace clones from `--from-run/repo`, because
accepted candidate commits are run-local and need not exist in the configured
upstream repository. A malformed history, missing run repository, or selected
SHA that does not resolve there is a setup error.

The source run, including its repository, is passed only as a read location.
Result artifacts and temporary repository state are created under
`--output-dir`; no target resolution, finding allocation, history append, or
memory update is performed.

## Output Contract

The authoritative `result.json` contains:

```json
{
  "status": "completed",
  "input": {
    "config_path": "...",
    "history_source": null,
    "base_sha": "...",
    "seed": 42,
    "goal": "...",
    "editable_paths": [],
    "frozen_paths": [],
    "gate_block": "...",
    "candidates_per_round": 4,
    "gen_steps": 216,
    "cognitive_steps": 148
  },
  "proposals": [],
  "abstained": false,
  "abstain_reason": null,
  "deliberation_telemetry": {},
  "trace": {},
  "usage": null,
  "elapsed_seconds": 0.0
}
```

Each proposal uses the existing `ResearchProposal` fields: `instruction`,
`research_target`, `evidence_refs`, and `material_difference`.

`proposals.md` is a human-readable projection containing the effective input
summary followed by numbered proposal instructions, research targets,
evidence references, and material differences. It is derived from
`result.json` and is not authoritative.

The output directory may also contain the isolated local repository used by
`Workspace`. The proposer worktree is always removed after the attempt.

## Failure Handling

Setup errors fail before model execution where possible. Model, proposer,
runtime, history, or serialization failures produce a non-zero script exit.

Once the output directory is available, a failed attempt writes a minimal
`result.json` with `status: "failed"`, the resolved input available at that
point, the exception type, and its message. It does not fabricate proposals.

Cleanup executes in a `finally` path. Cleanup failure is reported without
overwriting the original failure. An existing completed `result.json` is never
silently overwritten; the user must choose a new output directory.

## Reproducibility

The harness records:

- the resolved config path;
- the configured repository path;
- base SHA;
- SimpleLoop source revision when available;
- optional history source;
- optional Python random seed;
- effective proposer budgets and candidate width;
- prompt directory when configured;
- elapsed time and model usage exposed by the existing pipeline.

The seed is applied only around the proposer invocation and the prior random
state is restored afterwards, preventing the standalone harness from changing
process-global randomness for callers. Remote model output may still vary.

## Testing

Tests use fake model/runtime/orchestrator boundaries and temporary Git
repositories. They verify:

- the script does not construct or validate an Executor;
- the runner calls the complete `ProposerOrchestrator` with the same effective
  arguments as the normal loop;
- fresh mode provides empty memory and resolves the configured baseline;
- existing-run mode resolves the last selected SHA and does not modify the
  source run;
- seeding is applied and prior random state is restored;
- completed and abstained results serialize correctly;
- `proposals.md` faithfully projects final proposals;
- setup and proposer failures return non-zero and write a failed result when
  possible;
- worktree cleanup occurs on success and failure;
- an existing completed output is not overwritten.

No test invokes a real model, Apptainer image, Executor, benchmark, or Gate.

## Compatibility and Minimality

Normal `simpleloop` CLI behavior and the `ProposerOrchestrator` public result
type remain unchanged. The feature adds one repository-local script and
targeted tests, with no harness code or command registration in the
`simpleloop` package. It deliberately avoids a broader Proposer service
refactor until the standalone harness provides evidence that such a refactor
is necessary.
