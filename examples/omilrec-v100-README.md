# omilrec-v100 — SimpleLoop task for OMILRECV2 speed optimization (v1.0.0 baseline)

Wires the lean OMILRECV2 package (`../../omilrec-v100`, pinned at the
**unoptimized v1.0.0** baseline, ~514 ms/evt) into SimpleLoop as a
proposer→executor→judger speed-optimization task. This is the widest
optimization space — v1.0.0 is the original code before the 5-8x of hand
optimization that landed in v1.1.x.

## Layout

```
omilrec-v100.yaml         # this task config (goal, safety, eval, source)
../../omilrec-v100/       # the source repo to optimize (real git repo at v1.0.0)
  OMILRECV2/src/          # the algorithm — the only editable surface (7 .cc + .h)
  reference/ref_10evt.root  # FROZEN bit-identical baseline (10 events, tolerance 0)
  scripts/sl_eval.sh      # the eval entry point (build + e2e gate + bench)
  scripts/quick_bench.sh, benchmark.sh, diff_drift.py
  tests/test_consistency.py  # the e2e bit-identical gate
  docs/RUN.md             # operational guide
  CLAUDE.md               # source map + build/test quick-ref
```

## What the loop does on this task

- **proposer** reads `../../omilrec-v100/` (read-only) and proposes a speedup of
  the OMILRECV2 FCN / likelihood hot path (inlined in
  `OMILRECV2.cc::execute()`). It produces three different candidate families
  per round.
- **executor** starts all three candidates from the same accepted `base_sha`,
  edits `OMILRECV2/src/*.{cc,h}` in isolated worktrees, and runs up to three
  pipelines concurrently. The gate rejects any edit to
  tests/scripts/CMake/benchmarks/docs and **especially `reference/**`** (the
  frozen bit-identical baseline).
- **harness** runs the single eval command `bash scripts/sl_eval.sh --evtmax 10`
  *in the worktree* (the committed candidate, before the worktree is removed).
  The wrapper sources the JUNO env, builds the lib, runs the e2e bit-identical
  correctness gate, runs `quick_bench`, and prints:
  ```
  CORRECTNESS=PASS (10/10 events bit-identical)
  SPEED_MS=514.16  ms/evt (10 events, <machine>)
  EVAL_RESULT=ok
  ```
- **judger** grades the diff + that eval output: a candidate that lowers
  `SPEED_MS` with `CORRECTNESS=PASS` scores high; `CORRECTNESS=FAIL` or
  `EVAL_RESULT=correctness_fail` must score low.
- **selector** chooses the lowest `SPEED_MS` among candidates whose hard gates
  pass and whose risk is not high. Judger score is only a tie-breaker.

## The v1.0.0 correctness gate is strict

v1.0.0 has **no `test_fcn`** FCN replay test (that arrived in v1.10.0). Its
correctness contract is e2e **bit-identical**: the candidate's 10-event output
must match `reference/ref_10evt.root` exactly (tolerance 0, all 15 RecVertex
fields). This is far stricter than v1.11.0's 4mm/7keV gate.

Consequence: SimpleLoop's proposer is effectively restricted to **bit-identical
refactors** — hoisting loop-invariant work, caching, SoA data layout, precompute,
index partitioning. Any arithmetic change inside the FCN (log→log1p, pow→cube,
Kahan summation, float↔double) breaks the gate, because Minuit amplifies sub-ULP
differences into multi-mm vertex drift. This is the *real* constraint that the
v1.0.0→v1.8.0 optimization history operated under (the 1e-13 FCN gate was only
introduced later to permit precision-preserving optimizations).

If SimpleLoop produces no valid optimization under this strict gate, that is a
real finding ("v1.0.0 + bit-identical is too hard for an LLM optimizer"). The
fallback experiment is `omilrec.yaml` (v1.11.0, 4mm/7keV gate + `test_fcn`) or a
mid-history version like v1.8.0/v1.10.0 (1e-13 FCN gate, more permissive).

## Run

```bash
# from the SimpleLoop checkout
simpleloop validate --config examples/omilrec-v100.yaml
simpleloop run      --config examples/omilrec-v100.yaml --run-dir ./runs/omilrec-v100-001
```

The example defaults to `candidates_per_round: 3` and `max_workers: 3`. With
`max_rounds: 40`, one run can execute up to 120 candidates and three concurrent
Claude/build/eval pipelines, so plan compute and model usage accordingly. Set
both parallel values to `1` for serial-compatible execution.

Each round's eval builds from scratch in the worktree (600s budget, `--evtmax 10`
fits it). For a less noisy speed delta, raise SimpleLoop's per-command eval
timeout and use `--evtmax 100` in the eval command.

## Trace the result

```bash
git -C runs/omilrec-v100-001/repo log --oneline      # the round commit chain
cat runs/omilrec-v100-001/history.jsonl              # proposals, scores, feedback
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
× 3 reps (see `../../omilrec-v100/docs/RUN.md` → "Speed honesty").
