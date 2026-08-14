# SimpleLoop Phase 7: configuration and package convergence

## Goal

Finish the refactor without changing loop semantics. A reader should see one
configuration boundary and only the packages that own current behavior.

## Configuration boundary

`simpleloop.config.load()` is the sole public task-config entry point. New task
files use `schema: simpleloop.v1` and describe intent with these blocks:

- `goal`, optional `hints`
- `loop`
- `source`
- `world`
- `evaluation`
- `providers`
- `proposer`
- `executor`
- `rsi`

The loader validates unknown fields and translates the document once into the
resolved runtime dictionary already consumed by the composition root and worker
snapshot. This keeps Phase 7 small: no pipeline module reads YAML and no stable
runtime path is rewritten merely to rename configuration fields.

Old `kind: task` documents remain readable for one transition release through
`simpleloop.legacy_config`. Both external shapes reduce to a private normalized
validator; v1 never depends on the removable legacy adapter. `config.py` only
detects the document version and normalizes at the boundary.
New examples and newly authored configurations use `simpleloop.v1`.

The new schema exposes only implemented behavior. It does not advertise inert
`cwd`, network-policy, or arbitrary provider-command fields. Network remains
available inside the prepared Apptainer world, as required by the agents.

## Package ownership

The remaining transition packages are removed, not wrapped:

| Old owner | Final owner | Reason |
|---|---|---|
| `container.image` | `world.image` | SIF construction prepares a World |
| `roles.agent` | `stages.agent` | the agent call is a stage adapter over a World |
| `harness.evals` | `stages.evaluator` | evaluation execution and parsing are one stage |
| `harness.views` | `stages.gate` | gate presentation belongs with gate semantics |
| `harness.store` + `harness.memory` | `persistence.history` | one authoritative history owner |
| `harness.handoff` | `persistence.handoff` | handoffs are persisted observations |
| `harness.export` | `reporting.export` | export is an offline run report/handover |

After imports and tests migrate, `simpleloop/container`, `simpleloop/roles`, and
`simpleloop/harness` no longer exist. No compatibility import facade is kept.

## Non-goals

- No behavior rewrite of loop, round, candidate, scheduler, World, proposer, or
  RSI pipelines.
- No additional provider abstraction or sandbox backend.
- No second internal config model alongside the existing resolved snapshot.
- No removal of legacy *run snapshots*: old `config.resolved.json` files remain
  readable because completed runs must stay inspectable.

## Acceptance

1. A `simpleloop.v1` local config and HPC config resolve to the existing runtime
   contract, including RSI scheduling.
2. A legacy task config still resolves through `legacy_config.py`.
3. All checked-in active examples use `simpleloop.v1`.
4. No production or test import mentions `simpleloop.harness`,
   `simpleloop.roles`, or `simpleloop.container`; those directories are absent.
5. Full tests, compile checks, and the self-contained tiny loop pass.
