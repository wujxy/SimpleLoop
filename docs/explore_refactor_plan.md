# Explore 模块一次性重构计划

## 目标

将当前分散在 `simpleloop/memory/signals.py`、`simpleloop/memory/context.py`、`simpleloop/roles/proposer.py` 中的停滞检测与局部探索干预逻辑，重构为独立的 **Explore** 模块。

Explore 的职责不是替代 Proposer 做科学判断，也不是引入重型搜索算法；它只基于 Experiment Ledger 与 Finding Archive 的确定性事实，判断当前搜索是否陷入局部 exploitation、同族微变体、全局停滞或回归，并向 Proposer runtime 提供结构化搜索健康报告与最小必要的响应约束。

一句话目标：

> Explore 负责告诉 Scientist “你现在的搜索动力学是否健康”；Scientist 仍负责判断下一次实验是否值得做。

## 当前问题

最新提交已经加入了 cross-finding `global_stall`，但它仍是补丁式实现：

- `memory/signals.py` 同时承担 per-finding signal、global stall、objective classification、Jaccard 工具函数，职责开始混杂。
- `context.py` 直接理解并渲染 global stall policy，导致 context 渲染层包含搜索策略知识。
- `proposer.py` 只把停滞作为 `_maybe_nudge`，没有要求 Proposer 在 submit 前显式回应停滞。
- 没有 family-level 检测，因此无法识别“每轮新 Finding，但同一机制/代码区域连续失败”的局部探索。
- fixed-window `global_stall` 只能看到整体停滞，不能准确表达连续 no-improve run，也不能定位耗尽的机制家族。

这会导致 omilrec 这类长 run 中继续出现：

- 每轮新 Finding 绕开 per-finding `mechanism_challenge`。
- Proposer 在 `Calculate_EVLikelihood + hot-path micro-optimization` 这类家族里继续产小变体。
- 日志中出现“知道停滞”，但行为上仍然 submit。

## 设计边界

### Explore 做什么

Explore 只做确定性的搜索健康分析：

- 按 experiment 与 parent objective 分类：`improvement | neutral | regression | unclassified`。
- 计算 per-finding 健康信号，保留现有语义。
- 计算 family-level 停滞信号，识别同机制/同代码区域的局部 exploitation。
- 计算 global-level 停滞信号，识别整体连续 no-improve 或连续回归。
- 输出 compact、可渲染、可测试的 `ExploreReport`。
- 告诉 runtime 是否需要 `challenge_response`。

### Explore 不做什么

本次重构不引入：

- embedding clustering；
- MAP-Elites / quality-diversity archive；
- bandit / evolutionary controller；
- 自动选择下一轮 research family；
- 跨任务长期学习；
- Finding schema migration；
- 新数据库或持久化状态；
- 用 Explore 结论直接禁止某个科学方向。

Explore 是 search health monitor，不是 search optimizer。

## 模块结构

新增包：

```text
simpleloop/explore/
  __init__.py
  models.py
  classify.py
  families.py
  monitor.py
  render.py
```

保留 `simpleloop/memory/signals.py` 作为 compatibility wrapper，避免一次性改动面过大。长期可在后续版本中删除或收缩。

## 数据模型

文件：`simpleloop/explore/models.py`

建议使用 `dataclass(frozen=True)`，并提供 `to_dict()`，方便 startup pack、trace、telemetry 使用。

```python
@dataclass(frozen=True)
class ObjectiveClassification:
    experiment_id: str
    round: int
    candidate: int
    finding_id: str | None
    parent_sha: str
    candidate_sha: str | None
    objective: float | None
    parent_objective: float | None
    kind: str  # improvement | neutral | regression | unclassified


@dataclass(frozen=True)
class PolicySignal:
    name: str
    active: bool
    rule: str
    severity: str  # info | watch | challenge


@dataclass(frozen=True)
class FindingExploreHealth:
    finding_id: str
    question: str
    attempts: int
    evaluable_attempts: int
    implementation_failures: int
    improvements: int
    neutral: int
    regressions: int
    selected: int
    last_touched_round: int
    policy_signals: tuple[PolicySignal, ...]


@dataclass(frozen=True)
class FamilyExploreHealth:
    family_id: str
    code_region: str
    mechanisms: tuple[str, ...]
    finding_ids: tuple[str, ...]
    attempts: int
    evaluable_attempts: int
    improvements: int
    neutral: int
    regressions: int
    selected: int
    consecutive_no_improve: int
    recent_rounds: tuple[int, ...]
    policy_signals: tuple[PolicySignal, ...]


@dataclass(frozen=True)
class GlobalExploreHealth:
    attempts: int
    evaluable_attempts: int
    recent_window: int
    recent_improvements: int
    recent_neutral: int
    recent_regressions: int
    consecutive_no_improve_rounds: int
    recent_mechanisms: tuple[str, ...]
    policy_signals: tuple[PolicySignal, ...]


@dataclass(frozen=True)
class ExploreReport:
    first_round: bool
    hints_present: bool
    findings: tuple[FindingExploreHealth, ...]
    families: tuple[FamilyExploreHealth, ...]
    global_health: GlobalExploreHealth | None
    challenge_required: bool
    challenge_reasons: tuple[str, ...]
```

## Objective 分类

文件：`simpleloop/explore/classify.py`

从 `memory/signals.py` 移出并整理：

- `sha_objective_map(experiments, objective_key)`
- `classify_objective(obj, parent_obj, lower_is_better)`
- `classify_experiments(experiments, objective_key, lower_is_better)`

规则保持当前设计：

- gate failure 不算机制 refutation；
- parent objective 不可解析时标为 `unclassified`；
- selected 不用于 objective classification，因为 selected 受 sibling competition 污染；
- eligible candidate 才参与 improvement/neutral/regression。

## Family 识别

文件：`simpleloop/explore/families.py`

Family 是 Explore 的核心新增能力，用来捕捉“每轮新 Finding 但同族微变体”的问题。

### family key

不引入 embedding，不做语义聚类。使用 Finding 已有 tags：

```python
family_key = (
    primary_code_region_bucket(finding.code_regions),
    primary_mechanism_bucket(finding.mechanisms),
)
```

### code region bucket

规则保守：

- 若 Finding 有 `code_regions`，取第一个稳定 region。
- 对函数级 region 保留到函数或文件，不做复杂解析。
- 若为空，使用 candidate changed_paths 的 top bucket。
- 若仍为空，使用 `unknown-region`。

示例：

```text
OMILRECV2/src/OMILRECV2.cc:Calculate_EVLikelihood
OMILRECV2/src/OMILRECV2.cc
OMILRECV2/src
unknown-region
```

### mechanism bucket

规则：

- 优先使用 Finding mechanisms。
- 若多个 mechanism，保留排序后的 tuple，但 family_id 以第一个 dominant mechanism 为主。
- 若为空，使用 `unknown-mechanism`。

### 为什么不做自动合并 Finding

本次不修改 Finding 数据模型，也不自动把新 Finding 合并到旧 Finding。Explore 只在读侧聚合 family。这样：

- 不破坏现有 archive append-only 语义；
- 不引入 schema migration；
- 不让搜索健康模块改写研究问题边界；
- 仍能检测跨 Finding 停滞。

## Monitor 主入口

文件：`simpleloop/explore/monitor.py`

入口：

```python
def analyze_explore_health(
    findings: dict[str, Finding],
    experiments: list[Experiment],
    *,
    current_round: int,
    hints_present: bool,
    objective_key: str | None,
    lower_is_better: bool,
) -> ExploreReport:
    ...
```

若 `not experiments` 或缺少 objective key：

- `first_round=True`
- 空 findings/families
- `global_health=None`
- `challenge_required=False`

## Policy 规则

阈值放在 `monitor.py` 顶部，保持简单可调：

```python
FINDING_FEASIBILITY_FAILURES = 2
FINDING_MECHANISM_NEUTRAL = 2
FINDING_CONTRADICTORY_REGRESSIONS = 1

FAMILY_STALL_CONSECUTIVE_NO_IMPROVE = 3
FAMILY_REGRESSION_MIN = 2
FAMILY_OVEREXPLOITED_ATTEMPTS = 5

GLOBAL_STALL_CONSECUTIVE_NO_IMPROVE_ROUNDS = 3
GLOBAL_REGRESSION_MIN = 3
GLOBAL_RECENT_WINDOW = 5
```

### per-finding

保留现有信号：

- `feasibility_risk`
- `mechanism_challenge`
- `contradictory_result`

### family-level

新增：

- `family_stall`
  - active when `consecutive_no_improve >= 3`
  - severity `challenge`
- `family_regressing`
  - active when `regressions >= 2 and improvements == 0`
  - severity `challenge`
- `family_overexploited`
  - active when `attempts >= 5 and selected == 0`
  - severity `watch`

### global-level

新增：

- `global_stall`
  - active when `consecutive_no_improve_rounds >= 3`
  - severity `challenge`
- `global_regression_run`
  - active when recent regressions exceed threshold and no recent improvement
  - severity `challenge`

### challenge_required

`ExploreReport.challenge_required=True` 当存在任何 severity 为 `challenge` 的 active family/global signal。

per-finding `mechanism_challenge` 可以先保持为 nudge，不直接触发 challenge_required，避免对已有行为过度收紧。若后续实际 run 证明仍偏软，再升级。

## 渲染

文件：`simpleloop/explore/render.py`

提供三个函数：

```python
def render_explore_for_startup(report: ExploreReport) -> str
def render_explore_for_state_header(report: ExploreReport) -> str
def render_challenge_repair_message(report: ExploreReport) -> str
```

### startup pack 渲染

替代 `context.py` 里的 `_render_signals` 全局逻辑。输出 compact 文本：

```text
Explore health (ledger-derived search dynamics, NOT scientific verdicts):
  family stalled:
    - OMILRECV2/src/OMILRECV2.cc:Calculate_EVLikelihood + hot-path-micro-optimization
      attempts=5 imp/neutral/reg=0/1/4 consecutive_no_improve=5
      policy=family_stall
  global:
    recent_window=5 imp/neutral/reg=0/2/3 consecutive_no_improve_rounds=5
    policy=global_stall, global_regression_run
  POLICY: Before submitting in this state, either reframe/abandon or provide
  challenge_response explaining why the next proposal is not another variant
  of the stalled family and why it is worth one more experiment.
```

### state header 渲染

只渲染短标签：

```text
active_explore: family_stall, global_stall
challenge_required: true
```

避免每步塞长文。

### repair message

当 submit 缺少 challenge response：

```text
Protocol correction required (challenge_response_required).
Explore health shows active family/global stagnation. You may:
1. reframe_research,
2. abandon_direction,
3. submit only with challenge_response explaining what stalled, the null
   hypothesis, why this is not the same-family variant, and why evidence makes
   one more experiment worth its cost.
Return exactly one JSON action object.
```

## Runtime 协议改造

文件：`simpleloop/roles/proposer.py`

### submit_proposals schema

在 `submit_proposals` 顶层增加可选字段：

```json
{
  "action": "submit_proposals",
  "challenge_response": {
    "triggered_policy": "...",
    "stalled_family": "...",
    "what_was_exhausted": "...",
    "null_hypothesis": "...",
    "why_this_is_not_same_family_variant": "...",
    "why_worth_one_more_experiment": "...",
    "evidence_refs": ["experiment:r8c0"]
  },
  "proposals": [...]
}
```

平时可选；当 `ExploreReport.challenge_required` 为 true 时必填。

### frame_research schema

新增可选字段：

```json
{
  "search_posture": "exploit|explore|reframe|abandon_if_no_evidence",
  "stagnation_reading": "...",
  "family_to_avoid": "..."
}
```

默认不强制。若 challenge_required active，则 `frame_research` 必须提供：

- `search_posture`
- `stagnation_reading`

这样停滞期 frame 会真正变重，但正常 round 不 checklist 化。

### guard

扩展 `_validate_action_guard`：

```python
if name == "frame_research" and report.challenge_required:
    require search_posture + stagnation_reading

if name == "submit_proposals" and report.challenge_required:
    require challenge_response
    validate challenge_response.evidence_refs
```

注意：这不是禁止 submit，而是要求 Proposer 显式回应 Explore 诊断。

### evidence refs

`challenge_response.evidence_refs` 使用现有 verification ref 校验逻辑：

- 至少一个 `experiment:` 或 `source:`；
- `finding:` 可附加但不能单独成立；
- ref 必须是本轮实际可见或 startup 可引用的事实。

若短期实现成本过高，可以先复用 `_validate_verification_refs`。

## Memory 集成

文件：`simpleloop/memory/service.py`

当前 `build_startup_pack` 已经在 service 中计算 signals。改为：

```python
explore_report = analyze_explore_health(...)
return build_startup_pack(..., explore=explore_report)
```

为了兼容旧测试和旧调用，可以短期保留 `signals` 参数，但内部改为：

- 新路径使用 `explore`；
- `signals` 由 ExploreReport 转 dict 提供 compatibility。

## signals.py 处理

文件：`simpleloop/memory/signals.py`

重构后变成 thin wrapper：

```python
def compute_deliberation_signals(...):
    report = analyze_explore_health(...)
    return report.to_legacy_signals()
```

这样现有测试可以逐步迁移，不需要一次删除旧 API。

同时把 `tokenize`、`jaccard_overlap` 移到更合适的位置：

- 如果只被 proposer near-duplicate 用，可移到 `simpleloop/explore/classify.py` 或 `simpleloop/roles/proposer.py` 本地；
- 为减少改动，第一版可以继续从 `memory/signals.py` re-export。

## Trace 与 telemetry

ProposerResult trace 增加：

```json
"explore": {
  "challenge_required": true,
  "challenge_reasons": ["family_stall: ...", "global_stall"],
  "active_families": [...]
}
```

deliberation telemetry 增加：

```json
"explore_challenge_required": true,
"challenge_response_provided": true/false
```

这样重跑 omilrec 时可以直接看：

- challenge 是否触发；
- proposer 是否回应；
- 回应后是否 reframe/abandon/submit；
- 同族 no-improve 是否减少。

## Prompt 修改

文件：`simpleloop/prompts/proposer.md`

新增 Explore 语义，但保持角色化，不写成 checklist。

建议加入一小段：

```text
Explore health is not a verdict about the science. It is the lab's readout of
your search dynamics: which families have been repeatedly tried, which recent
rounds failed to advance, and where your attention may be collapsing into
local exploitation. When Explore requires a challenge response, do not treat it
as paperwork. Use it to test whether you are about to spend an experiment on
another variant of an exhausted idea.
```

在 terminal action 文案中加入：

```text
When Explore marks challenge_required, submit_proposals must include
challenge_response, unless you choose reframe_research or abandon_direction.
```

## 测试计划

新增：

```text
tests/test_explore_classify.py
tests/test_explore_families.py
tests/test_explore_monitor.py
tests/test_explore_render.py
```

扩展：

```text
tests/test_proposer_agent.py
tests/test_memory_service.py
tests/test_deliberation_signals.py
```

### 必测用例

1. baseline parent 不可解析时 experiment 为 `unclassified`，不制造假 improvement。
2. 同一 Finding 两个 neutral 仍触发 per-finding `mechanism_challenge`。
3. 每轮新 Finding，但相同 family 连续 3 次 no-improve，触发 `family_stall`。
4. 不同 family 交替探索，不触发单一 `family_stall`。
5. recent improvement 会重置 `consecutive_no_improve`。
6. r6-r10 风格 history 触发 `global_stall`。
7. active challenge 下 `submit_proposals` 无 `challenge_response` 会 repair。
8. active challenge 下 `reframe_research` 与 `abandon_direction` 不被阻止。
9. active challenge 下 `frame_research` 缺 `search_posture` 会 repair。
10. startup pack 渲染 Explore block，working state 只渲染 compact header。
11. legacy `compute_deliberation_signals` 仍返回旧结构所需字段。

## 验证方式

本地验证：

```bash
pytest tests/test_explore_classify.py
pytest tests/test_explore_families.py
pytest tests/test_explore_monitor.py
pytest tests/test_explore_render.py
pytest tests/test_proposer_agent.py
pytest tests/test_memory_service.py
pytest tests/test_deliberation_signals.py
pytest tests/
```

历史 run 验证：

- 用 `omilrec-v100-generator-proposer-001/history.jsonl` 构造 experiments。
- 检查 r6-r10 后：
  - `global_stall.active == true`
  - 对 `Calculate_EVLikelihood + hot-path-micro-optimization` 类 family，`family_stall.active == true`
  - `challenge_required == true`
- 检查 r11/r12 improvement 后 no-improve run 被重置。
- 检查 r13-r17 后重新触发 challenge。

行为验证：

- 重跑短版 omilrec。
- 观察：
  - `challenge_response_required` repair 是否出现；
  - `challenge_response_provided` 是否出现；
  - reframe/abandon 比例是否上升；
  - 同族连续 no-improve 是否减少；
  - worker 时间是否减少；
  - best objective 不应明显劣化。

## 兼容与风险

### 兼容策略

- 不改 Ledger schema。
- 不改 Finding schema。
- 不改 Experiment model。
- 保留 `compute_deliberation_signals` legacy API。
- startup pack 文本变化可接受，但保持 compact。

### 风险 1：guard 太强，压制有效 exploitation

缓解：

- 不禁止 submit，只要求 `challenge_response`。
- 阈值从 3 次 no-improve 开始，不在第一次回归就强制。
- per-finding signal 初期不进入 challenge_required，只作为 nudge。

### 风险 2：family 归类过粗

缓解：

- family key 使用现有 tags，不做复杂推断。
- 渲染时显示 family basis，让 Proposer 可反驳。
- challenge_response 允许说明“这不是同族变体”。

### 风险 3：prompt/JSON 协议变复杂

缓解：

- `challenge_response` 只在 challenge_required 时必填。
- 正常无停滞 round 行为不变。
- repair message 明确给三条合法路径：reframe、abandon、submit with response。

## 最终落地状态

重构完成后，SimpleLoop 的职责分层应变为：

```text
Experiment Ledger
  authoritative experiment facts

Finding Archive
  open research questions and tags

Frontier
  coverage view

Explore
  search health and local-exploration detection

Proposer Runtime
  phase control, evidence verification, challenge_response guard

Proposer Prompt
  Scientist identity and reasoning style
```

Explore 是当前认知元架构缺失的“搜索动力学感知层”。它不让 LLM 更神奇，但能让 LLM 不再轻易把连续失败合理化成“再试一个小变体”。
