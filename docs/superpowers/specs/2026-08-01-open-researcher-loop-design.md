# Open Researcher Loop

Date: 2026-08-01
Status: implemented

## Objective

SimpleLoop is an AI Scientist constrained by objective safety and validation,
not a fixed research workflow. The user supplies a goal and gates. The
Researcher may choose the hypothesis, inspect prior evidence, and make any
in-scope implementation change—including a major rewrite or an alternative
implementation—provided the Harness accepts it.

## Minimal architecture

```text
outer loop
  Researcher (Proposer)
      |
      | executable experiment instructions
      v
  Executor candidate workers
      |
      | commit SHA + changed paths + execution status
      v
  Harness evaluation and gates
      |
      | objective facts + gate facts + candidate history
      +--------------------------> Researcher
```

There is no Judger role. The Researcher owns interpretation, comparison,
acceptance of evidence, and the next proposal. The Executor changes code and
returns facts. The Harness alone runs evaluation and determines whether a
candidate may enter objective selection.

The outer loop remains explicit. Self-directed recursion is a future direction,
not part of this version.

## Researcher contract

The Researcher receives:

- the research goal;
- editable and frozen paths;
- declared gate descriptions;
- the current parent SHA;
- compact factual history: proposal, SHA, status, changed paths, gate results,
  objective metrics, eligibility, and selection.

It emits exactly `candidates_per_round` executable experiment instructions.
Prompts do not prescribe a reflection chain, research taxonomy, fixed mutation
size, or preferred implementation strategy.

The Researcher may decide what to investigate. It cannot decide what is true.

## Candidate and Harness contract

Every candidate starts from the same parent SHA. The Executor returns a commit
SHA when it changes code. The Harness then:

1. checks changed paths;
2. runs configured evaluation commands;
3. parses objective and physical/correctness gate facts;
4. marks the candidate eligible only when all gates pass and the objective is
   finite;
5. persists the attempt whether it passed or failed.

The current candidate schema uses `status`, `gate_passed`, and `eligible`.
This version is a clean break and does not migrate old run or prompt-history
schemas. History, worker manifests, remote results, and active prompt sets fail
at their existing input boundaries when current required facts are absent.

## Selection

Among eligible candidates, the Harness selects the best declared objective.
The parent advances only when that candidate improves the incumbent objective.
Failed, ineligible, and non-improving candidates remain factual history but
cannot become the parent.

There is no semantic score, risk opinion, or agent-authored acceptance decision.
Best-candidate data is derived from append-only history when needed; `Store`
does not maintain a second cache.

## Gates and authority

Gate authority remains outside the agents:

- editable/frozen path gate;
- evaluation command success;
- user-declared physical or correctness gates;
- finite objective requirement;
- incumbent-improvement rule.

This separates open research freedom from objective truth and admission.

## Memory and reporting

`history.jsonl` is the complete experiment record. Proposer views and episode
lookup expose compact factual projections and omit raw evaluator noise.
Summaries, export, and plots derive the best candidate from current history and
report objective progress without Judger scores.

## Prompt self-improvement

The Meta-Optimizer may revise the active Proposer and Executor semantics, while
the Prompt Gate protects role identity and Harness authority. Prompt evolution
does not modify evaluation commands or gates in this version.

## Non-goals

- autonomous Harness evolution;
- removing the outer loop;
- adding planner, critic, reviewer, risk model, or replacement Judger roles;
- compatibility adapters for old runs;
- subjective scoring or semantic candidate admission.

## Acceptance invariants

- Runtime topology is Researcher -> Executor -> Harness evaluation.
- No Judger runtime, prompt, config, or report field remains.
- Researcher prompts allow broad in-scope implementation strategies.
- Harness gates run after execution and alone control eligibility.
- Failed gates are recorded but never become parent candidates.
- All parallel candidates share the same parent.
- Best selection is deterministic and derived from current history.
- Current run and prompt-history schemas are the only supported schemas.
