# SimpleLoop Scientist-Proposer vNext 重构实施方案

> **用途**：直接交给 Codex / coding agent 在本地 WSL 中实施。  
> **目标分支基线**：`feature/proposer-branch-deepen`，但**本地 WSL clone 是最终 source of truth**。  
> **研究依据**：`simpleloop_scientist_proposer_inquiry_research.md`。  
> **本次边界**：只重构单轮 Proposer 的研究认知流程；不升级为跨-round persistent Researcher，不改 Candidate Worker / Gate / objective selection 的核心职责。  
> **首要验证方式**：用户已有的 proposer-only CLI。第一轮不跑完整 Worker/Gate，先判断 Scientist 的研究路径和最终 proposal 是否符合预期。

---

# 0. 这次重构到底要验证什么

这不是一次“把 Generator 和 Cognitive 两个 class 合并”的代码整理。

真正要验证的科学假设是：

> **同一个基础模型如果被置于一个真实支持 inquiry 的 runtime 中——先独立理解问题、构造 Working Model、解释当前 gap、广泛探索机制空间，然后才注入历史并由宽到窄深入——是否会从局部 opportunity search 自发跃迁到 mechanism/system-level scientific proposal？**

因此，这次实现的成功标准不是：

- 多了 `UNDERSTAND/MODEL/EXPLAIN/...` enum；
- prompt 中出现了 scientific method；
- 输出了一份漂亮的 WorkingModel JSON；
- tests pass。

真正成功必须能在 proposer-only trace 中看到：

```text
whole-problem investigation
        ↓
working representation/model
        ↓
competing explanations
        ↓
mechanism-level broad exploration
        ↓
history as evidence
        ↓
narrowing
        ↓
selected hypothesis drives targeted micro investigation
        ↓
proposal with mechanism + prediction
```

如果最终仍然是：

```text
先锁定某 hotspot
→ 想 cache/hoist
→ 前面补一份 WorkingModel
```

则认知重构失败，即使协议形式完全正确。

---

# 1. 当前代码事实与本次改造切口

在当前公开 `feature/proposer-branch-deepen` 中：

- `ProposerOrchestrator` 同时实例化 `GeneratorAgent` 与 `ProposerAgent`；
- `GeneratorAgent` 是 history-free 的 lever-space surveyor；
- `ProposerAgent` 明确被定义为 cognitive element；
- 当前 proposer prompt 的核心协议仍是 `Sieve → Enrich`；
- Cognitive 可通过 `feedback_generator` 将历史事实反馈给 Generator，再触发 `regenerate`；
- `HypothesisCard` 的 diversity signature 是 `region × mechanism × intervention_family`；
- `ResearchAgent` 已经有成熟的 model/tool action loop、protocol repair、WorkingState telemetry；
- `ResearchTools` / `ResearchCommandRunner` 当前默认接受 `history_dir`，research shell 会把 history 绑定进容器；
- 当前 proposer 在预算接近耗尽时存在 “submit partial proposal” 的 reminder / fallback 逻辑。

这些现有机制中，应当**复用**的是：

1. `ResearchAgent` 的通用 tool loop、action parsing / repair、timeout / usage 机制；
2. 现有 research bash / source inspection 基础设施；
3. 多 lane Orchestrator、quota / concurrency；
4. 5-of-9 generative basis 的 breadth idea；
5. memory service / Experiment Ledger / Findings / Frontier；
6. Candidate Worker 与后续 Harness。

应当**替换**的是：

1. Generator → Cognitive 的角色边界；
2. `feedback_generator → regenerate` 这一统一反馈结构；
3. “survey → lever map → hypothesis” 作为问题构造主线；
4. history 只在 prompt/action 层隐藏、但 runtime 仍可能访问的弱隔离；
5. 预算耗尽时强行交 partial proposal；
6. 当前过于 code-region-centric 的 hypothesis lineage。

---

# 2. 最终目标架构

每个 lane 只有一个 `ProposerAgent` / `ProposerScientist` runtime。

```text
                     ONE SCIENTIST

                        START
                          │
                          ▼
                     UNDERSTAND
            problem / boundary / whole view
                          │
                          ▼
                       MODEL
             construct a reasoning model
                          │
                          ▼
                      EXPLAIN
              competing causal / structural
                  explanations of the gap
                          │
                          ▼
                  FRESH EXPLORE
         broad mechanism / intervention space
                          │
                          ▼
             commit fresh hypothesis portfolio
                          │
══════════════════ HISTORY INJECTION ══════════════════
                          │
                          ▼
                       NARROW
              challenge with past evidence
                          │
                          ▼
                       DEEPEN
           selected hypothesis drives detailed
                investigation and prediction
                          │
                          ▼
                       PROPOSE
                          │
                          ▼
                  Candidate Worker
```

两个轴保持不变：

```text
breadth:
whole problem
→ representations/mechanisms
→ hypothesis portfolio
→ selected hypotheses
→ proposals

depth:
coarse orientation
→ working model
→ explanatory evidence
→ targeted detailed evidence
→ executor-ready intervention
```

---

# 3. 最重要的 runtime invariant：History Visibility

## 3.1 两类信息

### World evidence：Fresh 阶段允许

这些是当前世界本身：

- goal；
- accepted source/artifact；
- gate / hard constraints；
- baseline measurements；
- profiler / current data；
- domain materials；
- repo / source tree；
- current factual environment。

### Search-trajectory evidence：Fresh 阶段禁止

这些是过去的搜索路径：

- previous proposals；
- Experiment Ledger；
- Findings；
- Frontier；
- prior candidate outcomes；
- failed families；
- old interpretations；
- old proposal vocabulary。

## 3.2 同一 context 中 visibility 单调

必须成为代码 invariant：

```text
history_visible = False
        ↓
history injection
        ↓
history_visible = True
```

允许：

```text
False → True
```

不允许：

```text
True → False
```

**不能通过 prompt 让模型“忘掉刚看过的历史”。**

一旦看过历史，后续回 `EXPLORE / EXPLAIN / MODEL` 都属于：

```text
evidence-aware revision
```

而不是 fresh inquiry。

只有 `fresh_reframe` 创建**新的 LLM message context + 新的 history-locked tool view**，才能恢复真正独立视角。

---

# 4. 文件级重构建议

以下是推荐最终形态。Codex 在实施前应先检查本地 WSL clone，因为本地已有 proposer-only CLI，可能比公开分支更新。

## 4.1 `simpleloop/roles/research_agent.py`

### 保留

保留为底层通用 Agent Runtime：

- model call；
- `_step()`；
- JSON action parsing外围；
- protocol repair；
- timeout；
- usage accounting；
- repeated tool fingerprint；
- generic action telemetry。

### 修改

不要把 Scientist-specific scientific state 全塞进现有 `WorkingState`。

推荐：

```python
@dataclass
class WorkingState:
    # 原有 generic runtime telemetry
    counts: dict
    session_evidence: set[str]
    new_evidence: set[str]
    action_log: list[dict]
    protocol_repairs: int
    ...
```

继续保留。

另建 inquiry state：

```python
@dataclass
class InquiryState:
    phase: InquiryPhase
    history_visible: bool
    understanding: Understanding | None
    working_model: WorkingModel | None
    explanations: list[Explanation]
    lever_map: list[LeveragePoint]
    hypotheses: list[ResearchHypothesis]
    selected_hypothesis_ids: list[str]
    proposals: list[ResearchProposal]

    phase_transitions: list[PhaseTransition]
    reopen_counts: dict[str, int]
    fresh_reframes: int
```

可以 composition：

```python
ScientistSessionState(
    runtime=WorkingState(),
    inquiry=InquiryState(),
)
```

不要让通用 `ResearchAgent` 重新承担领域认知语义。

---

## 4.2 新建 `simpleloop/roles/inquiry.py`

推荐新文件，用于承载不属于 prompt、也不属于 generic tool runtime 的认知协议数据结构。

至少包含：

```python
class InquiryPhase(str, Enum):
    UNDERSTAND = "understand"
    MODEL = "model"
    EXPLAIN = "explain"
    EXPLORE = "explore"
    NARROW = "narrow"
    DEEPEN = "deepen"
```

以及：

```python
@dataclass(frozen=True)
class Understanding:
    problem: str
    target_outcome: str
    boundary: str
    current_account_of_the_whole: str
    key_unknowns: tuple[str, ...]
```

```python
@dataclass(frozen=True)
class ModelClaim:
    id: str
    claim: str
    evidence_refs: tuple[str, ...]
```

```python
@dataclass(frozen=True)
class WorkingModel:
    version: int
    representation: str
    explanatory_structure: str
    claims: tuple[ModelClaim, ...]
    important_unknowns: tuple[str, ...]
```

```python
@dataclass(frozen=True)
class Explanation:
    id: str
    phenomenon: str
    account: str
    model_basis: tuple[str, ...]
    expected_if_true: tuple[str, ...]
    evidence_needed: tuple[str, ...]
```

```python
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
    evidence_refs: tuple[str, ...] = ()
```

Fresh hypothesis 是有 lineage 与 critical unknown 的 conjecture，不要求 direct source evidence；若提供 evidence refs 则必须可验证，DEEPEN 再要求 critical-premise evidence。

这些 schema 应保持**语义小而强**，不要为了普遍科研扩成几十字段。

---

## 4.3 `simpleloop/roles/proposer.py`

这是主要重构文件。

### 当前

```text
ProposerAgent = cognitive element
research_batch(hypotheses=generator_output)
```

### 目标

`ProposerAgent` 成为完整 lane-local Scientist：

```python
result = proposer.run_lane(
    goal=...,
    editable=...,
    frozen=...,
    gates=...,
    source_path=...,
    repo_path=...,
    run_dir=...,
    memory_service=...,
    assigned_ops=...,
    select_quota=...,
    ...
)
```

它自己负责：

```text
UNDERSTAND
MODEL
EXPLAIN
EXPLORE
history injection
NARROW
DEEPEN
PROPOSE
```

不再接收一个外部 `hypotheses` list 作为起点。

### 必须删除/迁移

删除 production path 中：

```text
feedback_generator
generator_regenerate callback
select_for_enrich
Sieve → Enrich identity
```

用层级回退替代：

```text
continue_explore
reopen_explain
reopen_model
fresh_reframe
```

---

## 4.4 `simpleloop/roles/generator.py`

最终 production path 不再实例化 `GeneratorAgent`。

但不要粗暴删除其中所有逻辑。

### 应迁移

- G1–G9 definitions；
- basis parsing / filtering；
- `_replace_basis` 一类 prompt basis injection helper；
- 可能仍有价值的 hypothesis action parser helper；
- breadth accounting；
- 5-of-9 assigned-op semantics。

### 不再保留为 production agent

这些生成能力改成：

```text
Scientist EXPLORE mode 中的一组 generative operators
```

而不是第二个智能体。

### 迁移策略

Codex 可先：

1. 把 basis/constants/helper 移到 `inquiry.py` 或 `generative_basis.py`；
2. Orchestrator 不再 import `GeneratorAgent`；
3. tests 全迁移后，再决定是否删除 `generator.py`；
4. 如 self-improvement / historical snapshot 仍引用 `generator.md`，先做兼容迁移，不要一次删除导致旧 run 不能恢复。

最终目标是：

> active runtime 中不存在 GeneratorAgent ↔ ProposerAgent 通讯。

---

## 4.5 `simpleloop/roles/orchestrator.py`

### 当前

```python
self.generator = GeneratorAgent(...)
self.proposer = ProposerAgent(...)
```

每 lane：

```text
Generator
  ↓
hypotheses
  ↓
Cognitive
  ↓
feedback_generator → regenerate
```

### 目标

只实例化：

```python
self.proposer = ProposerAgent(...)
```

每 lane：

```python
self.proposer.run_lane(
    assigned_ops=lane_ops,
    select_quota=lane_quota,
    ...
)
```

继续保留：

- lane 数；
- `ceil(N/K)` quota 逻辑；
- lane 并行；
- stable aggregation；
- 每 lane 5-of-9 operator sampling。

删除：

- generator transcript；
- generator regeneration count；
- hypothesis version callback；
- feedback callback；
- Generator/Cognitive partner terminology。

### 随机性

为 proposer-only A/B 增加 deterministic seed 支持。

例如 Orchestrator：

```python
def run(..., random_seed: int | None = None):
```

每 lane 从 deterministic RNG 派生 generative-op subset。

这样才能比较：

```text
Current × seed 0..9
vNext  × seed 0..9
```

而不是被随机 lens 配额干扰。

---

## 4.6 `simpleloop/roles/research_tools.py`

这是本次非常关键、不能漏掉的地方。

### 当前隐患

当前 Generator 虽然：

```python
memory_service=None
```

但 `ResearchCommandRunner` 仍接收：

```python
history_dir=run_dir
```

并把：

```python
history=self.history_dir
```

传给 research container。

这意味着：

> **“prompt 没给 history tools”不等于真正 history-blind。**

模型如果通过 shell 找到 `/history.jsonl` / `/rounds`，理论上仍可能读到搜索轨迹。

### 必须修改为真实隔离

推荐：

```python
class ResearchCommandRunner:
    def __init__(
        ...,
        history_dir: Path | None,
    ):
```

当 `history_dir is None`：

```python
research_exec_argv(..., history=None)
```

runtime 不能 mount history。

`ResearchTools` 增加：

```python
history_enabled: bool
```

Fresh phases：

```python
ResearchTools(
    history_enabled=False,
    history_dir=None,
    memory_service=None,
)
```

History-aware phases：

```python
ResearchTools(
    history_enabled=True,
    history_dir=run_dir,
    memory_service=memory_service,
)
```

并且：

- `inspect_episode`
- `list_findings`
- `search_findings`
- `inspect_finding`
- `search_experiments`

在 `history_enabled=False` 时必须 runtime reject。

**三层同时防护：**

```text
prompt 不展示
+
action guard 不允许
+
shell/runtime 不 mount
```

不要只做其中一层。

---

# 5. Phase 与 Action 设计

Action 不要覆盖每个认知动作。

不要：

```text
abduce()
deduce()
represent()
reflect()
```

那会把模型变成 API workflow executor。

只结构化：

1. 外部研究行为；
2. 认知 commitment；
3. phase rollback。

---

## 5.1 UNDERSTAND

### 认知目标

> 把 raw task 转化成明确的研究问题，调查整个问题边界，不寻找 intervention。

### 可用 action

```text
run_research_command
commit_understanding
```

建议：

```json
{
  "action": "commit_understanding",
  "problem": "...",
  "target_outcome": "...",
  "boundary": "...",
  "key_unknowns": ["...", "..."]
}
```

这里不要要求长 report。

`commit_understanding` 只是：

> “我已经知道接下来要建模的是什么”。

### 禁止

- history actions；
- emit lever map；
- submit hypothesis；
- submit proposal。

---

## 5.2 MODEL

### 认知目标

> 构造一个足以解释 target outcome、支持 counterfactual 和后续 explanation 的 Working Model。

### action

```text
run_research_command
propose_working_model
continue_investigation
commit_working_model
```

### 为什么同时有 propose / commit

不能让：

```text
填一次 WorkingModel
→ 自动进入 EXPLAIN
```

合理流程应该允许：

```text
propose model
    ↓
model check
    ↓
发现关键未知
    ↓
继续调查
    ↓
revise
    ↓
commit
```

### `commit_working_model` 最小语义

```json
{
  "action": "commit_working_model",
  "model_version": 2,
  "model_check": {
    "explains_target": "...",
    "counterfactual": {
      "change": "...",
      "predicted_effect": "...",
      "model_claim_refs": ["M1", "M3"]
    },
    "important_unknowns": [{"question": "...", "why_it_matters": "..."}],
    "blocking_unknown": null,
    "why_model_is_sufficient_for_next_stage": "..."
  }
}
```

如果：

```text
blocking_unknown != null
```

则不能 commit，应返回 protocol correction：

> 这个未知会阻止下一阶段形成可靠 explanation，应先调查它。Important unknown 可以保留并进入 competing accounts；Working Model 的标准是 sufficient，不是 complete。

这就是 investigation stopping principle。

### 不做的 hard gate

不要要求：

- 读 N 个文件；
- 必须画 call graph；
- 必须包含 process/state/cost 三个字段；
- 必须有 X 个 model claims。

OMILREC 的 Process/State/Cost 是评估参考，不是通用 prompt schema。

---

## 5.3 EXPLAIN

### 认知目标

> Form an account of the mechanism, structural limitation, obstruction, or dependency that makes the target difficult, produces the gap, or creates the opportunity.

必须将：

```text
system understanding
```

和：

```text
intervention search
```

隔开。

### action

```text
run_research_command
submit_explanation
commit_explanation_set
reopen_model
```

示例 schema：

```json
{
  "action": "submit_explanation",
  "id": "E2",
  "phenomenon": "...",
  "account": "...",
  "model_basis": ["M1", "M4"],
  "expected_if_true": ["...", "..."],
  "evidence_needed": ["..."]
}
```

### competing explanations

默认应保留多个 materially distinct explanation。

实现上不要硬写：

```python
assert len(explanations) >= 3
```

建议：

- competing accounts 的 target 是 2 个 materially distinct explanations；
- 低于 target 仍可 commit，但必须有：

```json
"explanation_sufficiency_justification":
  "why current evidence already makes alternatives non-material"
```

目的是阻止“看到 hotspot → 原因显然就是 hotspot”，不是为了凑数。

### EXPLAIN 阶段禁止 intervention commitment

Explanation 可以说：

> repeated event-invariant work may dominate cost

不要直接写：

> 因此加一个 EventContext class。

后者属于 EXPLORE。

---

## 5.4 FRESH EXPLORE

### 认知目标

> 在 Working Model + competing explanations 上广泛构造 mechanism-level intervention space。

此时仍不看 search history。

### 可用 action

```text
run_research_command
emit_lever_map
submit_hypothesis
commit_hypothesis_portfolio
reopen_explain
reopen_model
```

### 5/9 Generative Basis

保留现有 5-of-9 机制。

但输入从：

```text
source + lever map
```

变为：

```text
WorkingModel
+
ExplanationSet
```

Generative basis 是：

> 扩展已经构造好的问题表示中的可能性空间

而不是：

> 负责理解问题。

### Lever Map 重定义

旧 lever map 倾向：

```text
region / role / structural_space
```

新 map 应更机制化，例如：

```json
{
  "id": "L3",
  "target_mechanism": "...",
  "why_leverage_exists": "...",
  "model_basis": ["M2", "M5"],
  "explanation_basis": ["E1"]
}
```

对于代码优化：

- lifetime；
- ownership；
- evaluation frequency；
- representation；
- information/dataflow；
- algorithmic decomposition；

可以自然出现。

**但这些 OMILREC 例子绝不能写进 production prompt。**

否则 benchmark 被污染。

### Hypothesis

```json
{
  "action": "submit_hypothesis",
  "id": "H4",
  "generative_op": "G9",
  "model_basis": ["M2", "M3"],
  "explanation_basis": ["E2"],
  "mechanism": "...",
  "intervention_family": "...",
  "scope": "...",
  "why_plausible": "...",
  "critical_unknown": "...",
  "evidence_refs": []
}
```

Hypothesis 此时不要变成 executor plan。

### 宽度 gate

不要强迫：

```text
5 lens × 2 ideas = exactly 10
```

否则新架构会出现填 hypothesis quota。

推荐：

```python
breadth_target = max(4, select_quota * 2)
```

同时把现有 “5 lenses × 2” 保留为 **breadth budget / target**，不是必须一一填满的数量合同。
低于 `breadth_target` 的非空 portfolio 可以 commit，但必须用 `portfolio_sufficiency_justification` 说明 attempted space，以及为什么更多方向只会 cosmetic 或 unsupported；数量本身不是绝对 minimum。

`commit_hypothesis_portfolio` 需要说明：

- materially distinct mechanism families 已覆盖；
- 未使用 lens 可以有理由；
- portfolio 中不存在明显同机制 cosmetic variants。

---

# 6. History Injection

Fresh Portfolio 一旦 commit：

```text
history_visible = False
          ↓
INJECT
          ↓
history_visible = True
```

Harness 做三件事：

1. 开启 memory actions；
2. research shell 开始 mount history；
3. 向当前 Scientist context 注入一个**紧凑的历史入口信息**。

不建议一次 dump 全部 history。

只注入：

- bounded factual experiment index（id、target、outcome、metric、changed scope）；
- 可用 memory tools 及按需检索方式。

不要在入口 dump abstentions、Findings interpretation、Explore 或 Frontier narrative。History injection 是 evidence entry point，不是旧研究世界观。

Scientist 自己决定读哪些历史。

### Scientist continuity 不等于 raw messages identity

必须连续的是同一个 `ScientistSessionState`、committed artifacts 与 evidence lineage，而不是同一个 Python `messages` object。第一版可以复用 transcript；phase boundary compaction 或 serialized scientific state + new context 也合法，只要不改变 epistemic `context_id` 或丢失可见性。

我们希望：

> 历史是后来进入同一个研究者视野的新 evidence。

它应该看到：

```text
“这是我在不知道历史时自己形成的 Model / Explanation / Portfolio”
```

然后拿历史来挑战它。

不是启动另一个 Reviewer Agent。

第一版 strict fresh history blindness 是建立干净实验条件的 policy，不是普遍科学方法论；prior empirical evidence 提前可见的变体留给后续独立 A/B。

---

# 7. NARROW

### 认知目标

> 历史现在是 evidence。哪些独立形成的 direction 仍值得实验成本？

### action

```text
run_research_command
inspect_episode
list_findings
search_findings
inspect_finding
search_experiments

select_for_deepen
continue_explore
reopen_explain
reopen_model
fresh_reframe
abandon_portfolio
```

### `select_for_deepen`

```json
{
  "action": "select_for_deepen",
  "selected": [
    {
      "hypothesis_id": "H4",
      "evidence_refs": ["experiment:r3c0", "finding:F-003"],
      "rationale": "..."
    }
  ]
}
```

最多 `select_quota`。

### Narrow 的科学判断边界

旧 cognitive prompt 非常强调：

> 不要判断“worth trying”，Harness 才知道 performance。

新 Scientist 需要稍微改变这一点。

Scientist **必须能判断哪条研究路线值得有限实验预算**，否则 Narrow 不成立。

但它不能：

- 把未运行的性能结果说成事实；
- 自己宣布 gate 会通过；
- 自己宣布某方案一定更快；
- 替 Harness 判定结果。

准确边界：

> **Scientist 可以根据证据做 research allocation judgement；Harness 仍是实验事实与最终 objective authority。**

---

# 8. 新证据后的回退：删除统一 `regenerate`

这是本次合并最重要的结构变化之一。

不再：

```text
feedback_generator()
→ regenerate()
```

而是问：

> 新 evidence 推翻的是哪一层？

## 8.1 Direction 被否定

```text
NARROW / DEEPEN
       ↓
continue_explore
       ↓
EVIDENCE-AWARE EXPLORE
```

保留：

- Working Model；
- Explanation Set；
- history visible = true。

## 8.2 Explanation 被否定

```text
reopen_explain
```

例如过去实验证明：

> 移除某类 repeated preparation 后速度几乎不变。

那么“这类 repeated work 是 dominant cause”的 E2 应被修订。

## 8.3 Working Model 被否定

```text
reopen_model
```

例如源码/历史证明原来认为共享的 state 实际生命周期完全不同。

重新建模。

但：

```text
history_visible = true
```

不回退。

## 8.4 Frame / representation 本身疑似被锚定

只有这时：

```text
fresh_reframe
```

### `fresh_reframe` 必须创建新的 context

新 context 只得到：

- goal；
- current accepted artifact/source；
- gates/constraints；
- current-world evidence access；
- 一个非常抽象的 reframe instruction，例如：

> Construct an independent representation of the problem from current evidence. Do not inherit the previous problem model.

不要把：

- old model；
- old explanations；
- old hypotheses；
- old proposal terms；
- search history；

传进去。

Parent runtime 保存原 state，fresh episode 完成：

```text
UNDERSTAND
→ MODEL
→ EXPLAIN
→ FRESH EXPLORE
```

后再注入 history。

第一版：

```python
MAX_FRESH_REFRAMES = 1
```

避免递归爆炸。

---

# 9. DEEPEN

### 认知目标

> 已经值得把昂贵的详细研究预算花到少数 hypothesis 上。验证 critical premise，推导 observable prediction，形成 intervention。

### action

```text
run_research_command
history actions

submit_proposals
return_to_narrow
continue_explore
reopen_explain
reopen_model
fresh_reframe
abandon_direction
```

### 每个 selected hypothesis 先解决

```text
critical_unknown
        ↓
targeted investigation
        ↓
evidence
        ↓
survive / fail
```

只有 survive 后再形成 proposal。

### 强制顺序的不是 tool list，而是 lineage

```text
Model Claim(s)
       ↓
Explanation
       ↓
Hypothesis
       ↓
Critical premise evidence
       ↓
Intervention
       ↓
Prediction
```

---

# 10. Proposal Schema

当前 `ResearchProposal` 如已有固定 serializer，优先保持 backwards compatibility。

建议新增 optional structured metadata，而不是破坏 Worker 读取文本。

例如：

```python
@dataclass
class ResearchProposal:
    # existing fields...
    ...

    model_claim_refs: tuple[str, ...] = ()
    explanation_refs: tuple[str, ...] = ()
    hypothesis_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    mechanism: str = ""
    prediction: str = ""
    affected_scope: str = ""
```

最终给 Executor 的 proposal text 仍然自然语言、身份内化，不要变成 schema dump。

但 trace 中必须能机械追：

```text
M* → E* → H* → evidence → P*
```

---

# 11. HypothesisCard / Diversity Signature 的处理

当前 `HypothesisCard` 是：

```text
region
× mechanism
× intervention_family
```

且 `region` 很容易落到文件/path。

这与下一版目标有一点张力，因为 mechanism-level / architecture-level direction 可能没有单一 file region。

有两个可选方案。

## 推荐方案：升级为 `ResearchHypothesis`

见 `inquiry.py`。

Diversity signature：

```text
mechanism_family
× intervention_family
× coarse_scope
```

其中 `coarse_scope` 可以是：

- subsystem；
- pipeline stage；
- whole-system；
- proof route；
- detector/inference component；

而不要求 path。

## 低风险兼容方案

暂时继续复用 `HypothesisCard`，但：

- `region` 允许 subsystem/whole-system；
- 不再自动把它理解成 path；
- 增加 model/explanation lineage；
- 后续再更名。

Codex 可根据本地引用范围选择，但最终不要让 file path 成为 breadth diversity 的中心定义。

---

# 12. Prompt 重构

## 12.1 `proposer.md` 成为唯一 Active Scientist Identity

旧：

```text
cognitive element
Sieve → Enrich
partner: Generator
```

全部移除。

新的 identity 只讲“你是谁、怎样研究”。

建议核心：

> You are the scientist responsible for deciding which interventions are worth an experiment.
>
> A plausible local opportunity is evidence about the problem, not yet the problem definition. Treat observations as clues about a larger structure or mechanism until you understand how the target outcome is produced.
>
> Do not seek completeness. Seek a model sufficient to make the next consequential research decision. When you investigate, prefer questions whose answers could change your representation, explanation, or choice of direction.
>
> Build working models that support explanation, counterfactual reasoning, and prediction—not summaries of available facts.
>
> Resist premature commitment. Understand broadly before spending detailed effort on one direction; keep materially different explanations alive before narrowing.
>
> Your models are provisional. When evidence contradicts a direction, repair the level of understanding that failed rather than defending the proposal.
>
> A proposal is justified only when it follows from a mechanism you understand and makes a prediction that an experiment can test.

### 不要写进 identity prompt 的东西

不要写：

```text
Step 1 UNDERSTAND
Step 2 MODEL
Step 3 ...
```

不要写：

```text
read at least 10 files
```

不要写 OMILREC 的：

```text
state lifetime
ownership
shared EventContext
charge/time
```

这些都是 benchmark leakage。

## 12.2 Phase Attention Block

由 runtime 动态附加一个很短的 block。

### UNDERSTAND

> Current mode: UNDERSTAND. Do not search for modifications yet. Investigate broadly and form a coarse account of how the whole problem produces the target outcome; restating the goal is not understanding. Identify unknowns that could change your later model.

### MODEL

> Current mode: MODEL. Construct a working representation that can explain the target outcome and support counterfactual reasoning. A list of components or facts is not sufficient. Do not seek completeness; seek a model sufficient for the next consequential research decision.

### EXPLAIN

> Current mode: EXPLAIN. Form an account of the mechanism, structural limitation, obstruction, or dependency that produces the gap or creates the opportunity. Keep materially different accounts alive where evidence permits. Do not design the intervention yet.

### EXPLORE

> Current mode: EXPLORE. Using the working model and explanations, search broadly across materially different mechanism families before investing deeply in any one direction.

### NARROW

> Current mode: NARROW. Past experiments are now available as evidence. Use them to support, refute, or revise the independently formed model and hypotheses. Historical vocabulary must not replace your own representation.

### DEEPEN

> Current mode: DEEPEN. Detailed investigation is now justified. Test each selected hypothesis's critical premise, trace its real scope, derive observable consequences, and submit only if the mechanism survives.

## 12.3 Identity / Phase / Harness 分工

```text
Identity Prompt
    = 什么叫专业研究

Phase Attention
    = 现在注意什么

Harness
    = 现在能看什么、能做什么、能 commit 什么
```

不要把三者混成一个超长 prompt。

---

# 13. Generative Basis 迁移

现有 G1–G9 的思想保留。

但修改其语义位置：

```text
旧：
survey source
→ lever map
→ G1-G9
→ idea

新：
Working Model
→ Explanation Set
→ mechanism-level Leverage Map
→ G1-G9
→ Research Hypotheses
```

这意味着生成元不再承担：

> “帮我理解问题。”

它承担：

> “在已经形成的问题表示中，系统扩展解释/干预空间。”

Codex 应尽量复用现有 basis 文本和 5-of-9 调度，但把 generator-specific identity 文案迁出。

---

# 14. Step Budget 与 Budget Exhaustion

不要再把固定 step 当 phase transition。

设计：

```text
phase defines cognitive objective
action loop remains free
commit action ends phase
```

例如：

```text
MODEL
  research
  propose model
  research
  revise
  research
  commit
```

### 总 budget

第一版建议使用**一个总 Scientist step budget**。

配置只接受 `scientist_steps`。`generator_steps`、`gen_steps` 与 `cognitive_steps` 属于已删除架构，出现时直接报配置错误，不做映射。默认 `scientist_steps = 364` 只是 hard safety ceiling，不是研究深度目标或 phase quota；一旦 artifact 足以支持下一 consequential decision，应尽早 commit。

但不规定：

```text
UNDERSTAND 5 steps
MODEL 5 steps
...
```

必须统计 steps to WorkingModel、steps to portfolio、steps to terminal outcome、每 phase tool/token/wall-time、以及 WorkingModel revision 序列，但不要硬切。

### 必须删除 partial proposal fallback

当前 proposer 的：

```text
Budget nearly exhausted. Submit your enriched proposal now...
```

以及“没有 proposal 就从 hypothesis 自动构造 partial proposal”的行为必须删除。

新语义：

```text
budget exhausted
+
no scientifically committed proposal
=
research_incomplete / abstain
```

proposer-only CLI 应输出已有：

- WorkingModel；
- Explanation；
- Portfolio；
- current phase；
- reason。

**宁可没有 proposal，也不要为了 API 成功率生成浅 proposal。**

这是这次认知重构能否成立的关键。

---

# 15. Tool Prompt 也必须 phase-aware

当前 `render_research_tool_prompt()` 会渲染所有 research tool spec。

修改为类似：

```python
render_research_tool_prompt(
    allowed_actions: set[str],
)
```

或者：

```python
ResearchToolView.for_phase(
    phase,
    history_visible,
)
```

模型只看到当前真正可用的 actions。

必须保证：

```text
prompt-visible actions
==
parser-allowed actions
==
guard-allowed actions
==
runtime-executable actions
```

避免“prompt 说不能、runtime 实际可以”。

---

# 16. proposer-only CLI：本次开发的主 benchmark

## 16.1 本地版本优先

公开 GitHub snapshot 中目前看不到用户新增的 proposer-only CLI。

因此 Codex 开始工作时：

1. 先读本地 `simpleloop/cli.py`；
2. 找到用户新增 proposer-only command；
3. **保留并扩展它**；
4. 不按公开版本重写或覆盖。

## 16.2 CLI 必须输出 Structured Research Trace

建议每 lane 输出一个 JSON：

```json
{
  "lane_id": 0,
  "assigned_generative_ops": ["G1", "G3", "G5", "G7", "G9"],

  "phase_transitions": [
    {"from": "understand", "to": "model", "step": 4},
    ...
  ],

  "history_injected_at_step": 18,

  "understanding": {...},
  "working_model": {...},
  "explanations": [...],
  "lever_map": [...],
  "fresh_hypotheses": [...],

  "narrow_decisions": [...],

  "selected_hypotheses": [...],
  "deep_evidence": [...],
  "proposals": [...],

  "reopen_counts": {
    "explore": 0,
    "explain": 1,
    "model": 0
  },

  "fresh_reframes": 0,

  "usage_by_phase": {
    "understand": {...},
    "model": {...},
    ...
  },

  "outcome": "proposals | research_incomplete | block"
}
```

### 注意

Trace 记录：

- action；
- tool observation；
- structured research artifact；
- phase transition；
- evidence refs。

不要求输出模型 hidden chain-of-thought。

我们评估的是**外显科研行为**。

## 16.3 Reproducibility

建议 CLI 支持或保留：

```text
--seed
--lanes
--trace-dir
```

如果已有类似选项则复用。

---

# 17. OMILREC proposer-only 认知验收协议

这一阶段**不要先跑 Candidate Worker**。

先固定同一个 OMILREC source checkout，比较 current 与 vNext。

## 17.1 Baseline capture

在改代码前保存：

```text
baseline/current/seed-00
...
baseline/current/seed-09
```

至少 5 次，最好 10 次。

保存：

- prompt snapshot；
- model；
- config；
- source SHA；
- random seed；
- proposer trace；
- final proposals；
- token/tool usage。

## 17.2 vNext

相同：

- source SHA；
- model；
- candidate quota；
- approximate total step/token budget；
- seeds；

跑 5–10 次。

## 17.3 首轮只评估 cognition

### Metric A — Premature locality

在 WorkingModel commit 前，是否已经锁定：

- 一个函数；
- 一个 loop；
- 一个具体 patch。

如果是，扣分。

局部文件当然可以作为证据读取，但不能成为早期 research commitment。

### Metric B — Whole-problem Working Model

对于 OMILREC，一个自然的高质量模型应该有能力描述：

- reconstruction process；
- optimizer / repeated FCN relation；
- major state/dependency/lifetime；
- total cost 如何产生。

**这些只作为 evaluator rubric，不写进 production prompt。**

### Metric C — EXPLAIN quality

必须看到从 observation 到 competing mechanism：

```text
hot FCN
≠
therefore optimize FCN locally
```

而是有类似：

```text
arithmetic intrinsic cost
vs repeated invariant work
vs representation/ownership
vs call topology
...
```

同样：这些具体答案只用于人工 evaluator，不喂给模型。

### Metric D — Mechanism-level breadth

Fresh Explore 的差异应该来自：

```text
mechanism families
```

而不是：

```text
同一 hotspot 的不同 micro optimization wording
```

### Metric E — Macro→Micro

DEEPEN 中读取具体源码的路径应该由 selected hypothesis 驱动。

例如：

```text
hypothesis about lifetime
→ 查 producer/consumer/loop boundary
```

而不是：

```text
继续随机扫热点
```

### Metric F — Proposal Lineage

能否明确追：

```text
M* → E* → H* → evidence → proposal/prediction
```

### Metric G — Abstraction consistency

只测，不强制：

```text
L0 expression / loop tiny local
L1 function-level mechanism
L2 cross-component ownership/interface/lifecycle/dataflow
L3 algorithmic / architectural decomposition
```

目标不是“所有 proposal 都必须 L2/L3”，也不把高 abstraction 当成天然更好。真正看的是 proposal level 是否匹配 Working Model 指向的 leverage level：局部机制可以推出 L1；ownership/lifecycle root mechanism 却只交 cosmetic local cache 才是不一致。L0–L3 discovery rate 仅作观察。

### Metric H — Factual accuracy

宏观不能靠幻觉换来。

WorkingModel claim 与 source 是否一致。

---

# 18. 不要污染 OMILREC benchmark

以下期望**绝对不能写进 proposer prompt**：

- EventContext；
- shared charge/time state；
- state lifetime mismatch；
- FCN call-count；
- ownership；
- data-oriented likelihood state；
- specific future OMILREC optimization family；
- “请提出跨文件修改”。

这些只存在：

- 本实施文档；
- 人工 evaluator rubric；
- retrospective comparison。

否则我们无法判断 Scientist 是自己推出来的，还是被提示词喂出来的。

---

# 19. Tests 迁移

当前测试明显编码了旧 Generator/Cognitive split。

Codex 必须系统迁移，不要为了让旧 test pass 保留假架构。

## 19.1 新增 `tests/test_scientist_proposer.py`

至少覆盖：

### Phase guard

- UNDERSTAND 不能 submit hypothesis/proposal；
- MODEL commit 前不能 EXPLAIN；
- EXPLAIN commit 前不能 fresh explore commit；
- EXPLORE 前不能 history search；
- NARROW 前不能 submit proposal；
- DEEPEN 才能 terminal submit。

### History lock

Fresh：

- memory actions reject；
- `ResearchTools.memory is None`；
- shell/container 不 mount history；
- prompt 不显示 history actions。

Injection 后：

- history mount 打开；
- memory actions 可用；
- `history_visible=True`。

### Monotonic visibility

同一 session：

```text
False → True
```

后不能重新设 False。

### Fresh reframe

- 创建 new message context；
- new tools 是 history-locked；
- 不携带 old model/explanation/hypothesis/history text；
- parent session state 不丢；
- max reframe bound 生效。

### Lineage

- explanation 必须引用 valid model claim；
- hypothesis 必须引用 valid model/explanation；
- proposal 必须引用 selected hypothesis；
- evidence refs 解析兼容。

### Budget exhaustion

无 valid proposal：

```text
outcome = research_incomplete
```

不得自动合成 partial proposal。

---

## 19.2 Orchestrator tests

- 不再 import/instantiate `GeneratorAgent`；
- assigned G ops 传进 Scientist；
- lane quota 不变；
- concurrency 不变；
- stable aggregation 不变；
- seed 可复现；
- no `feedback_generator` callback。

---

## 19.3 Tool tests

最关键：

```text
history_enabled=False
```

时 container argv 中不存在 history bind。

不要只测 memory service None。

---

## 19.4 旧 tests

预计：

- `tests/test_generator.py`
- `tests/test_cognitive_element.py`
- `tests/test_orchestrator.py`

需要迁移。

`test_generator.py` 中有价值的：

- generative-op assignment；
- lever map prerequisite；
- hypothesis parsing；

迁移到 Scientist EXPLORE tests。

旧：

```text
feedback_generator → regenerate
```

测试删除，替换 cognitive rollback tests。

---

# 20. Prompt Self-Improvement 兼容

当前 SimpleLoop 有 development-time prompt self-improvement。

Codex 必须先 inspect 本地：

- active prompt names；
- prompt snapshot migration；
- historical four-prompt compatibility。

目标：

```text
active:
proposer.md
executor.md
meta_optimizer.md
```

`generator.md` 不再是 active production semantic。

但如 historical snapshots 仍包含 generator prompt：

- 继续支持读取/审计旧 snapshot；
- migration 恢复到新 active set；
- 不要破坏旧 run 可追溯性。

Scientist 的 phase protocol/action grammar 建议属于**runtime hard structure**，不让 meta-optimizer 随意删除。

`proposer.md` 可继续 evolvable，但它优化的是 Scientist identity / methodology wording，而不是修改 phase visibility invariant。

---

# 21. Codex 实施顺序

本次虽然是“一次性重构”，但 coding agent 执行需要依赖顺序。

## Step 0 — Baseline

在任何改动前：

```text
git status
pytest
```

然后用本地 proposer-only CLI 保存 current traces。

**必须先有 baseline。**

## Step 1 — Inquiry data model

新增：

```text
InquiryPhase
InquiryState
WorkingModel
Explanation
ResearchHypothesis
trace models
```

暂不改变行为。

tests。

## Step 2 — 真正的 History Lock

先改：

```text
research_tools.py
ResearchCommandRunner
ResearchAgent._make_tools
```

支持 history disabled。

tests 必须证明 shell 不 mount history。

这是整个 fresh inquiry 的基础。

## Step 3 — Scientist phase runtime

在 `ProposerAgent` 中实现：

```text
UNDERSTAND
MODEL
EXPLAIN
EXPLORE
NARROW
DEEPEN
```

先不删除 Generator。

用 synthetic model replies 测 phase guard/action transition。

## Step 4 — 迁移 Generative Basis

把 G1–G9 / assigned subset / relevant parser/helper 接到 EXPLORE。

确保：

```text
model + explanation
```

是 generation 上游。

## Step 5 — History Injection / cognitive rollback

实现：

```text
portfolio commit
→ enable history
→ NARROW
```

及：

```text
continue_explore
reopen_explain
reopen_model
fresh_reframe
```

## Step 6 — Orchestrator 合并

删除：

```text
self.generator
generator.run
generator.regenerate
feedback callback
```

每 lane 直接调用 Scientist。

## Step 7 — 删除 Partial Submit

改为：

```text
research_incomplete
```

## Step 8 — Prompt 迁移

改 `proposer.md`。

将 generator basis 迁入新的 Scientist semantics。

处理 prompt self-improvement compatibility。

## Step 9 — proposer-only CLI

适配本地现有 command：

- structured trace；
- seed；
- phase usage；
- research_incomplete；
- artifact output。

## Step 10 — Test migration

删除对旧角色 split 的强制假设。

全部 tests 通过。

## Step 11 — Cognition-only evaluation

跑 vNext 5–10 次 OMILREC proposer-only。

**先不要跑 Worker。**

收 traces。

## Step 12 — 人工诊断

根据：

- premature locality；
- model quality；
- explanation quality；
- mechanism breadth；
- macro→micro；
- proposal lineage；
- proposal level 与 model-implied leverage level 的一致性；
- steps-to-artifact、model revisions 与 phase cost；

决定是否调整：

- identity wording；
- phase attention；
- action contract；
- artifact dependency。

只有认知路径基本过关后，才进入完整 Worker/Gate loop。

---

# 22. Mechanical Acceptance Criteria

Codex 宣布实现完成前必须满足：

- [ ] Orchestrator production path 不再实例化 `GeneratorAgent`；
- [ ] `feedback_generator` 不再是 production action；
- [ ] 一个 lane 一个 Proposer Scientist context；
- [ ] Fresh UNDERSTAND/MODEL/EXPLAIN/EXPLORE 不能访问 memory；
- [ ] Fresh research shell 不 mount history；
- [ ] history 只在 fresh portfolio commit 后开启；
- [ ] 同一 context history visibility 不可从 True 回 False；
- [ ] fresh_reframe 使用新 context；
- [ ] Explanation 位于 Model 与 Explore 之间；
- [ ] Hypothesis 引用 Model/Explanation；
- [ ] Proposal 引用 selected hypothesis + evidence + prediction；
- [ ] proposal 只能从 DEEPEN terminal 提交；
- [ ] budget exhaustion 不自动制造 partial proposal；
- [ ] G1–G9 breadth capability 保留；
- [ ] multiple lanes / quota / stable aggregate 保留；
- [ ] local proposer-only CLI 可运行；
- [ ] CLI 可输出 structured research trace；
- [ ] deterministic seed 可用于 A/B；
- [ ] tests pass。

---

# 23. Cognitive Acceptance Criteria

这些**不写成 hard code gate**，由用户根据 proposer-only runs 判断。

我们希望看到：

### 1. 它真的先调查 whole problem

不是：

```text
grep hotspot
→ 锁定 patch
```

### 2. WorkingModel 是推理模型

能解释：

```text
target outcome 如何产生
```

并做 counterfactual。

### 3. EXPLAIN 形成机制竞争

不是一个漂亮的“原因”段落。

### 4. Fresh Explore 是 mechanism space

不是局部代码 variation。

### 5. History 真的是后注入 evidence

第一次 broad model/search 不被旧 proposal vocabulary 主导。

### 6. Narrow 会修 belief

历史能导致：

```text
direction repair
explanation repair
model repair
```

而不是只有“换 idea”。

### 7. Deepen 有明确 research question

每个 micro source read 都能解释：

> 为什么这个事实会改变 selected hypothesis 的判断？

### 8. Proposal 是推导结果

不是独立于前面 research artifacts 的第二次生成。

---

# 24. 明确禁止 Codex 擅自做的设计

为了避免 implementation agent“顺手优化架构”，以下不属于本次任务：

- 不做跨-round persistent Scientist；
- 不让 Scientist 自己管理整个 Loop；
- 不做 Research Tree / Frontier 自动演化；
- 不增加 Reviewer/Critic 第二 Agent；
- 不增加自动 proposal merit scorer；
- 不做复杂 Bayesian belief store；
- 不要求固定文件阅读数；
- 不要求固定 tool sequence；
- 不要求 proposal 必须跨文件；
- 不要求 proposal 必须是 architecture-level；
- 不把 OMILREC expected solutions 写进 prompt；
- 不让 Harness 判断具体 scientific explanation；
- 不因为 phase step budget 到点强制 transition；
- 不为了兼容旧 tests 保留虚假的 Generator/Cognitive production split；
- 不在 fresh phase 仅通过 prompt 隐藏 history、却仍然 mount history。

---

# 25. 推荐的最终代码形态

```text
simpleloop/
└── roles/
    ├── research_agent.py
    │     generic LLM/tool runtime
    │
    ├── research_tools.py
    │     phase/history-aware tool view
    │
    ├── inquiry.py
    │     InquiryPhase
    │     InquiryState
    │     WorkingModel
    │     Explanation
    │     ResearchHypothesis
    │
    ├── proposer.py
    │     ONE Proposer Scientist runtime
    │
    ├── orchestrator.py
    │     multi-lane scheduling only
    │
    ├── hypothesis.py
    │     migrate/compat as needed
    │
    └── generator.py
          legacy during migration;
          no production agent dependency
```

最终职责：

```text
Orchestrator
    = how many independent Scientist lanes

Proposer Scientist
    = how one lane conducts inquiry

ResearchAgent
    = how an LLM/tool session runs

ResearchTools
    = what evidence the current phase can access

Worker
    = how a proposal becomes an experiment
```

---

# 26. 一份可直接给 Codex 的任务说明

下面这段可以和本文一起交给 Codex：

---

## Codex Mission

You are implementing the next SimpleLoop Proposer architecture in the local WSL repository.

Read this implementation plan and the accompanying inquiry research document before editing. Then inspect the local repository carefully; the local clone is authoritative and includes a proposer-only CLI that may not exist in the public branch.

The goal is **not** to mechanically create six enum phases. The goal is to make one lane-local Proposer Scientist actually conduct inquiry from broad/shallow to narrow/deep:

```text
UNDERSTAND
→ MODEL
→ EXPLAIN
→ FRESH EXPLORE
→ HISTORY INJECTION
→ NARROW
→ DEEPEN
→ PROPOSE
```

Preserve the existing multi-lane outer architecture, research tool infrastructure, generative basis, Worker/Harness boundary, and memory system where compatible.

The essential invariants are:

1. There is no production GeneratorAgent → CognitiveAgent boundary.
2. Search history is genuinely inaccessible before fresh exploration is committed — not only hidden from the prompt, but unavailable through memory tools and research-shell mounts.
3. History visibility is monotonic within one model context.
4. Explanation is distinct from Working Model and intervention exploration.
5. New evidence repairs the cognitive layer it invalidates:
   - direction → continue_explore
   - explanation → reopen_explain
   - model → reopen_model
   - representation anchoring → fresh_reframe in a new clean context
6. Artifacts are not decorative forms. Explanation must depend on model claims, hypotheses on model/explanation, and proposals on selected hypotheses/evidence/predictions.
7. Budget exhaustion must not manufacture a shallow partial proposal.
8. The production prompt must not contain OMILREC-specific expected optimization ideas.

Before large edits, capture baseline proposer-only traces. Implement incrementally with tests. After the runtime is complete, run the proposer-only CLI on OMILREC and return:

- git diff summary;
- test results;
- at least 3 full structured Scientist traces;
- final proposals;
- any places where the model repeatedly shortcuts the intended cognition;
- no full Candidate Worker optimization run yet.

Do not “improve” the design beyond this plan without explicitly documenting why the existing local code makes a specified item impossible or unsafe.

---

# 27. 最终开发判断

这次重构真正应该让我们回答的不是：

> “Scientist state machine 跑通了吗？”

而是：

> **“在完全不告诉模型 OMILREC 应该做 state lifetime / ownership / call topology 等架构优化的情况下，它能不能通过自己的 whole-problem inquiry，自主把这些或其它同等级的结构性 mechanism 提出来？”**

如果能：

下一步才值得让这个 Scientist 跨 round 保持 Working Model / Frontier，逐渐接管 Loop。

如果不能：

先不要扩大系统。直接根据 proposer-only traces 判断问题属于：

- UNDERSTAND 没有真正获得 whole view；
- WorkingModel 只是 summary；
- EXPLAIN 不会构造 competing mechanism；
- EXPLORE 仍被局部代码结构绑架；
- phase contract 太弱；
- artifact 变成填表；
- 或基础模型本身缺少 scientific problem construction 能力。

这就是 proposer-only CLI 在这一阶段最大的价值：  
**在任何昂贵实验之前，把“研究者到底会不会研究”单独测出来。**
