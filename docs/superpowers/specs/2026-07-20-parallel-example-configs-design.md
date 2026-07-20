# Parallel Example Configs Design

## Goal

Make the bundled `tiny_algo_opt` and OMILREC v1.0.0 examples exercise the new
parallel-candidate self-loop out of the box.

## Shared Parallel Defaults

Both examples use:

```yaml
loop:
  candidates_per_round: 3
  max_workers: 3
```

Each round therefore asks the proposer for three distinct candidate families
and permits all three executor/eval/judger pipelines to run concurrently.
Setting either example back to `1/1` remains the documented serial-compatible
fallback.

## OMILREC v1.0.0

`examples/omilrec-v100.yaml` already declares:

- Objective: `SPEED_MS`, lower is better.
- Gates: `CORRECTNESS` and `EVAL_RESULT`.

Only the two loop parallelism fields are added. The README explains that a
round now creates three candidates from one accepted `base_sha`, evaluates each
independently, and selects the lowest eligible `SPEED_MS`.

The existing 40-round limit is unchanged. With the new defaults this permits up
to 120 candidate executions, so the README must call out the increased Claude,
build, and benchmark resource use.

## tiny_algo_opt

`examples/tiny_algo_opt/task.yaml` gains the same parallel fields and an
objective-owned selector:

```yaml
eval:
  metrics:
    objective:
      key: ms_per_call
      lower_is_better: true
    gates:
      - key: CORRECTNESS
      - key: DRIFT
```

The benchmark already prints `ms_per_call=<number>`. The existing correctness
commands are wrapped so they also print parseable gate lines:

```yaml
- "PYTHONPATH=. python -m pytest tests/ -q && echo CORRECTNESS=PASS || echo CORRECTNESS=FAIL"
- "PYTHONPATH=. python scripts/check_drift.py && echo DRIFT=PASS || echo DRIFT=FAIL"
```

Every eval command is still run independently, so the benchmark runs even when
a gate fails. The selector excludes a candidate unless both gates are `PASS`
and `ms_per_call` is numeric, then chooses the lowest `ms_per_call`; judger
score is only a tie-breaker.

The README explains the three-candidate generation, objective selection, and
the `1/1` serial fallback.

## Files

- Modify `examples/tiny_algo_opt/task.yaml`.
- Modify `examples/tiny_algo_opt/README.md`.
- Modify `examples/omilrec-v100.yaml`.
- Modify `examples/omilrec-v100-README.md`.
- Add config-level tests to `simpleloop/tests/test_parallel_candidates.py`.

## Validation

Tests parse both real example YAML files with `yaml.safe_load` and assert:

- `candidates_per_round == 3`.
- `max_workers == 3`.
- OMILREC retains `SPEED_MS`, `CORRECTNESS`, and `EVAL_RESULT`.
- tiny declares `ms_per_call`, `CORRECTNESS`, and `DRIFT`.

Validation also runs:

```bash
bash examples/tiny_algo_opt/setup.sh
python -m simpleloop.cli validate --config examples/tiny_algo_opt/task.yaml
python -m simpleloop.cli validate --config examples/omilrec-v100.yaml
python -m pytest simpleloop/tests
```

The raw-YAML tests do not depend on nested-repo initialization. The CLI
validation explicitly initializes the tiny source repo with its existing
idempotent `setup.sh`.

## Out Of Scope

- Core loop, selector, proposer, executor, or judger changes.
- New command-line modes.
- Per-example dynamic worker detection.
- Changing `max_rounds`.
- Running a real Claude optimization loop.
