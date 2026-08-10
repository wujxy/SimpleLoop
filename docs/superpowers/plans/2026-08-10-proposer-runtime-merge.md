# Proposer Runtime Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge the current Generator and Cognitive proposer loops into one lane-local Proposer runtime while preserving the existing batch/funnel flow, action protocol, phase-specific context, regeneration budget, and parallel lanes.

**Architecture:** `ProposerOrchestrator` remains the outer lane scheduler. Each lane invokes one `ProposerAgent` runtime that owns the full phase sequence: history-free generation, history-aware cognitive audit/enrichment, and feedback-conditioned regeneration. Runtime state is shared across phases, but each model turn receives a phase-specific context view; the initial generation view never contains history, while regeneration receives only the compact feedback packet plus the original generation view.

**Tech Stack:** Python 3, dataclasses/enums, existing `ResearchAgent` tool loop, existing `ChatModel`, `MemoryService`, pytest, no new dependencies.

## Global Constraints

- Do not modify `.env`, credentials, secrets, production runtime configuration, or executor/harness behavior.
- Preserve the current batch/funnel quantities: one generation phase emits `_HYPOTHESES_PER_LANE` cards, the cognitive phase selects `_SELECT_PER_LANE`, and all selected proposals are submitted.
- Preserve current action names and required payloads: `run_research_command`, `emit_lever_map`, `submit_hypothesis`, memory actions, `select_for_enrich`, `feedback_generator`, `submit_proposals`, and `block`.
- Preserve phase information boundaries: initial generation has no dashboard/frontier/explore/history; cognitive audit has the current startup pack; regeneration receives only the generation view and feedback packet.
- Preserve `gen_steps`, `cognitive_steps`, `_MAX_REGENERATIONS`, lane scheduling, and stable lane-order collection.
- The Harness remains the only source of truth for correctness, Gate status, metrics, and merit; the Cognitive phase does not become a reviewer.
- Add or update tests for every changed behavior and run the relevant tests before claiming completion.

## File Map

- Create: `simpleloop/roles/proposer_runtime.py` — phase enum, shared runtime state, hypothesis-version records, context-view rendering, and phase/action policy helpers.
- Modify: `simpleloop/roles/proposer.py` — make `ProposerAgent` the unified runtime; retain cognitive parsers and result contracts, add generation phase dispatch, and remove the separate cognitive-only callback boundary.
- Modify: `simpleloop/roles/generator.py` — retain pure generator protocol helpers and semantics, but remove the standalone `GeneratorAgent` runtime after its logic is moved into `ProposerAgent`.
- Modify: `simpleloop/roles/orchestrator.py` — construct/use one unified proposer runtime per lane; remove `GeneratorAgent` construction and the external `generator_regenerate` callback.
- Modify: `simpleloop/memory/context.py` — add the compact regeneration context renderer without history dashboard/frontier blocks.
- Modify: `simpleloop/memory/service.py` — expose the regeneration-context façade if the runtime obtains all context views through `MemoryService`.
- Modify: `simpleloop/prompts/generator.md` — preserve generator semantics/actions but describe generation as a phase of one Proposer runtime.
- Modify: `simpleloop/prompts/proposer.md` — preserve cognitive actions and batch protocol while replacing the separate-partner contract with the unified-runtime phase contract.
- Create: `tests/test_proposer_runtime.py` — state transitions, context isolation, action allowlists, batch handoff, and regeneration context tests.
- Modify: `tests/test_generator.py` — move generator behavior tests from `GeneratorAgent` to the unified Proposer runtime or pure helpers.
- Modify: `tests/test_cognitive_element.py` — move cognitive-loop tests to the unified runtime and retain parser/guard coverage.
- Modify: `tests/test_orchestrator.py` — replace two-agent mocks with one unified runtime mock and preserve lane/funnel/parallelism assertions.
- Modify: `tests/test_parallel_candidates.py`, `tests/test_plot.py`, `tests/test_telemetry.py`, and `tests/test_runtime.py` only where mocks assert the old constructor or method boundary.

## Task 1: Add the Shared Proposer Runtime State and Context Views

**Files:**
- Create: `simpleloop/roles/proposer_runtime.py`
- Modify: `simpleloop/memory/context.py`
- Modify: `simpleloop/memory/service.py`
- Create: `tests/test_proposer_runtime.py`

**Interfaces:**
- Produces `ProposerPhase(str, Enum)` with values `GENERATE`, `COGNITIVE`, and `REGENERATE`.
- Produces `HypothesisVersion` with fields `hypothesis: HypothesisCard`, `version: int`, `origin: str`, and `feedback_refs: tuple[str, ...]`.
- Produces `ProposerRuntimeState` with fields `phase`, `cards`, `versions`, `selected_indices`, `generation_context`, `generation_messages`, `active_hypothesis_index`, `regeneration_count`, `phase_transitions`, and `feedback_packets`.
- Produces `render_regeneration_context(generation_context: str, hypothesis: HypothesisCard, feedback: dict, version: int) -> str`.
- Produces `allowed_actions(phase: ProposerPhase) -> frozenset[str]`.

- [ ] **Step 1: Write failing state and context tests.**

Add tests that assert:

```python
def test_initial_state_starts_history_free_generation():
    state = ProposerRuntimeState(generation_context="objective only")
    assert state.phase is ProposerPhase.GENERATE
    assert state.cards == []

def test_regeneration_view_contains_feedback_but_not_startup_history():
    context = render_regeneration_context(
        "objective only", _card(),
        {"observation": "r3c0 was neutral", "relation_to_seed": "same mechanism",
         "evidence_refs": ("experiment:r3c0",), "implication": "try a different lever"},
        version=2,
    )
    assert "r3c0 was neutral" in context
    assert "objective only" in context
    assert "dashboard" not in context.lower()
    assert "frontier" not in context.lower()

def test_phase_action_policy_preserves_existing_action_names():
    assert "submit_hypothesis" in allowed_actions(ProposerPhase.GENERATE)
    assert "submit_proposals" in allowed_actions(ProposerPhase.COGNITIVE)
    assert "submit_hypothesis" in allowed_actions(ProposerPhase.REGENERATE)
    assert "inspect_episode" not in allowed_actions(ProposerPhase.GENERATE)
```

- [ ] **Step 2: Run the focused tests and verify they fail.**

Run:

```bash
pytest -q tests/test_proposer_runtime.py
```

Expected: FAIL because the new state, renderer, and action-policy interfaces do not exist.

- [ ] **Step 3: Implement the minimal state and view layer.**

Keep this module pure: it must not call the model, memory service, filesystem, or research tools. `render_regeneration_context` may include the feedback fields and target hypothesis, but must never render the full startup pack. `allowed_actions` must be the only source used by the unified runtime to reject phase-incompatible actions.

- [ ] **Step 4: Add the memory façade only if the runtime needs it.**

Expose `MemoryService.build_regeneration_context(...)` as a thin delegation to `memory.context.render_regeneration_context(...)`; do not duplicate rendering logic in `MemoryService`.

- [ ] **Step 5: Run the focused tests and commit the self-contained state layer.**

Run:

```bash
pytest -q tests/test_proposer_runtime.py
```

Expected: PASS.

## Task 2: Make ProposerAgent Own the Generation and Cognitive Phase Protocols

**Files:**
- Modify: `simpleloop/roles/proposer.py`
- Modify: `simpleloop/roles/generator.py`
- Modify: `tests/test_proposer_runtime.py`
- Modify: `tests/test_generator.py`
- Modify: `tests/test_cognitive_element.py`

**Interfaces:**
- `ProposerAgent.run(...)` keeps the existing public arguments already consumed by `loop.py`, including `gen_steps` and `cognitive_steps`.
- Add internal `ProposerAgent._run_generation_phase(...) -> GenerationResult`.
- Add internal `ProposerAgent._run_cognitive_phase(...) -> BranchResult`.
- Add internal `ProposerAgent._run_regeneration_phase(state: ProposerRuntimeState, feedback: dict, ...) -> HypothesisCard`.
- `ProposerAgent._parse_action(...)` dispatches to the existing generator parser in `GENERATE/REGENERATE` and the existing cognitive parser in `COGNITIVE`.
- `ProposerAgent._validate_guard(...)` dispatches to the existing generator prerequisite guards or cognitive guards according to the current phase.

- [ ] **Step 1: Add failing unified-runtime tests for the complete batch handoff.**

Use a fake model that emits the existing generator sequence (`run_research_command`, `emit_lever_map`, repeated `submit_hypothesis` actions), followed by the existing cognitive sequence (`select_for_enrich`, `submit_proposals`). Assert that:

```python
result = agent.run(..., candidates_per_round=2, gen_steps=10, cognitive_steps=10)
assert len(result.proposals) == 1
assert trace_phases(result) == ["generate", "cognitive"]
assert generation_context_seen_by_model_does_not_contain_history
assert cognitive_context_seen_by_model_contains_startup_pack
```

Add a test that a generation-phase memory action and a cognitive-phase `submit_hypothesis` are rejected with the existing protocol-repair path rather than executed.

- [ ] **Step 2: Run the new unified tests and verify failure.**

Run:

```bash
pytest -q tests/test_proposer_runtime.py::test_unified_batch_handoff
pytest -q tests/test_proposer_runtime.py::test_phase_action_allowlist
```

Expected: FAIL because `ProposerAgent.run` still delegates to separate generator/cognitive loops.

- [ ] **Step 3: Move generator mechanics into ProposerAgent without changing action parsing.**

Move or reuse the pure helpers from `simpleloop.roles.generator`:

```python
_parse_generator_action
_replace_basis
_g_definition
GenerationResult
_GEN_PROTOCOL
```

The generation phase must retain the current survey → lever map → multiple `submit_hypothesis` flow, including `facts_read`, assigned-op validation, batch continuation prompts, and the `gen_steps` budget. `submit_hypothesis` ends only the generation phase; it does not end `ProposerAgent.run` until the expected batch count is collected.

- [ ] **Step 4: Reuse the existing cognitive loop as an internal phase.**

Move the body of `research_batch` behind `_run_cognitive_phase` or make `research_batch` a private-compatible wrapper called only by `run`. It must receive the cards produced by the generation phase, build the existing startup pack, preserve deduplication and selection behavior currently inside the cognitive phase, and keep `submit_proposals` and `block` as terminal actions.

Do not add merit scoring, a new reviewer, a new judge, or a new proposal schema.

- [ ] **Step 5: Implement phase-specific parser and guard dispatch.**

The dispatch must use the same parsers and guards, but enforce these policies:

```python
GENERATE = generator actions only
COGNITIVE = memory/cognitive actions only
REGENERATE = generator actions only
```

Protocol repair remains bounded by `ResearchAgent._MAX_PROTOCOL_REPAIRS`; generator parse failures still surface as `ProposerError` from the unified public runtime.

- [ ] **Step 6: Remove the standalone GeneratorAgent runtime after migration.**

Retain `generator.py` only for pure generator protocol helpers if they are still imported by tests or prompt code. Delete the separate `GeneratorAgent` tool-loop class once no production code imports it. Preserve a deliberate compatibility import only if an existing public test or package API requires it; do not keep a second production runtime hidden behind the compatibility name.

- [ ] **Step 7: Run generator, cognitive, and unified tests.**

Run:

```bash
pytest -q tests/test_proposer_runtime.py tests/test_generator.py tests/test_cognitive_element.py
```

Expected: PASS, with all existing survey/map/submit, cognitive selection/enrichment, budget, guard, and parser assertions migrated to the unified runtime.

## Task 3: Implement Feedback-Conditioned Regeneration with Shared Runtime State

**Files:**
- Modify: `simpleloop/roles/proposer.py`
- Modify: `simpleloop/roles/proposer_runtime.py`
- Modify: `tests/test_proposer_runtime.py`
- Modify: `tests/test_cognitive_element.py`
- Modify: `tests/test_generator.py`

**Interfaces:**
- `_run_regeneration_phase(...)` returns one `HypothesisCard` and records a new `HypothesisVersion`.
- `feedback_generator` remains a non-terminal cognitive action with its current required fields.
- `_MAX_REGENERATIONS` remains the hard per-lane limit.

- [ ] **Step 1: Write failing tests for context isolation and versioned regeneration.**

Add tests that assert:

```python
def test_regenerate_does_not_receive_full_cognitive_context(...):
    # The model sees generation context + compact feedback, not startup_pack.
    assert "Recent factual dashboard" not in captured_message
    assert "Research frontier" not in captured_message
    assert "r3c0 was neutral" in captured_message

def test_regeneration_returns_to_cognitive_phase(...):
    assert state.phase_transitions == [
        ("generate", "cognitive"),
        ("cognitive", "regenerate"),
        ("regenerate", "cognitive"),
    ]

def test_regeneration_budget_is_enforced_by_unified_runtime(...):
    with pytest.raises(ProposerError, match="budget exhausted"):
        trigger_more_than_three_feedback_actions()
```

- [ ] **Step 2: Run the focused tests and verify failure.**

Run:

```bash
pytest -q tests/test_proposer_runtime.py -k regeneration
```

Expected: FAIL because the current callback launches the standalone generator path and does not maintain unified phase state.

- [ ] **Step 3: Implement regeneration as a context-view branch.**

On `feedback_generator`:

1. Record the feedback packet and increment the lane regeneration count.
2. Build a regeneration view from the original history-free generation context, the current hypothesis/version, and the feedback packet.
3. Set phase to `REGENERATE` and allow only generator actions.
4. Run the existing survey → map → submit sequence with the `gen_steps` budget.
5. Store the returned card as a new `HypothesisVersion`.
6. Rebuild the cognitive context from the existing startup pack plus the updated card/envelope.
7. Set phase back to `COGNITIVE` and continue the same batch decision process.

The runtime may retain the full cognitive transcript in `ProposerRuntimeState` for telemetry and reproducibility, but that transcript must not be passed to the regeneration model call.

- [ ] **Step 4: Preserve the current batch feedback contract.**

Do not add a new required action field in this migration. Preserve the current `feedback_generator` payload and current lane-level callback semantics. The regenerated card is recorded as the current replacement candidate in the runtime state and is presented back to the cognitive phase for re-audit. If a later change needs explicit per-card targeting, it must be a separate action-protocol design rather than an incidental field addition here.

- [ ] **Step 5: Add telemetry for phase transitions and versions.**

Extend the existing proposer trace, without changing authoritative history schema, with:

```json
{
  "phase_transitions": [
    {"from": "generate", "to": "cognitive"},
    {"from": "cognitive", "to": "regenerate"},
    {"from": "regenerate", "to": "cognitive"}
  ],
  "hypothesis_versions": 2,
  "regenerations": 1
}
```

- [ ] **Step 6: Run all focused runtime and phase tests.**

Run:

```bash
pytest -q tests/test_proposer_runtime.py tests/test_cognitive_element.py tests/test_generator.py
```

Expected: PASS.

## Task 4: Rewire the Orchestrator to One Proposer Runtime per Lane

**Files:**
- Modify: `simpleloop/roles/orchestrator.py`
- Modify: `tests/test_orchestrator.py`
- Modify: `tests/test_parallel_candidates.py`

**Interfaces:**
- `ProposerOrchestrator.run(...)` keeps its current public signature.
- `ProposerOrchestrator` owns one model/runtime configuration and creates one lane-local `ProposerAgent` execution state per `_run_one_lane` call.
- `_run_one_lane(...)` invokes exactly one unified proposer entry point and returns the existing `LaneResult` shape.

- [ ] **Step 1: Update failing orchestrator tests to assert one runtime call per lane.**

Replace separate fake `GeneratorAgent` and `research_batch` mocks with a fake unified proposer exposing:

```python
def run_lane(self, *, gen_context, ..., gen_steps, cognitive_steps):
    return BranchResult(...)
```

Assert that for two lanes there are two runtime calls, no `GeneratorAgent` object is constructed, and all generated cards/proposals still follow the existing funnel quotas.

- [ ] **Step 2: Run the updated orchestrator tests and verify failure.**

Run:

```bash
pytest -q tests/test_orchestrator.py
```

Expected: FAIL because `_run_one_lane` still calls `self.generator.run()` followed by `self.proposer.research_batch()`.

- [ ] **Step 3: Remove the two-agent construction and callback.**

In `ProposerOrchestrator.__init__`, remove `GeneratorAgent` construction. Keep a unified `ProposerAgent` configuration. In `_run_one_lane`, pass the lane's history-free `gen_context`, source paths, memory service, quotas, and budgets to the unified runtime exactly once. Delete the nested `generator_regenerate` callback; regeneration is now internal to the proposer runtime.

- [ ] **Step 4: Preserve lane isolation and concurrency.**

Do not share mutable `ProposerRuntimeState` between lanes. Each `_run_one_lane` invocation must own its cards, versions, messages, phase, evidence, and regeneration count. The shared `MemoryService` remains read-only during proposer execution. Keep `_MAX_LANE_WORKERS`, stable result ordering, and current error-to-`LaneResult(outcome="error")` behavior.

- [ ] **Step 5: Preserve lane trace fields and add phase data.**

Keep `lane_id`, `assigned_ops`, `all_cards`, `sig`, `outcome`, proposal counts, evidence refs, tool calls, and partial status. Add the unified runtime's phase transition and regeneration fields without removing existing keys.

- [ ] **Step 6: Run orchestrator and parallel-candidate tests.**

Run:

```bash
pytest -q tests/test_orchestrator.py tests/test_parallel_candidates.py
```

Expected: PASS, including duplicate signatures, frozen-region handling, blocked lanes, abstention, lane trace, feedback budget, and parallel candidate behavior.

## Task 5: Align Prompts with the Unified Runtime Without Changing Actions

**Files:**
- Modify: `simpleloop/prompts/generator.md`
- Modify: `simpleloop/prompts/proposer.md`
- Modify: `tests/test_prompt_templates.py`
- Modify: `tests/test_generator.py`
- Modify: `tests/test_cognitive_element.py`

**Interfaces:**
- Prompt files continue to define the same action schemas and role-specific semantics.
- The runtime, not the prompt, enforces phase action availability and context visibility.

- [ ] **Step 1: Add failing prompt assertions for the new identity contract.**

Assert that the generator semantic says it is the generation phase of one Proposer runtime, and the cognitive semantic says `feedback_generator` returns to the same runtime's regeneration phase. Assert that neither prompt instructs the model to call or hand off to another Agent object.

- [ ] **Step 2: Update only identity and phase language.**

Keep the generator's survey → map → batch hypothesis instructions, generative basis, facts requirements, and `submit_hypothesis` schema. Keep the cognitive Sieve → Select → Enrich flow and current block reasons. Replace separate-partner wording with:

```text
You are one Proposer runtime operating in a phase-specific context.
The initial generation phase cannot see history.
The cognitive phase can inspect history.
feedback_generator requests a history-conditioned regeneration view.
```

- [ ] **Step 3: Run prompt and parser tests.**

Run:

```bash
pytest -q tests/test_prompt_templates.py tests/test_generator.py tests/test_cognitive_element.py
```

Expected: PASS.

## Task 6: Migrate Integration Mocks, Remove Dead Boundaries, and Verify the Full Repository

**Files:**
- Modify: `tests/test_plot.py`
- Modify: `tests/test_telemetry.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `simpleloop/roles/__init__.py` only if it exports the removed runtime class.
- Modify: `simpleloop/roles/orchestrator.py`, `simpleloop/roles/proposer.py`, and `simpleloop/roles/generator.py` for dead imports/docstrings.

**Interfaces:**
- Existing public loop entry point remains `ProposerOrchestrator.run(...)`.
- Existing `ProposerResult`, `BranchResult`, `LaneResult`, and history schema remain compatible.

- [ ] **Step 1: Search for stale two-agent references.**

Run:

```bash
rg -n "GeneratorAgent|generator_regenerate|self\.generator|self\.proposer\.research_batch|cognitive partner|separate agent" simpleloop tests docs
```

Expected after cleanup: only deliberate compatibility/documentation references remain; no production orchestrator path constructs or invokes two proposer agents.

- [ ] **Step 2: Update integration fakes to the unified entry point.**

Each fake proposer must implement the public `run(...)` or lane runtime entry point used by the production object. Keep test assertions about proposal counts, telemetry, plotting, failure handling, and candidate scheduling unchanged unless they directly assert the removed internal boundary.

- [ ] **Step 3: Run focused integration tests.**

Run:

```bash
pytest -q tests/test_plot.py tests/test_telemetry.py tests/test_runtime.py tests/test_parallel_candidates.py
```

Expected: PASS.

- [ ] **Step 4: Run compilation and the complete test suite.**

Run from `SimpleLoop/`:

```bash
python -m compileall -q simpleloop
pytest -q tests
```

Expected: compilation succeeds and the complete test suite passes.

- [ ] **Step 5: Run a repository-level stale-reference check.**

Run:

```bash
rg -n "GeneratorAgent|generator_regenerate|self\.generator|self\.proposer\.research_batch" simpleloop
```

Expected: no production runtime references to the removed two-agent orchestration boundary.

## Verification Criteria

The implementation is complete only when all of the following are true:

1. One lane invokes one unified `ProposerAgent` runtime, not a GeneratorAgent followed by a ProposerAgent.
2. Initial batch generation produces the same number and shape of hypothesis cards as before.
3. Generation model turns do not receive dashboard, frontier, explore, Finding, or experiment-history content.
4. Cognitive model turns receive the existing startup pack and preserve current batch selection/enrichment semantics.
5. `feedback_generator` uses the same runtime state, but regeneration receives a scoped generation view rather than the full cognitive transcript.
6. Regeneration creates a new hypothesis version, returns to cognitive audit, and stops after `_MAX_REGENERATIONS`.
7. Current action names, required payloads, proposal result contracts, lane parallelism, and history schema remain compatible.
8. Phase transitions and hypothesis versions are observable in the non-authoritative trace.
9. Existing Harness, Executor, candidate selection, and outer Loop behavior is unchanged.
10. `python -m compileall -q simpleloop` and `pytest -q tests` pass.

