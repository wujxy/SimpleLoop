# SimpleLoop Search Memory Design

**Status:** Approved design, pending implementation plan
**Date:** 2026-07-23
**Scope:** Per-run long-run memory for the existing
`Proposer -> Executor -> Judger` loop

## 1. Goal

Add the smallest useful Search Memory to the current SimpleLoop so that the
Proposer can learn from a long run instead of repeatedly reinterpreting the
entire round history.

The desired learning loop is:

```text
completed experiment history
        ↓
next-round reflection
        ↓
optional reusable insight
        ↓
simple history-informed search
        ↓
continue / switch
        ↓
next proposal
```

The design must preserve the current role boundaries and the existing
`reflection -> decision -> proposal` behavior. It must not add a Memory Agent,
an additional model call, or a complex persistent search-state machine.

## 2. First Principles

Long-run learning needs two different representations:

```text
What happened                    What was learned
history.jsonl                    insights.jsonl
complete experimental evidence  compact reusable understanding
```

The two representations have different responsibilities:

- History is the complete, append-only record of experiments.
- Insights are compact, append-only interpretations derived during reflection.
- An Insight never replaces its supporting history.
- Every Insight points back to the history records that support it.
- The Proposer reads compact Insights by default and retrieves old detailed
  history only when the compact form is insufficient.
- Old Insights are never edited, merged, retired, or deleted in the MVP.
  Later evidence creates another Insight.

This keeps experimental facts auditable while preventing the Proposer from
having to reread a growing round-by-round transcript on every call.

## 3. MVP Product Principles

The MVP solves only these problems:

1. Preserve every candidate experiment as episodic memory.
2. Let the Proposer extract a reusable Insight while performing its normal
   next-round reflection.
3. Present accumulated Insights as compact long-run search context.
4. Let the Proposer retrieve the exact historical candidate behind an Insight
   reference when needed.
5. Let the resulting understanding flow through the existing
   `reflection -> decision -> proposal` chain.
6. Keep the Proposer structured-output contract small and stable.

The MVP explicitly does not implement:

- hypothesis or family state machines;
- persistent `promising`, `stalled`, `negative`, or `blocked` states;
- memory update actions such as `revise` or `retire`;
- confidence scores;
- automatic Insight merging or deduplication;
- embeddings or vector search;
- semantic novelty judges;
- a Memory Agent;
- an extra reflection or consolidation model call;
- cross-run memory;
- automatic forgetting or TTLs;
- causal graphs;
- complex search policies.

## 4. Memory Model

### 4.1 Episodic Memory: `history.jsonl`

The existing per-run `history.jsonl` remains the full experimental record and
source of evidence.

Every candidate is addressable by a stable reference:

```text
r<round>c<candidate>
```

Examples:

```text
r0c0
r1c1
r12c2
```

Legacy serial rounds are normalized as candidate zero:

```text
serial round 7 -> r7c0
```

The candidate episode retains the existing facts:

- proposal and family;
- parent, candidate, selected, and accepted commit information;
- objective and gate metrics;
- risk;
- Judger feedback;
- changed paths;
- selection outcome.

History remains append-only. Search Memory does not duplicate or rewrite these
records.

### 4.2 Semantic Memory: `insights.jsonl`

Each run gains one append-only file:

```text
runs/<run>/insights.jsonl
```

Each record has exactly three fields:

```json
{
  "id": "I31",
  "text": "Dense copying helps when it removes a genuinely sparse gather, but can regress when the original access is already sequential.",
  "refs": ["r29c0", "r30c0"]
}
```

The fields are:

- `id`: a stable Insight identifier assigned by the harness;
- `text`: the smallest reusable form of the learned conclusion, expressed as
  one or two concise, generalizing sentences;
- `refs`: the historical candidate records that ground the conclusion.

At most one Insight is created by a Proposer call. Most rounds may create none.

The Insight ID is derived from the round whose Proposer created it:

```text
round 31 Proposer Insight -> I31
```

This avoids a separate counter and makes append behavior idempotent across
resume handling.

### 4.3 Append-Only Correction

The MVP never edits an old Insight. If later evidence narrows or contradicts
an older conclusion, the Proposer creates another Insight:

```json
{
  "id": "I43",
  "text": "Sequential traversal alone does not rule out packing benefits when unused fields dominate cache traffic; the earlier access-order explanation was incomplete.",
  "refs": ["r41c0", "r43c0"]
}
```

The history of changing understanding remains visible instead of being
silently overwritten.

## 5. Proposer Context

The Proposer prompt is assembled in this order:

```text
1. Role boundaries
2. Task goal
3. Current accepted base
4. Accumulated search insights
5. Recent round outcomes
6. Optional historical-reference lookup instructions
7. Search guidance
8. Output contract
```

### 5.1 Accumulated Insights

All Insights are rendered as compact text rather than raw JSON:

```text
Accumulated search insights:

[I4] Hoisting expensive invariant work from unconditional FCN paths has
repeatedly helped; similar work behind cold gates has measured as noise.
Evidence: r0c0, r3c0, r4c0

[I31] Dense copying helps when it removes a genuinely sparse gather, but can
regress when the original access is already sequential.
Evidence: r29c0, r30c0
```

The compact Insight list is the Proposer's default long-run view.

### 5.2 Recent Outcomes

Only the most recent six round records are rendered in full using the current
Proposer-visible fields:

- proposal;
- family;
- metrics;
- risk;
- feedback;
- selected and accepted state;
- candidate and base SHAs;
- changed paths.

Older detailed rounds are no longer appended to every Proposer prompt. They
remain available through their references.

The responsibilities are:

```text
Insights          long-run reusable understanding
Recent outcomes   concrete short-term evidence
Reference lookup  optional old experimental detail
Current source    whether an old lesson still applies now
```

## 6. Historical Reference Lookup

The Proposer must not use `cat history.jsonl` as the normal retrieval path.
Reading the complete file would reintroduce unbounded context growth.

SimpleLoop provides one small, deterministic, read-only interface:

```bash
simpleloop memory show r29c0
```

It resolves the reference against the current run's `history.jsonl` and returns
only the candidate fields useful to the Proposer:

```json
{
  "ref": "r29c0",
  "family": "dense_snapshot",
  "proposal": "...",
  "parent_sha": "...",
  "candidate_sha": "...",
  "selected": true,
  "metrics": {
    "SPEED_MS": 551.7002,
    "CORRECTNESS": true
  },
  "risk": "low",
  "feedback": "...",
  "changed_paths": [
    "OMILRECV2/src/OMILRECV2.cc"
  ]
}
```

The lookup does not return:

- raw evaluation output;
- unrelated candidates;
- unrelated rounds;
- the final report;
- the full history file.

When implementation detail matters, the Proposer continues to use the existing
Git inspection path:

```bash
git diff <parent_sha>..<candidate_sha>
git show <candidate_sha>:<path>
git show <current_base_sha>:<path>
```

Reference lookup is optional. The Proposer should use an Insight directly when
it contains enough information. It may retrieve old evidence when:

- an Insight is important to the current decision but too compact;
- recent evidence appears to conflict with an older Insight;
- the applicability of an Insight may have changed in the current source;
- the Proposer is considering revisiting an old direction;
- it needs to distinguish a failed mechanism from a failed implementation.

## 7. Simple Search Behavior

The MVP adds no separate search algorithm or search controller. The Proposer
uses its existing source-reading and reasoning abilities over a better memory
projection:

```text
Accumulated Insights
        +
Recent outcomes
        +
Current accepted source
        ↓
Identify which prior lessons apply
        ↓
Optionally inspect referenced old experiments
        ↓
Judge whether the current family has a distinct opportunity
        ↓
continue / switch
        ↓
next proposal
```

The Proposer should use the memory to consider:

- whether an effective mechanism transfers to a new source location;
- whether a failure invalidated a general mechanism or only one implementation;
- whether a proposed direction merely restates an old attempt;
- whether an old mechanism is already present in the current accepted source;
- whether the current family still has a substantively different experiment;
- whether an uncovered source opportunity is more valuable than continuing.

These are soft search considerations, not persistent statuses or additional
output fields.

## 8. Proposer Output Contract

The existing batch output remains intact. The MVP adds only two top-level
fields:

```json
{
  "reflection": "...",
  "insight": "",
  "insight_refs": [],
  "proposals": [
    {
      "family": "time_pdf_lookup",
      "decision": "switch",
      "proposal": "..."
    }
  ]
}
```

The four reasoning-chain entries are presented as parallel output-contract
items in the prompt:

- `reflection`
- `insight`
- `insight_refs`
- `decision`

`proposal` remains the primary action output immediately following `decision`,
as in the current prompt.

### 8.1 `reflection`

`reflection` keeps the current prompt's compact, evidence-oriented tone.

When prior history exists, it should use at most one or two dense sentences to:

- identify the historical evidence that matters for this round;
- relate accumulated Insights, recent results, and current accepted source;
- judge whether the current target bottleneck or optimization hypothesis has
  one concrete, substantively different next opportunity, or has exhausted its
  worthwhile headroom.

Round zero may leave it empty.

### 8.2 `insight`

`insight` is the smallest reusable part of the reflection, written as one or
two concise, generalizing sentences.

It is not:

- another recap of the recent round;
- a restatement of the proposal;
- an implementation note;
- a separate line of reasoning.

It should be non-empty only when the reflection produces a durable conclusion
that is useful beyond the immediate next proposal and is not already expressed
by an accumulated Insight.

When there is no such conclusion, it is an empty string.

### 8.3 `insight_refs`

`insight_refs` lists the exact historical candidate records supporting the new
Insight.

The prompt must include concrete examples so the Proposer knows the required
format from its first memory-producing round:

```json
["r0c0", "r1c1"]
```

Valid references use:

```text
r<round>c<candidate>
```

When `insight` is empty, `insight_refs` is an empty list. When `insight` is
non-empty, every reference must resolve to an existing candidate in the current
run.

### 8.4 `decision`

`decision` preserves the current exact tokens and per-candidate location:

- `continue`: the reflection identifies a worthwhile, substantively different
  next experiment on the same target bottleneck or optimization hypothesis;
- `switch`: the reflection finds no worthwhile next experiment there, so a
  different target bottleneck or optimization hypothesis should be pursued.

In multi-candidate mode, each proposal has its own decision. The shared
reflection describes the round's overall search understanding.

### 8.5 `proposal`

`proposal` remains the primary output:

- for `continue`, it gives a substantively different next experiment in the
  same family;
- for `switch`, it moves to a genuinely different target or mechanism.

It remains grounded in the current accepted base and identifies the target,
suspected waste, mechanism to test, expected benefit, and one-round scope
without prescribing implementation details.

## 9. Prompt Wording

The existing Proposer prompt should be extended with the following wording,
keeping its current soft guidance and connected reasoning style.

```text
- "reflection" (mandatory when prior history exists; round 0 may leave it
  empty):

  In at most 1–2 dense sentences, use the accumulated insights, the most
  relevant recent outcomes, and the current accepted source to identify the
  historical evidence that matters for this round. Judge whether the current
  target bottleneck or optimization hypothesis still has one concrete,
  substantively distinct next opportunity, or has stalled or exhausted its
  worthwhile headroom. Accumulated insights are compact guides to older
  experience; when an important insight is too compact to support the choice,
  conflicts with recent evidence, or may no longer match the current source,
  you may inspect its referenced history records before deciding.

- "insight":

  If the reflection yields a durable lesson that is not already captured by
  the accumulated insights and may help future rounds, give its smallest
  reusable form as one or two concise, generalizing sentences. The insight
  should be the durable part of the reflection, not a separate recap,
  implementation note, or proposal. Otherwise return an empty string.

- "insight_refs":

  Give the exact historical candidate references that support the new insight,
  using the form "r<round>c<candidate>", for example ["r0c0", "r1c1"].
  Return an empty list when "insight" is empty.

- "decision":

  Encode the routing judgement made in "reflection" as exactly one token:
  `continue` — the reflection identifies a worthwhile, substantively distinct
  next experiment on the same target bottleneck or optimization hypothesis.
  `switch` — the reflection finds no worthwhile next experiment there, so
  another target bottleneck or optimization hypothesis should be pursued.

- "proposal" (the primary output):

  Propose the single highest-value direction that follows from the decision.
  If `continue`, give a substantively distinct next experiment on the same
  target or hypothesis; if `switch`, move to a genuinely different target or
  hypothesis. Ground it in the current accepted base by naming the target file,
  function, or subsystem, the suspected waste, the optimization mechanism to
  test, the expected benefit, and the one-round scope. State what should be
  tested, not how to implement it.

- The fields should form one connected line of reasoning: "reflection"
  explains the current understanding, "insight" preserves only the reusable
  part when one exists, "decision" expresses the resulting search judgement,
  and "proposal" is the next action implied by that judgement.
```

The wording deliberately avoids presenting Observe, Reflect, Learn, Search,
and Propose as independent mandatory sections. It guides one continuous
reasoning process and allows most rounds to produce no Insight.

## 10. Persistence Flow

For each round:

```text
load insights.jsonl
load recent six history records
        ↓
build Proposer prompt
        ↓
Proposer returns:
reflection
optional insight + insight_refs
candidate decisions + proposals
        ↓
Executor / evaluation / Judger
        ↓
append completed round to history.jsonl
        ↓
if insight is non-empty and all refs are valid:
append I<round> to insights.jsonl
        ↓
next round sees the new Insight
```

The Insight is based only on experiments already completed before the Proposer
call. Persisting it after the current round record keeps run writes ordered and
resume behavior simple without changing its evidential meaning.

## 11. Validation and Failure Behavior

The harness performs only minimal validation:

- `insight` is a string;
- `insight_refs` is a list of strings;
- an empty Insight requires an empty reference list;
- a non-empty Insight requires at least one reference;
- every reference resolves in the current run;
- each reference has the normalized `r<round>c<candidate>` form;
- an Insight ID for the same round is not appended twice.

The harness does not judge whether an Insight is causally correct, important,
novel, or well generalized. Those are Proposer reasoning responsibilities.

If a non-empty Insight contains invalid references, the candidate generation
must not be lost. The MVP records the round normally, emits a warning, and does
not append that Insight. It does not retry the Proposer solely for an Insight
failure.

Failure to read or parse `insights.jsonl` should fail clearly before the
Proposer call rather than silently discarding long-run memory.

## 12. Compatibility

- Existing `history.jsonl` files remain valid.
- Missing `insight` and `insight_refs` fields from old responses default to
  empty values.
- Legacy serial history records are exposed as `r<round>c0`.
- Static-proposal mode produces no Insight unless a future design explicitly
  adds an external source.
- `--continue` reloads the existing append-only Insights and uses the next
  target round for the next possible Insight ID.
- Executor, Judger, candidate execution, candidate selection, gates, and
  accepted lineage behavior do not change.

## 13. MVP Acceptance Criteria

The implementation is complete when:

1. Every current-run candidate can be retrieved by `r<round>c<candidate>`.
2. The Proposer prompt contains all accumulated compact Insights.
3. The prompt contains only the most recent six detailed round records.
4. The Proposer can optionally retrieve a referenced old candidate without
   reading the complete history.
5. The structured output contains `reflection`, `insight`, `insight_refs`, and
   the existing proposals with per-candidate `decision`.
6. The prompt shows `insight_refs` examples including `r0c0` and `r1c1`.
7. A non-empty valid Insight is appended exactly once with an `I<round>` ID.
8. An empty Insight causes no Insight write.
9. Invalid Insight references do not discard completed candidate work.
10. No additional model or Agent call is introduced.
11. Existing runs and `--continue` remain compatible.

Product evaluation should observe:

- fewer `already-implemented` duplicate candidates;
- fewer renamed retries of previously disproven mechanisms;
- Proposals that reuse or appropriately qualify earlier Insights;
- useful transfer of lessons between different source locations;
- stable Proposer prompt size relative to detailed round history.

## 14. Final Architecture

```text
┌─────────────────────────────────────────────┐
│ history.jsonl                               │
│ complete append-only candidate experiments │
│ refs: r0c0, r1c1, ...                       │
└──────────────────────┬──────────────────────┘
                       │ reflection grounds insight in refs
                       ▼
┌─────────────────────────────────────────────┐
│ insights.jsonl                              │
│ compact append-only reusable conclusions   │
│ ids: I0, I4, I31, ...                       │
└──────────────────────┬──────────────────────┘
                       │ render all compact insights
                       ▼
┌─────────────────────────────────────────────┐
│ Proposer context                            │
│ current base                                │
│ accumulated insights                        │
│ recent six detailed rounds                 │
│ optional `memory show <ref>`                │
└──────────────────────┬──────────────────────┘
                       ▼
              connected reflection
                       │
              optional new insight
                       │
                simple search
                       │
              continue / switch
                       │
                 next proposal
```

The MVP is therefore one append-only Insight index, one exact historical
reference lookup, and two small additions to the current Proposer output.
