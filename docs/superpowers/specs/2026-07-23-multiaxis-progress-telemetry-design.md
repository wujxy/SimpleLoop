# SimpleLoop Multi-Axis Progress Telemetry Design

## Goal

Extend live SimpleLoop monitoring from the current two-panel, round-only
`progress.png` into:

- one 3-by-3 overview image;
- nine corresponding standalone detail images;
- three y dimensions: Judger score, the configured objective, and the
  objective ratio versus the run baseline;
- three x dimensions: round, cumulative wall-clock worktime, and cumulative
  processed Claude tokens.

The feature must work for serial/static runs, parallel self-loop runs, and
continued runs. Monitoring remains observational: telemetry or plotting
failures must not terminate or change the optimization lineage.

`final_report.md` generation will be removed. Existing reports in old run
directories are left untouched.

## Confirmed Metric Semantics

### Objective

The objective key and direction continue to come from the existing metrics
configuration:

```yaml
metrics:
  objective:
    key: SPEED_MS
    lower_is_better: true
```

No monitoring-specific metric configuration is added.

### Objective Ratio

The plotting layer automatically derives:

```text
ratio = current objective value / fixed baseline objective value
```

Its y-axis label is:

```text
<key> ratio (vs baseline)
```

For example, `SPEED_MS ratio (vs baseline)`. A value of `1.0` equals the
baseline, a value below `1.0` means the raw metric decreased, and a value above
`1.0` means it increased. The formula does not invert based on
`lower_is_better`; that configuration remains available for explanatory titles
and incumbent selection but does not change ratio arithmetic.

The ratio is omitted when the baseline or current value is missing, Boolean,
non-numeric, non-finite, or when the baseline is zero. Missing ratio values are
never estimated from the first candidate or first accepted round.

### Worktime

Worktime is cumulative wall-clock time spent actively running SimpleLoop. It
starts when a fresh run begins and includes:

- setup and baseline evaluation;
- proposer, executor, and judger calls;
- builds and evaluation commands;
- scheduling and waits while the run process is active.

The clock uses a monotonic source. On `--continue`, the new active segment is
added to the last persisted cumulative worktime; time while SimpleLoop was not
running is excluded.

### Processed Tokens

Each completed Claude call contributes:

```text
input_tokens
+ cache_creation_input_tokens
+ cache_read_input_tokens
+ output_tokens
```

The total covers the proposer and every executor and judger call. Static
proposal mode has no proposer usage. Parallel candidate usage is counted once
per actual Claude call.

If an expected call does not return parseable usage, the global processed-token
coordinate becomes unavailable from that call onward and no token count is
estimated.

## Persisted Telemetry

Add a focused `simpleloop/telemetry.py` module. It owns a thread-safe run-level
tracker and an atomically written `run_dir/telemetry.json`.

The run-level document contains only:

- last persisted cumulative active worktime;
- cumulative processed tokens, or `null` after incomplete usage;
- the fixed initial baseline metrics;
- the telemetry snapshot taken when baseline evaluation completed.

The first fresh-run baseline evaluation fixes the baseline metrics. A
`--continue` baseline re-evaluation may still serve the Judger as it does
today, but it must not overwrite the persisted baseline used by plots. If the
initial baseline metric was unavailable, ratio panels remain unavailable.

The tracker exposes snapshots containing:

```text
worktime_seconds
processed_tokens | null
```

Snapshots are persisted into `history.jsonl`:

- each parallel candidate receives its snapshot when that candidate finishes;
- the generation record receives a snapshot after all candidates finish and
  selection is complete;
- each serial/static round receives its snapshot when the round finishes;
- failure records receive a snapshot when they are persisted.

The run-level file is refreshed after baseline evaluation, completed Agent
calls, candidate completion, and round/generation completion. An interrupted
in-flight operation that never reaches a persistence boundary cannot be
reconstructed and is not guessed on resume.

## Agent Integration

Keep the public proposer, executor, and judger return contracts unchanged.
`Agent` accepts an optional call-completion observer shared by the three
role-scoped instances. `_run` parses token usage from the Claude JSON envelope
and reports a call record to that observer.

The observer is invoked for successful calls and best-effort for failed or
timed-out calls. A completed call without usage marks the cumulative token
coordinate incomplete. Observer exceptions are caught and emitted as concise
telemetry warnings; they never replace the Agent's real result or error.

The shared tracker uses a lock because parallel candidate workers call the
executor and judger Agents concurrently. Candidate and generation snapshots are
taken from the same tracker, so their coordinates describe globally cumulative
resources consumed by the run.

## Series Semantics

Extend the pure history-to-series transformation so rendering remains
separable from collection and can be tested without pixel inspection.

### Candidate Points

- Round x coordinate: one-based round number.
- Worktime x coordinate: the candidate-completion snapshot.
- Token x coordinate: the candidate-completion processed-token snapshot.
- Score y coordinate: numeric Judger score.
- Objective y coordinate: numeric configured objective.
- Ratio y coordinate: numeric objective divided by the fixed baseline.

All candidates are scatter points. The one selected candidate from each
generation receives a prominent marker. Selected scores are connected across
successive rounds using the x coordinate of each selected candidate's
completion snapshot.

### Incumbent Lines

The objective and ratio panels show the accepted incumbent as step lines.

- Round panels start with the fixed baseline at `x=0`.
- Worktime and token panels place the baseline at the baseline-completion
  snapshot.
- A parallel incumbent changes at the generation-completion coordinate because
  selection is only known after the generation finishes.
- A serial incumbent changes at the round-completion coordinate.
- A round with no winner carries the prior incumbent forward.

The ratio panels include a horizontal `y=1.0` reference line.

### Missing And Legacy Data

Legacy history without telemetry continues to populate the score-vs-round and
objective-vs-round panels. Panels requiring absent baseline, worktime, or token
data display a clear unavailable message. Missing coordinates or metrics omit
only the affected points.

## Plot Outputs

`progress.png` becomes the backward-compatible 3-by-3 overview:

| | Round | Cumulative worktime | Cumulative processed tokens |
|---|---|---|---|
| Score | panel | panel | panel |
| `<key>` | panel | panel | panel |
| `<key> ratio (vs baseline)` | panel | panel | panel |

Generate these nine standalone detail images:

- `progress-score-vs-round.png`
- `progress-score-vs-worktime.png`
- `progress-score-vs-tokens.png`
- `progress-objective-vs-round.png`
- `progress-objective-vs-worktime.png`
- `progress-objective-vs-tokens.png`
- `progress-objective-ratio-vs-round.png`
- `progress-objective-ratio-vs-worktime.png`
- `progress-objective-ratio-vs-tokens.png`

The generic filenames stay stable when the configured objective key changes.

Rendering conventions:

- all candidates use light scatter points;
- selected candidates use prominent markers or lines;
- accepted incumbents use prominent step lines;
- score uses the fixed range `[0, 1]`;
- round ticks use one-based integers;
- worktime is stored in seconds and displayed in hours;
- tokens are stored and displayed as processed-token counts;
- objective titles retain the configured lower/higher-is-better direction.

Build the common series once per refresh, then render the overview and details.
Each PNG is rendered to its own temporary file and atomically replaces its
previous version. If one image fails, its last valid version remains while
other images may still update.

## Removing `final_report.md`

Remove future report generation rather than deleting old artifacts:

- remove calls that write `final_report.md`;
- remove `Store.write_final_report()` and report-only helpers;
- remove or replace tests whose only contract is report content;
- update current user documentation that promises a generated report;
- retain the CLI's returned and printed summary;
- do not scan, edit, or delete existing run directories.

Historical design documents may continue to describe the behavior that existed
when they were written.

## Failure Handling

- Telemetry collection, usage parsing, persistence, and plotting are non-fatal.
- `telemetry.json` uses a temporary file and atomic replacement.
- A corrupt telemetry file on `--continue` produces a warning. Continued
  worktime and token coordinates become unavailable rather than guessed.
- Invalid objective, baseline, ratio, or coordinate values are skipped.
- Temporary PNGs are cleaned up best-effort after failures.
- Plot warnings identify the affected output without dumping large payloads.

## Tests

### Telemetry Unit Tests

- Parse all four Claude token categories and calculate processed tokens.
- Treat absent, Boolean, negative, or malformed usage as unavailable.
- Mark cumulative tokens unavailable after the first incomplete call.
- Accumulate monotonic worktime across fresh and continued active segments.
- Preserve the initial baseline across `--continue`.
- Atomically persist and reload telemetry state.
- Keep tracker updates correct under concurrent call completion.

### Agent Integration Tests

- Extract usage from a real-shaped Claude JSON envelope.
- Keep `run_json` and `run_text` return contracts unchanged.
- Notify the observer on success, failure, and missing usage.
- Ensure observer failures do not mask the Agent result.

### History And Loop Tests

- Persist candidate-completion and generation-completion snapshots.
- Persist serial/static and failure snapshots.
- Count proposer, executor, and judger usage with the confirmed static and
  parallel semantics.
- Resume worktime without counting the stopped interval.
- Never overwrite the fixed baseline during continue-mode baseline evaluation.

### Series And Rendering Tests

- Build all nine metric/axis combinations.
- Calculate `current / baseline` ratio and reject invalid baselines.
- Place baseline and incumbent points at the specified coordinates.
- Distinguish candidate-completion from generation-completion coordinates.
- Preserve legacy round-only data and mark unavailable panels.
- Produce a valid 3-by-3 overview PNG and nine valid detail PNGs.
- Preserve an existing image when its replacement render fails.
- Verify score limits, ratio reference line, and readable worktime/token
  formatting through renderer-level assertions where practical.

### Report Removal Tests

- Verify fresh and continued runs do not create `final_report.md`.
- Remove obsolete report-content assertions.
- Verify the CLI summary remains available.

## Scope Boundaries

- No arbitrary expression language or monitoring-specific derived-metric
  configuration.
- No inference or backfill of exact telemetry for old runs.
- No pricing or cost calculation.
- No deletion or migration of existing run artifacts.
- No change to candidate selection, acceptance gates, or optimization lineage.
