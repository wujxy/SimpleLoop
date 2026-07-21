# SimpleLoop examples

Three filled-in task configs + a generic reference template. Each folder is a
self-contained optimization task: a `task.yaml` (the SimpleLoop config), the
source repo it optimizes (a sibling git repo, referenced by relative path), and
a README explaining the loop/gate/run.

```
examples/
  task.yaml                  # generic reference template (not runnable — fill it in)
  tiny_algo_opt/             # self-contained toy target (Python, ~µs-scale)
  omilrec-opt/               # OMILRECV2 FCN optimization — paper reproduction + v1.11.0 sibling
  omilrec-post-v107-opt/     # OMILRECV2 from v1.0.0, gated by post-v1.0.7 FCN + relaxed e2e
```

## `task.yaml` — reference template

A heavily-commented, minimal config covering the full schema (`task` / `safety`
/ `loop` / `eval` / `source`, including the optional `eval.metrics` objective +
gates block). Not runnable as-is — its `source.path` is a placeholder. Copy it,
point `source.path` at a real git repo, and `simpleloop validate --config <copy>`
before running. Schema + validation live in `../simpleloop/config.py` (strict:
unknown top-level keys error at validate, not silently mid-run).

## `tiny_algo_opt/` — the toy target

A deliberately-slow-but-correct 2-D Manhattan pair-counting function. The whole
target ships in-repo (`repo/tinyalgo/__init__.py`) — initialize it once with
`setup.sh` (it has no `.git`, so SimpleLoop's `git clone --local` needs it
initialized), then run. Good for exercising SimpleLoop end-to-end without the
JUNO environment: correctness (`pytest`) + drift (`check_drift.py`) are hard
gates, `bench.py`'s `ms_per_call` is the objective. See
`tiny_algo_opt/README.md`.

## `omilrec-opt/` — OMILRECV2, paper reproduction + v1.11.0

Two sibling tasks on the OMILRECV2 reconstruction FCN, plus the static
paper-reproduction proposal batches:

- `task.yaml` — the v1.0.0 paper-reproduction task. Target: the unoptimized
  v1.0.0 package (`../../../omilrec-v100`, monolithic `OMILRECV2.cc::execute()`
  inlined FCN, ~514 ms/evt). Correctness gate is **bit-identical**
  (`reference/ref_10evt.root`, tolerance 0) — only bit-identical refactors pass.
  The static proposals (`omilrec-paper-proposals-{test,main,total}.yaml`) are
  written against this monolithic layout; replay them with `--proposals`.
- `omilrec-v1.11.0.yaml` — a sibling task on the optimized v1.11.0 package
  (`../../../omilrec`), which has the free-function FCN + the 1e-13 FCN drift
  gate + 4 mm / 7 keV e2e gate — permits precision-preserving optimizations the
  bit-identical gate forbids.

`omilrec-paper-proposals-summary.md` indexes the 18 proposals and the
recommended smoke / main / total run order. See `omilrec-opt/README.md`.

## `omilrec-post-v107-opt/` — v1.0.0 start, post-v1.0.7-gated

The independent optimization experiment: start from the v1.0.0 source shape,
but gate with the post-v1.0.7 FCN replay truth (≤ 1e-13 vs the frozen
v1.0.7-rev1 golden pack) **and** the relaxed reconstruction tolerances
(4 mm / 7 keV / 10 ps / 0.1 PE). Target: the freshly-prepared
`../../../omilrec-v100-postv107-gated` package (branch `postv107-gated-v100`).
Verified start point ~873 ms/evt (100 events) on Intel Xeon 8358P. This is the
task that combines v1.0.0's wide optimization space with v1.11.0's permissive
correctness contract. See `omilrec-post-v107-opt/README.md`.

## Running any of them

```bash
# from the SimpleLoop checkout (cwd = SimpleLoop/)
simpleloop validate --config examples/<folder>/task.yaml
simpleloop run      --config examples/<folder>/task.yaml --run-dir ./runs/<name>-001
```

The two OMILRECV2 tasks need the JUNO environment and the external bench input
on this machine — but the eval wrapper sources the JUNO env itself, so the
SimpleLoop agent subprocess does not need a pre-sourced shell. Run SimpleLoop
under **bash** (the CVMFS setup leaves ROOT unset under zsh). `claude` must be
installed and authenticated. See each folder's README for per-task notes.
