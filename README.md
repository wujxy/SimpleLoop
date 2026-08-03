# SimpleLoop

SimpleLoop is an open AI Researcher loop with deterministic experiment
authority:

```text
Goal → Researcher → Executor → path gate/commit → Harness eval/gates
     → factual SHA/metrics/history → next Researcher call
```

The Researcher owns investigation, interpretation, and the choice of what to
try. It may propose a local edit, a large refactor, or a replacement
implementation; the current code is a starting artifact, not a prescribed
solution shape. The Executor implements one proposal. The Harness alone owns
commits, evaluation, facts, admission, and objective-based selection.

Research freedom therefore does not weaken evidence: the Researcher can decide
what to investigate, but cannot decide what is true.

## Install and run

```bash
pip install -e .
export HEPAI_API_KEY='<your-key>'
simpleloop init --config examples/task.yaml
simpleloop run --config examples/task.yaml --run-dir ./runs/001
```

The host needs Apptainer. The Proposer model is called through HEPAI from the
frontend; Claude Code (the Executor), research commands, and task dependencies
run inside the configured image. Each run clones the source repository into its
own run directory; the source repository remains untouched.

Useful commands:

```bash
simpleloop validate --config examples/task.yaml
simpleloop plot --config examples/task.yaml --run-dir ./runs/001
python scripts/plot_details.py --config examples/task.yaml --run-dir ./runs/001
simpleloop memory show r3c0 --run-dir ./runs/001
```

`simpleloop plot` writes a 2×3 overview. The detail script writes six
single-panel images. `--continue` resumes a run up to the configured total
round count. `--proposals batch.yaml` replaces Researcher generation with a
fixed proposal batch for controlled experiments.

## Minimal configuration

```yaml
kind: task
task:
  goal: >
    Minimize SPEED_MS while satisfying every configured gate. The current
    implementation is the starting artifact, not a constraint on a solution.
safety:
  editable_paths: ["src/**", "CMakeLists.txt"]
  frozen_paths: ["tests/**", "scripts/**", "references/**"]
loop:
  max_rounds: 10
  candidates_per_round: 1
  max_workers: 1
roles:
  researcher:
    api: hepai
    model: gpt-5.5
    base_url: https://aiapi.ihep.ac.cn/apiv2
    max_steps: 50
    command_timeout_seconds: 120
    command_output_cap_chars: 12000
  executor:
    api: anthropic
    model: glm-5
    base_url: https://open.bigmodel.cn/api/anthropic
runtime:
  image: /path/to/runtime.sif
  binds: []
eval:
  commands:
    - "python -m pytest -q tests/ && echo CORRECTNESS=PASS || echo CORRECTNESS=FAIL"
    - "python scripts/bench.py"
  metrics:
    objective:
      key: SPEED_MS
      lower_is_better: true
    gates:
      - key: CORRECTNESS
        description: "The frozen correctness suite passes."
source:
  path: /path/to/repo
  baseline_ref: HEAD
```

The Goal states the outcome, not a menu of implementation techniques.
`editable_paths` defines the artifact surface the Researcher may redesign;
tests, evaluators, references, and thresholds normally remain frozen.

## Scientific Proposer runtime

The macro loop remains Proposer → Executor → Harness. Within its turn, the
Proposer may inspect the accepted code, Git diffs, recent full experiment facts,
all compact Insights, and older factual episodes on demand. It may propose a
small edit, broad refactor, or replacement implementation, but it cannot modify
a candidate, invoke the Executor, run the authoritative evaluation, or decide
whether a claim is true. Research Bash is read-only, offline, time/output
bounded, and uses temporary scratch space.

An Insight is one short Proposer-written hypothesis/index per completed round;
it points back to factual candidate records and never overrides Harness facts.
The Executor implements proposals. The Harness alone checks changed paths,
commits valid edits, runs evaluation and physical/correctness gates, and decides
eligibility and the next parent. Static `--proposals` mode bypasses HEPAI and
Insight generation entirely.

## Gates and selection

Every attempt is recorded, including failed and rejected attempts. Gate results
are applied in one ordered pipeline after implementation:

1. `PATHS` checks changed paths before commit.
2. The Harness commits path-valid work and runs every eval command.
3. `EVAL_COMMANDS` and configured physical/correctness gates are normalized.
4. Only `gate_passed: true` attempts with a numeric objective are `eligible`.
5. The best eligible objective wins; candidate id is the deterministic tie
   breaker. A candidate must improve the incumbent objective to advance the
   single-parent chain.

`selected` records the round winner. Rejected attempts keep their SHA when a
commit exists, metrics, changed paths, gate details, and failure state in
`history.jsonl`, but never become a parent candidate. The Researcher receives
that factual history and can inspect the repository and diffs itself.

## Trust boundary

| Area | Responsibility |
| --- | --- |
| `roles/proposer.py` | factual context → open experiment proposals |
| `roles/executor.py` | one proposal → complete implementation → SHA |
| `candidate_worker.py` | candidate execution and factual result assembly |
| `harness/gate.py` | path, eval-command, and configured gate normalization |
| `harness/evals.py` | evaluator execution and `KEY=VALUE` metric parsing |
| `harness/store.py` | factual JSONL history and objective selection |
| `harness/workspace.py` | isolated worktrees and Harness-owned commits |
| `reporting/plot.py` | 2×3 factual progress overview |

The path gate is an early safety stage, not a competing acceptance system.
Final admission is the conjunction of all Harness gates.

## Development-time prompt self-improvement

```yaml
self_improvement:
  interval_rounds: 10
```

This keeps the outer loop: it pauses the artifact loop between fixed segments,
lets a Meta Optimizer edit `proposer.md`, `executor.md`, and the evolvable part
of `meta_optimizer.md`, validates the prompt set, snapshots accepted changes,
then continues. Each run has its own v000 lineage. Historical four-prompt
snapshots remain auditable; restored active sets are migrated to the current
three-prompt system. Harness, Goal, Gates, evaluator facts, and task source are
not prompt-evolution targets in this version.
