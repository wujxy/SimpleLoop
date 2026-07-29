# tiny_algo_opt — SimpleLoop example

A small, self-contained optimization task for exercising SimpleLoop end-to-end.
The target is a deliberately-slow-but-correct 2-D Manhattan pair-counting
function; the loop should make it faster without breaking correctness.

## Layout
```
tiny_algo_opt/
  apptainer.def     # independently buildable SimpleLoop runtime
  apptainer.sif     # generated locally; ignored by Git
  task.yaml          # SimpleLoop task config (goal, safety, eval, source)
  repo/              # the git repo to optimize (real working tree)
    tinyalgo/__init__.py   # count_pairs — the optimization target
    tests/test_correctness.py
    scripts/check_drift.py # numerical-equivalence gate
    scripts/bench.py       # speed benchmark (writes benchmarks/speed.csv)
```

## Run

```bash
# from the SimpleLoop checkout
simpleloop init --config examples/tiny_algo_opt/task.yaml
simpleloop run --config examples/tiny_algo_opt/task.yaml \
  --run-dir examples/tiny_algo_opt/runs/run-001
```

`init` creates the target repository's Git baseline and builds the missing
`examples/tiny_algo_opt/apptainer.sif` from the inferred same-name
`apptainer.def`. Re-running it reuses both. The repository itself is mounted
from the run directory and is not copied into the image.

The example defaults to `candidates_per_round: 3` and `max_workers: 3`. Set
both values to `1` for serial-compatible execution.

## What the loop does on this task
- **proposer** reads `repo/tinyalgo/` and proposes three different speedup
  families per round.
- **executor** runs each candidate from the same accepted `base_sha` in its own
  worktree; up to three candidates execute concurrently.
- **harness** runs the three eval commands (pytest, drift, bench) and feeds their
  stdout to the judger. It selects the lowest `ms_per_call` candidate among
  candidates whose `CORRECTNESS` and `DRIFT` gates pass.
- **judger** grades the diff + eval output: a change that breaks correctness or
  drift should score low; a change that lowers `ms_per_call` with all gates green
  should score high. Its score is only a tie-breaker for equal objective values.

## Trace the result
```bash
git -C examples/tiny_algo_opt/runs/run-001/repo log --oneline
```
