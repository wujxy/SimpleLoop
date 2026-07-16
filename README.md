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

Claude Code (`claude`) must be installed and authenticated — the three roles run
as `claude -p` subprocesses.

## Run

```bash
simpleloop validate --config examples/task.yaml
simpleloop run --config examples/task.yaml --run-dir ./runs/001
```

Each run:
- clones your source repo locally into `runs/001/repo` (`git clone --local`,
  source repo untouched),
- runs `max_rounds` rounds, each in a fresh worktree,
- writes `history.jsonl` and `final_report.md`.

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
eval:                       # optional; omit -> judger judges on diff alone
  commands:
    - "python -m pytest -q tests/"
source:
  path: /path/to/your/repo
  baseline_ref: HEAD
```

`eval.commands` are run by the **harness** (deterministic) after the commit, not
by the judger agent. The judger sees `git diff` + the commands' stdout.

## Design

| module | role |
|---|---|
| `loop.py` | serial main loop, scheduling only |
| `agent.py` | `claude -p` subprocess wrapper (timeout, JSON/no-JSON modes) |
| `proposer.py` | goal+history → `{"proposal"}` |
| `executor.py` | proposal → agent edits → gate → harness commit → SHA |
| `judger.py` | diff+eval → `{"score","feedback"}` (harness runs eval) |
| `gate.py` | pre-commit frozen/editable diff check (deterministic) |
| `workspace.py` | repo clone, worktree, harness commit, diff, env |
| `config.py` | read+validate task config |
| `store.py` | history JSONL + best-score tracking |
| `cli.py` | entry point |

**Commit chain:** rounds link — round 0 forks `baseline_ref`, round n forks
round n-1's SHA (only when a commit was produced; a gate-rejected or empty round
leaves the chain in place).

**Run isolation:** each run is its own cloned repo, so an agent rummaging in git
only ever sees its own run's chain.
