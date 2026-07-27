# Post-v1.0.7 Proposer Diversity Goal Design

## Goal

Reduce search anchoring in `examples/omilrec-post-v107-opt/task_hints.yaml`
without prescribing a new optimization direction.

## Change

- Remove the `Reference directions include...` paragraph from `task.goal`.
  Its examples can bias repeated proposals toward the named mechanisms.
- Add a short outcome-aware instruction telling the proposer:
  - not to repeat minor variants of the same code region or mechanism;
  - to switch after repeated neutral or regressed results unless new evidence
    materially distinguishes the retry;
  - to keep the three candidates in a batch meaningfully diverse.
- Preserve the FCN and consistency gate descriptions, including their safe and
  forbidden transformation examples. Those define correctness boundaries
  rather than preferred search directions.
- Preserve the existing user change from `proposer_recent_rounds: 4` to `8`.

## Scope and Verification

Only the task goal text changes. No runtime behavior, evaluator, source path, or
gate is modified. Verification consists of parsing the YAML through the existing
configuration loader and running the relevant configuration tests.
