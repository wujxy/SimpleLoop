# Proposer Feedback Projection Design

**Status:** Approved design, pending implementation plan  
**Date:** 2026-07-23  
**Branch:** `v0.0.4`  
**Scope:** Separate Judger's complete technical feedback from the concise
search-context feedback visible to Proposer

## 1. Goal

Keep the Judger's complete technical diagnosis in the append-only history and
final report while giving the Proposer a shorter, less implementation-oriented
account of what each experiment taught the search.

The intended data flow is:

```text
Judger
├── feedback
│   └── history.jsonl / final_report.md
│
└── feedback_for_proposer
    ├── recent-six-round Proposer view
    ├── simpleloop memory show
    └── later Insight generation
```

The change must preserve the minimal character of SimpleLoop. It adds one
free-prose field and does not add a feedback state machine, confidence values,
retry actions, a Memory Agent, or a new model call.

## 2. Motivation

The existing `feedback` field serves two different audiences:

- the history and final report need a detailed, auditable explanation of the
  landed change, measured result, and latent implementation risk;
- the Proposer needs only the smallest reusable lesson that helps it choose
  future search directions.

In long parallel runs, complete Judger feedback contains details such as helper
implementations, call-site wiring, cache lifetimes, pointer validity, field
layout, and possible compiler effects. Feeding those details back to the
Proposer encourages it to re-enter implementation review instead of performing
light source grounding and direction selection.

Search Memory in `v0.0.4` reduces default history context to accumulated
Insights plus the six most recent generations, but its historical episode
lookup currently returns complete `feedback`. That lookup must follow the same
role boundary as the recent-history projection; otherwise it becomes an escape
hatch through which technical feedback re-enters the Proposer context.

## 3. Design Principles

1. **One fact, two projections.** The Judger produces a complete diagnostic and
   a concise search-context rendering in the same call.
2. **Free prose remains sufficient.** The short rendering is a string, not a
   structured mechanism classification.
3. **The Judger still does not choose the next direction.** The short rendering
   summarizes what the result suggests; `continue` or `switch` remains the
   Proposer's decision.
4. **Proposer-visible paths are consistent.** Recent outcomes and historical
   reference lookup expose the same short feedback field.
5. **No fallback to technical feedback.** Missing legacy short feedback remains
   unavailable instead of silently exposing the complete diagnostic.
6. **Full evidence remains auditable.** `history.jsonl` and the final report
   retain complete `feedback`.

## 4. Judger Output Contract

The Judger's structured output gains one required free-prose field:

```json
{
  "score": 0.72,
  "risk": "low",
  "feedback": "Complete four-part technical feedback.",
  "feedback_for_proposer": "Short mechanism-level search context."
}
```

### 4.1 `feedback`

The existing contract remains unchanged:

- complete technical diagnosis;
- `LANDING_STATE`, `Implemented`, `Result`, and `Analysis` parts;
- suitable for `history.jsonl` and `final_report.md`;
- may name concrete implementation details and latent correctness risks.

### 4.2 `feedback_for_proposer`

`feedback_for_proposer` is:

- required and nonblank for every new successful Judger response;
- one or two concise free-prose sentences;
- the smallest reusable lesson about the attempted optimization mechanism;
- allowed to distinguish evidence about the general mechanism from effects
  that may be specific to the current implementation;
- allowed to acknowledge uncertainty;
- not responsible for repeating metrics already supplied separately by the
  harness;
- not an implementation plan or a `continue`/`switch` instruction.

The prompt should express this in the same explanatory, role-oriented style as
the current Judger prompt:

> `feedback_for_proposer` is a short search-context note for later proposal
> generation. In one or two concise sentences, capture the smallest reusable
> lesson about the attempted mechanism. When the evidence permits, distinguish
> what the result says about the mechanism from what may be specific to this
> implementation. Keep implementation diagnosis in `feedback`; this note does
> not need to repeat metrics already provided by the harness.

This is guidance about the field's purpose rather than a list of hard semantic
prohibitions.

## 5. Persistence

Every new serial or parallel candidate record in `history.jsonl` stores both:

```json
{
  "feedback": "...",
  "feedback_for_proposer": "..."
}
```

The complete `feedback` remains the source used by the final report and any
human audit. No existing report behavior changes.

Loop-generated failure records that do not receive a successful Judger response
must still contain a `feedback_for_proposer` key. Their existing concise loop
failure message may be used for that field because no separate technical
diagnosis exists.

## 6. Proposer Recent-History Projection

`views.for_proposer()` continues to expose only the most recent six round
records, but each candidate projection changes from:

```json
{
  "feedback": "complete technical feedback"
}
```

to:

```json
{
  "feedback_for_proposer": "short search-context feedback"
}
```

The complete `feedback` key must not appear in the projected record.

`landing_state` may continue to be derived internally from complete feedback
because it is an existing deterministic projection. Deriving that field does
not expose the complete prose to the Proposer.

The rendered prompt labels the field explicitly as
`feedback_for_proposer="..."` so its role is clear.

## 7. Search Memory Historical Lookup

`memory.resolve_episode()` and `simpleloop memory show r<round>c<candidate>`
must follow the same projection boundary as recent history.

The historical episode returned to Proposer changes from:

```json
{
  "feedback": "complete technical feedback"
}
```

to:

```json
{
  "feedback_for_proposer": "short search-context feedback"
}
```

The lookup must not return complete `feedback`, even though the command is an
explicit request for older evidence. Proposal, family, metrics, risk, selection
state, commit references, and changed paths remain available and are sufficient
for search-oriented historical comparison.

This keeps the Search Memory layers aligned:

```text
Insights                 compact long-run understanding
Recent outcomes          concrete short-term search evidence
Historical lookup        compact evidence for one old experiment
history.jsonl             complete audit record, not Proposer context
```

## 8. Legacy History

History written before this feature has no `feedback_for_proposer` field.

For such records:

- do not fall back to complete `feedback`;
- keep the `feedback_for_proposer` key in projected recent outcomes and
  historical episode output;
- use the empty string as its value;
- continue returning all other existing candidate facts.

No history migration, model-generated backfill, or additional summarization
step is introduced.

New Judger output is stricter: missing or blank `feedback_for_proposer` is a
parse error, just as missing or blank complete feedback is an error.

## 9. Error and Failure Behavior

- A structured Judger response missing `feedback_for_proposer` is rejected.
- A non-string or blank `feedback_for_proposer` is rejected.
- A candidate worker failure caused by invalid Judger output remains local to
  that candidate under the existing parallel failure handling.
- Loop-generated failure records provide a concise short field directly.
- Legacy history without the field remains readable and retrievable, but does
  not expose complete feedback as a fallback.

No new recovery mechanism is added.

## 10. Files and Interfaces

Expected implementation scope:

- `simpleloop/judger.py`
  - extend `Judgment`;
  - extend the JSON Schema and parser;
  - add the short field to the final delivery contract.
- `simpleloop/loop.py`
  - persist the short field for serial and parallel candidate records;
  - populate it for loop-generated failure records.
- `simpleloop/store.py`
  - persist the field in serial records where the store owns record creation.
- `simpleloop/views.py`
  - expose only `feedback_for_proposer` to Proposer;
  - keep complete feedback out of the projection.
- `simpleloop/proposer.py`
  - render the short field under its explicit name.
- `simpleloop/memory.py`
  - return only the short field from historical episode lookup.
- focused tests under `simpleloop/tests/`
  - update Judgment fixtures and assert the projection boundary.

No changes are required to:

- Insight record shape;
- Insight append behavior;
- candidate selection;
- objective or gate parsing;
- Executor context;
- final report format;
- CLI command shape;
- task configuration.

## 11. Tests

Tests must cover:

1. Judger Schema requires both feedback strings.
2. Judger parsing rejects missing, blank, or non-string
   `feedback_for_proposer`.
3. Judger parsing preserves complete and short feedback independently.
4. Serial history records persist both fields.
5. Parallel candidate records persist both fields.
6. Final report continues to use complete `feedback`.
7. Recent Proposer projection contains `feedback_for_proposer` and excludes
   complete `feedback`.
8. The rendered Proposer prompt contains the short field and does not contain a
   unique technical sentinel placed only in complete feedback.
9. `memory.resolve_episode()` and `simpleloop memory show` return the short
   field and exclude complete feedback.
10. Legacy records without the short field return an empty short field without
    falling back to complete feedback.
11. Existing Insight rendering, validation, references, and append-only
    behavior remain unchanged.
12. Loop-generated candidate failures include a concise short field.

## 12. Non-Goals

This change does not:

- classify mechanisms as promising, stalled, or negative;
- add confidence or evidence fields;
- ask the Judger to choose `continue` or `switch`;
- summarize or migrate old technical feedback;
- add a technical mode to `memory show`;
- alter the six-round recent-history window;
- limit Proposer tool calls or reasoning effort;
- change proposal count or search policy;
- redesign the full Judger feedback.

These concerns can be evaluated separately after observing whether the shorter
feedback projection reduces Proposer implementation-level investigation.

## 13. Acceptance Criteria

The design is complete when:

- every new Judger result contains both feedback strings;
- full technical feedback remains present in history and reports;
- no normal Proposer context path exposes complete feedback;
- recent outcomes and historical Search Memory lookup expose only
  `feedback_for_proposer`;
- old records remain readable without falling back to complete feedback;
- Search Memory Insight behavior is unchanged;
- all focused and full SimpleLoop tests pass.
