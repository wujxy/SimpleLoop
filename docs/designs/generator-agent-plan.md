# Generator Agent: make the Generator a code-reading agent

## Problem

The Generator is a single `model.complete()` call — it cannot read code. It
receives only `objective/gates/editable_paths/base_sha` (glob patterns, not a
file listing), so it guesses file names from general knowledge. In the log
(`omilrec-v100-generator-proposer-008-continue-4.log`), Lane 2 guessed
`OMILRECV2/src/Rec/OMILREC.cc` (doesn't exist; real file is
`OMILRECV2/src/OMILRECV2.cc`). The Cognitive side misused `feedback_generator`
to send the file-name error back — but file names are source facts, not
history, and fixing them is the Cognitive side's enrich job, not feedback.

Root cause: the Generator should be an **agent** that can read code (via
`run_research_command` on the parent-sha source tree), not a single LLM call.
It reuses the Cognitive side's tool loop (`_step` + `ResearchTools`) — the
only differences are prompt (generator.md, G-angle reasoning) and context
(history-free: no dashboard/frontier/exhausted list). The terminal action is
`submit_hypothesis` (one card), not `submit_proposals`/`block`.

## Design

### Shared infrastructure (extract, don't duplicate)

The Cognitive side's tool loop lives in `proposer.py`:
- `_step()` — one model turn with protocol repair (lines 1000-1070)
- `ResearchTools` (from `research_tools.py`) — `run_research_command` + memory tools
- `WorkingState` — per-branch state (counts, evidence, action_log, ...)
- Guard/validation helpers (`_validate_action_guard`, `_fingerprint`, ...)

**Extract a shared base class `ResearchAgent`** into a new module
`simpleloop/roles/research_agent.py` containing:
- `WorkingState` (moved from proposer.py)
- `ResearchAgent.__init__(model, runtime, timeout_seconds, max_steps, command_timeout_seconds, command_output_cap_chars, usage_observer)`
- `ResearchAgent._step(state, messages, system_prompt, deadline, usages, step_label)` — the model-turn + protocol-repair loop, **parameterized by an action parser callback** so each subclass plugs its own `_parse_action`
- Shared helpers: `_bump`, `_truncate`, `_fingerprint`, `_register_evidence`, `_render_state_header`, `_build_telemetry`, `_build_trace`, `_action_summary`, `_result_summary`
- The `TemporaryDirectory` + `ResearchTools` setup (shared by both agents)

`ProposerAgent` and `GeneratorAgent` both inherit `ResearchAgent` and provide:
- Their own `system_prompt` (from `load_semantic('proposer'|'generator')`)
- Their own `_parse_action` (terminal actions differ)
- Their own `_validate_action_guard` (Cognitive has block-evidence guard; Generator has none)
- Their own terminal-action handling in the loop

### Generator agent (`generator.py`)

`GeneratorAgent(ResearchAgent)` with:

**`run(context, source_path, repo_path, run_dir, prompt_dir, assigned_ops, max_steps)`**:
- system_prompt = `load_semantic('generator')` + runtime protocol (tool schemas + `submit_hypothesis` action schema)
- messages = `[{"role":"user","content": context}]` (history-free context)
- Tool loop (≤ max_steps): each step, model calls `_step()` → parse action:
  - `run_research_command` → execute via `ResearchTools`, append result, continue
  - `submit_hypothesis` → parse card, return `GenerationResult(cards=[card])`
  - (no `block`, no `submit_proposals`, no memory tools — the prompt doesn't mention them)
- Budget exhausted without `submit_hypothesis` → return best card from last model reply (partial, never abandon)

**`regenerate(context, feedback, transcript, source_path, repo_path, run_dir, prompt_dir, assigned_ops, max_steps)`**:
- Same as `run` but messages start with `context + transcript + feedback_text`
- Same tool loop — the Generator can read code again to find a real region for the new hypothesis

### Action parsing for Generator

Add `submit_hypothesis` to the Generator's action parser:
```json
{"action":"submit_hypothesis",
 "hypothesis":{"generative_op":"G6","region":"...","mechanism":"...",
  "intervention_family":"...","why_plausible":"...","critical_unknown":"..."}}
```
This reuses the existing `HypothesisCard` parsing logic from `_parse_hypotheses`.

The Generator does NOT parse `submit_proposals`, `block`, or
`feedback_generator` — those are the Cognitive side's actions. It DOES parse
`run_research_command` (shared tool). Memory tools
(`inspect_episode`/`list_findings`/`search_findings`/`search_experiments`) are
not in the Generator's prompt — it has no history to query.

### Prompt changes (`generator.md`)

1. **"What you produce"**: change from "return a JSON object with hypotheses
   array" to "use `run_research_command` to read the source tree, then
   `submit_hypothesis` with one card". The card schema stays the same.

2. **"Your context"**: change "you do NOT receive history" to "you do NOT
   receive history (no dashboard, no frontier, no exhausted list). You CAN
   read the source tree via `run_research_command` — use it to find real
   files and functions before submitting."

3. **"Rules"**: remove "Do not check the code" (it can now check). Replace
   with "Read the source to find the real region before you
   `submit_hypothesis`." Keep "Be unverified" in the sense that the
   hypothesis is still a lead (not a proposal), but the region should be a
   real file. Keep the G-angle reasoning rule.

4. **"Regeneration"**: add "you can read the source again to find a real
   region for the new hypothesis."

### Orchestrator changes (`orchestrator.py`)

1. `GeneratorAgent` construction in `__init__`: pass `runtime`,
   `command_timeout_seconds`, `command_output_cap_chars` (same as
   `ProposerAgent`).

2. `_run_one_lane`: pass `source_path`, `repo_path`, `run_dir` to
   `generator.run()` and `generator.regenerate()`. These are already
   available in the lane's kwargs.

3. `generator_regenerate` callback: pass `source_path`, `repo_path`,
   `run_dir` to `generator.regenerate()`.

4. Generator step budget: add `_GEN_STEPS = 5` (or derive from
   `max_branch_steps`) — the Generator needs fewer steps than the Cognitive
   side (it just finds a region, doesn't enrich).

### Cognitive side prompt fix (`proposer.md`)

Add a section explaining the Generator is a code-reading agent that sometimes
gets surface details wrong. The Cognitive side fixes file names during
enrich — it does NOT block on wrong file names (that's not `false_claim` —
the mechanism isn't refuted, just the path), and does NOT use
`feedback_generator` for source facts (only for history evidence).

Narrow `false_claim`: "the hypothesis's **mechanism** is refuted by the code"
(not "the hypothesis asserts a fact about the code that the code refutes" —
a wrong file name is a surface error, not a mechanism refutation).

Clarify `feedback_generator` scope: "history (Ledger, Findings, prior
experiments — NOT the source tree)".

## Files changed

| File | Change |
|------|--------|
| `simpleloop/roles/research_agent.py` | **NEW**: shared `ResearchAgent` base class, `WorkingState`, shared helpers extracted from proposer.py |
| `simpleloop/roles/generator.py` | Rewrite: `GeneratorAgent(ResearchAgent)` with tool loop, `submit_hypothesis` action, `run()` and `regenerate()` as agent methods |
| `simpleloop/roles/proposer.py` | `ProposerAgent(ResearchAgent)` — inherit from base, remove duplicated helpers, keep cognitive-specific terminal actions (`submit_proposals`/`block`/`feedback_generator`) |
| `simpleloop/roles/orchestrator.py` | Pass `runtime`/`source_path`/`repo_path`/`run_dir`/`command_*` to generator; add generator step budget |
| `simpleloop/prompts/generator.md` | Agent prompt: `run_research_command` to read source, `submit_hypothesis` terminal, keep G-angle deep reasoning |
| `simpleloop/prompts/proposer.md` | Generator-is-agent section, narrow `false_claim`, clarify `feedback_generator` = history only |
| `tests/test_generator.py` | Rewrite: test agent tool loop, `submit_hypothesis` action, code-reading behavior |
| `tests/test_orchestrator.py` | Update: generator now needs `source_path`/`repo_path`/`run_dir` |
| `tests/test_cognitive_element.py` | Update: `WorkingState` moved to `research_agent.py` |

## What stays the same

- `HypothesisCard` dataclass and its parsing
- G1-G9 generative basis and `_replace_basis` / `_sample_generative_ops`
- `ResearchTools` and `ResearchCommandRunner` (the tool implementations)
- The 1:1 lane architecture in orchestrator.py
- The `feedback_generator` mechanism (cognitive → generator regeneration)
- `_MAX_REGENERATIONS = 3` budget
- `candidates_per_round` as sole width
- The Cognitive side's Sieve → Enrich → submit|block protocol
- All existing Cognitive side behavior (it already works well)

## Execution order

1. Extract `research_agent.py` (shared base + helpers moved from proposer.py)
2. Make `ProposerAgent` inherit from `ResearchAgent` (verify cognitive tests pass)
3. Rewrite `GeneratorAgent` as agent inheriting `ResearchAgent`
4. Update `orchestrator.py` to pass paths/runtime to generator
5. Update `generator.md` prompt (agent-style, code-reading)
6. Update `proposer.md` prompt (false_claim narrowing, feedback scope)
7. Rewrite tests for generator agent
8. Run full test suite
