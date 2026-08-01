# Proposer Visible Action Summaries

Date: 2026-08-01
Status: approved

## Objective

Make the existing `ProposerAgent.run()` investigation loop observable to a
terminal user without exposing model context or tool-result contents. Also
raise the default investigation budget from 20 to 50 model steps.

This is an observability change to the current Proposer Agent Runtime. It does
not change the outer Proposer -> Executor -> Harness/Gates loop, the available
tools, or any authority boundary.

## Evidence from `test004.log`

The test run shows that the Proposer operated correctly: it spent about 44
seconds before submitting one proposal, the Executor produced commit
`107b85e7f44fbcd4df5a9e64084d63e184d2da1b`, all configured gates passed, and
the candidate was selected. The missing information is the Proposer's internal
action sequence during that 44-second interval.

No Insight in this first-round run is expected: `write_insight` requires a
valid historical experiment reference, and round zero had no earlier candidate
to cite.

## First-principles boundary

The user needs to know whether the Proposer is waiting for the model, using a
research tool, or submitting an experiment. They do not need the full prompt,
raw model response, or tool output to answer that operational question.

Therefore the MVP adds direct terminal summaries inside
`ProposerAgent.run()`. It deliberately does not add:

- a logging framework or injected callback;
- a structured trace file or new persistence schema;
- a configuration switch or log-level hierarchy;
- model token streaming or a heartbeat thread;
- full command output, model responses, prompt text, or Insight text.

These additions would create new interfaces or state without being necessary
for basic runtime visibility.

## Output contract

A run emits a concise lifecycle and one line before and after meaningful work:

```text
[proposer] started max_steps=50
[proposer step 1/50] thinking
[proposer step 1/50] action=search_history query_chars=11
[proposer step 1/50] result=ok matches=3
[proposer step 2/50] thinking
[proposer step 2/50] action=run_research_command cwd=source command_chars=12
[proposer step 2/50] result=ok exit_code=0 output_chars=1842 truncated=false
[proposer step 3/50] thinking
[proposer step 3/50] action=submit_proposals count=1
[proposer] finished steps=3 elapsed=44.1s
```

The exact result fields depend on the action. Summaries contain only action
types, counts, fixed-enum fields such as `cwd`, exit status, output size, and
truncation status. Model-controlled command, query, reference, Insight, and
proposal text is never printed, even in truncated form. This prevents the
model from copying prior context or a tool-result body into a later action and
thereby leaking it through the activity log.

The following data is never printed by this feature:

- system or user prompts;
- accumulated model messages;
- raw model JSON or reasoning;
- tool-result bodies;
- full Insight text;
- environment variables or credentials.

For `write_insight`, the summary contains only reference count. For
`submit_proposals`, it contains only proposal count because the outer loop
already prints the accepted proposal text. Failures retain the existing raised
exception behavior; no raw payload is added to the log.

## Implementation shape

`ProposerAgent.run()` remains the owner of the loop and prints the lifecycle,
thinking, action, result, and completion lines. Small private formatting
helpers are acceptable only where needed to keep the loop readable and expose
safe metadata. No public API changes are required.

The elapsed time uses a monotonic clock. The existing model timeout, command
timeout, telemetry, and exception semantics remain unchanged.

## Step-budget decision

The default `researcher.max_steps` changes from 20 to 50. Fifty is a safety cap,
not a prescribed research workflow: the Proposer can submit at any earlier
step. It gives adaptive investigations 2.5 times the current room while keeping
the worst-case model-call budget materially below a default of 100. Users may
still explicitly configure 100 when an experiment justifies that cost.

Only the actual default and documentation describing that default need to
change. Existing task YAML files with explicit `max_steps` values are explicit
per-run budgets, not defaults, and are not bulk-rewritten. In particular, the
user's currently modified `examples/tiny_algo_opt/task.yaml` remains untouched.

## Tests

Focused tests will verify:

- lifecycle, thinking, action, result, and completion summaries are visible;
- action-specific metadata is useful but bounded;
- model-controlled action text, tool-result contents, and Insight text are not
  printed, including when a later action echoes earlier private content;
- the configured step count appears in output;
- the configuration default is 50;
- existing successful, malformed-action, timeout, and max-step behavior is
  unchanged.

## Non-goals

- changing Proposer tools or scientific prompt;
- exposing hidden chain-of-thought;
- persisting or replaying an action trace;
- changing Executor or Harness logging;
- changing the outer-loop scheduling model;
- raising every existing task's explicit step budget.
