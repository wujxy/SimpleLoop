# SimpleLoop

A minimal LLM optimization loop: **proposer → executor → judger**, one
generation per round, serial single-parent commit chain, no early stop.

```
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
simpleloop image build examples/apptainer.def
simpleloop validate --config examples/task.yaml
simpleloop run --config examples/task.yaml --run-dir ./runs/001
```

The generic `examples/task.yaml` still needs a real `source.path`; its build
command creates the adjacent `examples/apptainer.sif`. Generated `*.sif` files
are ignored by Git. To reuse one shared image, set `runtime.image` to that SIF.

Each run:

- clones your source repo locally into `runs/001/repo` (`git clone --local`,
  source repo untouched),
- preflights one mandatory Apptainer runtime and validates the baseline before
  any optimization role consumes tokens,
- runs `max_rounds` rounds, each candidate in a fresh worktree,
- writes `history.jsonl`, `insights.jsonl`, `telemetry.json`, `progress.png`,
  and nine detail progress images.

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
  binds:                      # optional; absolute same-path directory mounts
    - /cvmfs
    - /data/juno
eval:                       # optional; omit -> judger judges on diff alone
  commands:
    - "python -m pytest -q tests/"
  metrics:                  # optional; declares the key=value lines the
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

`eval.commands` are run by the **harness** (deterministic) after the commit, not
by the judger agent. With `eval.metrics` declared, the harness also parses the
objective/gate `KEY=VALUE` lines out of eval output and computes the deltas the
judger cites — the judger never extracts numbers from prose (a known
hallucination vector). Best selection is then harness-owned too: among
gate-pass, risk-not-high candidates, take the best objective; the judger's 0-1
score demotes to a quality signal. A configured baseline eval must succeed,
expose its objective, and pass every gate before the first proposer starts.

## Design

| module | role |
|---|---|
| `loop.py` | main loop: RunContext, generations, winner selection, chaining |
| `runtime.py` | mandatory Apptainer argv, environment policy, binds, preflight |
| `image.py` | `apptainer build --fakeroot` shortcut |
| `agent.py` | containerized `claude -p` wrapper (timeout, JSON/no-JSON modes) |
| `proposer.py` | goal+history+insights → N candidate directions (+ reflection) |
| `executor.py` | proposal → agent edits → gate → harness commit → SHA |
| `judger.py` | diff+metrics → `{"score","risk","feedback",...}` |
| `evals.py` | harness-owned eval execution + `KEY=VALUE` metric parsing |
| `gate.py` | pre-commit frozen/editable diff check (deterministic) |
| `workspace.py` | repo clone, worktree, harness commit, diff |
| `config.py` | read+validate task config |
| `store.py` | history JSONL + harness-owned best selection |
| `memory.py` | `r<round>c<candidate>` episode refs + insights JSONL |
| `views.py` | per-role history projections (what the proposer may see) |
| `telemetry.py` | baseline, active worktime, and processed-token state |
| `plot.py` | 3×3 overview and nine detail progress plots |
| `cli.py` | entry point (`run` / `validate` / `image build` / `memory show`) |

**Commit chain:** rounds link — round 0 forks `baseline_ref`, round n forks the
last accepted round's SHA (a round with no accepted winner leaves the chain in
place; the rejected candidates remain in history).

**Run isolation:** each run is its own cloned repo, so an agent rummaging in git
only ever sees its own run's chain. Apptainer additionally isolates the
software environment from an activated host virtualenv or JUNO shell. Normal
home/network access remains enabled, so this is runtime isolation rather than a
complete security sandbox.
