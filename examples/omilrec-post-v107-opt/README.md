# omilrec-post-v107-opt — SimpleLoop task: optimize from v1.0.0 under the post-v1.0.7 gate

Wires the freshly-prepared `omilrec-v100-postv107-gated` package
(`../../../omilrec-v100-postv107-gated`, branch `postv107-gated-v100`) into
SimpleLoop. This is the **independent optimization experiment**: start from the
v1.0.0 source shape, but gate with the post-v1.0.7 FCN replay truth **and** the
relaxed reconstruction tolerances — so the loop may attempt precision-preserving
optimizations the v1.0.0 bit-identical gate (`../omilrec-opt/task.yaml`)
forbids, while still being measured against the immovable v1.0.7-rev1 baseline.

The prepared package is documented under the package itself
(`../../../omilrec-v100-postv107-gated/`): its FCN source, the v1.0.7-rev1
fixture pack, the eval wrapper, and the verified baseline numbers live there,
not here.

## Verified baseline (the optimization start point)

On Intel Xeon Platinum 8358P @ 2.60GHz, single thread, Release build, the
prepared package's `sl_eval_post_v107.sh --evtmax 100` reports:

```
FCN=PASS                    # 16/16 fixture replay, max rel drift 1.311e-14 (≤ 1e-13)
CONSISTENCY=PASS            # 4 non-degenerate events, 0.0 abs diff vs ref_18evt.root
SPEED_MS=872.97715          # ~873 ms/evt (100 events) — the start point
EVAL_RESULT=ok
```

This ~873 ms/evt is what every optimization round must beat on the **same
machine_tag** (the harness writes `benchmarks/speed.csv`; compare with
`--baseline-ref` or the prior row). See the package's `docs/RUN.md` → "Speed
honesty" for the 100-evt × 3-reps methodology.

## Layout

```
omilrec-post-v107-opt/
  apptainer.sif          # symlink -> ../junosw-apptainer.sif (shared; see below)
  task.yaml               # this task config (goal, safety, eval, source)
../../../omilrec-v100-postv107-gated/   # the source repo to optimize
  OMILRECV2/src/          # the algorithm — the only editable surface
    omilrec_fcn.cc          # calculate_ev_likelihood — the Minuit objective (free function)
    omilrec_likelihood.cc, omilrec_charge_ll.cc, omilrec_time_pdf.cc, ...
  tests/unit/test_fcn.cc  # the 1e-13 FCN drift gate (16/16 vs v1.0.7-rev1 goldens)
  tests/fixtures/v107_rev1/  # FROZEN v1.0.7-rev1 golden pack — never touch
  tests/test_consistency.py  # relaxed e2e gate (4 mm / 7 keV / 10 ps / 0.1 PE)
  tests/reference/ref_18evt.root  # FROZEN v1.0.7-rev1 18-evt reference
  scripts/sl_eval_post_v107.sh  # the eval entry point (build + test_fcn + e2e + bench)
  scripts/quick_bench.sh, benchmark.sh
  benchmarks/{speed,drift}.csv   # the ledgers
  docs/RUN.md, CLAUDE.md
```

## What the loop does on this task

- **proposer** reads `../../../omilrec-v100-postv107-gated/` (read-only) and
  proposes a speedup of the free-function FCN / likelihood hot path. It produces
  three different candidate families per round.
- **executor** starts all three candidates from the same accepted `base_sha`,
  edits `OMILRECV2/src/*.{cc,h}` in isolated worktrees, and runs up to three
  pipelines concurrently. The gate rejects any edit to tests/scripts/CMake/
  benchmarks/docs and **especially `tests/fixtures/**` and `tests/reference/**`**
  (the frozen v1.0.7-rev1 truth).
- **harness** runs the single eval command
  `bash scripts/sl_eval_post_v107.sh --evtmax 10` *in the worktree* (the
  committed candidate, before the worktree is removed). The wrapper sources the
  JUNO env, builds the lib + `test_fcn`, runs the FCN drift gate, runs the
  relaxed e2e reconstruction gate, runs `quick_bench`, and prints:
  ```
  FCN=PASS
  CONSISTENCY=PASS
  SPEED_MS=NNN.NN  ms/evt (10 events, <machine>)
  EVAL_RESULT=ok
  ```
- **judger** grades the diff + that eval output: a candidate that lowers
  `SPEED_MS` with `FCN=PASS` and `CONSISTENCY=PASS` scores high; `FCN=FAIL` or
  `CONSISTENCY=FAIL` must score low (Minuit amplifies FCN arithmetic drift into
  multi-mm vertex errors — a faster-but-wrong FCN is worse than useless).
- **selector** chooses the lowest `SPEED_MS` among candidates whose hard gates
  pass and whose risk is not high.

## The gate contract (why arithmetic changes are constrained)

The FCN drift gate replays `calculate_ev_likelihood` against the frozen
v1.0.7-rev1 golden pack at ≤ 1e-13 relative (observed baseline ~1.3e-14, 6 ULP).
Any arithmetic change inside the FCN (log→log1p, pow→cube, Kahan summation,
float↔double, evaluation-order changes) can push drift past 1e-13 — and Minuit
then amplifies the sub-ULP LL difference into multi-mm vertex drift that fails
the relaxed e2e gate on the degenerate events. **If drift exceeds 1e-13, bisect
the FCN change — do not relax the tolerance and do not regenerate the golden
pack** (see the package's `CLAUDE.md` → "FCN Idempotency"). Safe optimizations
are bit-identical or precision-preserving refactors: hoisting, caching, index
partitioning, SoA layout, reciprocal precompute (verified per-term).

## Run

```bash
# from the SimpleLoop checkout — build the SHARED junosw image once
simpleloop image build examples/junosw-apptainer.def \
            --output   examples/junosw-apptainer.sif
simpleloop validate --config examples/omilrec-post-v107-opt/task.yaml
simpleloop run      --config examples/omilrec-post-v107-opt/task.yaml \
                    --run-dir   ./runs/omilrec-postv107-001
```

`runtime.image: apptainer.sif` resolves relative to this task config, and
`apptainer.sif` here is a symlink to the shared `../junosw-apptainer.sif`.
Both omilrec tasks (omilrec-opt and omilrec-post-v107-opt) reuse that one
image; build it once and both are ready. To point at a different image, set
`runtime.image` to its absolute path.

The task defaults to `candidates_per_round: 3` and `max_workers: 3`. With
`max_rounds: 40`, one run can execute up to 120 candidates and three concurrent
Claude/build/eval pipelines, so plan compute and model usage accordingly. Set
both parallel values to `1` for serial-compatible execution.

Each round's eval builds from scratch in the worktree (600s budget, `--evtmax 10`
fits it). For a less noisy speed delta, raise SimpleLoop's per-command eval
timeout and use `--evtmax 100` in the eval command (the verified baseline above
used 100 events × 3 reps).

## Trace the result

```bash
git -C runs/omilrec-postv107-001/repo log --oneline   # the round commit chain
cat runs/omilrec-postv107-001/history.jsonl           # proposals, scores, feedback
```

## Prerequisites (external, read-only, already on this machine)

- Runtime binds: `/cvmfs`, `/data/juno`, and
  `/datafs/users/wujxy/agent-sci/omilrec_opt`
- JUNO env: `/cvmfs/juno.ihep.ac.cn/el9_amd64_gcc11/Release/J26.1.1/setup.sh`
- Bench input: `/data/juno/dingxf/inputs/index_12628_rtraw_1.json`
- RecMap dir: `/data/juno/dingxf/OMILREC_maps`

The YAML mounts those large directory trees at unchanged paths. The eval
wrapper sources the JUNO env inside the SIF, so an activated host virtualenv or
JUNO shell does not affect the agent or authoritative evaluation. Claude
authentication is reused through the normal home mount.

## Note on the speed signal

`quick_bench.sh` at `--evtmax 10` has ~±6% noise. A single round's `SPEED_MS` is
a hint, not a verdict — the judger is told to reward *real measured* improvement
and to compare against prior rounds' numbers in the history. For a definitive
delta, compare baseline vs candidate on the same production build at 100 events
× 3 reps (see `../../../omilrec-v100-postv107-gated/docs/RUN.md` → "Speed honesty").
