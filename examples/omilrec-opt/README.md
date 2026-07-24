# omilrec-opt — SimpleLoop task for OMILRECV2 speed optimization

Two sibling optimization tasks plus the static paper-reproduction proposal
batches, all targeting the OMILRECV2 FCN / likelihood hot path:

- **`task.yaml`** — the paper-reproduction task. Wires the unoptimized **v1.0.0**
  package (`../../../omilrec-v100`, ~514 ms/evt) into SimpleLoop. This is the
  widest optimization space — v1.0.0 is the original monolithic code
  (`OMILRECV2::execute()` inlined FCN) before the 5-8x of hand optimization that
  landed in v1.1.x. The v1.0.0 correctness gate is **bit-identical**
  (`reference/ref_10evt.root`, tolerance 0), so only bit-identical refactors pass.
  The static proposals below are written against this monolithic layout.
- **`omilrec-v1.11.0.yaml`** — a sibling task on the optimized **v1.11.0** package
  (`../../../omilrec`), which has the free-function FCN
  (`OMILRECV2/src/omilrec_fcn.cc`) and the 1e-13 FCN drift gate + 4 mm / 7 keV e2e
  gate. This permits precision-preserving optimizations the bit-identical gate
  forbids. (The static paper proposals target the v1.0.0 monolith, not this
  layout, so use them with `task.yaml`, not here.)

## Layout

```
omilrec-opt/
  apptainer.sif                  # symlink -> ../junosw-apptainer.sif (shared; see below)
  task.yaml                       # the v1.0.0 paper-reproduction task config
  omilrec-v1.11.0.yaml            # sibling task config targeting v1.11.0
  omilrec-paper-proposals-test.yaml    # static proposals 1-4   (smoke)
  omilrec-paper-proposals-main.yaml    # static proposals 1-12  (primary)
  omilrec-paper-proposals-total.yaml  # static proposals 1-18  (full set)
  omilrec-paper-proposals-summary.md   # index + recommended order
../../../omilrec-v100/            # v1.0.0 source repo (task.yaml target)
  OMILRECV2/src/                  # the algorithm — the only editable surface
  reference/ref_10evt.root        # FROZEN bit-identical baseline (10 events, tol 0)
  scripts/sl_eval.sh              # the eval entry point (build + e2e gate + bench)
  tests/test_consistency.py       # the e2e bit-identical gate
  docs/RUN.md, CLAUDE.md
../../../omilrec/                 # v1.11.0 source repo (omilrec-v1.11.0.yaml target)
  OMILRECV2/src/omilrec_fcn.cc    # the free-function Minuit objective
  tests/unit/test_fcn.cc          # the 1e-13 FCN drift gate
  tests/fixtures/v107_rev1/       # the frozen v1.0.7-rev1 golden pack
```

## What the loop does on this task

- **proposer** reads `../../../omilrec-v100/` (read-only) and proposes a speedup of
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
fallback experiment is the sibling `omilrec-v1.11.0.yaml` in this folder
(v1.11.0, 4mm/7keV gate + `test_fcn`), or `../omilrec-post-v107-opt/` (v1.0.0
start gated by the post-v1.0.7 FCN replay + relaxed reconstruction tolerances).

## Run

```bash
# from the SimpleLoop checkout — build the SHARED junosw image once
simpleloop image build examples/junosw-apptainer.def \
            --output   examples/junosw-apptainer.sif
simpleloop validate --config examples/omilrec-opt/task.yaml
simpleloop run      --config examples/omilrec-opt/task.yaml --run-dir ./runs/omilrec-opt-001

# replay the paper's optimization sequence instead of the live proposer:
simpleloop run --config examples/omilrec-opt/task.yaml \
              --proposals examples/omilrec-opt/omilrec-paper-proposals-main.yaml \
              --run-dir   ./runs/omilrec-opt-paper

# the v1.11.0 sibling (free-function FCN, 1e-13 gate):
simpleloop run --config examples/omilrec-opt/omilrec-v1.11.0.yaml --run-dir ./runs/omilrec-v1110-001
```

`runtime.image: apptainer.sif` resolves relative to this task config, and
`apptainer.sif` here is a symlink to the shared `../junosw-apptainer.sif`.
Both omilrec tasks (omilrec-opt and omilrec-post-v107-opt) reuse that one
image; build it once and both are ready.

The v1.0.0 task defaults to `candidates_per_round: 3` and `max_workers: 3`. With
`max_rounds: 40`, one run can execute up to 120 candidates and three concurrent
Claude/build/eval pipelines, so plan compute and model usage accordingly. Set
both parallel values to `1` for serial-compatible execution. The v1.11.0 sibling
is configured serial (`max_rounds: 4`, single candidate).

Each round's eval builds from scratch in the worktree (600s budget, `--evtmax 10`
fits it). For a less noisy speed delta, raise SimpleLoop's per-command eval
timeout and use `--evtmax 100` in the eval command.

## Trace the result

```bash
git -C runs/omilrec-opt-001/repo log --oneline      # the round commit chain
cat runs/omilrec-opt-001/history.jsonl              # proposals, scores, feedback
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
× 3 reps (see `../../../omilrec-v100/docs/RUN.md` → "Speed honesty").
