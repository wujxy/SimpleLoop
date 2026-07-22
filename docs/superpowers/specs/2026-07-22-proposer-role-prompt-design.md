# Proposer Role Prompt Redesign

## Goal

Make the proposer behave as the hypothesis generator in an optimization loop:
it uses measured history as search experience, glances at accepted source when
useful, and proposes the next experiments without taking over implementation,
code review, or validation.

The redesign changes the role semantics, not the proposer's freedom of thought.
It does not prescribe phases, command counts, line budgets, or a fixed research
procedure.

## Role boundary

The proposer owns the question **what may be worth trying next**.

- The executor owns feasibility investigation, implementation details, complete
  call-site discovery, and local verification.
- The judger owns evaluation of the landed diff, correctness, performance, and
  latent risk.
- The harness owns gate enforcement and selection of the accepted candidate.

A proposal is therefore a code-grounded hypothesis, not a proof or an
implementation plan. Source reading supports hypothesis generation; it is not
an independent audit task. Prior outcomes are presented as trustworthy team
experience and do not normally need to be re-audited through historical diffs.

## Reasoning chain

The existing batch shape stays unchanged:

```text
previous evidence -> reflection -> decision -> proposal
```

`reflection` is a short, generation-level reading of what history implies for
the next search. Each candidate's `decision` expresses whether its direction
continues a promising area or switches elsewhere. Its `proposal` is the
experiment that follows from that route. The prompt describes these fields as
one thought rather than three independent summaries.

Because one reflection is shared by a multi-candidate batch, it establishes the
overall search reasoning; it is not required to separately prove every
candidate's route or enumerate every target.

## Prompt content retained

Only the following operational facts remain:

- task goal and configured gates;
- accepted `base_sha` and same-parent candidate semantics;
- prior outcomes, including non-selected and regressed attempts;
- objective change against the direct accepted parent as the useful performance
  signal, while gate passage alone is not evidence of benefit;
- optional committed-source access through targeted `git show`/`git grep`
  because the proposer repository has no checked-out worktree;
- editable and frozen path boundaries;
- exact candidate count and the existing JSON field contract.

The prompt removes language that asks for the globally highest-value direction,
proof that an area is exhausted, exhaustive self-audit of historical SHAs,
implementation-level float/cache reasoning, or certainty that a proposal will
work.

## Output contract

The existing Schema limits remain visible:

- `reflection`: at most 600 characters; empty only with no prior history;
- `family`: non-blank, at most 64 characters, unique after trim/case-fold;
- `decision`: `continue` or `switch`;
- `proposal`: at most 800 characters;
- exactly the configured number of candidates.

These are interface constraints, not a prescribed thinking workflow.

## Self-review of potentially over-hard language

The final prompt must avoid turning soft role guidance back into a detailed
procedure:

- no command, turn, time, or source-line budget;
- no mandatory exploration phases or candidate-slot workflow;
- no requirement to prove optimality, exhaustion, safety, or implementation
  completeness;
- no per-candidate checklist whose completion requires deep source tracing;
- no instruction to inspect historical diffs unless the proposer naturally
  finds a quick source glance useful;
- no leader/employee or judgment metaphor that blurs proposer and judger roles.

The proposal description remains naturally actionable: identify a real code
area, a plausible waste, a broad mechanism, why it may help, and a one-round
scope, while leaving concrete design choices to the executor.

## Tests

Prompt-level tests will assert that the rendered prompt:

1. defines a proposal as a grounded hypothesis rather than a proof;
2. assigns implementation and validation to executor/judger;
3. states the `reflection -> decision -> proposal` chain;
4. includes the 600/64/800 limits and exact candidate count;
5. retains direct-parent objective guidance and the no-checkout source note;
6. does not contain the old over-exploration triggers such as
   `highest-value`, `stalled/exhausted`, or invitations to self-audit every
   historical SHA.

