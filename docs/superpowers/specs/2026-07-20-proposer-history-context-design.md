# Proposer History Context Design

## Goal

Reduce proposer prompt growth for runs lasting tens to roughly one hundred
rounds, without changing persisted history or removing the evidence needed to
avoid repeated proposals.

## Context Model

The proposer needs three kinds of history:

1. Full proposals from the most recent rounds for continued investigation and
   correction.
2. A compact record of older attempts and their outcomes to avoid repetition.
3. Candidate SHAs so it can inspect an old implementation when the compact
   record is insufficient.

This MVP compresses only old proposal text. It does not attempt to put a fixed
upper bound on a thousand-round prompt.

## Round Record And Round Number

A **round record** is one top-level object in `history.jsonl`, and therefore one
item in the `history` list returned by `Store.history()`. It represents one
completed loop generation.

The **round number** is only the value stored in that object's `round` field. It
is an identifier used in logs and reports; it is not the source of truth for
recency.

For example:

```python
history = [
    {"round": 3, ...},
    {"round": 5, ...},
    {"round": 12, ...},
]
```

There are three round records. The most recent two records are those with round
numbers 5 and 12 because they are the final two list items, not because their
numbers are consecutive.

In parallel self-loop mode, one round record contains a `candidates` list with K
candidate attempts. The record still consumes one position in the six-record
recent window. Every candidate in that record receives the same full-or-compact
proposal treatment.

## Projection Rules

Apply compression only in `views.for_proposer(history)`. The append-only
`history.jsonl`, `Store.history()`, and final report retain the full proposal.

Use two named module constants:

```python
_PROPOSER_FULL_PROPOSAL_ROUNDS = 6
_PROPOSER_OLD_PROPOSAL_CHARS = 300
```

Determine the recent window by record position:

```python
full_proposal_start = max(0, len(history) - _PROPOSER_FULL_PROPOSAL_ROUNDS)
```

For records at or after `full_proposal_start`, project:

```python
{"proposal": full_text}
```

For earlier records, normalize and truncate:

```python
normalized = " ".join(full_text.split())
head = normalized[:_PROPOSER_OLD_PROPOSAL_CHARS]
if len(normalized) > _PROPOSER_OLD_PROPOSAL_CHARS:
    head += "..."
```

Project old records as:

```python
{"proposal_head": head}
```

Do not include a `proposal` key for old records. If the normalized text is at
most 300 characters, do not append an ellipsis.

Apply the same rule to:

- Legacy serial records, where `proposal` is a top-level field.
- Parallel generation records, where each item in `candidates` has a proposal.

All other proposer-visible fields remain unchanged:

- candidate SHA
- accepted, selected, parent, base, and selected SHA state
- metrics
- score and risk
- changed paths
- feedback
- `feedback_for_report`

`eval_block` remains excluded.

## Prompt Rendering

The proposer prompt renderer must accept either projected key:

- Recent history: `proposal="..."`
- Older history: `proposal_head="..."`

This distinction tells the proposer that an older text is incomplete and that
the retained SHA can be used for targeted inspection when necessary.

## Compatibility

- No configuration fields are added.
- No history schema is changed.
- Existing run directories and `--continue` remain compatible.
- Manual `--proposals` history uses the same projection behavior because the
  distinction is based on stored round records, not proposal source.
- The input history list and its nested candidate dictionaries must not be
  mutated.

## Tests

Unit tests must verify:

1. Histories of six records or fewer retain complete `proposal` fields.
2. In a seven-record history, only the first record uses `proposal_head`.
3. Recency follows list position when round numbers are non-contiguous.
4. Old text collapses whitespace and truncates to 300 characters plus an
   ellipsis only when needed.
5. Every candidate in an old parallel generation uses `proposal_head`, while
   every candidate in a recent generation uses `proposal`.
6. SHA, metrics, score, risk, changed paths, and both feedback fields survive
   projection unchanged.
7. Projection does not mutate the source history.
8. Prompt rendering emits `proposal_head=` for old records and `proposal=` for
   recent records.

## Out Of Scope

- Compressing feedback or diagnostic text.
- Model-generated summaries.
- Family-level aggregation.
- Token counting or a hard prompt-size budget.
- A fixed context bound for thousand-round runs.
- User-configurable window or character limits.
