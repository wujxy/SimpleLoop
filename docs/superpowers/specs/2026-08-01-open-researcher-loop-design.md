# Open Researcher Loop Design

**Date:** 2026-08-01

**Status:** Approved conversational design, written specification pending final review

## 1. Purpose

Simplify SimpleLoop around one scientific decision-maker. The Proposer becomes
the Researcher, the Executor remains its implementation worker, and the Harness
owns commits, evaluation, gates, objective selection, persistence, and recovery.

The resulting loop is:

```text
Researcher
    -> Proposal
Executor
    -> worktree changes
Harness
    -> changed-path gate
    -> commit
    -> evaluation
    -> evaluation/physics gates
    -> objective selection
    -> factual experiment result
Researcher
    -> autonomous investigation and next Proposal
```

The current artifact and historical experiments are evidence and starting
points. They do not constrain the form, scale, or algorithmic character of a
future solution. The Goal, immutable evaluator, declared Gates, editable
artifact boundary, and resource budget are the constraints.

## 2. Design Principles

1. **One scientific owner.** The Researcher investigates evidence, interprets
   results, and decides what to try next.
2. **No interpretive middleman.** The Harness returns facts rather than a
   Judger-authored narrative.
3. **Open solution space.** Any implementation strategy is admissible within
   the editable artifact, including large refactors, replacement algorithms,
   new source files, and production build integration.
4. **Closed factual authority.** The Researcher cannot define whether a Gate
   passed or what metric was measured. The Harness derives those facts from
   immutable evaluation.
5. **All attempts are evidence.** Successful, rejected, failed, regressing, and
   non-selected candidates are persisted.
6. **Staged execution, unified admission.** Safety gates may short-circuit work
   before evaluation, but the persisted result exposes one complete gate view.
7. **MVP subtraction.** Do not replace the Judger with another reviewer,
   planner, critic, risk model, or semantic scoring layer.

## 3. Scope

### 3.1 Included

- Remove the Judger role and every runtime dependency on it.
- Reduce the Proposer output contract to a batch of free-text proposals.
- Remove the mandatory reflection/insight/continue-switch protocol.
- Return factual candidate results directly to the next Proposer call.
- Introduce explicit staged gate results covering changed paths, eval command
  completion, and configured physics/correctness gates.
- Persist every business-terminal candidate attempt.
- Base normal candidate selection only on deterministic gates and the declared
  objective.
- Simplify task prompts/configuration so they describe outcomes and boundaries,
  not recommended research techniques.
- Update prompt self-improvement to evolve only Researcher, Executor, and Meta
  Optimizer prompts.
- Preserve local and HEPJob execution, parallel candidates, resumption,
  telemetry, plotting, export, and static-proposal mode.

### 3.2 Excluded

- Harness evolution or agent-authored Gates.
- A persistent or recursively self-invoking Researcher session.
- New planner, reviewer, critic, or semantic-memory agents.
- Multi-objective or Pareto selection.
- Hidden-evaluation infrastructure changes.
- A new filesystem sandbox or role-specific Apptainer mount policy.
- General restructuring of the existing large `loop.py` outside code directly
  affected by removing the Judger and changing candidate records.

The existing Proposer tool restriction (`Read,Bash`, no `Edit`/`Write`) remains
for this MVP. Hard read-only filesystem isolation is a separate security task.

## 4. Role Boundaries

### 4.1 Researcher (renamed responsibility, existing Proposer module)

The existing `simpleloop.roles.proposer` module remains the implementation
location to minimize churn. Its semantic identity changes from direction
generator to Researcher.

The Researcher may:

- inspect the accepted source and any persisted candidate SHA;
- inspect history, diffs, metrics, gate results, and evaluation logs;
- select any implementation strategy and any modification scale;
- revisit, replace, or discard earlier approaches;
- give the Executor as much or as little implementation detail as it considers
  useful.

The Harness does not require the Researcher to run particular commands, follow
a reflection sequence, classify a proposal as continue/switch, maintain a
family taxonomy, or produce semantic insights.

The structured response is:

```json
{
  "proposals": [
    "free-text instruction for one Executor"
  ]
}
```

The array length remains exactly `candidates_per_round`. This is a user-owned
resource/scheduling decision rather than a prescribed research method. The
Harness assigns candidate identifiers; proposals do not carry agent-authored
family or decision labels.

### 4.2 Executor

The Executor receives the Goal, one Proposal, declared Gate descriptions, and
the editable/frozen boundary. It owns implementation investigation and source
editing inside its isolated worktree. It does not choose the research direction,
commit code, declare Gate results, or select the next parent.

The Proposal is an instruction from the Researcher, not a schema-enforced
"direction" of a prescribed abstraction level. The Executor may implement a
small change or a broad replacement according to that instruction and the
available resource budget.

### 4.3 Harness

The Harness owns:

- changed-path discovery and path-boundary enforcement;
- candidate commits and SHA creation;
- evaluation process execution and return codes;
- metric parsing;
- configured correctness/physics Gate results;
- eligibility and objective selection;
- history, telemetry, resumption, and artifact provenance.

The Harness never writes a qualitative assessment of an experiment.

## 5. Candidate Pipeline and Gates

### 5.1 Execution order

```text
Executor edits worktree
    -> collect changed paths
    -> PATHS gate
        FAIL: persist rejection; do not commit or evaluate
        PASS:
            -> Harness commit and SHA
            -> Harness evaluation
            -> EVAL_COMMANDS gate
            -> configured Gates (FCN, CONSISTENCY, ...)
            -> persist unified result
            -> deterministic objective selection
```

The PATHS gate runs before evaluation because evaluating a candidate that
changed an evaluator, reference, or frozen script would destroy the trust
boundary. Gate reporting is unified even though gate execution is staged.

### 5.2 Normalized gate view

Every candidate record contains a `gates` mapping. Each entry uses:

```json
{
  "passed": true,
  "detail": "optional factual detail"
}
```

The Harness always supplies:

- `PATHS`: whether all changed paths are editable and not frozen;
- `EVAL_COMMANDS`: whether every configured evaluation command returned zero.

It also supplies one entry for every configured metric Gate. A gate not run
because an earlier safety stage failed has `passed: null` and a factual detail
such as `not run because PATHS failed`.

Configured Gate values continue to originate from Harness parsing of evaluator
output. `EVAL_COMMANDS` originates directly from subprocess return codes and
cannot be overridden by evaluator text.

### 5.3 Candidate states

Business-terminal candidates use one of these statuses:

- `COMPLETED`: committed, evaluated, and all Gates passed;
- `NO_CHANGE`: Executor produced no changed paths;
- `PATH_GATE_REJECTED`: changed paths violated the editable/frozen boundary;
- `EXECUTOR_FAILED`: the Executor call failed before a usable change;
- `EVAL_FAILED`: evaluation could not be run to completion because of a Harness
  exception such as timeout or launch failure;
- `GATE_REJECTED`: evaluation completed, but `EVAL_COMMANDS` or a configured
  Gate did not explicitly pass.

Every state is written to history. A path-rejected attempt has no candidate SHA
in this MVP; its changed paths and violations remain available. A candidate
that reached evaluation has a SHA even if evaluation or a physics Gate failed.

For `NO_CHANGE`, `PATHS` passes vacuously while `EVAL_COMMANDS` and configured
Gates are `null` because no artifact exists to evaluate. For `EXECUTOR_FAILED`,
all Gates are `null`. For `EVAL_FAILED`, `PATHS` is true,
`EVAL_COMMANDS.passed` is false with the Harness exception as factual detail,
and configured Gates are `null`.

### 5.4 Admission terminology

- `gate_passed`: `PATHS`, `EVAL_COMMANDS`, and every configured Gate explicitly
  passed.
- `eligible`: a committed candidate has `gate_passed=true` and a numeric
  objective value.
- `selected`: the candidate won deterministic objective selection and becomes
  the next parent.

In normal Researcher mode, a selected candidate must improve the incumbent.
Among improving eligible candidates, the best objective wins. Exact objective
ties use candidate identifier order; no subjective score exists.

Static-proposal mode preserves its controlled-experiment behavior: its single
candidate may advance when all Gates pass even if the objective regresses.

## 6. Factual Experiment Record

New candidate history rows contain only proposal provenance, Harness facts,
and telemetry:

```json
{
  "candidate": 0,
  "proposal": "...",
  "parent_sha": "...",
  "sha": "...",
  "status": "COMPLETED",
  "changed_paths": ["..."],
  "gates": {
    "PATHS": {"passed": true, "detail": ""},
    "EVAL_COMMANDS": {"passed": true, "detail": ""},
    "FCN": {"passed": true, "detail": ""}
  },
  "metrics": {"SPEED_MS": 559.415, "FCN": true},
  "gate_passed": true,
  "eligible": true,
  "selected": true,
  "eval_block": "...",
  "telemetry": {}
}
```

The full history stays authoritative. The normal Proposer prompt receives a
compact factual index containing candidate identifiers, proposals, SHAs,
statuses, selected state, changed paths, gate results, and metrics. Raw eval
text stays out of the default prompt but remains persisted and queryable.

The `insights.jsonl` write path is retired. Existing files may remain in old run
directories, but new Proposer calls neither load nor append semantic insights.
`simpleloop memory show` continues to provide exact historical episode lookup
using factual fields.

## 7. Compatibility and Resumption

New runs write the new factual schema. Continuation of an old Judger-era run is
supported by normalizing legacy rows on read:

- legacy `accepted` maps to `gate_passed` when the new field is absent;
- a legacy candidate with a SHA, all declared metric Gates true, and a numeric
  objective maps to `eligible=true` unless legacy `risk=high` had already kept
  it out of the accepted chain;
- existing `selected` and `selected_sha` remain authoritative for the historical
  parent chain;
- legacy `score`, `risk`, `feedback`, `feedback_for_proposer`, `family`,
  `decision`, and `reflection` may be read but are not rendered into new
  Researcher prompts or written for new rounds.

An in-flight round created by the old candidate manifest schema must remain
resumable. Manifest readers tolerate legacy `family`/`decision` fields and
ignore them. New manifests contain only candidate identity, proposal, parent,
run/worktree/result paths, prompt directory, and retry metadata. Legacy
prior/baseline metric context is tolerated but not written into new manifests.

## 8. Task Configuration

The configuration schema continues to own Goal, safety boundaries, scheduling,
runtime, evaluation, source, backend, and prompt self-improvement interval.

Task examples are simplified according to these rules:

- Goal states the outcome, such as minimizing `SPEED_MS` while satisfying every
  configured Gate.
- Goal does not list recommended optimization techniques, hot paths, safe
  transformations, or prohibited solution shapes.
- Gate descriptions state observable checks and thresholds, not predictions
  about which implementation techniques are safe.
- The editable artifact includes production source file creation and the
  production build wiring necessary to integrate a replacement implementation.
- Tests, evaluator scripts, references, benchmarks, thresholds, and measurement
  wiring remain frozen.

The active OMILREC examples use `OMILRECV2/src/**` plus the necessary production
build file as editable paths. Controlled hint/no-hint experiment files may
remain as explicitly named research fixtures, but the default `task.yaml` is
the open, non-prescriptive task.

## 9. Prompt Self-Improvement

`PROMPT_NAMES` becomes `proposer`, `executor`, and `meta_optimizer`.

The Meta Optimizer may edit those three semantic prompts only. Its immutable
identity core describes the two-agent Researcher/Executor relationship and the
Harness factual boundary. Prompt history, static prompt gates, segmented outer
supervision, rollback on invalid prompt files, and the existing no-final-trigger
behavior remain unchanged.

When continuing a pre-refactor self-improvement lineage, initialization removes
`judger.md` from the active prompt directory and restores only the three current
prompt names from an old snapshot. Historical version directories remain
untouched as provenance. A newly created or migrated active prompt set that
contains `judger.md` after initialization fails the static prompt gate.

This feature remains prompt-only. It does not edit Harness code, machine
schemas, Goal, Gates, run facts, or task source.

## 10. Error Handling and Backend Semantics

- Executor failures remain candidate-local business failures.
- Missing or invalid Researcher proposal structure aborts the generation because
  no executable batch exists.
- Eval nonzero return codes are factual `EVAL_COMMANDS=false` results, not
  infrastructure retries.
- Eval launch/timeout exceptions produce `EVAL_FAILED` and retain the candidate
  SHA.
- HEPJob retries remain reserved for infrastructure loss such as a missing
  completion marker, held/lost jobs, or worker death.
- A candidate result is written atomically before the worker completion marker,
  preserving the current remote completion contract.
- A round with no selected candidate preserves the incumbent parent.

## 11. Testing Strategy

Implementation follows test-driven development. Required coverage includes:

1. Proposer schema accepts exactly K free-text proposals and rejects legacy
   semantic fields in new responses.
2. Proposer prompts contain Goal, Gates, accepted SHA, and factual history but
   no mandatory reflection workflow, Judger fields, optimization recipes, or
   prescribed investigation commands.
3. Executor prompts permit any implementation scale inside the artifact while
   preserving Harness commit/eval authority.
4. PATHS failure is persisted, skips commit/eval, and cannot become parent.
5. Eval command return codes populate `EVAL_COMMANDS` and participate in final
   admission independently of evaluator-emitted text.
6. Configured Gate pass/fail/unknown values populate the unified gate view.
7. Every candidate terminal state is stored and projected factually.
8. Selection uses only gates, numeric objective, incumbent improvement, and
   deterministic candidate order.
9. Old histories and in-flight manifests remain resumable without exposing
   legacy Judger narratives to the Researcher.
10. Local and HEPJob candidate workers produce the same business result schema.
11. Prompt self-improvement operates with the three remaining prompt files and
    rejects a reintroduced Judger prompt.
12. README and active task examples describe the open Researcher architecture.

The known pre-existing baseline mismatch remains outside this feature:
`examples/tiny_algo_opt/task.yaml` configures two parallel candidates while
`tests/test_parallel_candidates.py` expects three.

## 12. Acceptance Criteria

The refactor is complete when:

1. Runtime execution invokes only Researcher and Executor agents.
2. No valid commit/eval result can be invalidated by a narrative agent failure.
3. New Researcher prompts receive direct factual evidence and no Judger
   interpretation.
4. New Proposer output contains only proposal text required for scheduling.
5. Every candidate attempt is present in history with explicit staged gate
   results and a terminal status.
6. Only selected eligible candidates become parents.
7. Default task configuration defines outcomes and evaluator boundaries without
   constraining solution form or modification scale.
8. Existing scheduling, isolation, provenance, evaluation, backends, resumption,
   and self-improvement supervision continue to work.
