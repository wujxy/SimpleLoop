# Structured Free-Text Truncation Design

**Status:** Approved design
**Date:** 2026-07-23
**Scope:** Proposer and Judger structured-output free-text handling

## 1. Goal

Prevent an otherwise valid Proposer or Judger response from failing because a
free-text field exceeds its requested length.

The model prompt continues to request a concise generation limit `N`. The
harness accepts additional headroom and truncates only above `N+500`. An
overlong free-text field must never, by itself, exhaust structured-output
retries or terminate the loop.

Structural contract violations remain hard failures.

## 2. Problem

The current implementation uses the same `N+300` boundary in two places:

1. as `maxLength` in the provider-facing JSON Schema;
2. as the local parser's slicing boundary.

The provider validates the schema before SimpleLoop receives the structured
result. A response longer than `N+300` is therefore rejected and retried inside
Claude Code, so the local parser never gets the opportunity to truncate it.

The failed run demonstrated this path: five otherwise structurally valid
Proposer responses were rejected because `reflection` exceeded its 900-character
schema limit. The internal retries exhausted and terminated the loop.

The intended responsibilities must instead be:

```text
Prompt       requests concise text at N
JSON Schema  validates structure, not free-text length
Parser       accepts headroom and truncates above N+500
```

## 3. Unified Length Policy

The same policy applies to every Proposer and Judger free-text field:

```text
normalized length <= N+500
    accept unchanged

normalized length > N+500
    print a warning
    truncate to N+500
    continue normal processing
```

Here, normalized length is measured after leading and trailing whitespace is
removed. The harness does not warn merely because a response is between `N`
and `N+500`; that interval is intentional tolerance. A warning means that
SimpleLoop modified the returned text.

The configured limits are:

| Field | Prompt target N | Parser limit N+500 |
|---|---:|---:|
| `proposer.reflection` | 600 | 1100 |
| `proposer.insight` | 500 | 1000 |
| `proposer.insight_refs[]` | 32 | 532 |
| `proposer.proposals[].family` | 64 | 564 |
| `proposer.proposals[].proposal` | 800 | 1300 |
| `judger.feedback` | 500 | 1000 |
| `judger.feedback_for_proposer` | 300 | 800 |

The prompt targets remain unchanged. Only the tolerance margin changes from
300 to 500.

## 4. JSON Schema Contract

Remove `maxLength` from all Proposer and Judger free-text schema properties.

The schemas continue to enforce:

- exact required keys;
- `additionalProperties: false`;
- value types;
- nonblank strings through `minLength` and `pattern`;
- exact Proposer candidate count;
- `decision` and `risk` enums;
- Judger score range.

Removing free-text `maxLength` does not make malformed structured output
acceptable. It only moves a model-unreliable character-count constraint out of
the provider retry boundary.

## 5. Parser Normalization

Use one shared helper for Proposer and Judger free text. Its interface must
accept:

- the string value;
- the `N+500` parser limit;
- the role label used in logs;
- the precise field path.

The helper:

1. strips leading and trailing whitespace;
2. measures the normalized string;
3. returns it unchanged when it is at or below the parser limit;
4. prints one warning and returns the prefix at the parser limit when it is
   over the limit.

Type and blank-string validation remains in the role parser before calling the
helper.

Example warnings:

```text
[proposer] warning: reflection length 1131 exceeds 1100; truncated to 1100
[proposer] warning: proposals[0].proposal length 1428 exceeds 1300; truncated to 1300
[judger r14-c2] warning: feedback length 1082 exceeds 1000; truncated to 1000
```

The Judger passes its existing candidate-qualified label so warnings remain
attributable under parallel execution.

Family uniqueness is checked after normalization and truncation. Two distinct
raw family strings that become equal after truncation, trimming, and
case-folding remain a hard Proposer parse failure.

## 6. Error Semantics

Free-text overlength is recoverable:

```text
overlength -> warning -> truncation -> continue
```

The following remain hard contract failures:

- invalid or unparseable JSON;
- a missing or additional key;
- a wrong value type;
- a blank required string;
- an invalid enum;
- an out-of-range Judger score;
- the wrong number of Proposer candidates;
- duplicate normalized Proposer families.

A Proposer structural failure continues to abort the generation because no
valid candidate batch exists. A Judger structural failure retains the current
candidate-local failure behavior and does not invalidate other parallel
candidates.

No additional model retry, repair call, or fallback parser is introduced.

## 7. Compatibility

Existing valid outputs are unchanged.

Outputs in the old tolerance interval keep their current values:

- Proposer text up to the former `N+300` boundary;
- Judger text up to the former `N+300` boundary.

Outputs between `N+300` and `N+500`, previously rejected by the provider-facing
schema, are now accepted unchanged. Outputs above `N+500` are accepted after a
visible deterministic truncation.

Stored history and insight formats do not change.

## 8. Implementation Scope

Expected implementation files:

- `simpleloop/agent.py`
  - provide the small shared free-text normalization helper;
- `simpleloop/proposer.py`
  - change the tolerance constant from 300 to 500;
  - remove free-text `maxLength` declarations;
  - normalize all free-text fields through the shared helper;
- `simpleloop/judger.py`
  - change the tolerance constant from 300 to 500;
  - remove free-text `maxLength` declarations;
  - normalize both feedback fields through the shared helper;
- `simpleloop/tests/test_parallel_candidates.py`
  - update schema and Proposer parser coverage;
- relevant Judger/agent tests
  - update schema and parser coverage for both feedback fields.

No loop scheduling, candidate selection, memory persistence, or prompt
semantics change is required.

## 9. Tests

Add or update focused tests for:

1. Proposer and Judger free-text schema properties do not contain
   `maxLength`.
2. All existing structural schema constraints remain present.
3. A value exactly at `N+500` is returned unchanged without a warning.
4. A value at `N+501` is truncated to `N+500` with one warning containing the
   role, field path, original length, and final limit.
5. Every Proposer free-text field follows the same policy.
6. Both Judger feedback fields follow the same policy.
7. Candidate-qualified Judger labels appear in warnings.
8. Family uniqueness is checked after truncation.
9. The failed-run shape, including a 1131-character reflection, is accepted and
   truncated to 1100 rather than rejected.
10. Missing keys, extra keys, invalid types, blank strings, invalid enums,
    score-range violations, wrong candidate counts, and duplicate families
    remain rejected.
11. The full SimpleLoop unit suite passes.

## 10. Success Criteria

The change is successful when:

- no Proposer or Judger call can fail solely because a free-text field is too
  long;
- every actual truncation is visible in the run log;
- the model still receives the original concise `N` targets;
- the parser consistently applies `N+500`;
- all non-length structured-output guarantees remain strict.
