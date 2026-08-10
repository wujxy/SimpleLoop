# Scientist-Proposer vNext Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前 Generator → Cognitive 双 Agent proposer 改造成每 lane 一个 Scientist runtime，使其按 `UNDERSTAND → MODEL → EXPLAIN → FRESH EXPLORE → HISTORY INJECTION → NARROW → DEEPEN → PROPOSE` 完成单轮 inquiry，并用 proposer-only trace 验证认知路径。

**Architecture:** `ProposerOrchestrator` 只负责 lane、quota、并发和稳定聚合；每个 lane 创建一个独立 `ProposerAgent`，以 `ScientistSessionState(runtime=WorkingState(), inquiry=InquiryState())` 驱动同一消息上下文。Fresh 阶段同时在 prompt、action guard、memory façade 和 Apptainer bind 四层锁住 search history；portfolio commit 后在同一 context 单调开启 history。只有 `fresh_reframe` 能创建新的 clean context，其他 evidence conflict 按 direction/explanation/model 层级回退。

**Tech Stack:** Python 3.11、dataclasses、Enum、现有 `ResearchAgent` / `ChatModel` / `MemoryService` / Apptainer runtime、pytest；不增加依赖。

## Global Constraints

- `docs/simpleloop_scientist_proposer_inquiry_research.md` 与 `docs/simpleloop_proposer_vnext_implementation_plan.md` 是本计划的已批准设计输入。
- 本计划取代 `docs/superpowers/plans/2026-08-10-proposer-runtime-merge.md`；旧计划保留 `Sieve → Enrich`、`feedback_generator` 和 generator regeneration，不得用于实施 vNext。
- 不修改 `.env`、凭据、密钥、生产 endpoint 或 Candidate Worker / Gate / objective selection 的职责。
- 不做跨-round persistent Scientist、Reviewer/Critic、proposal merit scorer、Research Tree 或复杂 belief store。
- Fresh 阶段可见 current-world evidence，但不可见 previous proposals、Ledger、Findings、Frontier、prior outcomes 或旧 search vocabulary。
- 同一个 LLM context 的 history visibility 只允许 `False → True`；`fresh_reframe` 必须创建新 context，且第一版最多一次。
- Prompt-visible actions、parser-accepted actions、guard-allowed actions 与 runtime-executable actions 必须完全一致。
- Working Model 必须支持 counterfactual；Explanation 必须引用 model claims；Hypothesis 必须引用 model/explanation；Proposal 必须引用 selected hypothesis、deep evidence 和 prediction。
- `max_steps` 是总保险丝，不按 phase 固定切片；budget exhaustion 无 valid proposal 时返回 `research_incomplete`，不得自动制造 partial proposal。
- 保留 G1–G9、每 lane 随机 5-of-9、lane quota、lane 并发、stable aggregation、Worker/Harness 接口和历史 run 可审计性。
- Production prompt 不得写入 EventContext、state lifetime、ownership、FCN call topology 等 OMILREC 期望答案。
- 所有行为修改先写失败测试，再写最小实现；每个 task 完成后运行列出的 focused tests。

---

## File Map

- Create: `simpleloop/roles/inquiry.py` — phase、artifact、lineage、context-local visibility 与 session state。
- Modify: `simpleloop/container/runtime.py` — `history=None` 时完全不产生 history bind。
- Modify: `simpleloop/roles/research_tools.py` — phase-aware tool prompt、history-enabled façade 与 runtime rejection。
- Modify: `simpleloop/roles/research_agent.py` — 继续只承载 generic loop；支持显式 tools/view，补充 phase usage/evidence telemetry hook。
- Modify: `simpleloop/memory/context.py` — 分离 fresh world context 与 history-entry pack。
- Modify: `simpleloop/memory/service.py` — 暴露 `build_history_entry_pack()` façade。
- Modify: `simpleloop/memory/models.py` — 向 `ResearchProposal` 增加 backwards-compatible lineage metadata。
- Modify: `simpleloop/roles/generator.py` — 只保留 G1–G9 常量和纯 prompt basis helper；移除 production `GeneratorAgent`。
- Modify: `simpleloop/roles/hypothesis.py` — 保留 legacy `HypothesisCard` 审计兼容，不再作为 production breadth 模型。
- Rewrite: `simpleloop/roles/proposer.py` — Scientist action parser、guards、phase loop、history injection、rollback、fresh reframe、trace。
- Rewrite: `simpleloop/roles/orchestrator.py` — lane-local Scientist factory、deterministic op sampling、stable aggregation。
- Modify: `simpleloop/config.py` — `scientist_steps` 与旧预算兼容映射。
- Modify: `simpleloop/loop.py` — 只向 Orchestrator 传 Scientist budget。
- Modify: `scripts/proposer_harness.py` — 保存 structured Scientist trace、seed/config/model/source 元数据。
- Modify: `simpleloop/prompts/__init__.py` — 区分 active 三 prompt 与 legacy-loadable generator prompt。
- Rewrite: `simpleloop/prompts/proposer.md` — 唯一 active Scientist identity，不含 phase checklist 或 OMILREC leakage。
- Modify: `simpleloop/self_improvement/history.py`, `gate.py` — 新 snapshot 使用三 prompt，旧四 prompt snapshot 仍可读取/恢复。
- Create: `tests/test_scientist_proposer.py` — phase、artifact、lineage、rollback、budget、trace 的主测试。
- Modify: `tests/test_runtime.py`, `tests/test_research_tools.py` — 真实 history bind lock。
- Modify: `tests/test_generator.py`, `tests/test_cognitive_element.py`, `tests/test_orchestrator.py` — 迁移旧架构测试，删除假兼容断言。
- Modify: `tests/test_config_execution.py`, `tests/test_parallel_candidates.py`, `tests/test_proposer_harness.py`, `tests/test_proposer_harness_runner.py`, `tests/test_proposer_cli.py`, `tests/test_prompt_templates.py`, `tests/test_prompt_self_improvement.py` — integration 与兼容测试。

---

## Commit Checkpoints

After each code-changing task passes its focused tests, commit only that task with the following message; Task 0 and Task 12 create ignored evaluation artifacts and are not committed.

| Task | Commit message |
|---|---|
| 1 | `feat: add scientist inquiry state` |
| 2 | `feat: enforce proposer history isolation` |
| 3 | `feat: split fresh and history context views` |
| 4 | `feat: add scientist phase protocol` |
| 5 | `feat: run fresh scientist inquiry phases` |
| 6 | `refactor: move generative basis into scientist explore` |
| 7 | `feat: add scientist narrowing and rollback` |
| 8 | `feat: export scientist proposal lineage traces` |
| 9 | `refactor: schedule one scientist runtime per lane` |
| 10 | `refactor: activate scientist proposer prompt` |
| 11 | `test: complete scientist proposer migration` |

For each checkpoint run `git diff --check`, inspect `git status --short`, and stage only the exact files named by that task; never stage the user-provided research/implementation documents or the superseded untracked plan unless the user explicitly asks.

---

### Task 0: Capture the Controlled Current Baseline Before Editing

**Files:**

- Read: `runs/omilrec-v100-generator-proposer-008/config.orig.yaml`
- Read: `runs/omilrec-v100-generator-proposer-008/repo/`
- Create (ignored run artifacts): `runs/proposer-vnext-baseline/current/seed-00/` through `seed-04/`

**Interfaces:**

- `scripts/proposer_harness.py --config ... --from-run ... --output-dir ... --seed N`
- Each output must contain `result.json` and `proposals.md` without invoking Executor or Gate。

- [ ] **Step 1: Record repository and baseline source identity.**

Run:

```bash
git status --short
git rev-parse HEAD
git -C runs/omilrec-v100-generator-proposer-008/repo rev-parse HEAD
```

Expected: record the SimpleLoop SHA, OMILREC source SHA, and the pre-existing untracked design documents; do not clean or stage user files.

- [ ] **Step 2: Run five current proposer-only seeds.**

Run the following five commands from `SimpleLoop/`:

```bash
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/current/seed-00 --seed 0
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/current/seed-01 --seed 1
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/current/seed-02 --seed 2
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/current/seed-03 --seed 3
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/current/seed-04 --seed 4
```

Expected: five completed result files with identical model/config/source SHA and distinct recorded seeds. If external model access is unavailable, stop before code edits and report baseline capture as the blocker.

- [ ] **Step 3: Verify baseline artifacts mechanically.**

Run:

```bash
jq -s 'map({status, seed:.input.seed, sha:.input.base_sha, proposals:(.proposals|length), trace_keys:(.trace|keys)})' runs/proposer-vnext-baseline/current/seed-*/result.json
```

Expected: five `status="completed"` entries; current traces expose old cards/lanes but no `working_model`, `explanations`, `history_injected_at_step`, or M→E→H→P lineage.

---

### Task 1: Add the Inquiry Domain Model and Monotonic Context State

**Files:**

- Create: `simpleloop/roles/inquiry.py`
- Create: `tests/test_scientist_proposer.py`

**Interfaces:**

- Produces `InquiryPhase`, `Understanding`, `ModelClaim`, `WorkingModel`, `Explanation`, `LeveragePoint`, `ResearchHypothesis`, `HypothesisSelection`, `PhaseTransition`, `InquiryState`, and `ScientistSessionState`.
- `ResearchHypothesis.signature()` returns `(mechanism_family, intervention_family, coarse_scope)` in canonical lowercase form.
- `InquiryState.set_history_visible(True, step=step)` is idempotent only after visibility is true and raises on attempted disable; each state belongs to one `context_id`.
- `ScientistSessionState.start_fresh_context()` archives the parent `InquiryState`, increments `context_id`, resets generic evidence/fingerprint state, and enforces `MAX_FRESH_REFRAMES = 1`.

- [ ] **Step 1: Write failing artifact and visibility tests.**

Add:

```python
def test_inquiry_starts_fresh_in_understand():
    session = ScientistSessionState.fresh()
    assert session.inquiry.phase is InquiryPhase.UNDERSTAND
    assert session.inquiry.context_id == 0
    assert session.inquiry.history_visible is False

def test_history_visibility_is_monotonic_per_context():
    state = InquiryState(context_id=0)
    state.set_history_visible(True, step=12)
    assert state.history_visible is True
    assert state.history_injected_at_step == 12
    with pytest.raises(ValueError, match="cannot be hidden"):
        state.set_history_visible(False, step=13)

def test_research_hypothesis_signature_is_not_path_centered():
    hypothesis = ResearchHypothesis(
        id="H1", generative_op="G2", model_basis=("M1",),
        explanation_basis=("E1",), mechanism="State Lifetime",
        intervention_family="Ownership Lift", scope="whole-system",
        why_plausible="shared state is rebuilt", critical_unknown="lifetime",
        evidence_refs=("source:src/a.cc",),
    )
    assert hypothesis.signature() == (
        "state lifetime", "ownership lift", "whole-system",
    )
```

- [ ] **Step 2: Run the new tests and verify failure.**

Run:

```bash
pytest -q tests/test_scientist_proposer.py -k 'inquiry or visibility or signature'
```

Expected: FAIL because `simpleloop.roles.inquiry` does not exist.

- [ ] **Step 3: Implement the minimal immutable artifacts and mutable state.**

Use these exact public shapes:

```python
class InquiryPhase(str, Enum):
    UNDERSTAND = "understand"
    MODEL = "model"
    EXPLAIN = "explain"
    EXPLORE = "explore"
    NARROW = "narrow"
    DEEPEN = "deepen"

@dataclass(frozen=True)
class Understanding:
    problem: str
    target_outcome: str
    boundary: str
    key_unknowns: tuple[str, ...]

@dataclass(frozen=True)
class ModelClaim:
    id: str
    claim: str
    evidence_refs: tuple[str, ...]

@dataclass(frozen=True)
class WorkingModel:
    representation: str
    explanatory_structure: str
    claims: tuple[ModelClaim, ...]
    important_unknowns: tuple[str, ...]

@dataclass(frozen=True)
class Explanation:
    id: str
    phenomenon: str
    claim: str
    model_basis: tuple[str, ...]
    expected_if_true: tuple[str, ...]
    evidence_needed: tuple[str, ...]

@dataclass(frozen=True)
class LeveragePoint:
    id: str
    target_mechanism: str
    why_leverage_exists: str
    model_basis: tuple[str, ...]
    explanation_basis: tuple[str, ...]

@dataclass(frozen=True)
class ResearchHypothesis:
    id: str
    generative_op: str | None
    model_basis: tuple[str, ...]
    explanation_basis: tuple[str, ...]
    mechanism: str
    intervention_family: str
    scope: str
    why_plausible: str
    critical_unknown: str
    evidence_refs: tuple[str, ...]
```

`InquiryState` contains the fields required by the spec plus `context_id`, `history_injected_at_step`, `narrow_decisions`, and `deep_evidence_refs`. `ScientistSessionState` composes the existing `WorkingState`, current `InquiryState`, and `archived_inquiries`; do not move generic counters into `InquiryState`.

- [ ] **Step 4: Implement state-only transition helpers.**

Add methods with no model/tool dependencies:

```python
def transition(self, target: InquiryPhase, *, step: int, reason: str) -> None:
    self.phase_transitions.append(PhaseTransition(
        context_id=self.context_id, source=self.phase,
        target=target, step=step, reason=reason,
        history_visible=self.history_visible,
    ))
    self.phase = target

def set_history_visible(self, visible: bool, *, step: int) -> None:
    if self.history_visible and not visible:
        raise ValueError("history cannot be hidden in the same context")
    if visible and not self.history_visible:
        self.history_visible = True
        self.history_injected_at_step = step
```

- [ ] **Step 5: Run tests and commit the pure model layer.**

Run:

```bash
pytest -q tests/test_scientist_proposer.py -k 'inquiry or visibility or signature'
```

Expected: PASS without importing model, memory, filesystem, or container modules from `inquiry.py` except `WorkingState` for `ScientistSessionState` composition.

---

### Task 2: Enforce the History Lock in Container, Tools, Prompt, and Execution

**Files:**

- Modify: `simpleloop/container/runtime.py`
- Modify: `simpleloop/roles/research_tools.py`
- Modify: `simpleloop/roles/research_agent.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_research_tools.py`

**Interfaces:**

- `ApptainerRuntime.research_exec_argv(..., history: str | Path | None, ...)` omits `/history.jsonl` and `/rounds` binds when `history is None`.
- `render_research_tool_prompt(allowed_actions: Collection[str])` renders only requested tool specs and rejects unknown actions.
- `ResearchTools(..., history_enabled: bool, history_dir: Path | None, memory_service)` enforces a consistent view.
- `ResearchTools.execute()` rejects every `MEMORY_TOOL_ACTIONS` call when history is disabled, even if a caller accidentally retained a memory object.
- `ResearchAgent._make_tools()` forwards the explicit history view; it does not infer visibility from `memory_service is None`.

- [ ] **Step 1: Add failing no-bind and memory-rejection tests.**

Add:

```python
def test_research_argv_without_history_has_no_history_bind(tmp_path):
    runtime = _make_runtime(tmp_path)
    argv = runtime.research_exec_argv(
        ["true"], source=tmp_path, repo=tmp_path,
        history=None, scratch=tmp_path, cwd="source",
    )
    assert not any("/history.jsonl" in arg for arg in argv)
    assert not any("/rounds" in arg for arg in argv)

def test_history_disabled_tools_reject_memory_actions(tmp_path):
    tools = _tools(tmp_path, memory=_FakeMemoryService(), history_enabled=False)
    result = tools.execute(
        {"action": "search_experiments", "query": "cache"},
        deadline=time.monotonic() + 10,
    )
    assert result == {"ok": False, "error": "history is not available in this phase"}

def test_tool_prompt_matches_allowed_actions():
    prompt = render_research_tool_prompt({"run_research_command"})
    assert "run_research_command" in prompt
    assert "search_experiments" not in prompt
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run:

```bash
pytest -q tests/test_runtime.py -k research_argv
pytest -q tests/test_research_tools.py -k 'history or prompt'
```

Expected: FAIL because history is required and all tools are always rendered/executable.

- [ ] **Step 3: Make the container history bind optional.**

Implement this branch before the existing source/repo/scratch binds:

```python
if history is not None:
    evidence = Path(history).resolve()
    history_file = evidence / "history.jsonl"
    rounds = evidence / "rounds"
    if history_file.is_file():
        argv.extend(["--bind", f"{history_file}:/history.jsonl:ro"])
    if rounds.is_dir():
        argv.extend(["--bind", f"{rounds}:/rounds:ro"])
```

Do not bind an empty directory as a substitute for no history.

- [ ] **Step 4: Make `ResearchTools` fail closed.**

Constructor invariants:

```python
if history_enabled and (history_dir is None or memory_service is None):
    raise ValueError("history-enabled tools require history_dir and memory_service")
if not history_enabled:
    history_dir = None
    memory_service = None
```

At the start of `execute()`:

```python
if name in MEMORY_TOOL_ACTIONS and not self.history_enabled:
    return {"ok": False, "error": "history is not available in this phase"}
```

- [ ] **Step 5: Filter prompt specs from the same action set.**

Implement:

```python
def render_research_tool_prompt(allowed_actions) -> str:
    allowed = frozenset(allowed_actions)
    known = {spec.action for spec in RESEARCH_TOOL_SPECS}
    unknown = allowed - known
    if unknown:
        raise ValueError(f"unknown research actions: {sorted(unknown)}")
    return "\n".join(
        f"- {spec.schema}\n  {spec.description}"
        for spec in RESEARCH_TOOL_SPECS if spec.action in allowed
    )
```

- [ ] **Step 6: Run focused tests.**

Run:

```bash
pytest -q tests/test_runtime.py tests/test_research_tools.py
```

Expected: PASS, including the existing history-enabled bind and memory dispatch tests.

---

### Task 3: Add Fresh and History-Entry Context Views

**Files:**

- Modify: `simpleloop/memory/context.py`
- Modify: `simpleloop/memory/service.py`
- Modify: `tests/test_memory.py`
- Modify: `tests/test_memory_service.py`

**Interfaces:**

- Rename only the semantics, not the serialized history: `build_generation_context()` remains as a compatibility façade for `build_fresh_inquiry_context()`.
- Produce `build_history_entry_pack(...) -> str` containing recent factual dashboard, abstentions, Explore/Frontier summaries, and memory-tool cheatsheet, but no instruction to submit immediately.
- Fresh context contains goal, gates, accepted SHA, editable/frozen paths, and hints when configured; it contains no dashboard/frontier/history wording.

- [ ] **Step 1: Write failing view-separation tests.**

Add:

```python
def test_fresh_inquiry_context_has_world_evidence_only():
    text = build_fresh_inquiry_context(
        goal="speed", editable=["src/**"], frozen=["tests/**"],
        base_sha="abc", gate_block="FCN=true", hints=["preserve order"],
    )
    assert "speed" in text and "FCN=true" in text
    assert "preserve order" in text
    assert "dashboard" not in text.lower()
    assert "frontier" not in text.lower()

def test_history_entry_pack_does_not_force_a_proposal():
    text = build_history_entry_pack(
        experiments=[], frontier={}, recent_abstentions=[],
        tool_cheatsheet="search_experiments(...)", explore=None,
    )
    assert "History is now available as evidence" in text
    assert "Submit between" not in text
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run:

```bash
pytest -q tests/test_memory.py tests/test_memory_service.py -k 'fresh or history_entry'
```

Expected: FAIL because the two explicit view builders do not exist.

- [ ] **Step 3: Extract the compact history-only renderer.**

Reuse `_render_dashboard`, `_render_abstentions`, `render_explore_for_startup`, `_render_frontier`, and `MEMORY_TOOL_CHEATSHEET`; do not duplicate Ledger parsing. End the pack with:

```text
History is now available as evidence. Use it to support, refute, or revise the independently formed model and hypotheses. Search it on demand; do not replace your representation with historical vocabulary.
```

- [ ] **Step 4: Add the `MemoryService` façade.**

Add:

```python
def build_history_entry_pack(self, *, current_round: int,
                             recent_rounds: int = 2,
                             explore: ExploreReport | None = None) -> str:
    history = read_history(self.history_path)
    experiments = build_experiments(history)
    findings = self.load_findings()
    frontier = compute_frontier(...)
    return build_history_entry_pack(
        experiments=experiments, frontier=frontier,
        recent_abstentions=_recent_abstentions(history, recent_rounds),
        recent_rounds=recent_rounds,
        tool_cheatsheet=MEMORY_TOOL_CHEATSHEET,
        explore=explore,
    )
```

Extract `_recent_abstentions()` once and reuse it from the legacy startup pack.

- [ ] **Step 5: Run context/service tests.**

Run:

```bash
pytest -q tests/test_memory.py tests/test_memory_service.py tests/test_views_and_parse.py
```

Expected: PASS; existing startup-pack behavior remains available for historical tests, but vNext will not use it before NARROW.

---

### Task 4: Implement the Scientist Action Grammar, Phase Policy, and Artifact Guards

**Files:**

- Modify: `simpleloop/roles/proposer.py`
- Modify: `tests/test_scientist_proposer.py`

**Interfaces:**

- `phase_allowed_actions(phase, history_visible) -> frozenset[str]` is the single policy source.
- `_parse_scientist_action(text) -> dict` parses all phase actions without deciding whether the current phase permits them.
- `_validate_scientist_guard(session, action, source_root, select_quota) -> str | None` enforces phase, lineage, breadth, history, selection, and reframe constraints.
- `_build_phase_system_prompt(prompt_dir, phase, history_visible) -> str` combines one identity, one short attention block, and only the allowed action/tool schemas.

- [ ] **Step 1: Add failing phase-policy and parser tests.**

Cover the exact action sets:

```python
FRESH_ACTIONS = {
    InquiryPhase.UNDERSTAND: {"run_research_command", "commit_understanding"},
    InquiryPhase.MODEL: {"run_research_command", "propose_working_model",
                         "continue_investigation", "commit_working_model"},
    InquiryPhase.EXPLAIN: {"run_research_command", "submit_explanation",
                           "commit_explanation_set", "reopen_model"},
    InquiryPhase.EXPLORE: {"run_research_command", "emit_lever_map",
                           "submit_hypothesis", "commit_hypothesis_portfolio",
                           "reopen_explain", "reopen_model"},
}
```

NARROW adds memory actions plus `select_for_deepen`, `continue_explore`, `reopen_explain`, `reopen_model`, `fresh_reframe`, `abandon_portfolio`; DEEPEN adds memory actions plus `submit_proposals`, `return_to_narrow`, `continue_explore`, `reopen_explain`, `reopen_model`, `fresh_reframe`, `abandon_direction`.

`phase_allowed_actions()` unions `{"block"}` into every phase. `block` keeps the existing exact payload `{"action":"block","reason_kind":"false_claim|frozen|contradiction","explanation":"...","evidence_refs":["source:path"]}` and remains terminal only when at least one cited source path was read in the current context; it is not a merit/ROI abstention.

- [ ] **Step 2: Add failing lineage and commit-gate tests.**

Add assertions that:

```python
assert guard(commit_model_with_remaining_unknown) == "model_unknown_blocks_commit"
assert guard(explanation_referencing_missing_M9) == "unknown_model_claim"
assert guard(hypothesis_referencing_missing_E9) == "unknown_explanation"
assert guard(proposal_for_unselected_H2) == "proposal_requires_selected_hypothesis"
assert guard(submit_proposal_in_narrow) == "action_not_allowed_in_narrow"
```

Also test one explanation may commit only with a non-empty `single_explanation_justification`; otherwise the committed set contains at least two distinct explanation IDs.

- [ ] **Step 3: Run focused tests and verify failure.**

Run:

```bash
pytest -q tests/test_scientist_proposer.py -k 'policy or parser or guard or lineage'
```

Expected: FAIL because the Scientist grammar does not exist.

- [ ] **Step 4: Implement exact commitment schemas.**

Use these terminal/commit payloads:

```json
{"action":"commit_understanding","problem":"...","target_outcome":"...","boundary":"...","key_unknowns":["..."]}
{"action":"continue_investigation","question":"...","decision_impact":"..."}
{"action":"commit_explanation_set","explanation_ids":["E1","E2"],"single_explanation_justification":null}
{"action":"commit_hypothesis_portfolio","hypothesis_ids":["H1","H2","H3","H4"],"coverage_rationale":"...","unused_generative_ops":[]}
{"action":"select_for_deepen","selected":[{"hypothesis_id":"H2","evidence_refs":["experiment:r3c0"],"rationale":"..."}]}
{"action":"propose_working_model","working_model":{"representation":"...","explanatory_structure":"...","claims":[{"id":"M1","claim":"...","evidence_refs":["source:path"]}],"important_unknowns":["..."]}}
{"action":"commit_working_model","working_model":{"representation":"...","explanatory_structure":"...","claims":[{"id":"M1","claim":"...","evidence_refs":["source:path"]}],"important_unknowns":[]},"model_check":{"explains_target":"...","counterfactual":{"change":"...","predicted_effect":"...","model_claim_refs":["M1"]},"remaining_decision_changing_unknown":null,"why_ready":"..."}}
{"action":"submit_explanation","id":"E1","phenomenon":"...","claim":"...","model_basis":["M1"],"expected_if_true":["..."],"evidence_needed":["..."]}
{"action":"emit_lever_map","levers":[{"id":"L1","target_mechanism":"...","why_leverage_exists":"...","model_basis":["M1"],"explanation_basis":["E1"]}]}
{"action":"submit_hypothesis","id":"H1","generative_op":"G2","model_basis":["M1"],"explanation_basis":["E1"],"mechanism":"...","intervention_family":"...","scope":"...","why_plausible":"...","critical_unknown":"...","evidence_refs":["source:path"]}
{"action":"submit_proposals","proposals":[{"instruction":"...","research_target":{"mode":"new","question":"...","mechanisms":[],"code_regions":[]},"model_claim_refs":["M1"],"explanation_refs":["E1"],"hypothesis_id":"H1","evidence_refs":["source:path"],"mechanism":"...","prediction":"...","affected_scope":"..."}]}
{"action":"continue_explore","reason":"...","evidence_refs":["experiment:r3c0"]}
{"action":"reopen_explain","reason":"...","evidence_refs":["experiment:r3c0"]}
{"action":"reopen_model","reason":"...","evidence_refs":["experiment:r3c0"]}
{"action":"return_to_narrow","reason":"...","evidence_refs":["source:path"]}
{"action":"fresh_reframe","reason":"...","evidence_refs":["experiment:r3c0"]}
{"action":"abandon_portfolio","reason":"...","evidence_refs":["experiment:r3c0"]}
{"action":"abandon_direction","hypothesis_id":"H1","reason":"...","evidence_refs":["source:path"]}
```

`commit_working_model` uses the exact `working_model` + `model_check` schema from the implementation document. `model_check.remaining_decision_changing_unknown` must be `null` to transition.

- [ ] **Step 5: Implement minimal lineage guards.**

The guard checks IDs and evidence availability, not scientific truth:

```python
model_ids = {claim.id for claim in session.inquiry.working_model.claims}
explanation_ids = {item.id for item in session.inquiry.explanations}
hypothesis_ids = {item.id for item in session.inquiry.hypotheses}
```

Require `min_distinct_hypotheses = max(4, select_quota * 2)` distinct `ResearchHypothesis.signature()` values at portfolio commit. Treat 5 lenses × 2 ideas as a target/budget, never an exact card quota.

- [ ] **Step 6: Build phase prompts from the same policy.**

`_build_phase_system_prompt()` must render:

```python
identity = load_semantic("proposer", prompt_dir)
attention = PHASE_ATTENTION[phase]
actions = phase_allowed_actions(phase, history_visible)
tools = render_research_tool_prompt(actions & RESEARCH_ACTIONS)
protocol = render_scientist_action_protocol(actions)
return "\n\n".join((identity, attention, protocol, tools, RUNTIME_BOUNDARIES))
```

Do not keep global `_PROTOCOL_BLOCK`, `_PARTIAL_SUBMIT_REMINDER`, `select_for_enrich`, `feedback_generator`, or Sieve terminology.
Use this exact `PHASE_ATTENTION` mapping (with `NARROW` and `DEEPEN` wording from the approved implementation document):

```python
PHASE_ATTENTION = {
    InquiryPhase.UNDERSTAND: "Current mode: UNDERSTAND. Do not search for modifications yet. Investigate the problem broadly enough to understand what is being changed or explained, how the target outcome arises, and which unknowns could change your later model.",
    InquiryPhase.MODEL: "Current mode: MODEL. Construct a working representation that can explain the target outcome and support counterfactual reasoning. A list of components or facts is not sufficient.",
    InquiryPhase.EXPLAIN: "Current mode: EXPLAIN. Explain why the current gap or phenomenon occurs. Keep materially different explanations alive where evidence permits. Do not design the intervention yet.",
    InquiryPhase.EXPLORE: "Current mode: EXPLORE. Using the working model and explanations, search broadly across materially different mechanism families before investing deeply in any one direction.",
    InquiryPhase.NARROW: "Current mode: NARROW. Past experiments are now available as evidence. Use them to support, refute, or revise the independently formed model and hypotheses. Historical vocabulary must not replace your own representation.",
    InquiryPhase.DEEPEN: "Current mode: DEEPEN. Detailed investigation is now justified. Test each selected hypothesis critical premise, trace its real scope, derive observable consequences, and submit only if the mechanism survives.",
}
```

- [ ] **Step 7: Run focused parser/guard tests.**

Run:

```bash
pytest -q tests/test_scientist_proposer.py -k 'policy or parser or guard or lineage'
```

Expected: PASS.

---

### Task 5: Implement Fresh Inquiry Through Portfolio Commitment

**Files:**

- Rewrite: `simpleloop/roles/proposer.py`
- Modify: `simpleloop/roles/research_agent.py`
- Modify: `tests/test_scientist_proposer.py`

**Interfaces:**

- Produces `ScientistResult` with `proposals`, `outcome`, `reason`, `usage`, `deliberation_telemetry`, and `trace`.
- `ProposerAgent.run_lane(..., assigned_ops: tuple[str, ...], select_quota: int, scientist_steps: int) -> ScientistResult` is the only lane-local production entry point.
- Fresh phases use one `messages` list and one `InquiryState(context_id=0)`; phase transitions are action-driven, not step-driven.
- Portfolio commit switches tools and appends history in the same messages context exactly once.

- [ ] **Step 1: Write a failing synthetic full-fresh-cycle test.**

Feed a fake model this sequence:

```text
run_research_command
commit_understanding
propose_working_model
run_research_command
commit_working_model
submit_explanation E1
submit_explanation E2
commit_explanation_set
emit_lever_map
submit_hypothesis H1..H4
commit_hypothesis_portfolio
```

Assert the same message list is used, transitions are `understand→model→explain→explore→narrow`, and `history_injected_at_step` equals the portfolio commit step.

- [ ] **Step 2: Add a failing early-history test.**

Capture every `ResearchTools` construction and assert before portfolio commit:

```python
assert tools.history_enabled is False
assert tools.memory is None
assert tools.command_runner.history_dir is None
assert "search_experiments" not in system_prompt
```

After commit, assert all four invert together and `history_visible is True`.

- [ ] **Step 3: Run focused tests and verify failure.**

Run:

```bash
pytest -q tests/test_scientist_proposer.py -k 'fresh_cycle or early_history or injection'
```

Expected: FAIL because `run_lane()` is absent.

- [ ] **Step 4: Implement one total-budget loop.**

Use this control shape:

```python
for step in range(1, scientist_steps + 1):
    phase = session.inquiry.phase
    system = self._build_phase_system_prompt(prompt_dir, phase,
                                             session.inquiry.history_visible)
    action, reply_text = self._step(
        session.runtime, messages, system, deadline, usages, step,
        source_root=source_path, steps_budget=scientist_steps,
    )
    terminal = self._apply_action(...)
    if terminal is not None:
        return terminal
return self._research_incomplete(session, usages, scientist_steps)
```

Do not create one model session per phase. Recreate `ResearchTools` only when visibility changes or a fresh reframe creates a new context.

- [ ] **Step 5: Implement artifact application and transitions.**

Commit behavior:

- `commit_understanding` stores `Understanding`, transitions to MODEL。
- `propose_working_model` stores a provisional model without transition。
- `commit_working_model` replaces it after guard success, transitions to EXPLAIN。
- `submit_explanation` upserts by ID; `commit_explanation_set` freezes the selected set and transitions to EXPLORE。
- `emit_lever_map` replaces the current mechanism map; `submit_hypothesis` upserts by ID。
- `commit_hypothesis_portfolio` filters to committed IDs, enables history, appends `MemoryService.build_history_entry_pack()`, swaps to history-enabled tools, transitions to NARROW。

- [ ] **Step 6: Preserve generic telemetry while adding phase usage.**

Record per accepted action:

```python
session.runtime.action_log.append({
    "context_id": session.inquiry.context_id,
    "phase": phase.value,
    "action": action["action"],
    "step": step,
})
```

Aggregate usage events into `usage_by_phase[phase.value]`; do not expose hidden reasoning or full model chain-of-thought.

- [ ] **Step 7: Run fresh-cycle tests.**

Run:

```bash
pytest -q tests/test_scientist_proposer.py -k 'fresh_cycle or early_history or injection'
```

Expected: PASS.

---

### Task 6: Migrate G1–G9 into EXPLORE and Retire GeneratorAgent

**Files:**

- Modify: `simpleloop/roles/generator.py`
- Modify: `simpleloop/roles/proposer.py`
- Modify: `simpleloop/roles/hypothesis.py`
- Rewrite: `tests/test_generator.py`
- Modify: `tests/test_scientist_proposer.py`

**Interfaces:**

- `GENERATIVE_OPS`, `g_definition()`, and `replace_basis()` remain pure helpers in `generator.py`.
- `GeneratorAgent`, `GenerationResult`, `run()`, `regenerate()`, and `_tool_loop()` have no production callers and are removed after tests migrate.
- EXPLORE receives `WorkingModel`, committed Explanation Set, mechanism `LeveragePoint` map, and lane `assigned_ops` in its state header/prompt.
- `HypothesisCard` remains loadable only for legacy tests/traces; production uses `ResearchHypothesis`.

- [ ] **Step 1: Rewrite generator tests around pure basis and Scientist EXPLORE.**

Retain tests for extracting G definitions, replacing the selected subset, rejecting an unassigned `generative_op`, requiring a lever map before hypothesis submission, and breadth signature count. Delete tests for `GeneratorAgent.run`, `GeneratorAgent.regenerate`, partner wording, and partial batch return.

- [ ] **Step 2: Run migrated tests and verify failure.**

Run:

```bash
pytest -q tests/test_generator.py tests/test_scientist_proposer.py -k 'generative or lever or hypothesis'
```

Expected: FAIL until EXPLORE consumes the pure helpers and new hypothesis schema.

- [ ] **Step 3: Inject the selected basis into the EXPLORE system prompt.**

Only in EXPLORE/NARROW/DEEPEN where generative operators are relevant:

```python
semantic = load_semantic("proposer", prompt_dir)
basis = render_generative_basis(assigned_ops)
context = render_inquiry_artifacts(
    working_model=session.inquiry.working_model,
    explanations=session.inquiry.explanations,
    lever_map=session.inquiry.lever_map,
)
```

The G text must not be used in UNDERSTAND to construct the problem representation.

- [ ] **Step 4: Remove the production Generator runtime.**

After `rg` confirms no callers, delete `GeneratorAgent` and its tool loop from `generator.py`; update module/docstrings and imports. Keep no compatibility alias that instantiates a second agent.

- [ ] **Step 5: Run focused tests and stale-reference check.**

Run:

```bash
pytest -q tests/test_generator.py tests/test_scientist_proposer.py
rg -n 'GeneratorAgent|generator_regenerate|feedback_generator|select_for_enrich' simpleloop/roles
```

Expected: tests PASS; no production references remain.

---

### Task 7: Implement NARROW, DEEPEN, Cognitive Rollback, Fresh Reframe, and Honest Abstention

**Files:**

- Modify: `simpleloop/roles/proposer.py`
- Modify: `tests/test_scientist_proposer.py`
- Rewrite: `tests/test_cognitive_element.py`

**Interfaces:**

- `select_for_deepen` stores at most `select_quota` `HypothesisSelection` objects and transitions NARROW → DEEPEN.
- `continue_explore`, `reopen_explain`, and `reopen_model` keep `history_visible=True` and the same messages context.
- `fresh_reframe` archives current inquiry, creates a new clean messages context and history-disabled tools, and restarts UNDERSTAND; maximum one per lane.
- Only DEEPEN accepts `submit_proposals`; budget exhaustion returns `outcome="research_incomplete"` with zero proposals.

- [ ] **Step 1: Add failing rollback tests.**

Assert:

```python
def test_direction_rollback_keeps_model_explanations_and_history(...):
    result = issue("continue_explore")
    assert result.phase is InquiryPhase.EXPLORE
    assert result.history_visible is True
    assert result.working_model is original_model
    assert result.explanations == original_explanations

def test_explanation_and_model_rollback_keep_history(...):
    assert issue("reopen_explain").history_visible is True
    assert issue("reopen_model").history_visible is True
```

- [ ] **Step 2: Add failing fresh-reframe isolation tests.**

Capture the new context and assert it contains goal/current source/gates plus the abstract reframe instruction, but not old model, explanations, hypotheses, proposal terms, dashboard, Frontier, or history. Assert the archived parent state remains in `session.archived_inquiries` and a second reframe is repaired/rejected.

- [ ] **Step 3: Add failing DEEPEN lineage and budget tests.**

Require at least one research/history observation after selection before proposal submission. Assert valid proposal metadata traces `model_claim_refs`, `explanation_refs`, `hypothesis_id`, `evidence_refs`, `mechanism`, `prediction`, and `affected_scope`. Assert exhaustion before a valid proposal produces:

```python
assert result.outcome == "research_incomplete"
assert result.proposals == ()
assert result.trace["current_phase"] == "deepen"
```

- [ ] **Step 4: Run focused tests and verify failure.**

Run:

```bash
pytest -q tests/test_scientist_proposer.py tests/test_cognitive_element.py -k 'rollback or reframe or deepen or budget'
```

Expected: FAIL until the history-aware half is implemented.

- [ ] **Step 5: Implement level-specific rollback.**

For every rollback action require `reason` and `evidence_refs`. Apply:

```python
ROLLBACK_TARGET = {
    "continue_explore": InquiryPhase.EXPLORE,
    "reopen_explain": InquiryPhase.EXPLAIN,
    "reopen_model": InquiryPhase.MODEL,
    "return_to_narrow": InquiryPhase.NARROW,
}
```

Clear only downstream artifacts invalidated by the target: reopening EXPLAIN clears explanations/lever map/hypotheses/selections/proposals; reopening MODEL clears working model and every downstream artifact; continuing EXPLORE clears selections/proposals but retains model/explanations. Never call `set_history_visible(False)`.

- [ ] **Step 6: Implement fresh reframe as a true new context.**

On `fresh_reframe`:

```python
session.archive_current_inquiry()
session.inquiry = InquiryState(context_id=old_context_id + 1)
session.runtime = WorkingState(
    counts=session.runtime.counts,
    action_log=session.runtime.action_log,
    protocol_repairs=session.runtime.protocol_repairs,
)
messages = [{"role": "user", "content": fresh_world_context + REFRAME_NOTE}]
tools = self._make_tools(..., history_enabled=False,
                         history_dir=None, memory_service=None)
```

Do not copy old assistant/user messages. When the new portfolio commits, inject history normally and continue NARROW.

- [ ] **Step 7: Implement honest terminal outcomes.**

`submit_proposals` returns `outcome="proposals"`. `abandon_portfolio` or exhaustion returns `research_incomplete` (or `block` only for a factual/frozen/contradiction contract failure already supported). Delete `_PARTIAL_SUBMIT_REMINDER` and every code path that synthesizes `ResearchProposal` from a hypothesis.

- [ ] **Step 8: Run Scientist/cognitive tests.**

Run:

```bash
pytest -q tests/test_scientist_proposer.py tests/test_cognitive_element.py
```

Expected: PASS with no partial proposal fallback tests remaining.

---

### Task 8: Extend Proposal Metadata and Structured Scientist Trace

**Files:**

- Modify: `simpleloop/memory/models.py`
- Modify: `simpleloop/roles/proposer.py`
- Modify: `scripts/proposer_harness.py`
- Modify: `tests/test_proposer_harness.py`
- Modify: `tests/test_proposer_harness_runner.py`

**Interfaces:**

- `ResearchProposal` keeps existing positional fields and adds optional metadata after them.
- `ScientistResult.trace` emits one JSON-safe object with all external research actions, observations summaries, artifacts, transitions, visibility events, rollbacks, usage, outcome, and archived reframe contexts.
- Standalone `result.json` records exact seed, model config, SimpleLoop SHA, source SHA, budgets, proposal metadata, and full structured lane traces.

- [ ] **Step 1: Add failing backwards-compatibility and trace-shape tests.**

Add:

```python
proposal = ResearchProposal(
    instruction="change X", research_target=NewFindingTarget(question="why X"),
)
assert proposal.model_claim_refs == ()
assert proposal.hypothesis_id is None

assert lane_trace.keys() >= {
    "lane_id", "assigned_generative_ops", "contexts",
    "phase_transitions", "history_injected_at_step", "understanding",
    "working_model", "explanations", "lever_map", "fresh_hypotheses",
    "narrow_decisions", "selected_hypotheses", "deep_evidence",
    "proposals", "reopen_counts", "fresh_reframes",
    "usage_by_phase", "outcome",
}
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run:

```bash
pytest -q tests/test_proposer_harness.py tests/test_proposer_harness_runner.py -k 'proposal or trace'
```

Expected: FAIL because metadata and trace fields are absent.

- [ ] **Step 3: Extend `ResearchProposal` compatibly.**

Append:

```python
model_claim_refs: tuple[str, ...] = ()
explanation_refs: tuple[str, ...] = ()
hypothesis_id: str | None = None
mechanism: str = ""
prediction: str = ""
affected_scope: str = ""
```

Reuse the existing `evidence_refs`; do not create a duplicate deep-evidence field on the proposal.

- [ ] **Step 4: Serialize trace without hidden reasoning.**

Trace action entries contain action name, phase, context, step, and bounded observation summary/evidence refs. Do not store raw hidden chain-of-thought. Artifact objects use explicit `to_dict`/serializer functions so tuples become JSON lists and proposal targets retain existing serialization.

- [ ] **Step 5: Update proposer-only artifacts.**

`_proposal_to_dict()` includes the new metadata. `run_proposer()` passes `random_seed=seed` to Orchestrator and records `scientist_steps`. Continue using `--output-dir` as the trace directory; do not add redundant `--trace-dir` or a lane override because lane count is deterministically derived from candidate quota.

- [ ] **Step 6: Run harness tests.**

Run:

```bash
pytest -q tests/test_proposer_harness.py tests/test_proposer_harness_runner.py tests/test_proposer_cli.py
```

Expected: PASS, including failure cleanup and existing output refusal.

---

### Task 9: Rewire Orchestrator, Deterministic Scheduling, and Scientist Budget

**Files:**

- Rewrite: `simpleloop/roles/orchestrator.py`
- Modify: `simpleloop/config.py`
- Modify: `simpleloop/loop.py`
- Modify: `scripts/proposer_harness.py`
- Rewrite: `tests/test_orchestrator.py`
- Modify: `tests/test_config_execution.py`
- Modify: `tests/test_parallel_candidates.py`

**Interfaces:**

- `ProposerOrchestrator.run(..., scientist_steps: int = 364, random_seed: int | None = None)` replaces separate gen/cognitive budgets.
- `_sample_generative_ops(rng: random.Random) -> tuple[str, ...]` uses a run-local RNG.
- `_new_proposer() -> ProposerAgent` creates one independent runtime object per concurrent lane; no mutable phase/session state is shared across lanes.
- Existing `_lane_quotas()`, `_MAX_LANE_WORKERS`, exception isolation, stable lane-order collection, and `ProposerResult` outer contract remain.

- [ ] **Step 1: Rewrite failing orchestrator tests around one Scientist call per lane.**

Assert two lanes produce two `run_lane()` calls, each gets its own proposer object, assigned ops and quota are passed through, and no `GeneratorAgent`/callback exists. Preserve tests for duplicate signatures, frozen paths, lane errors, all-lane abstention, concurrency, and stable aggregation where still semantically relevant.

- [ ] **Step 2: Add deterministic seed tests.**

Add:

```python
first = orchestrator.run(..., random_seed=7)
second = orchestrator.run(..., random_seed=7)
third = orchestrator.run(..., random_seed=8)
assert assigned_ops(first) == assigned_ops(second)
assert assigned_ops(first) != assigned_ops(third)
```

- [ ] **Step 3: Add budget config compatibility tests.**

Required behavior:

```python
assert load_config(loop={}).scientist_steps == 216 + 148
assert load_config(loop={"gen_steps": 20, "cognitive_steps": 12}).scientist_steps == 32
assert load_config(loop={"scientist_steps": 40}).scientist_steps == 40
```

Reject a config that specifies `scientist_steps` together with either legacy field to avoid ambiguous precedence.

- [ ] **Step 4: Run focused tests and verify failure.**

Run:

```bash
pytest -q tests/test_orchestrator.py tests/test_config_execution.py tests/test_parallel_candidates.py
```

Expected: FAIL on old two-agent calls and missing budget/seed interfaces.

- [ ] **Step 5: Implement a lane-local proposer factory.**

Store immutable constructor arguments on the Orchestrator, then:

```python
def _new_proposer(self) -> ProposerAgent:
    return ProposerAgent(
        model=self.model, runtime=self.runtime,
        timeout_seconds=self.timeout_seconds,
        max_steps=1,
        command_timeout_seconds=self.command_timeout_seconds,
        command_output_cap_chars=self.command_output_cap_chars,
        usage_observer=self.usage_observer,
    )
```

Call this inside `_run_one_lane`. This is required because `ProposerAgent` owns context-local phase/parser state and lanes execute concurrently.

- [ ] **Step 6: Remove generator orchestration.**

Delete `self.generator`, generation context handoff, `LaneState.hypothesis_versions`, `gen_transcript`, regeneration count/callback, and old card-centric trace fields. `_run_one_lane` calls exactly:

```python
result = self._new_proposer().run_lane(
    goal=goal, editable=editable, frozen=frozen,
    memory_service=memory_service, base_sha=base_sha,
    source_path=source_path, repo_path=repo_path, run_dir=run_dir,
    current_round=current_round, gate_block=gate_block,
    prompt_dir=prompt_dir, hints=hints,
    assigned_ops=lane.assigned_ops, select_quota=select_quota,
    scientist_steps=scientist_steps,
)
```

- [ ] **Step 7: Wire budget and seed callers.**

`loop.py` passes `cfg["scientist_steps"]`; standalone harness passes the same plus `random_seed=seed`. Remove global `random.seed()` save/restore from the harness because Orchestrator now owns an isolated RNG.

- [ ] **Step 8: Run orchestrator/config/integration tests.**

Run:

```bash
pytest -q tests/test_orchestrator.py tests/test_config_execution.py tests/test_parallel_candidates.py tests/test_proposer_harness_runner.py
```

Expected: PASS.

---

### Task 10: Replace the Active Identity Prompt and Preserve Historical Prompt Audit

**Files:**

- Rewrite: `simpleloop/prompts/proposer.md`
- Modify: `simpleloop/prompts/__init__.py`
- Modify: `simpleloop/self_improvement/history.py`
- Modify: `simpleloop/self_improvement/gate.py`
- Modify: `tests/test_prompt_templates.py`
- Modify: `tests/test_prompt_self_improvement.py`

**Interfaces:**

- `ACTIVE_PROMPT_NAMES = ("proposer", "executor", "meta_optimizer")`.
- `LOADABLE_PROMPT_NAMES = ACTIVE_PROMPT_NAMES + ("generator",)` allows historical reads.
- New prompt snapshots copy/compare/restore only active names; restore from an old four-file snapshot ignores its extra `generator.md` but does not mutate the archived snapshot.
- Prompt gate permits an optional legacy `generator.md` in an existing active directory, but does not require or optimize it.

- [ ] **Step 1: Add failing active/legacy prompt tests.**

Assert `load_semantic("generator")` still reads the packaged legacy file, new `v000` snapshots contain exactly the three active prompts plus manifest, and restoring an old snapshot with generator creates a three-prompt active directory while the old snapshot remains unchanged.

- [ ] **Step 2: Add failing Scientist identity tests.**

Assert proposer identity contains suspended judgment/model/explanation/prediction concepts, and does not contain `cognitive element`, `Sieve`, `Enrich`, `Generator partner`, phase checklist, `feedback_generator`, or OMILREC expected solution terms.

- [ ] **Step 3: Run prompt tests and verify failure.**

Run:

```bash
pytest -q tests/test_prompt_templates.py tests/test_prompt_self_improvement.py
```

Expected: FAIL on current four-active-prompt and cognitive identity assumptions.

- [ ] **Step 4: Write the concise Scientist identity.**

Use the identity principles in the approved implementation document. Keep action schemas and phase attention out of `proposer.md`; they are runtime hard structure produced by Task 4.

```text
You are the scientist responsible for deciding which interventions are worth an experiment.

A plausible local opportunity is evidence about the problem, not yet the problem definition. Treat observations as clues about a larger structure or mechanism until you understand how the target outcome is produced.

Build working models that support explanation, counterfactual reasoning, and prediction—not summaries of available facts. Resist premature commitment. Understand broadly before spending detailed effort on one direction, and keep materially different explanations alive while evidence permits.

Your models are provisional. When evidence contradicts a direction, repair the level of understanding that failed rather than defending the proposal. A proposal is justified only when it follows from a mechanism you understand and makes a prediction that an experiment can test.
```

- [ ] **Step 5: Split active from loadable names.**

Implement:

```python
ACTIVE_PROMPT_NAMES = ("proposer", "executor", "meta_optimizer")
LOADABLE_PROMPT_NAMES = ACTIVE_PROMPT_NAMES + ("generator",)
PROMPT_NAMES = ACTIVE_PROMPT_NAMES  # compatibility for active-set callers
```

`load_semantic()` validates against `LOADABLE_PROMPT_NAMES`. History and gate iterate `ACTIVE_PROMPT_NAMES`; optional legacy generator is read-only compatibility, not active production semantics.

- [ ] **Step 6: Run prompt/self-improvement tests.**

Run:

```bash
pytest -q tests/test_prompt_templates.py tests/test_prompt_self_improvement.py
```

Expected: PASS, including crash recovery and old snapshot restoration.

---

### Task 11: Migrate Remaining Tests, Remove Dead Boundaries, and Verify Mechanics

**Files:**

- Modify: `tests/test_plot.py`
- Modify: `tests/test_telemetry.py`
- Modify: `tests/test_runtime.py`
- Modify: `tests/test_parallel_candidates.py`
- Modify: `simpleloop/roles/__init__.py` if stale exports exist
- Modify: affected docs/comments that describe active runtime behavior

**Interfaces:**

- Public outer entry remains `ProposerOrchestrator.run(...) -> ProposerResult`.
- Candidate Worker, target resolution, Ledger schema, Gate, plotting, and telemetry consumers continue to accept proposals and abstentions.

- [ ] **Step 1: Run a production stale-reference scan.**

Run:

```bash
rg -n 'GeneratorAgent|generator_regenerate|feedback_generator|select_for_enrich|Sieve|Enrich|partial proposal|gen_steps|cognitive_steps' simpleloop scripts tests
```

Expected: identify only legacy compatibility/docs and tests that still need migration; no production role/orchestrator references are allowed at task completion.

- [ ] **Step 2: Update integration fakes to `run_lane()` / `scientist_steps`.**

Preserve assertions about proposal count, candidate scheduling, telemetry writes, plot input, failure propagation, and static mode. Change only internal two-agent assumptions.

- [ ] **Step 3: Run all focused integration tests.**

Run:

```bash
pytest -q tests/test_plot.py tests/test_telemetry.py tests/test_runtime.py tests/test_parallel_candidates.py tests/test_static_mode.py
```

Expected: PASS.

- [ ] **Step 4: Run compilation, type-facing imports, and the full suite.**

Run:

```bash
python -m compileall -q simpleloop scripts/proposer_harness.py
pytest -q tests
```

Expected: compilation succeeds and the complete suite passes.

- [ ] **Step 5: Run final stale-reference and prompt-leak scans.**

Run:

```bash
rg -n 'GeneratorAgent|generator_regenerate|feedback_generator|self\.generator|research_batch' simpleloop
rg -n -i 'EventContext|state lifetime|ownership|FCN call|OMILREC|charge/time' simpleloop/prompts/proposer.md
```

Expected: first command finds no production boundary; second command returns no OMILREC benchmark leakage.

---

### Task 12: Run vNext Cognition-Only A/B and Produce the Review Handoff

**Files:**

- Create (ignored run artifacts): `runs/proposer-vnext-baseline/vnext/seed-00/` through `seed-04/`
- Read: current baseline artifacts from Task 0

**Interfaces:**

- Same config, from-run history, OMILREC source SHA, model, candidate quota, approximate total step budget, and seeds as current baseline.
- No Candidate Worker, Gate, or full optimization run in this task.

- [ ] **Step 1: Run the same five seeds on vNext.**

Run:

```bash
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/vnext/seed-00 --seed 0
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/vnext/seed-01 --seed 1
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/vnext/seed-02 --seed 2
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/vnext/seed-03 --seed 3
python scripts/proposer_harness.py --config runs/omilrec-v100-generator-proposer-008/config.orig.yaml --from-run runs/omilrec-v100-generator-proposer-008 --output-dir runs/proposer-vnext-baseline/vnext/seed-04 --seed 4
```

Expected: five complete or honest `research_incomplete` outputs.

- [ ] **Step 2: Verify mechanical trace invariants.**

Run:

```bash
jq -e '
  .status == "completed" and
  ([.trace.lanes[] | .phase_transitions[]?.to] | index("model") != null) and
  ([.trace.lanes[] | .phase_transitions[]?.to] | index("explain") != null) and
  ([.trace.lanes[] | select(.history_injected_at_step != null)] | length > 0)
' runs/proposer-vnext-baseline/vnext/seed-*/result.json
```

Expected: PASS for completed research paths; `research_incomplete` lanes still expose their last valid artifacts/current phase.

- [ ] **Step 3: Perform the cognition rubric review.**

For each seed record:

- Premature locality before Working Model commit。
- Whole-problem model accuracy and counterfactual usefulness。
- Competing explanation quality。
- Mechanism-family breadth rather than wording diversity。
- Selected macro hypothesis → targeted micro reads。
- M→E→H→evidence→proposal/prediction lineage。
- Proposal abstraction L0/L1/L2/L3 as an observation, not a hard gate。
- Factual accuracy against source。
- Tool/token/wall-time cost。

- [ ] **Step 4: Prepare the final handoff without running Workers.**

Report:

```text
git diff --stat
full pytest result
five baseline result paths
five vNext result paths
at least three full structured Scientist traces
final proposals / research_incomplete outcomes
repeated cognitive shortcuts or artifact-form-filling patterns
mechanical acceptance checklist
```

Do not claim cognitive success solely because tests pass; separate mechanical correctness from the user's trace-based cognition judgment.

---

## Final Mechanical Acceptance Criteria

- [ ] Production Orchestrator neither imports nor instantiates `GeneratorAgent`.
- [ ] One concurrent lane owns one isolated `ProposerAgent` and one current message context.
- [ ] Fresh UNDERSTAND/MODEL/EXPLAIN/EXPLORE cannot reach memory actions or history binds.
- [ ] Portfolio commit is the only normal history injection point.
- [ ] Visibility is monotonic inside each context; fresh reframe creates a new context and is bounded to one.
- [ ] Explanation is distinct from Model and intervention exploration.
- [ ] Hypothesis diversity is mechanism/intervention/scope based, not path based.
- [ ] Proposal can be submitted only in DEEPEN with selected-hypothesis lineage, evidence, mechanism, prediction, and scope.
- [ ] Budget exhaustion never fabricates a partial proposal.
- [ ] G1–G9, 5-of-9 assignment, lane quotas, concurrency, stable aggregation, Worker/Harness, and old run audit remain.
- [ ] Standalone harness records deterministic seed and complete structured trace.
- [ ] Active production prompt set is proposer/executor/meta_optimizer; legacy generator prompt remains audit-loadable only.
- [ ] Full tests and compile checks pass.
- [ ] Cognition-only A/B is reviewed before any Worker/Gate experiment run.
