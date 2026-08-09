# Generator grounding-first: propose-requires-survey + lever map

## Problem (confirmed from log)

`omilrec-v100-generator-proposer-008-continue-5.log`: 11/12 hypotheses submitted at **step 2** (one `ls`/`grep`, then immediately `submit_hypothesis`). All cluster on `OMILRECV2.cc` / `PMTRealInputTool.cc` / `RecHelper.cc` with cache/memoization mechanisms — the salience-biased single-family fixation the user described.

Root cause: the Generator's `submit_hypothesis` has only **Gate 1** (`has_read_source` — at least one `run_research_command`) and **Gate 2** (`facts_read` non-empty). Both are satisfiable in one shallow `ls` + one fabricated-fact-list. Grounding is **self-reported** (the model writes `facts_read`), not **extrinsic** (verified against what was actually surveyed). There is no lever map and no prerequisite coupling between survey depth and emit.

## Design (from discussion)

Four phases inside the Generator's `_tool_loop`, replacing the flat "read a bit → submit" loop:

1. **Phase 1 — declare survey plan** (operational actions, task-general, not hardcoded "read code")
2. **Phase 2 — execute survey** (real tool calls, material extrinsic in context)
3. **Phase 3 — synthesize lever map** (explicit artifact: parts, roles, structural-leverage space; each entry traces to surveyed material)
4. **Phase 4 — diverge + emit** (proposals grounded-in the map, not contained-by it; new levers allowed if traced to surveyed material)

Teeth (structural, not merit, not patches):
- **Prerequisite coupling**: `submit_hypothesis` rejected if no Phase-2 survey material in context (generalizes Gate 1 from "any read" to "survey plan executed").
- **Traceability coupling**: each `facts_read` entry and each lever-map entry must reference a path/region actually examined in Phase 2. Perfunctory/fabricated maps fail pointer resolution.
- **Grounded-in not contained-by**: proposals may reference existing map levers OR declare new levers, but new levers must still trace to surveyed material. Map is an extensible palette, not a closed set.

Explicitly NOT added (patches that would stack):
- No map-size floor/ceiling (size = what survey revealed).
- No "proposal must cover whole map" breadth check.
- No "read N files before submit" coverage check.
- No coupling-to-gate marker injection (identity's job, per discussion).

CoT is an auxiliary in Phase 1/3 prompts, not the backbone.

## Files to change

### 1. `simpleloop/prompts/generator.md` — rewrite the semantic prompt

The current prompt frames the Generator as a "scout, not an analyst" that stays "light and fast" and submits a "raw lead." This framing is the root cause at the identity level — it *instructs* shallowness. Rewrite to:

- **Identity**: a lever-space surveyor that builds a factual basis before diverging. Bold and broad *because* it has the whole map, not despite not having one.
- **Four-phase structure** described as the working method (declare survey → execute → synthesize lever map → diverge+emit). Task-general: "survey the subject matter of this task" not "read the code." The prompt enumerates *non-exhaustive* instances (code: read artifacts + profiling; math: definitions + prior results; experiments: data + methodology) without hardcoding any.
- **Lever map**: defined as a structural map of "what each part does + where there is structural space to act" — not a summary. The stance is "where can I act," not "what is this."
- **Grounded-in**: proposals reference map levers or declare new ones traced to surveyed material. Explicitly NOT "must be contained by the map."
- **facts_read**: now must reference specific surveyed material (path:region or equivalent), not free-form claims. This is the traceability anchor.
- Keep the G1-G9 generative basis unchanged (it's the divergence lens set, orthogonal to grounding).
- Keep the "no history" boundary (Generator still sees no dashboard/frontier).

### 2. `simpleloop/roles/generator.py` — multi-phase runtime

**New action types** (added to `_parse_generator_action`):
- `declare_survey_plan`: `{action, plan: [{action: "run_research_command", command, cwd}, ...], rationale: str}`. The plan is a list of *operational* tool calls the agent intends to execute, with a rationale for why these access the factual basis for *this* task. Parsed and validated but not executed by the parser — execution happens in the loop. This makes Phase 1 explicit and its output inspectable.
- `emit_lever_map`: `{action, levers: [{part, role, structural_space, surveyed_ref}, ...]}`. The lever map artifact. `surveyed_ref` is a reference to material examined in Phase 2 (e.g. `source:OMILRECV2/src/OMILRECV2.cc:Calculate_EVLikelihood`). Validated for non-emptyness and ref format.
- `submit_hypothesis`: extended. The existing `facts_read` field now requires each entry to be traceable (reference a surveyed path/region). Add optional `grounded_lever` field referencing a lever from the map (existing or newly declared). If the hypothesis declares a new lever, it must include `new_lever_surveyed_ref` tracing to surveyed material.

**Phase tracking in `_tool_loop`**:
- Track `phase` state: `1=plan_declared, 2=surveying, 3=map_emitted, 4=emitting`.
- Phase transitions are driven by the agent's actions (declare_survey_plan → Phase 2; tool calls execute the plan → Phase 2 complete when plan exhausted or agent moves on; emit_lever_map → Phase 3 complete; submit_hypothesis → Phase 4).

**Prerequisite coupling** (the承重墙):
- `submit_hypothesis` rejected if `emit_lever_map` not yet issued (no map → no emit). Repair message, continue.
- `emit_lever_map` rejected if no survey tool calls executed since `declare_survey_plan` (no survey → no map). Repair message, continue.
- `declare_survey_plan` rejected if plan is empty or contains non-operational entries.

**Traceability coupling**:
- For each `facts_read` entry and each lever's `surveyed_ref`, verify the referenced path was actually examined (track examined paths/regions from Phase 2 tool call outputs, like the proposer's `_validate_block_evidence` does with `new_evidence`). Reject with repair if a ref doesn't resolve.

**Grounded-in (not contained-by)**:
- `submit_hypothesis.grounded_lever` may reference an existing map lever (by part/role match) OR include `new_lever_surveyed_ref`. Both are valid. No "must match existing lever" constraint.

**Budget**: the orchestrator currently gives `gen_steps = 8 + 4 * hypotheses_per_lane`. Increase to account for 4 phases: `gen_steps = 12 + 6 * hypotheses_per_lane` (Phase 1 ~1 step, Phase 2 survey ~4-6 steps, Phase 3 map ~1 step, Phase 4 emit ~1 step per hypothesis). This is a knob, tunable.

**Backward compat**: `regenerate()` reuses `_tool_loop` and gets the same structure. The `regenerate` prompt adds the feedback as an additional user message but the 4-phase structure still applies (the generator re-surveys to find a real region for the new hypothesis).

### 3. `simpleloop/roles/hypothesis.py` — extend HypothesisCard

Add optional fields:
- `grounded_lever: str | None = None` — reference to the lever in the map this hypothesis acts on (free-form, matches a lever's `part`/`role`).
- `new_lever_surveyed_ref: str | None = None` — when the hypothesis declares a new lever not in the map, the surveyed material it traces to.
- `surveyed_refs: tuple[str, ...] = ()` — the specific material examined that grounds this hypothesis (extracted from `facts_read` refs or explicit).

The `signature()` dedup key is unchanged (region × mechanism × intervention) — grounding fields don't affect dedup.

### 4. `simpleloop/roles/orchestrator.py` — adjust gen step budget

Change `gen_steps = 8 + 4 * hypotheses_per_lane` → `gen_steps = 12 + 6 * hypotheses_per_lane` (or read from config). No other orchestrator changes — the lane structure, parallelism, and cognitive-element handoff are unchanged. The cognitive element receives richer cards (with grounding fields) but its sieve/enrich logic is unaffected (it already reads `facts_read`).

### 5. `simpleloop/config.py` — add `gen_steps_base` and `gen_steps_per_hyp` knobs

Add to the `loop` section:
- `gen_steps_base` (default 12): base step budget for the generator.
- `gen_steps_per_hyp` (default 6): additional steps per hypothesis in the lane.

This makes the budget tunable without code changes. `orchestrator.py` reads these instead of hardcoding.

### 6. `tests/test_generator.py` — update tests

Existing tests that do `run_research → submit_hypothesis` need to go through the full 4-phase sequence now:
- Update `FakeModel` reply sequences to include `declare_survey_plan`, `emit_lever_map`.
- Add tests for the prerequisite couplings:
  - `submit_hypothesis` before `emit_lever_map` → rejected with repair.
  - `emit_lever_map` before any survey → rejected with repair.
  - `declare_survey_plan` with empty plan → rejected.
- Add tests for traceability:
  - `facts_read` referencing a path not surveyed → rejected.
  - `emit_lever_map` lever with `surveyed_ref` not surveyed → rejected.
- Add test for grounded-in: `submit_hypothesis` with `new_lever_surveyed_ref` tracing to surveyed material → accepted (not rejected for not matching existing lever).
- Keep the G1-G9 basis tests unchanged.

## What is NOT changed

- **Cognitive element (`proposer.py`)**: untouched. Its sieve/enrich is the downstream audit. It already reads `facts_read`. The richer cards flow through unchanged code.
- **G1-G9 generative basis**: unchanged. Grounding is orthogonal to the divergence lenses.
- **Sieve/merit architecture**: untouched. This is purely generation-side.
- **Memory/history boundary**: Generator still sees no history. Grounding is about the *task's subject matter* (source/artifacts), not about prior experiments.
- **No coupling-to-gate marker**: per discussion, gate-adjacent timidity is an identity problem, not a marker-injection problem.

## Residual (acknowledged, not solved here)

- **Shallow survey**: the prerequisite coupling enforces *some* survey, not *deep* survey. A 2-entry lever map from a shallow survey passes. This is the honest residual — treated by identity + budget, not by a coverage check (which would be a patch). The identity rewrite in the prompt is the primary treatment; the increased step budget gives room for the identity to act.
- **Map form-filling**: the traceability coupling catches fabricated refs but not perfunctory-but-traceable maps. Same residual, same treatment.

## Validation

1. `pytest tests/test_generator.py` — all updated tests pass.
2. `pytest tests/test_orchestrator.py` — orchestrator integration unaffected.
3. `pytest tests/test_hypothesis.py` — HypothesisCard changes don't break dedup/signature.
4. Manual: run one round on omilrec-v100-gated and check the generator log shows >2 steps before submit, lever map emitted, and hypotheses spanning >1 region/mechanism family.
