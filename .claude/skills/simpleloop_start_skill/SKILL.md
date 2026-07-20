---
name: simpleloop_start_skill
description: >-
  Use when a user wants to prepare a new target project for SimpleLoop: assemble
  the source repo + editable/frozen paths + a correctness gate, author the task
  config (including the eval.metrics block that declares the objective and gates),
  rewrite the eval script to emit the structured key=value lines the harness
  parses, or launch the loop and sanity-check the first round. Guides the agent
  through the whole onboarding so the user's project is ready to drive the
  proposer→executor→judger loop with harness-owned metrics (not judger-hallucinated
  numbers).
---

# SimpleLoop Start Skill — prepare a task and run the loop

Print `[skill: simpleloop_start_skill]` before proceeding.

SimpleLoop is a minimal serial optimization loop: a **proposer** invents a
direction, an **executor** implements it in a git worktree, the **harness** runs
the eval commands and parses the structured metrics, and a **judger** grades the
round. One round at a time, no batch, no parallel. The harness owns the numbers
(parsed from eval output + best selection); the judger only interprets — it must
never be the source of a metric, because an LLM asked to extract numbers from
prose and do arithmetic on them hallucinates (a prior run invented a "baseline
874.50" that appears in no ground-truth source and propagated across 12 rounds
of feedback).

This skill guides you (the agent helping the user) through preparing a target
project so the loop runs correctly the first time. Do the parts in order.

- **Part 1 — Assemble the resource pack.** The source repo, the editable/frozen
  discipline, and the correctness reference.
- **Part 2 — Author the config + rewrite the eval script to emit structured
  output.** This is the contract the whole design rests on. Get it wrong and the
  harness can't parse the metrics, the judger falls back to reading prose, and it
  hallucinates numbers — exactly the defect this fixes.
- **Part 3 — Launch and sanity-check round 0.** Confirm the metrics actually
  parsed, or stop and fix the eval before the loop runs on garbage.

SimpleLoop is general-purpose. There are two worked examples in the repo:
`examples/tiny_algo_opt/` (a tiny Python pair-counter — the simplest complete
task) and the OMILREC run under `runs/omilrec-v100-main/` (a real C++ physics
reconstruction). Treat them as *one worked example of the structure*, not as the
structure itself. Every concrete path, command, tolerance, and metric name below
is a **placeholder you must replace for the actual target project.** Never copy
OMILREC-specific values (8358P, `--evtmax 10`, `SPEED_MS`, bit-identical
tolerance) into a different task — those are the target's contract, not
SimpleLoop's.

---

## Part 1 — Assemble the resource pack

Goal of Part 1: a git repo SimpleLoop can `clone --local`, with a clear
editable/frozen split and a correctness reference the gate compares against.

### 1.1 Decide the optimization contract first

Before touching files, pin down with the user, in one short written note:

- **Objective** — what is being improved? (speed, accuracy, memory, binary size,
  a score…). This becomes the `eval.metrics.objective.key` in the config and the
  `KEY=` line the eval script must print.
- **Direction** — is lower better (speed, memory, error) or higher (accuracy,
  coverage)? This is `eval.metrics.objective.lower_is_better`. It is not
  guessable — declare it, or best selection inverts.
- **Correctness gate** — what must not break? (a test suite, a bit-identical
  diff, a tolerance band). This becomes a `eval.metrics.gates` entry and its own
  `KEY=` line. A round that fails a gate is never selected as best, even if its
  objective is best.
- **Editable vs frozen** — which source may the executor change, which files must
  it never touch (tests, references, build config, benchmarks)? The frozen gate
  rejects the whole round if the executor touches a frozen path.

Do not proceed to 1.2 until the user has confirmed these four.

### 1.2 The source repo

SimpleLoop clones the source repo with `git clone --local --no-checkout` into a
per-run bare clone, then makes a worktree per round. Requirements:

- It must be a real git repo (`.git` exists). `source.path` in the config points
  at it (relative to the config file, or absolute).
- `source.baseline_ref` is the starting commit (a branch name or SHA). The
  baseline eval runs once on this commit before round 0, so the judger has a
  "vs baseline" axis. Pick the **unoptimized** starting point — the whole point
  is to measure improvement from here.
- The correctness reference (golden output, fixture, baseline-of-record) lives
  in the repo under a **frozen** path. The executor must never regenerate or
  touch it. (OMILREC's is `reference/ref_10evt.root`; tiny_algo_opt's is the
  hardcoded `BASELINE` in `check_drift.py`.)

### 1.3 Editable / frozen discipline

In the config:

```yaml
safety:
  editable_paths:
    - "src/**/*.cc"      # the executor may only change these
    - "src/**/*.h"
  frozen_paths:
    - "tests/**"         # touching these rejects the round
    - "reference/**"
    - "CMakeLists.txt"
```

The frozen gate is hard: if the executor's diff touches any frozen path, the
round is voided (no commit, no score). Put everything the executor shouldn't
touch here — tests, references, build files, benchmark scripts, docs. The most
common onboarding bug is forgetting to freeze the benchmark script, so the
executor's self-verification run appends a timing row to it and the gate rejects
the real source edit.

---

## Part 2 — Author the config + structured eval output

### 2.1 The task config

Minimal schema (see `simpleloop/config.py` for the strict validator — unknown
keys fail at load, not silently):

```yaml
kind: task

task:
  goal: >
    <one paragraph: what to optimize, the correctness gate, the allowed edits.
     The proposer and judger both read this — be specific about the objective
     and the bit-identical / tolerance contract.>

safety:
  editable_paths: ["src/**/*.cc", "src/**/*.h"]
  frozen_paths: ["tests/**", "reference/**", "CMakeLists.txt"]

loop:
  max_rounds: 20
  agent_timeout_seconds: 3600   # per claude call; large refactors need the room

eval:
  commands:
    - "bash scripts/sl_eval.sh"   # one self-contained command: build + gate + bench
  metrics:
    objective:
      key: SPEED_MS              # the KEY= line to parse; REPLACE with your metric
      lower_is_better: true
    gates:
      - key: CORRECTNESS         # a KEY=PASS/FAIL line; REPLACE with your gate

source:
  path: ../../my-repo
  baseline_ref: main
```

**The `eval.metrics` block is where "what gets optimized" is declared.**
SimpleLoop never hardcodes `SPEED_MS` or `CORRECTNESS` — those are your keys.
The next user's objective could be `binary_size_kb` or `coverage_pct`; the
harness parses whatever keys the config declares. Only two roles exist:

- `objective` — the thing being optimized. Required: `key` + `lower_is_better`.
- `gates` — pass/fail keys that veto a round. Optional list of `{key}`.

Do **not** add `noise_floor`, `reps`, or any other metric-role key — they are
rejected by the validator. They were intentionally deferred until a real run
proves they're needed; adding them speculatively bloats the schema for an
unvalidated requirement.

Omitting `eval.metrics` entirely is legal — it drops to a diff-only judger with
best-by-score (the legacy behavior). But then you get no harness-owned numbers
and the judger will hallucinate them. **For any perf task, always declare
metrics.**

### 2.2 Rewrite the eval script to emit structured `KEY=VALUE` lines

**This is the contract the whole design rests on.** The harness parses eval
output for lines of the exact shape `KEY=<value>` (key at line start, value up to
the first whitespace). For every key you declared in `eval.metrics`, your eval
script must print such a line. The harness parses them into a metrics dict; the
judger cites those numbers and is forbidden from introducing any not listed.

The OMILREC eval (`scripts/sl_eval.sh`) is the reference shape — it prints, at
the end:

```
CORRECTNESS=PASS
SPEED_MS=843.66630  ms/evt (10 events)
EVAL_RESULT=ok
```

The harness reads `CORRECTNESS=PASS` (gate → True), `SPEED_MS=843.66630`
(objective → 843.66630 as float; the trailing `ms/evt (10 events)` is ignored
because parsing stops at the first whitespace after the value), and
`EVAL_RESULT=ok` (gate → True). Gate values are normalized: `PASS`/`ok`/`1`/
`true`/`success` → pass; `FAIL`/`failed`/`*_fail`/`0`/`false` → fail; `NA`/
empty/unrecognized → unknown (treated as not-passed). A non-numeric objective
value (e.g. `SPEED_MS=NA` on a crashed round) is omitted — the FACTS block shows
"unknown" and best selection skips it, never a hallucinated placeholder.

**Your eval script must do the equivalent for your keys.** Concretely:

1. Run the build (if any). On failure, print `<GATE_KEY>=build_fail` and exit
   non-zero — the harness reads the gate as failed.
2. Run the correctness gate. Print `<GATE_KEY>=PASS` or `<GATE_KEY>=FAIL`.
3. Run the benchmark / measurement. Print `<OBJECTIVE_KEY>=<number>` with the
   number as the first whitespace-delimited token after `=`. Trailing units /
   annotations are fine (`SPEED_MS=843.66  ms/evt (10 events)`) — they're
   ignored.
4. Print an overall result line if useful (`EVAL_RESULT=ok`), declared as a
   second gate if you want it to veto.

If the eval currently prints prose like `drift OK: 12 cases match` or
`ms_per_call=0.1234 (n=200)` (the tiny_algo_opt scripts do exactly this), you
must **add** the bare `KEY=VALUE` lines. You can keep the prose for humans; just
ensure the parseable lines are present. Example transform for tiny_algo_opt's
`check_drift.py`:

```
# before (prose only — harness can't parse):
drift OK: 12 cases match baseline

# after (add a parseable gate line; keep the prose):
DRIFT=PASS
drift OK: 12 cases match baseline
```

and for `bench.py`:

```
# before:
ms_per_call=0.1234  (n=200, radius=5)

# after (the objective key must match eval.metrics.objective.key):
MS_PER_CALL=0.1234
ms_per_call=0.1234  (n=200, radius=5)
```

then declare in the config:

```yaml
eval:
  commands:
    - "PYTHONPATH=. python -m pytest tests/ -q && PYTHONPATH=. python scripts/check_drift.py && PYTHONPATH=. python scripts/bench.py"
  metrics:
    objective:
      key: MS_PER_CALL
      lower_is_better: true
    gates:
      - key: DRIFT
```

(Combining the three commands into one `&&`-chain is fine — the harness parses
all `KEY=` lines from the combined stdout. Or list them separately; the harness
concatenates.)

### 2.3 The failure mode this contract exists to prevent — make it loud

**If your eval does not print the declared keys, the harness cannot parse them.
The metrics dict comes back empty. The judger falls back to reading the raw
prose, extracts numbers itself, does arithmetic itself — and hallucinates.** This
is not a hypothetical: a prior OMILRECV2 run had the judger invent a "baseline
874.50 ms/evt" that appears in no ground-truth source (the real baseline was
945.54, visible in the quick_bench `compare vs` line the judger was supposed to
read but misread), and that invented number propagated across all 12 rounds of
feedback, making every "vs baseline" delta wrong.

There is **no dry-run enforcement** of this contract (intentionally — the skill
is advisory). Nothing fails the launch if your eval script is wrong. So:

- Double-check that every key in `eval.metrics` (objective + every gate) has a
  matching `KEY=` line in the eval output, for both the PASS path and the FAIL
  path (a crashed build, a failed gate).
- The objective value must be a bare number as the first token after `=`. Not
  `SPEED_MS: 843.66`, not `speed was 843.66` — `SPEED_MS=843.66`.
- Gate values must be recognizable tokens: `PASS`/`FAIL`/`ok`/`*_fail`/etc.
  Avoid bespoke tokens like `CORRECTNESS=good` — the harness reads `good` as
  unknown (not-passed), and the round is never best-eligible.

---

## Part 3 — Launch and sanity-check round 0

### 3.1 Launch

From the SimpleLoop repo root:

```bash
python -m simpleloop <path-to-task.yaml> <run-dir>
# or, with a fixed proposal batch (skips the claude proposer):
python -m simpleloop <path-to-task.yaml> <run-dir> --proposals <proposals.yaml>
```

The run dir holds `history.jsonl` (one record per round: proposal, sha, score,
risk, feedback, eval_block, **metrics**), `final_report.md`, and the per-run
repo clone + worktrees.

### 3.2 Sanity-check round 0 — do not skip

After round 0 lands, read its record in `history.jsonl` and confirm:

1. **`metrics` is populated.** The objective key and every gate key are present
   with the right types (objective a float, gates `true`/`false`). If `metrics`
   is `{}` or keys are missing, **stop** — the eval script isn't emitting the
   declared `KEY=` lines. Fix the eval (Part 2.2) and re-run; do not let the
   loop continue on hallucinated numbers.
2. **The judger's feedback cites the harness-computed deltas**, not invented
   numbers. The judger prompt now receives a FACTS block the harness built
   (`THIS round: SPEED_MS = ... | CORRECTNESS = PASS`, `Delta vs prior: ...`,
   `Delta vs baseline: ...`). The feedback should reference those exact numbers.
   If the feedback names a number that isn't in the FACTS block, the judger is
   hallucinating — which means the FACTS block was empty (metrics didn't parse),
   and you're back to problem 1.
3. **`best` is selected by the objective among gate-pass + risk-not-high
   rounds**, not by judger score. The final report prints both the metric-best
   and the highest-score round; if they differ, that's the design working as
   intended (the fastest safe commit wins over the highest-scoring risky one).

If any of these fail, the fix is almost always in the eval script (Part 2),
not in SimpleLoop. The harness is deterministic; the contract is what varies
per project.

### 3.3 What each run produces

- `history.jsonl` — the append-only record. `metrics` per round is the
  harness-parsed ground truth; `eval_block` is the raw text (kept for the record
  and for the judger to verify a specific claim, but not the source of numbers).
- `final_report.md` — human summary: the metric-selected best, the highest-score
  round (and the divergence if they differ), per-round metrics + risk + feedback.
- The per-run repo (bare clone) + worktrees — for tracing any round's commit.

---

## When to use each part

- **Part 1 + 2** — when onboarding a new target project. Do them in order; 1.1
  (the contract) is the part that fails silently if skipped.
- **Part 3** — every launch, including re-runs. The round-0 sanity check catches
  an eval contract break before it corrupts a long run.
- If the user already has a working task and just wants to add `eval.metrics` to
  an existing config + rewrite their eval to emit `KEY=` lines, skip to Part 2.