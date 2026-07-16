# omilrec — SimpleLoop task for OMILRECV2 speed optimization

Wires the lean OMILRECV2 package (`../../omilrec`) into SimpleLoop as a
proposer→executor→judger speed-optimization task.

## Layout

```
omilrec.yaml      # this task config (goal, safety, eval, source)
../../omilrec/    # the source repo to optimize (real git repo at ff71809/v1.11.0)
  OMILRECV2/src/        # the algorithm — the only editable surface
  scripts/sl_eval.sh    # the eval entry point (build + test_fcn + bench)
  scripts/quick_bench.sh, benchmark.sh, diff_drift.py
  tests/                # frozen (FCN golden pack + e2e reference)
  docs/RUN.md           # operational guide
  CLAUDE.md             # source map + FCN idempotency rules
```

## What the loop does on this task

- **proposer** reads `../../omilrec/` (read-only) and proposes a speedup of the
  OMILRECV2 FCN / likelihood hot path.
- **executor** edits `OMILRECV2/src/*.{cc,h}` in a fresh worktree; the harness
  commits. The gate rejects any edit to tests/scripts/CMake/benchmarks/docs.
- **harness** runs the single eval command `bash scripts/sl_eval.sh --evtmax 10`
  *in the worktree* (the committed candidate, before the worktree is removed).
  The wrapper sources the JUNO env, builds the lib + `test_fcn`, runs the FCN
  drift gate, runs `quick_bench`, and prints:
  ```
  FCN=PASS (max_rel=1.3e-14)
  SPEED_MS=164.01  ms/evt (10 events, <machine>)
  EVAL_RESULT=ok
  ```
- **judger** grades the diff + that eval output: a candidate that lowers
  `SPEED_MS` with `FCN=PASS` scores high; `FCN=FAIL` or `EVAL_RESULT=fcn_fail`
  must score low (Minuit amplifies FCN arithmetic drift into multi-mm vertex
  errors — a faster-but-wrong FCN is worse than useless).

## Run

```bash
# from the SimpleLoop checkout
simpleloop validate --config examples/omilrec.yaml
simpleloop run      --config examples/omilrec.yaml --run-dir ./runs/omilrec-001
```

Each round's eval builds from scratch in the worktree (600s budget, `--evtmax 10`
fits it). For a less noisy speed delta, raise SimpleLoop's per-command eval
timeout and use `--evtmax 100` in the eval command.

## Trace the result

```bash
git -C runs/omilrec-001/repo log --oneline      # the round commit chain
cat runs/omilrec-001/history.jsonl              # proposals, scores, feedback
```

## Prerequisites (external, read-only, already on this machine)

- JUNO env: `/cvmfs/juno.ihep.ac.cn/el9_amd64_gcc11/Release/J26.1.1/setup.sh`
- Bench input: `/data/juno/dingxf/inputs/index_12628_rtraw_1.json`
- RecMap dir: `/data/juno/dingxf/OMILREC_maps`

The eval wrapper sources the JUNO env itself, so SimpleLoop's agent subprocess
does not need a pre-sourced JUNO shell. Run SimpleLoop under **bash** (the CVMFS
setup leaves ROOT unset under zsh). `claude` must be installed and authenticated.

## Note on the speed signal

`quick_bench.sh` at `--evtmax 10` has ~±6% noise. A single round's `SPEED_MS` is
a hint, not a verdict — the judger is told to reward *real measured* improvement
and to compare against prior rounds' numbers in the history. For a definitive
delta, compare baseline vs candidate on the same production build at 100 events
× 3 reps (see `../../omilrec/docs/RUN.md` → "Speed honesty").
