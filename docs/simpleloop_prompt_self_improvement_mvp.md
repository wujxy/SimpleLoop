# SimpleLoop Prompt Self-Improvement MVP

This document describes the current development-time outer loop. The artifact
loop is intentionally smaller than the outer orchestration:

```text
Goal → Researcher → Executor → Harness evaluation/gates/history
                         ↑                         │
                         └──── next proposal ──────┘

fixed artifact segment → pause → Meta Optimizer → prompt gate
                       → snapshot or restore → next segment
```

## Authority model

- The Researcher investigates source and factual history, interprets evidence,
  and decides what experiment to propose next.
- The Executor owns the concrete implementation of one proposal.
- The Harness owns commits, evaluation, metric parsing, Gates, eligibility,
  objective selection, and factual history.
- The Meta Optimizer may change agent semantics, but not Goal, Gates, Harness,
  evaluator facts, or task source.

The Researcher is open with respect to research method and implementation
scale. The fixed boundary is epistemic and operational: an agent cannot declare
its own result valid or admit itself into the parent chain.

## Evolvable prompt set

Each run initializes an independent v000 containing:

```text
prompts/proposer.md
prompts/executor.md
prompts/meta_optimizer.md
```

The Meta Optimizer can rewrite the first two files and the evolvable section of
its own prompt. The identity core, prompt filenames, file permissions, and
machine protocols are fixed by the Harness. A valid investigation may return
`no_change`; prompt churn is not an objective.

Existing runs created by the former four-prompt architecture retain their
historical snapshots unchanged. When such a run is opened or a historical
snapshot is restored, the active directory drops the obsolete fourth prompt
and receives the current fixed Meta Optimizer identity core. This preserves
audit history without reviving an obsolete runtime role.

## Trigger and transaction semantics

With:

```yaml
self_improvement:
  interval_rounds: 10
```

the supervisor runs ten completed artifact rounds, pauses the loop, and invokes
the Meta Optimizer. It does not invoke an optimizer after the final segment,
because there is no later artifact work on which the new prompts could act.

An optimizer trigger is transactional:

1. Record its parent prompt version and trigger round.
2. Let the Meta Optimizer inspect run artifacts and edit the active prompt set.
3. Apply the deterministic prompt gate.
4. Snapshot a changed valid set, record `no_change`, or restore the parent on
   invalid output, errors, or interruption.

`--continue` resumes the same run-local lineage and recovers interrupted
transactions before starting new artifact work. Static `--proposals` mode is a
controlled experiment and cannot be combined with self-improvement.

## Why the outer loop remains

The Researcher is not yet a self-rewriting Gödel machine. Keeping the outer
supervisor makes prompt mutations auditable, reversible, and separated from
task experiments. This is a deliberate temporary boundary: current work opens
the inner Researcher while retaining deterministic control over changes to the
research process itself.
