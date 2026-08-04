# SimpleLoop Research History & Scientific Memory 重构设计

## 1. 重构结论

SimpleLoop 当前的问题不是 Proposer 每轮使用新会话，也不是 Observe–Investigate–Checkpoint 状态机本身，而是：

> **新一轮 Proposer 醒来后，接收到的 persistent laboratory state 被错误地设计成了“上一轮候选的强制总结任务”。**

当前实现要求：

1. Proposer 读取上一轮每个 candidate；
2. 在 `submit_proposals` 中为每个 candidate 生成 annotation；
3. Loop 将 annotation 回填到上一轮 candidate 的 `note`；
4. 后续 Proposer 启动时看到全历史 `ref: note` 目录；
5. 最新一轮没有 note，并被明确描述为“this round's job”。

这条链路由 `ProposerResult.annotations`、`_parse_annotations()`、`inflight_round.json`、`Store.backfill_notes()` 和 `render_history_directory()` 共同构成。它把上一轮候选变成下一轮的注意力中心，并把历史维护绑定到了 proposal 提交路径。

本次重构应整体废弃：

```text
Candidate Note
Round Annotation
上一轮全覆盖总结
note backfill
ref: note 全历史目录
```

新的核心研究对象应当是：

```text
Experiment
Finding
Research Frontier
Retrieval Context
```

目标架构：

```text
Immutable Experiment Ledger
        ↓
Active Findings Archive
        ↓
Diversity-aware Retrieval
        ↓
Fresh Proposer Scientist Session
        ↓
New Proposals linked to Findings
        ↓
Executor + Harness
        ↓
New Experiment Evidence
```

---

## 2. 保留与废弃的现有设计

### 2.1 保留：Proposer Scientist 状态机

当前 `ProposerAgent.run()` 在单一 `messages` 中维护 `ResearchPhase`，Observe、Investigate 和 Checkpoint 只是状态转换，不是三个 Agent。Research tools 不改变 phase，只有控制动作改变状态，只有 Checkpoint 可以 `submit_proposals`。

继续保持：

```text
Round 内：
    一个 Scientist
    一个 system prompt
    一条 messages 链
    Observe ↔ Investigate ↔ Checkpoint

Round 间：
    新 Scientist session
    继承外部显式研究状态
```

### 2.2 保留：Harness 的不可变事实权

当前 `history.jsonl` 已经接近合格的 Experiment Ledger。`Store.append_generation()` 自动保存：

- proposal；
- parent SHA；
- candidate SHA；
- status；
- bounded eval output；
- metrics；
- changed paths；
- gates；
- gate_passed；
- eligible；
- selected；
- telemetry。

这些事实由 Harness 自动产生，不依赖 LLM 解释，应继续作为唯一证据源。

### 2.3 废弃：强制 annotations

删除：

```python
class ProposerResult:
    proposals: list[str]
    annotations: list[dict]
```

改为：

```python
class ProposerResult:
    proposals: list[ResearchProposal]
    usage: object = None
```

删除：

- `_parse_annotations()`；
- `_prior_candidate_refs()`；
- terminal action 中的 `annotations`；
- 对上一轮 candidate ref 的全覆盖校验；
- annotation 长度限制；
- annotation 缺失导致的协议失败。

### 2.4 废弃：note 回填事务

从 `loop.py` 删除：

- `round_annotations`；
- inflight journal 中的 `annotations`；
- `Store.backfill_notes()`；
- `_reconcile_completed_inflight()` 中的 note backfill；
- “当前轮先持久化、然后回填上一轮 note”的恢复逻辑。

新的 inflight journal 只需要保存：

```yaml
round_id:
parent_sha:
proposals:
jobs:
```

### 2.5 废弃：全历史 `ref: note` 启动上下文

当前 `_initial_context()` 会把所有历史 candidate 渲染为 `ref: note`，并明确告诉模型最新一轮补 note 是当前任务。

这一部分改成：

```text
Current accepted state
Recent factual dashboard
Research frontier
Memory access instructions
```

而不是注入全历史语言摘要。

---

## 3. 设计依据

### 3.1 Experiment Archive，而不是连续对话

AlphaEvolve 将生成程序和 evaluator 结果保存在 programs database，由数据库决定未来 prompt 使用哪些程序；持续性存在于 archive 和采样策略，而不是单个永久 LLM 会话。

因此 SimpleLoop 每轮启动新 Proposer session 是合理设计，不应恢复跨轮完整 messages。

### 3.2 Finding 是研究问题级对象

DeepScientist 维护结构化 Findings Memory。记录按照 Idea Finding、Implement Finding、Progress Finding 等研究成熟度组织；实验结果更新对应 Finding，而不是为每次实验生成独立的未来指导 note。其规模达到数千条时，会由独立 retrieval model 选取少量 Top-K Findings 输入当前研究周期。

SimpleLoop 应借鉴 Finding 概念，但不照搬其 reviewer、UCB 和多级高成本验证体系。

### 3.3 Active Research State 与长期知识分离

AutoSci 将正在进行的 Idea、Experiment 等项目对象放入 Active Research Memory，并为它们维护显式 lifecycle；跨项目可复用的知识则进入独立 Long-Term Knowledge Memory。系统可以恢复研究状态而不依赖 chat history。

SimpleLoop 当前只需要项目内 Active Research Memory，不需要完整跨项目知识图谱。

### 3.4 检索必须兼顾探索

DeepScientist 显式区分 utility、quality 与 exploration value，并使用 acquisition function 平衡 exploitation 和 exploration。

SimpleLoop 暂时不需要 UCB，但 retrieval 不能只返回语义最相似历史，否则仍会持续强化当前代码区域和机制族。

---

## 4. 新的 Memory 数据模型

### 4.1 Experiment Ledger

存储位置：

```text
runs/<run_id>/history.jsonl
```

继续采用 append-only，每条 candidate 是一个 Experiment。

建议增加：

```yaml
experiment_id: r12c3
finding_id: F-008
research_question: optional snapshot
proposal_family: optional machine index
schema_version: 2
```

完整 candidate schema：

```yaml
experiment_id: r12c3
round: 12
candidate: 3

finding_id: F-008

proposal: >
  Hoist repeated QPDF bin lookup outside the event-inner loop.

parent_sha: abc...
candidate_sha: def...

status: COMPLETED

metrics:
  SPEED_MS: 706.2

gates:
  FCN:
    passed: true
  E2E:
    passed: true

gate_passed: true
eligible: true
selected: false

changed_paths:
  - OMILRECV2/src/OMILREC.cc

eval_block: ...
telemetry: ...
```

原则：

- Experiment 永远不被 LLM 改写；
- 不保存 LLM 对结果的权威解释；
- 不回填 note；
- 所有高层记忆必须通过 `experiment_id` 回溯到这里。

### 4.2 Active Finding

Finding 不是总结，也不是结论，而是一个持续研究问题的容器。

建议存储：

```text
runs/<run_id>/memory/findings.jsonl
```

或：

```text
runs/<run_id>/memory/findings/F-008.json
```

MVP schema：

```yaml
id: F-008

question: >
  QPDF 热循环中是否仍存在可安全提升的不变量工作？

scope:
  mechanisms:
    - invariant-hoisting
    - lookup
  code_regions:
    - OMILRECV2/src/OMILREC.cc

state: active

created_round: 3
last_touched_round: 12

experiment_refs:
  - r3c0
  - r7c1
  - r12c3

parent_finding_id: null

stats:
  attempts: 3
  eligible: 2
  selected: 1
  best_objective: 706.2
```

Finding 不应包含：

```yaml
summary: "这个方向已被证明有效"
recommendation: "以后必须继续这个方向"
truth: validated
```

因为这些字段会再次成为未经核验的语言结论。

Finding 只回答：

```text
这是哪个研究问题？
它涉及哪些机制和代码区域？
做过哪些实验？
当前是否仍在研究前沿？
```

### 4.3 Finding operational lifecycle

当前阶段只使用操作状态：

```text
open
active
dormant
archived
```

含义：

- `open`：研究问题已建立，但尚无实验；
- `active`：近期 proposal 或 experiment 正在研究它；
- `dormant`：一段时间未被选择，但仍可检索；
- `archived`：明确不再进入默认 frontier，但原始证据仍保留。

不要在这一版加入：

```text
supported
contradicted
validated
falsified
```

这些是 epistemic claim，需要更严格的证据聚合和 trust guard。当前 Proposer 不应通过一次输出给科学问题盖棺定论。

---

## 5. Proposal 数据模型

当前 proposal 是裸字符串：

```json
{
  "proposals": [
    "Hoist repeated lookup..."
  ]
}
```

改成结构化研究实验：

```json
{
  "action": "submit_proposals",
  "proposals": [
    {
      "instruction": "Hoist repeated QPDF lookup...",
      "research_target": {
        "mode": "existing",
        "finding_id": "F-008"
      }
    },
    {
      "instruction": "Replace the event-local AoS access...",
      "research_target": {
        "mode": "new",
        "question": "Whether event-local AoS layout is the dominant remaining cache cost.",
        "mechanisms": ["data-layout", "cache-locality"],
        "code_regions": ["OMILRECV2/src/OMILREC.cc"]
      }
    }
  ]
}
```

这不是让 Proposer 维护过去，而是在当前作出科学决策时说明：

> 这个实验正在回答哪个问题？

允许 `mode: new` 非常重要。任何 proposal 都不必被迫归入已有 Finding，避免 Finding Archive 重新压缩探索空间。

Loop 收到结果后：

1. existing finding：关联 `finding_id`；
2. new finding：Memory Store 分配新 ID；
3. 将 ID 写入 candidate spec；
4. Harness 完成实验后，将 `experiment_id` 自动追加到对应 Finding。

Proposer 不负责在实验后更新 Finding。

---

## 6. Retrieval 设计

### 6.1 不在启动时暴露全部历史

新 Proposer 启动包只包含：

```text
Research objective
Harness Gates
Current accepted SHA
Recent factual dashboard
Compact research frontier
Available memory tools
```

例如：

```yaml
recent_dashboard:
  rounds: 2
  best_objective: 706.2
  recent_experiments:
    - ref: r12c0
      finding_id: F-008
      eligible: true
      selected: false
      objective: 711.5
    - ref: r12c1
      finding_id: F-014
      eligible: false
      failed_gates: [FCN]

frontier:
  active_findings:
    - id: F-008
      question: QPDF 热循环是否仍有可提升不变量？
      attempts: 3
      last_touched_round: 12
    - id: F-014
      question: CalLTOF 重复几何计算是否支配当前成本？
      attempts: 1
      last_touched_round: 12

  dormant_count: 11
  experiment_count: 63
```

不提供 Finding 结论，不要求处理最近全部候选。

### 6.2 Memory tools

新增：

```text
list_findings
search_findings
inspect_finding
search_experiments
inspect_episode
```

#### `list_findings`

```json
{
  "action": "list_findings",
  "state": "active|open|dormant|all",
  "limit": 20
}
```

#### `search_findings`

```json
{
  "action": "search_findings",
  "query": "QPDF lookup and cache locality",
  "mode": "relevant|contrasting|diverse",
  "limit": 5
}
```

#### `inspect_finding`

返回：

- question；
- operational state；
- code regions；
- mechanisms；
- experiment refs；
- derived numerical stats。

不生成新的语言总结。

#### `search_experiments`

```json
{
  "action": "search_experiments",
  "query": "lookup",
  "filters": {
    "selected": false,
    "gate_passed": true,
    "changed_path": "OMILREC.cc"
  },
  "limit": 10
}
```

#### `inspect_episode`

继续复用当前工具，返回完整 factual episode。

### 6.3 Diversity-aware retrieval

默认检索不能只是相似度 Top-K。

一次自动 context retrieval 应分成三组：

```text
Relevant
    当前研究问题最相关的 Finding / Experiment

Contrasting
    与当前假设结果不一致或机制相近但结果相反的实验

Diverse
    来自不同代码区域、机制族或远距离 lineage 的研究方向
```

建议默认预算：

```text
3 relevant
2 contrasting
2 diverse
```

初期可采用：

```text
BM25 / token match
+ structured filters
+ code-region overlap
+ mechanism tags
+ recency
+ objective stats
+ MMR diversity reranking
```

当实验量增长到数百或数千条后，再加入 embedding retrieval。

避免第一版直接依赖独立 retrieval LLM，防止引入新的主观信息层。

---

## 7. Research Frontier

Research Frontier 是 Active Findings 的动态视图，不是新的存储真相。

由系统确定性生成：

```yaml
active:
  - F-008
  - F-014

dormant:
  - F-002
  - F-005

coverage:
  code_regions:
    OMILREC.cc: 8
    QPDF.cc: 12
    CalLTOF.cc: 2

  mechanisms:
    caching: 14
    invariant-hoisting: 9
    data-layout: 2
    algorithm-replacement: 0
```

Frontier 的作用是让 Scientist 看见：

- 哪些问题正在被研究；
- 哪些区域已被密集采样；
- 哪些区域或机制几乎未探索。

这比 candidate notes 更能支持探索，因为它展示的是**搜索覆盖度**，而不是把上一轮具体方案当成未来中心。

---

## 8. 跨 round 继承模型

继续保持每轮 fresh Proposer session：

```text
Round N Proposer
    messages_N
    WorkingState_N
        ↓ exit and discard

Persistent Laboratory State
    Experiment Ledger
    Findings Archive
    Research Frontier
        ↓

Round N+1 Proposer
    new messages_N+1
    new WorkingState_N+1
```

跨 round 继承：

- accepted SHA；
- Experiment Ledger；
- Finding Archive；
- derived Frontier；
- 按当前问题检索出的 evidence。

不继承：

- 上一轮完整 messages；
- hidden reasoning；
- shell 输出；
- frame_research 文本；
- conclude_research 文本；
- 临时观察；
- attention trajectory。

原则：

> 继承实验室，不继承上一位 Scientist 的脑内过程。

---

## 9. 写入职责

| 数据 | 写入者 | 时机 |
|---|---|---|
| proposal、SHA、metrics、gates、logs | Harness | candidate 完成时 |
| 新 Finding 的 question 与 scope | Proposer | 当前 proposal 提交时 |
| proposal–Finding 关联 | Loop / Memory Store | proposal 接收时 |
| experiment–Finding 关联 | Memory Store | experiment 持久化后 |
| attempts、best objective、last touched | Memory Store | 自动派生 |
| operational state | Memory policy | 根据活动状态更新 |
| embedding / BM25 index | Retriever | 自动 |
| 科学结论 | 暂不持久化 | 当前版本不实现 |

Proposer 不再承担：

```text
总结上一轮全部实验
维护所有 Finding
更新全部索引
判断所有失败意味着什么
```

---

## 10. 代码重构映射

### 10.1 `simpleloop/roles/proposer.py`

删除：

- `ProposerResult.annotations`；
- `_TERMINAL_ACTION_PROMPT` 中 annotations；
- `_parse_annotations()`；
- `_prior_candidate_refs()`；
- `_initial_context()` 中 notebook notes；
- `_parse_action()` 的 annotation 校验。

修改：

```python
@dataclass(frozen=True)
class ResearchProposal:
    instruction: str
    research_target: ExistingFindingTarget | NewFindingTarget
```

增加 research tools action：

```text
list_findings
search_findings
inspect_finding
search_experiments
inspect_episode
run_research_command
```

状态机保持不变。

### 10.2 `simpleloop/roles/research_tools.py`

删除：

- `render_history_directory()`；
- 所有 note 相关说明。

拆分职责：

```text
ResearchCommandRunner
    源码和 Git 调查

ScientificMemoryTools
    Findings 和 Experiments 检索
```

也可以保留一个 `ResearchTools` façade，但内部依赖：

```python
ResearchTools(
    command_runner=...,
    memory_service=...,
)
```

### 10.3 `simpleloop/harness/store.py`

继续维护 `history.jsonl`。

增加 candidate 字段：

```text
experiment_id
finding_id
schema_version
```

删除任何 `note` / `backfill_notes` 逻辑。

`Store.history()` 不再被直接全量塞入 Proposer prompt，而是供 Memory Service 查询。

### 10.4 `simpleloop/harness/memory.py`

当前文件只负责 episode ref 解析和读取。建议重命名为：

```text
experiment_ledger.py
```

保留：

- `read_history()`；
- `_parse_episode_ref()`；
- `resolve_episode()`。

新的 scientific memory 放入独立模块，不与 Harness factual memory 混合。

### 10.5 新建 `simpleloop/memory/`

```text
simpleloop/memory/
├─ models.py
├─ finding_store.py
├─ experiment_index.py
├─ retrieval.py
├─ frontier.py
├─ context.py
└─ migration.py
```

#### `models.py`

定义 Finding、ResearchProposal、RetrievalHit。

#### `finding_store.py`

负责 append/update Finding 和 experiment linking。

#### `experiment_index.py`

从 `history.jsonl` 构建结构化索引。

#### `retrieval.py`

负责 relevant / contrasting / diverse retrieval。

#### `frontier.py`

确定性生成 active frontier 和 coverage statistics。

#### `context.py`

构造 Proposer round startup pack。

#### `migration.py`

处理已有 runs。

### 10.6 `simpleloop/loop.py`

删除：

```text
round_annotations
annotations in inflight metadata
backfill_notes
note reconciliation
```

新流程：

```python
proposal_result = proposer.run(memory_context=...)

resolved_proposals = memory_service.resolve_targets(
    proposal_result.proposals,
    round_id=round_id,
)

candidates = backend.run_candidates(
    proposals=[p.instruction for p in resolved_proposals],
    proposal_metadata=[{"finding_id": p.finding_id}],
)

store.append_generation(...)

memory_service.link_completed_experiments(
    round_id=round_id,
    candidates=candidates,
)
```

### 10.7 `candidate_worker.py`

`CandidateSpec` 增加：

```python
finding_id: str | None
```

Executor 仍然只接收 `instruction`，不需要看到完整 Finding 历史。

### 10.8 测试

删除已经过时的 memory API 测试，不继续兼容两套接口。

新增测试：

```text
test_experiment_ledger_is_append_only
test_finding_links_multiple_experiments
test_new_proposal_can_create_new_finding
test_proposal_can_reuse_existing_finding
test_proposer_has_no_annotation_obligation
test_loop_never_backfills_history
test_retrieval_returns_relevant_contrasting_diverse
test_frontier_reports_underexplored_regions
test_fresh_round_does_not_reuse_messages
test_inspect_episode_remains_factual
```

---

## 11. Migration

### 11.1 已有 `history.jsonl`

保留全部实验事实。

迁移时：

- 给 candidate 补 `experiment_id`；
- 补 `schema_version`；
- 删除或忽略 `note`；
- 不把旧 note 转成 Finding；
- 不把旧 note 当长期知识。

原因是旧 note 是在强制总结机制下产生的，包含严重历史锚定偏差。

### 11.2 Findings 初始化

推荐两种模式。

#### Clean start

从重构后的下一轮开始创建 Findings。

过去的 experiments 仍可通过 `search_experiments` 检索，但不自动组成 Findings。

这是最可信的模式。

#### Optional offline bootstrap

用确定性路径和机制规则，或一次离线 clustering，将历史 proposal 聚成候选 Finding，供人确认后导入。

不要让在线 Proposer 在正常 proposal round 中完成迁移。

---

## 12. 分阶段实现

### Phase 1：替换数据模型

完成：

- 删除 annotation / note；
- Experiment Ledger schema v2；
- Finding Store；
- structured proposal target；
- experiment–finding linking；
- basic frontier。

### Phase 2：检索系统

完成：

- `search_findings`；
- `inspect_finding`；
- `search_experiments`；
- structured filters；
- relevant / contrasting / diverse buckets；
- MMR reranking。

### Phase 3：规模化

当实验数量达到数百以上时增加：

- embedding index；
- mechanism classifier；
- automatic clustering suggestions；
- archive compaction；
- finding deduplication；
- diversity-aware sampling policy。

### Phase 4：跨 run 科学知识

只有当 SimpleLoop 开始跨任务复用研究知识时，再增加：

```text
Long-Term Method Memory
Scientific Claims
Cross-run Findings
Trust Guard
```

当前不实现。

---

## 13. 验收标准

### 13.1 架构标准

- 每轮只有一次 Proposer 调用；
- 每轮仍然是 fresh session；
- 状态机保持单 Scientist；
- 没有 Judger 或 memory summarizer；
- 没有 annotation；
- 没有 note backfill；
- Experiment Ledger 不可变；
- Finding 不覆盖 Experiment；
- Proposal 可创建全新 Finding；
- Proposer 不需要维护上一轮全部历史。

### 13.2 行为标准

与当前 annotation 版本比较：

- proposal mechanism diversity 提升；
- 新代码区域 proposal 比例提升；
- 与上一轮候选同族重复率下降；
- 历史 episode 重复实验率下降；
- Proposer 主动检索旧 evidence，但不会逐轮全量回顾；
- retrieval 能返回相关、冲突和多样化历史；
- objective 改善速度不低于 free-form baseline；
- token 成本不随完整历史线性增长。

### 13.3 关键 ablation

```text
A. Free-form Proposer，无科学状态机
B. Scientist 状态机 + 新 Memory
C. Scientist 状态机 + 当前强制 annotation
```

当前结果已说明 C 存在严重探索压缩。下一步重点比较 A 与 B，以判断：

- 科学状态机是否提高研究深度；
- 新 Memory 是否减少重复而不损伤探索；
- Finding Frontier 是否比 candidate notes 更适合作为跨轮状态。

---

## 14. 最终架构

```text
┌───────────────────────────────────────────────┐
│ Persistent Laboratory State                   │
│                                               │
│  Immutable Experiment Ledger                  │
│    r0c0, r0c1, ...                            │
│    proposal / SHA / diff / metrics / gates    │
│                                               │
│  Active Findings Archive                      │
│    F-001, F-002, ...                          │
│    question / scope / experiment refs         │
│                                               │
│  Research Frontier                            │
│    active / dormant / coverage                │
└───────────────────────┬───────────────────────┘
                        │ balanced retrieval
                        ▼
┌───────────────────────────────────────────────┐
│ Fresh Round N Proposer Scientist              │
│                                               │
│  Observe                                      │
│     accepted source                           │
│     recent factual dashboard                  │
│     research frontier                         │
│     on-demand memory retrieval                │
│                                               │
│  Investigate                                  │
│     search Findings                           │
│     search Experiments                        │
│     inspect evidence                          │
│     inspect source / Git                      │
│                                               │
│  Checkpoint                                   │
│     continue / reframe / submit               │
└───────────────────────┬───────────────────────┘
                        │ structured proposals
                        ▼
┌───────────────────────────────────────────────┐
│ Executor + Deterministic Harness              │
│                                               │
│  implementation                               │
│  commit                                       │
│  eval / gates                                 │
│  objective selection                          │
└───────────────────────┬───────────────────────┘
                        │ append evidence
                        └──────────────→ Persistent Laboratory State
```

## 15. 最终原则

> SimpleLoop 的跨轮连续性不应来自模型会话，也不应来自上一轮 Proposer 写给下一轮的总结，而应来自一个结构化、可检索、可追溯的实验室状态。

具体来说：

```text
Experiment 保存事实
Finding 组织问题
Frontier 展示搜索覆盖
Retriever 选择当前相关且多样的证据
Proposer 负责下一次科学决策
Harness 负责决定实验事实
```

这套设计保留了当前 Proposer Scientist Runtime 的正确部分，同时完整替换导致探索坍缩的 note memory。
