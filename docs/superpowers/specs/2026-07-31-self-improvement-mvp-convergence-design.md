# Self-Improvement MVP Convergence Design

## Goal

Turn the newly added prompt-only outer loop into a minimal, correctly owned
`self_improvement` capability without creating a premature multi-strategy
framework.

The public contract describes the outer loop. Prompt evolution remains the
only internal implementation in this release.

## Public configuration

The complete enabled configuration is:

```yaml
self_improvement:
  interval_rounds: 2
```

The block's presence enables self-improvement. Its absence runs the ordinary
artifact loop. `interval_rounds` is the only user decision and must be a
positive integer.

The old `prompt_self_improvement` key is removed without a compatibility
alias. Strict configuration validation therefore reports it as an unknown
top-level key.

The following values are implementation details and are not configurable:

- optimizer executable: the existing Claude `Agent` default;
- active prompt and history locations;
- maximum prompt character count: 30,000.

No `mode`, `target`, optimizer backend, or strategy registry is introduced
until a second implementation exists.

## Run ownership and filesystem layout

Every new run starts from the package-shipped v000 prompts. Prompt state never
flows implicitly from one run directory to another.

```text
run_dir/
├── self_improvement/
│   ├── prompts/
│   └── prompt_history/
├── history.jsonl
└── ...
```

`--continue` resumes only the active version and inflight state inside the same
run directory. Two independent run directories therefore both initialize
v000, even when they use the same task config.

The outer supervisor holds a run-local `.self-improvement.lock` for the whole
segmented execution. Existing snapshot, rollback, event recording, and
inflight recovery semantics remain run-local.

## Dependency direction

The outer supervisor owns self-improvement policy and state. It invokes the
artifact loop with an internal active-prompt path.

The artifact loop, candidate worker, and roles may consume an injected prompt
path, but they must not read the `self_improvement` configuration block. A
normal artifact run receives no injected path and continues to load the
package prompts.

This keeps the inner loop independent of why a different prompt set is active
and prevents the public outer-loop name from spreading through role code.

## Segmentation and triggers

The supervisor runs complete artifact-loop segments ending at successive
`interval_rounds` boundaries.

After a segment:

- trigger the optimizer only when at least one artifact round remains;
- accept a changed prompt set after the static prompt gate succeeds;
- record `no_change` when the prompt diff is empty;
- restore the parent version and record `rejected` after optimizer or gate
  failure.

There is no optimizer call after the final artifact round because that prompt
version would receive no evidence in the current run.

## Optimizer report

The optimizer writes:

```yaml
diagnosis: non-empty text
evidence:
  - reference selected during investigation
```

`diagnosis` is required and `evidence` is a list of strings. The harness
derives changed versus no-change from the actual prompt diff. The redundant
agent-declared `status` and overlapping `intent` fields are removed.

Accepted version manifests retain the derived changed-file list plus the
diagnosis and evidence.

## Safety boundary

This convergence does not claim to add an operating-system write sandbox.
The current prompt gate continues to validate the active prompt set, UTF-8
readability, symlinks, fixed meta identity core, expected filenames, and the
internal 30,000-character limit.

Future harness-code self-improvement requires a separate design based on a
disposable worktree, changed-path validation, evaluation gates, and explicit
promotion. That machinery is deliberately not anticipated with unused public
options in this prompt-only MVP.

## Errors and observability

Optimizer failures remain non-fatal to unattended artifact execution:
the parent prompts are restored and the event is recorded as rejected.

The returned summary includes a `self_improvement` object containing the
active prompt version and trigger outcomes for this run, so callers do not
need to inspect internal YAML or JSONL files to determine whether prompt
evolution was accepted, unchanged, or rejected.

## Testing

Tests must cover:

- absence of `self_improvement` disables the outer loop;
- the minimal block resolves only `interval_rounds`;
- removed and unknown fields fail strict validation;
- the old top-level name fails strict validation;
- two run directories using one task config each initialize v000;
- `--continue` resumes the same run's active prompt version;
- the supervisor injects the active prompt path into the artifact loop;
- the inner loop and workers do not read outer-loop configuration;
- no optimizer runs after the final segment;
- non-final accepted, no-change, rejected, and interrupted triggers retain
  their existing rollback and recovery behavior;
- the reduced optimizer report is validated and copied into manifests;
- ordinary runs still use package prompts;
- related and full project test suites pass.

## Acceptance

The implementation is complete when the minimal YAML contract works, each run
has an independent v000-based prompt lineage, the inner artifact loop is
decoupled from outer-loop configuration, final-round optimizer work is
eliminated, the reduced report protocol is enforced, documentation matches the
new behavior, and tests pass.

The final handoff must also present the resulting outer-loop architecture,
data flow, state ownership, trigger/recovery sequence, and intentionally
deferred harness-code evolution boundary for user evaluation.
