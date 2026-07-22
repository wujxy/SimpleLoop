# Agent JSON Length Tolerance Design

## Scope

Make the smallest local changes needed to prevent overlong free-text fields
from voiding otherwise valid candidates, and make parallel candidate failures
visible in the run log.

## Contract

- The proposer keeps its existing generation limits: `reflection` 600 chars and
  `proposal` 800 chars. Its parser accepts longer valid output but truncates only
  above 900 and 1100 chars respectively.
- The judger receives a JSON Schema with `feedback.maxLength = 500`. Its parser
  accepts feedback through 800 chars and truncates anything longer to 800 rather
  than rejecting the judgment.
- This N+300 tolerance applies only to free text. Types, required/exact keys,
  enums, score range, exact candidate count, and case-insensitive unique family
  names remain hard failures.
- The executor remains text/edit based and receives no JSON Schema.

## Schema/Parser Alignment

- Add the missing judger schema for exact keys, score range, risk enum, and
  non-blank feedback capped at the requested generation length.
- Add non-blank patterns to proposer free-text/identifier fields. Keep the
  parser's family uniqueness check and state that requirement explicitly in the
  proposer prompt because case-insensitive uniqueness is not expressible by the
  current JSON Schema shape.
- Do not introduce a shared policy abstraction or retry mechanism.

## Logging

- Label parallel judgers with their round/candidate ID.
- Print candidate-local failures before converting them to the existing
  `_candidate_failure` record.

## Tests

Add focused tests for judger schema delivery, N+300 truncation boundaries,
proposer truncation boundaries, retained structural rejection, and
candidate-specific logging. Run the full SimpleLoop unit suite afterward.
