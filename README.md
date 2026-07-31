# SimpleLoop

A minimal LLM optimization loop: **proposer → executor → judger**, one
generation per round, serial single-parent commit chain, no early stop.

```text
user goal → proposer (reads history + insights, proposes N candidate directions)
          → executor (edits code in a worktree, delivers a commit SHA)   ┐ per
          → harness (computes diff + runs eval + parses metrics)         │ candidate
          → judger (grades the diff + metrics, gives feedback + score)   ┘
          → harness selects the round's winner (gates + risk + objective)
          → feedback + insights feed the next round's proposer
```

By default each round has one candidate (`candidates_per_round: 1`), which is
the classic serial loop; raising it fans out candidates within a round (up to
`max_workers` concurrently), while the accepted chain stays single-parent.

## Why

Bigger optimization frameworks (Pareto frontiers, candidate pools, score matrices,
minibatch eval) are powerful but heavy. SimpleLoop keeps only what a single-chain
loop needs: three LLM roles, one deterministic gate, harness-owned commits,
eval and metric parsing, and one JSONL history.

## Install

```bash
pip install -e .
```

The host needs Apptainer. Claude Code, Bash, Git, GCC/G++, Make, CMake, and
Node.js live inside the configured SIF. Claude authentication is reused through
Apptainer's normal home mount.

## Run

```bash
simpleloop init --config examples/task.yaml
simpleloop run --config examples/task.yaml --run-dir ./runs/001
```

First replace the placeholder `source.path` in the generic
`examples/task.yaml`. `simpleloop init` creates a Git baseline when the source
directory is not already a repository, builds a missing Apptainer image, and
reuses an existing image after preflight. Generated `*.sif` files are ignored
by Git. `simpleloop image build` remains available for manual image management.

Each run:

- clones your source repo locally into `runs/001/repo` (`git clone --local`,
  source repo untouched),
- preflights one mandatory Apptainer runtime and validates the baseline before
  any optimization role consumes tokens,
- runs `max_rounds` rounds, each candidate in a fresh worktree,
- writes `history.jsonl`, `insights.jsonl`, `telemetry.json`, and the 3×3
  `progress.png` overview (refreshed after every round).

Offline replotting: `simpleloop plot` redraws the 3×3 overview from the
persisted run artifacts; the nine single-panel detail images are drawn by an
external script so a live run never spends time on them:

```bash
simpleloop plot --config examples/task.yaml --run-dir ./runs/001
python scripts/plot_details.py --config examples/task.yaml --run-dir ./runs/001
```

Trace any run's commits with `git -C runs/001/repo log --oneline`. Inspect one
historical candidate by reference with `simpleloop memory show r3c0 --run-dir
runs/001`.

Two extra run modes:

- `--proposals batch.yaml` — skip the claude proposer and drive the loop from a
  fixed list of direction strings (controlled-experiment mode; acceptance is by
  hard gates alone).
- `--continue` — resume an existing run-dir; `loop.max_rounds` becomes the
  target TOTAL round count, so bump it in the config before continuing.

## Config

```yaml
kind: task
task:
  goal: "Optimize the speed of the algorithm under src/..."
safety:
  editable_paths: ["src/**/*.cc", "src/**/*.h"]
  frozen_paths:  ["tests/**", "scripts/**", "CMakeLists.txt"]
loop:
  max_rounds: 4
  candidates_per_round: 1     # optional; >1 fans out candidates per round
  max_workers: 1              # optional; candidate concurrency within a round
  agent_timeout_seconds: 3600 # optional; per claude call budget
  proposer_recent_rounds: 6   # optional; rounds of history fed to the proposer
runtime:
  image: /path/to/simpleloop-runtime.sif
  definition: /path/to/simpleloop-runtime.def  # optional; inferred from image
  binds:                      # optional; absolute same-path directory mounts
    - /cvmfs
    - /data/juno
eval:                       # required
  commands:                 # required non-empty; harness-run after each commit
    - "python -m pytest -q tests/"
  metrics:                  # required; declares the key=value lines the
    objective:              # harness parses out of eval output
      key: SPEED_MS
      lower_is_better: true
    gates:
      - key: CORRECTNESS
        description: "bit-exact against the reference output"
source:
  path: /path/to/your/repo
  baseline_ref: HEAD
```

All three Claude roles and `eval.commands` run inside the same SIF with
`apptainer exec --cleanenv`. The complete run directory is mounted read/write
automatically; large external resources are mounted from `runtime.binds` at
unchanged absolute paths. Git clone/worktree/gating/history logic remains on
the host. There is no host-execution fallback.

`simpleloop init` stages all files when it creates the baseline commit, so set
the source repository's `.gitignore` before initializing it.

`eval.commands` are run by the **harness** (deterministic) after the commit, not
by the judger agent. The harness also parses the objective/gate `KEY=VALUE`
lines (declared in the required `eval.metrics` block) out of eval output and
computes the deltas the judger cites — the judger never extracts numbers from
prose (a known hallucination vector). Best selection is harness-owned too:
among gate-pass, risk-not-high candidates, take the best objective; the
judger's 0-1 score demotes to a quality signal (there is deliberately no
score-based selection mode). The baseline eval must succeed, expose its
objective, and pass every gate before the first proposer starts.

## Design

The package is split by trust boundary: `roles/` holds the LLM roles (they
think, they never own ground truth), `harness/` holds the deterministic
machinery an LLM must not be trusted with, `container/` is the Apptainer
boundary, `reporting/` is observability.

| module | role |
| --- | --- |
| `loop.py` | main loop: RunContext, generations, winner selection, chaining |
| `config.py` | read+validate task config |
| `cli.py` | entry point (`run` / `validate` / `plot` / `image build` / `memory show`) |
| `roles/agent.py` | containerized `claude -p` wrapper (timeout, JSON/no-JSON modes) |
| `roles/proposer.py` | goal+history+insights → N candidate directions (+ reflection) |
| `roles/executor.py` | proposal → agent edits → gate → harness commit → SHA |
| `roles/judger.py` | diff+metrics → `{"score","risk","feedback",...}` |
| `harness/evals.py` | harness-owned eval execution + `KEY=VALUE` metric parsing |
| `harness/gate.py` | pre-commit frozen/editable diff check (deterministic) |
| `harness/workspace.py` | repo clone, worktree, harness commit, diff |
| `harness/store.py` | history JSONL + harness-owned best selection |
| `harness/memory.py` | `r<round>c<candidate>` episode refs + insights JSONL |
| `harness/views.py` | per-role history projections (what the proposer may see) |
| `container/runtime.py` | mandatory Apptainer argv, environment policy, binds, preflight |
| `container/image.py` | `apptainer build --fakeroot` shortcut |
| `reporting/telemetry.py` | baseline, active worktime, and processed-token state |
| `reporting/plot.py` | 3×3 overview (loop-refreshed; details via `scripts/plot_details.py`) |

**Commit chain:** rounds link — round 0 forks `baseline_ref`, round n forks the
last accepted round's SHA (a round with no accepted winner leaves the chain in
place; the rejected candidates remain in history).

**Run isolation:** each run is its own cloned repo, so an agent rummaging in git
only ever sees its own run's chain. Apptainer additionally isolates the
software environment from an activated host virtualenv or JUNO shell. Normal
home/network access remains enabled, so this is runtime isolation rather than a
complete security sandbox.

## Development-time self-improvement

The outer loop currently evolves prompts only. Add the block to run the
artifact loop in fixed segments and invoke a Meta Optimizer between segments:

```yaml
self_improvement:
  interval_rounds: 10
```

Each new run creates an independent identity-internalized v000 prompt set
under `run_dir/self_improvement/`. Accepted edits create v001, v002, and so
on within that run; `--continue` resumes only that lineage. The artifact loop
is stopped while the Meta Optimizer runs, and no optimizer is called after the
final artifact segment because there are no later rounds to evaluate its
prompts. Static `--proposals` mode stays separate from self-improvement.
