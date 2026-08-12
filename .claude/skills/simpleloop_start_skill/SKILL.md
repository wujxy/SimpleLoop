---
name: simpleloop_start_skill
description: >-
  Use when a user wants to prepare a new target project for SimpleLoop: assemble
  the resource pack (source git repo + editable/frozen split + correctness
  reference + Apptainer runtime image), author the task config (including the
  required eval.metrics block that declares the objective and gates), write or
  rewrite the eval script to emit the structured KEY=VALUE lines the harness
  parses, or launch the loop and sanity-check the first round. Guides the agent
  through the whole onboarding so the user's project is ready to drive the
  proposer→executor→judger loop with harness-owned metrics (not
  judger-hallucinated numbers).
---

# SimpleLoop Start Skill — prepare a task and run the loop

Print `[skill: simpleloop_start_skill]` before proceeding.

## What SimpleLoop is

SimpleLoop is a minimal LLM optimization loop over a git repo:

```text
user goal → proposer (reads history + insights, proposes candidate directions)
          → executor (edits code in a fresh worktree, delivers a commit SHA)  ┐ per
          → harness (diff gate + runs eval + parses metrics, deterministic)   │ candidate
          → judger (grades the diff + metrics, gives feedback + score)        ┘
          → harness selects the round's winner (gates + risk + objective)
          → feedback + insights feed the next round's proposer
```

One accepted commit per round, serial single-parent chain. By default one
candidate per round (`candidates_per_round: 1`); raising it fans out candidates
within a round (up to `max_workers` concurrently) while the accepted chain
stays single-parent.

Two design rules everything below follows from:

- **The harness owns the numbers.** It runs the eval commands, parses the
  structured `KEY=VALUE` metrics out of their output, computes the deltas, and
  selects `best` by the objective among gate-pass, risk-not-high candidates.
  The judger only interprets — it must never be the source of a metric,
  because an LLM asked to extract numbers from prose and do arithmetic
  hallucinates (a prior run invented a "baseline 874.50" that appears in no
  ground-truth source and propagated across 12 rounds of feedback).
- **Everything agent-facing runs inside one mandatory Apptainer image.** All
  three Claude roles and every eval command execute in the configured SIF with
  `apptainer exec --cleanenv`. There is no host-execution fallback. Git
  clone/worktree/gate/history logic stays on the host.

This skill guides you (the agent helping the user) through preparing a target
project so the loop runs correctly the first time. Do the parts in order:

- **Part 1 — Assemble the resource pack.** Source repo, editable/frozen
  discipline, correctness reference, runtime image.
- **Part 2 — Author the config + the eval script.** The `KEY=VALUE` contract
  is what the whole design rests on: get it wrong and the metrics don't parse,
  and the run either aborts at baseline or degrades to hallucinated numbers.
- **Part 3 — Validate, launch, and sanity-check round 0.**

SimpleLoop is general-purpose. Worked examples in the repo:
`examples/task.yaml` (heavily-commented reference template, not runnable
as-is), `examples/tiny_algo_opt/` (a tiny Python pair-counter — the simplest
complete task), and the `examples/omilrec-*` tasks (a real C++ physics
reconstruction). Treat them as *worked examples of the structure*, not the
structure itself: every concrete path, command, tolerance, and metric name
below is a **placeholder you must replace for the actual target project**.
Never copy OMILREC-specific values (`--evtmax 10`, `SPEED_MS`, `FCN`,
bit-identical tolerance, `/cvmfs` binds) into a different task — those are the
target's contract, not SimpleLoop's.

---

## Part 1 — Assemble the resource pack

Goal: a git repo SimpleLoop can `clone --local`, a clear editable/frozen
split, a correctness reference for the gate, and a SIF image the whole loop
runs inside.

### 1.1 Decide the optimization contract first

Before touching files, pin down with the user, in one short written note:

- **Objective** — what is being improved? (speed, accuracy, memory, binary
  size, a score…). This becomes `eval.metrics.objective.key` and the `KEY=`
  line the eval script must print.
- **Direction** — lower is better (speed, memory, error) or higher (accuracy,
  coverage)? This is `eval.metrics.objective.lower_is_better`. It is not
  guessable — declare it, or best selection inverts.
- **Correctness gate(s)** — what must not break? (a test suite, a
  bit-identical diff, a tolerance band). Each becomes an `eval.metrics.gates`
  entry with its own `KEY=` line. A candidate that fails a gate is never
  selected, even if its objective is best.
- **Editable vs read-only** — which source may either lane change? `editable_paths`
  is the shared writable world (a **whitelist**: only these paths are mounted
  read-write, and they are the only thing a proposal may touch). `read_only_paths`
  is the read world (mounted read-only into both lanes — tests, references, build
  config, benchmarks); the proposer surveys it, neither lane may edit it. The
  mount IS the constraint — there is no after-the-fact diff gate.
- **Runtime environment** — what must exist inside the container for build +
  eval to run? (compilers, Python packages, system libs, external data trees).
  This decides which Apptainer definition to build and which host directories
  to bind.

Do not proceed until the user has confirmed these five.

### 1.2 The source repo

SimpleLoop clones the source repo with `git clone --local` into a per-run
clone, then makes a worktree per candidate. Requirements:

- It must be a real git repo (`.git` exists) — `source.path` in the config
  points at it (relative to the config file, or absolute). If the user hands
  you a bare directory, initialize it:
  `git init -q && git add -A && git commit -qm init`
  (that is exactly what `examples/tiny_algo_opt/setup.sh` does).
- `source.baseline_ref` is the starting commit (branch name or SHA; default
  `HEAD`). The baseline eval runs once on this commit before round 0 — pick
  the **unoptimized** starting point, since improvement is measured from here.
- Binary fixtures must be real files, not LFS pointers — `git clone --local`
  does not resolve LFS, so de-LFS any reference data the eval needs (the
  omilrec repo did exactly this).
- The correctness reference (golden output, fixture, baseline-of-record)
  lives in the repo under a **frozen** path. The executor must never
  regenerate or touch it. (tiny_algo_opt's is the hardcoded `BASELINE` table
  in `scripts/check_drift.py`; omilrec's is a reference ROOT file.)

### 1.3 Editable / read-only discipline

```yaml
safety:
  editable_paths:
    - "src/**/*.cc"      # either lane may ONLY change these (the writable world)
    - "src/**/*.h"
  read_only_paths:
    - "tests/**"         # mounted read-only into both lanes; not editable
    - "scripts/**"
    - "reference/**"
    - "benchmarks/**"
    - "CMakeLists.txt"
```

The mount is hard and deterministic: only `editable_paths` are writable, and
anything not in `editable_paths` or `read_only_paths` is **absent** from the
container entirely. There is no after-the-fact diff gate — the mount IS the
constraint. Put everything neither lane should edit in `read_only_paths` —
tests, references, build files, benchmark and eval scripts, docs. List the full
frozen set so the proposer's view of the tree is complete. A classic onboarding
bug: the eval/benchmark script writes its output (a CSV, a log) into the
worktree, the executor's self-verification run leaves that file modified, and
the harness commits it alongside the real edit — list those output dirs as
read-only or make the script write outside the tree.

### 1.4 The runtime image (mandatory)

The config's `runtime` block is **required** — validation fails without a
readable SIF file. The host only needs Apptainer; Claude Code, bash, git,
compilers, Node.js all live inside the image (Claude authentication is reused
through Apptainer's normal home mount).

- Start from an existing definition: `examples/apptainer.def` is the lean
  runtime (bash/git/python + claude/node — enough for pure-Python tasks like
  tiny_algo_opt); `examples/junosw-apptainer.def` adds gcc/cmake and the JUNO
  system-library chain. For a new target, copy the lean def and add what the
  target's build + eval need.
- Build it: `simpleloop image build path/to/apptainer.def` (default output is
  the adjacent `.sif`, matching a relative `runtime.image`; use `--output`
  and `--force` to manage a shared image).
- `runtime.binds` mounts host directories into the container at unchanged
  absolute paths — use it for large external resources (data trees, `/cvmfs`).
  Each entry must be an existing absolute directory. The run directory itself
  is mounted read/write automatically; small fixtures should just live in the
  source repo instead.
- Because eval runs with `--cleanenv` in a non-login shell, the eval command
  must source its own environment (e.g. a `setup.sh`) — do not rely on the
  user's host environment or login-shell profile.

---

## Part 2 — Author the config + the eval script

### 2.1 The task config

Current schema (see `simpleloop/config.py` for the strict validator — unknown
keys at any level fail at load, not silently mid-run):

```yaml
kind: task

task:
  goal: >
    <one paragraph: what to optimize, the correctness gates, the allowed
     edits, where the hot code is. The proposer and judger both read this —
     be specific about the objective and the tolerance contract.>

safety:
  editable_paths: ["src/**/*.cc", "src/**/*.h"]   # required, non-empty
  read_only_paths: ["tests/**", "scripts/**", "CMakeLists.txt"]

loop:
  max_rounds: 20                # required
  agent_timeout_seconds: 3600   # optional (default 3600); per claude call
  candidates_per_round: 1       # optional (default 1); >1 fans out per round
  max_workers: 1                # optional (default 1); candidate concurrency
  proposer_recent_rounds: 6     # optional (default 6); history fed to proposer

runtime:                        # REQUIRED — no host fallback
  image: apptainer.sif          # relative to this config file, or absolute
  binds:                        # optional; absolute same-path dir mounts
    - /cvmfs

eval:                           # REQUIRED
  commands:
    - "bash scripts/sl_eval.sh" # run by the harness in the candidate worktree
  metrics:                      # REQUIRED — declares what the harness parses
    objective:
      key: SPEED_MS             # the KEY= line to parse; REPLACE per project
      lower_is_better: true     # required bool
    gates:
      - key: CORRECTNESS        # a KEY=PASS/FAIL line; REPLACE per project
        description: >
          <optional but recommended: what this gate checks and why a FAIL
           vetoes the round — the LLM roles read this>

source:
  path: ../../my-repo           # relative to this config file, or absolute
  baseline_ref: main            # optional, default HEAD
```

**The `eval.metrics` block is required and is where "what gets optimized" is
declared.** SimpleLoop never hardcodes `SPEED_MS` or `CORRECTNESS` — those are
your keys; the next project's objective could be `binary_size_kb` or
`coverage_pct`. Exactly two roles exist:

- `objective` — the thing being optimized: `key` + `lower_is_better`, nothing
  else.
- `gates` — pass/fail keys that veto a candidate. Optional list of
  `{key, description?}`. Give each gate a `description` — it is the only place
  the proposer/judger learn what the gate semantically means.

Do **not** add `noise_floor`, `reps`, or any other metric-role key — the
validator rejects them (intentionally deferred until a real run proves the
need). Diff-only / score-based runs without metrics are **no longer
supported**.

### 2.2 The eval script — emit structured `KEY=VALUE` lines

**This is the contract the whole design rests on.** After each candidate's
commit, the harness runs `eval.commands` in the worktree (inside the SIF) and
scans the combined stdout+stderr for lines of the exact shape:

- `KEY=<value>` at the start of a line (leading whitespace allowed), value =
  token up to the first whitespace. Match the key's case exactly as declared.
- Objective value must parse as a number: `SPEED_MS=843.66  ms/evt (10 events)`
  → 843.66 (the trailing annotation is ignored). A non-numeric value
  (`SPEED_MS=NA` on a crashed round) is treated as unknown — best selection
  skips it, never a placeholder.
- Gate values are normalized: `PASS`/`ok`/`true`/`1`/`yes`/`success` → pass;
  `FAIL`/`false`/`0`/`no`/`error`, or any token containing `fail`
  (`build_fail`, `correctness_fail`) → fail; `NA`/empty/unrecognized → unknown,
  which counts as **not passed**. Avoid bespoke tokens like
  `CORRECTNESS=good` — that reads as unknown and the candidate is never
  best-eligible.
- A key absent from the output stays absent from the metrics dict —
  downstream shows "unknown", never a default.

Two equally valid ways to produce the lines:

**(a) Shell-level, no script changes** — wrap existing commands in the config
(tiny_algo_opt does exactly this):

```yaml
eval:
  commands:
    - "PYTHONPATH=. python -m pytest tests/ -q && echo CORRECTNESS=PASS || echo CORRECTNESS=FAIL"
    - "PYTHONPATH=. python scripts/check_drift.py && echo DRIFT=PASS || echo DRIFT=FAIL"
    - "PYTHONPATH=. python scripts/bench.py"     # already prints ms_per_call=0.1234
```

**(b) A single self-contained eval script** — for builds and multi-stage
pipelines, write one `scripts/sl_eval.sh` that sources the environment,
builds, runs the gate(s), runs the benchmark, and prints all the lines at the
end (the omilrec `sl_eval.sh` shape):

```
CORRECTNESS=PASS
SPEED_MS=843.66630  ms/evt (10 events)
EVAL_RESULT=ok
```

On the failure paths, still print the lines: on build failure print
`<GATE_KEY>=build_fail` and exit non-zero; on a failed gate print
`<GATE_KEY>=FAIL`. Every declared key needs a line on **both** the pass path
and the fail path.

Hard constraints to design around:

- **Each eval command has a fixed 600-second timeout** (not configurable).
  Size the workload to fit — fewer events, a smaller benchmark n — or split
  into multiple commands (the harness concatenates their output before
  parsing). Remember each candidate starts from a **fresh worktree**: no build
  cache, so the 600s includes a cold build unless the script sets up its own
  external cache (e.g. ccache in a bound directory).
- The eval script itself must live under a **frozen** path, and any files it
  writes into the worktree will show up in the executor's diff — write
  outputs outside the tree or freeze their directory.
- The script must be self-sufficient inside `--cleanenv`: source its own
  environment explicitly.

### 2.3 The failure modes this contract prevents — and the one it aborts on

**Baseline acceptance (new):** before the first proposer call, the harness
runs the eval once on `baseline_ref`. If the eval command exits non-zero, the
objective is missing/non-finite, or any gate does not pass, the run **aborts**
with a `BaselineAcceptanceError` — no tokens spent. This catches a broken eval
contract early, but only on the pass path; you must still hand-check the fail
path (deliberately break something and confirm the `FAIL` line prints).

**Metric hallucination:** if a candidate's eval stops printing the declared
keys mid-run (e.g. only on some code path), the metrics dict comes back
partial, FACTS go unknown, and the judger has nothing solid to cite. The fix
is always in the eval script, not in SimpleLoop.

Checklist before launch:

- Every key in `eval.metrics` (objective + every gate) has a matching `KEY=`
  line in the eval output, on both PASS and FAIL paths.
- Objective is a bare number as the first token after `=` — not
  `SPEED_MS: 843.66`, not `speed was 843.66`.
- Gate tokens are from the recognized set (`PASS`/`FAIL`/`ok`/`*_fail`/…).
- The full eval chain finishes well under 600s per command from a cold
  worktree, inside the SIF.

---

## Part 3 — Validate, launch, and sanity-check round 0

### 3.1 Validate, then launch

```bash
# 1. static validation — schema, paths, SIF existence; fails fast and clearly
simpleloop validate --config path/to/task.yaml

# 2. run
simpleloop run --config path/to/task.yaml --run-dir runs/001
```

Two extra run modes:

- `--proposals batch.yaml` — a YAML/JSON list of direction strings; skips the
  claude proposer, round i uses proposals[i] (controlled-experiment mode; runs
  len(proposals) rounds, ignoring `max_rounds`).
- `--continue` — resume an existing run-dir. Rounds already in
  `history.jsonl` are skipped and the commit chain resumes;
  `loop.max_rounds` becomes the target TOTAL round count, so bump it in the
  config before continuing.

### 3.2 Sanity-check round 0 — do not skip

The baseline acceptance check already guarantees the baseline eval passed and
the objective parsed. After round 0 lands, read its record in
`<run-dir>/history.jsonl` (one JSON object per round; per-candidate details
under `candidates[]`, the selected one mirrored at top level) and confirm:

1. **`metrics` is populated for the candidate** — objective a float, each gate
   `true`/`false`. If keys are missing on a candidate that took a different
   code path than baseline (crash, gate fail), check the eval's fail path
   prints its lines; fix the eval script, then `--continue`.
2. **The judger's feedback cites the harness-computed deltas** (the FACTS
   block: `THIS round: … | Delta vs prior: … | Delta vs baseline: …`), not
   invented numbers. A number in the feedback that isn't in the metrics means
   the FACTS block was empty — back to problem 1.
3. **Selection behaves**: `selected_candidate`/`accepted` reflect gates + risk
   + objective, not the judger's score. A gate-fail candidate must never be
   selected.

### 3.3 What a run produces, and how to inspect it

- `history.jsonl` — the append-only ground truth: per round `proposal`,
  `sha`, `metrics`, `risk`, `feedback`, `changed_paths`, `candidates[]`,
  `telemetry`.
- `insights.jsonl` — the run's Search Memory (durable cross-round insights fed
  back to the proposer).
- `telemetry.json`, `progress.png` — multi-axis progress, refreshed each
  round. For the nine per-panel detail images run:
  `simpleloop plot --config task.yaml --run-dir runs/001`
- The per-run repo clone — trace any accepted commit with
  `git -C runs/001/repo log --oneline`.
- Inspect one historical candidate in full:
  `simpleloop memory show r3c0 --run-dir runs/001`

If anything looks wrong, the fix is almost always in the eval script or the
config (Part 2), not in SimpleLoop — the harness is deterministic; the
contract is what varies per project.

---

## When to use each part

- **Part 1 + 2** — when onboarding a new target project. Do them in order;
  1.1 (the contract) is the part that fails silently if skipped.
- **Part 3** — every launch, including re-runs. `validate` is cheap; the
  round-0 check catches a fail-path contract break the baseline check can't.
- If the user already has a working task and just wants to adjust gates,
  fan-out (`candidates_per_round`), or the eval contract, jump to Part 2 and
  re-run `simpleloop validate` before continuing the run.
