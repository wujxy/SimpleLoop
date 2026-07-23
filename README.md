# SimpleLoop

A minimal serial LLM optimization loop: **proposer → executor → judger**, one of
each per round, no batch, no parallel, no early stop.

```
user goal → proposer (reads history, proposes a direction)
          → executor (edits code, delivers a commit SHA)
          → harness (computes diff + runs eval commands)
          → judger (grades the diff + eval output, gives feedback + score)
          → feedback feeds the next round's proposer
```

## Why

Bigger optimization frameworks (Pareto frontiers, candidate pools, score matrices,
minibatch eval) are powerful but heavy. SimpleLoop keeps only what a single-chain
serial loop needs: three LLM roles, one deterministic gate, harness-owned commits
and eval, and one JSONL history. ~700 lines.

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
- runs `max_rounds` rounds, each in a fresh worktree,
- writes `history.jsonl`, `telemetry.json`, `progress.png`, and nine detail progress images.

Trace any run's commits with `git -C runs/001/repo log --oneline`.

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
runtime:
  image: /path/to/simpleloop-runtime.sif
  binds:                      # optional; absolute same-path directory mounts
    - /cvmfs
    - /data/juno
eval:                       # optional; omit -> judger judges on diff alone
  commands:
    - "python -m pytest -q tests/"
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
by the judger agent. The judger sees `git diff` + the commands' stdout. A
configured baseline eval must succeed, expose its objective, and pass every
gate before the first proposer starts.

## Design

| module | role |
|---|---|
| `loop.py` | serial main loop, scheduling only |
| `runtime.py` | mandatory Apptainer argv, environment policy, binds, preflight |
| `image.py` | `apptainer build --fakeroot` shortcut |
| `agent.py` | containerized `claude -p` wrapper (timeout, JSON/no-JSON modes) |
| `proposer.py` | goal+history → `{"proposal"}` |
| `executor.py` | proposal → agent edits → gate → harness commit → SHA |
| `judger.py` | diff+eval → `{"score","feedback"}` (harness runs eval) |
| `gate.py` | pre-commit frozen/editable diff check (deterministic) |
| `workspace.py` | repo clone, worktree, harness commit, diff, env |
| `config.py` | read+validate task config |
| `store.py` | history JSONL + best-score tracking |
| `telemetry.py` | baseline, active worktime, and processed-token state |
| `plot.py` | 3×3 overview and nine detail progress plots |
| `cli.py` | entry point |

**Commit chain:** rounds link — round 0 forks `baseline_ref`, round n forks
round n-1's SHA (only when a commit was produced; a gate-rejected or empty round
leaves the chain in place).

**Run isolation:** each run is its own cloned repo, so an agent rummaging in git
only ever sees its own run's chain. Apptainer additionally isolates the
software environment from an activated host virtualenv or JUNO shell. Normal
home/network access remains enabled, so this is runtime isolation rather than a
complete security sandbox.
