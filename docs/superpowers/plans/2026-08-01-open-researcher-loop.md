# Open Researcher Loop: Implementation Record

Date: 2026-08-01
Status: complete

Design: [open-researcher-loop-design.md](../specs/2026-08-01-open-researcher-loop-design.md)

## Scope

Simplify the inner loop to Researcher -> Executor -> Harness evaluation while
keeping the outer loop, hard gates, factual history, and deterministic objective
selection.

## Completed changes

1. Removed the Judger role, prompt, invocation, configuration, score fields, and
   score-based reporting.
2. Opened the Proposer prompt: it receives goals, constraints, parent SHA, and
   factual evidence, then chooses its own research strategy.
3. Reduced candidate execution to an instruction, an Executor-produced commit,
   and Harness-owned changed-path/evaluation gates.
4. Unified eligibility after all gates. Failed attempts remain in history but
   cannot advance the parent.
5. Added deterministic objective selection across parallel candidates, all
   starting from the same parent SHA.
6. Replaced narrative Judger memory with compact factual Proposer views and
   explicit episode lookup.
7. Updated summaries, export, plotting, telemetry, and self-improvement for the
   reduced topology.
8. Removed old-run field fallbacks, active-prompt migration, and Store best
   caches. Current history, manifests, remote results, and active prompts are
   rejected at their input boundaries instead of migrated.
9. Updated fixtures to the current schema and aligned example fanout assertions
   with the example configuration.

## Supported candidate facts

```text
candidate, proposal, parent_sha, sha, status,
changed_paths, eval_block, metrics, gates,
gate_passed, eligible, selected, telemetry
```

`history.jsonl` is authoritative. Best SHA, round, and candidate are computed
from it when summaries or exports need them.

## Verification

Run from the repository root:

```bash
python -m compileall -q simpleloop
pytest -q tests
rg -n 'candidate_status|_normalize_active_set|best_sha:|best_round:|best_candidate:' simpleloop
```

Expected results:

- compilation succeeds;
- the complete test suite passes;
- the compatibility/cache search returns no runtime matches;
- the example configuration tests assert the values declared in their YAML.

## Explicitly deferred

- Harness evolution;
- recursive self-iteration without an outer loop;
- compatibility with pre-refactor run directories;
- additional research roles or semantic scoring layers.
