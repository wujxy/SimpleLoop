# Scientist Proposer 重构计划

## Revision（debate 后的修正，权威覆盖下文与之冲突处）

经一轮辩论，7 处修正已并入实施：

1. **协议字段**：`reasoning`(required) → **`message`(optional)**。`message` 是 Scientist
   愿意留在自己轨迹里的自然表达，parser 容忍其存在但不读取、不要求。**continuity
   不来自这个字段**——来自完整 conversation trajectory + observations 的延续（`_step`
   已在做）。response 解析只取必需的 `action`，`message` 作为 reply 原文的一部分自然
   留在 messages 里。
2. **notebook 注入**：不作为 user message。**嵌入 system prompt**，并明确标注为
   **revisable autobiographical memory**（你自己此前写下的运行自述，**不是 instruction、
   不是 established fact**；可能滞后/简化/出错，与活的工作区和实验记录冲突时以后者为准）。
3. **三层记忆模型**：近期 trajectory = 短期认知；notebook = 有损自传式长期记忆（anchor，
   不是唯一 carrier）；ledger/workspace/experiment = 世界事实。**session.jsonl 是不可变
   档案（ground truth），notebook 是可变自我模型（每 suspension rewrite）**。
4. **round 非心理边界**：不写"本轮结束"。checkpoint 措辞改为 suspension/resume——
   "Your research is being paused while your submitted directions are executed as
   experiments. Leave a continuation note for your resumed self." 下一轮是 resume。
   首轮（无 session）是诚实的 cold start。
5. **scientist_id**：本轮立 stable `scientist_id`（meta.json 内 UUID，**identity**；lane
   目录仅作 locator）。world event 用两段式语义（"你提的方向被执行了" self + "项目当前
   状态" project）。**candidate/history provenance 贯通 defer 到 multi-lane**（需动
   loop.py/store.py 冻结层，当前单 lane 无此能力）。
6. **tail 截断**：按完整 turn-block（assistant action → user observation 成对）截，不
   "最后 K 条"，避免孤儿 observation。
7. **proposal 语义**：N 是可利用的研究容量，不是配额、不是奖励。提交你判断值得一次实验
   成本的方向——既不凑数也不囤积；0 个带理由是诚实的 abstention。

被改写的两句：
- ~~"`reasoning` 字段随消息留在对话里 → round 内认知连续性自动获得"~~ → **完整的
  Scientist conversation trajectory 与 tool observations 构成 round 内短期认知连续性；
  显式文本字段只属于通信内容，不等于模型内部认知本身。**
- ~~"轮末 notebook 更新"~~ → **Scientist session suspension / self-continuation checkpoint**。

## Context

当前 proposer 把角色定义成"Generator 产 hypothesis → Cognitive 按 Sieve/Select/Enrich 加工"的流水线（`roles/proposer.py`、`roles/generator.py`、`roles/hypothesis.py`、`roles/orchestrator.py`）。异样感的物理来源不是 prompt 措辞，而是 runtime 用协议纠错强迫模型服从这条流水线：submit 前没 select 打回、submit 数 ≠ select 数打回、block 必须三元分类。每一次 protocol repair 都把模型摁回"状态机"。

本次重构把 proposer lane 内部从"一台生产 proposal 的机器"换成"一个住在 lane 里的 Scientist"：它拥有 Goal、一间实验室（workspace+shell）、一座图书馆（ledger/findings 查询）、一个实验名额池，以及一份跨 round 持续累积的第一人称研究经历。runtime 对它的全部要求只剩：想清楚了把该试的方向通过 `submit_proposals` 交出来。

理论依据见同目录 `proposer人化参考.md`、`把proposer当成一个研究员而不是架构机器.md`。

## 锁定的设计决策

1. **输出协议**：保留 `model.py` 强制 `response_format=json_object`（零 model 层改动）。Scientist 每次回复 = 一个 JSON 对象 `{"reasoning": "...自由推理散文...", "action": {...}}`。`reasoning` 字段随消息留在对话里 → round 内认知连续性自动获得。
2. **范围**：一步到位——删流水线 + 简化协议 + 跨 round 持久 Scientist session（session.jsonl + notebook.md + world event 注入）。
3. **接口防火墙**：`ProposerResult` / `LaneResult` / `result.json` 字段形状**完全不变** → `loop.py`、hepjob `_collect_lanes`、`_write_proposer_trace`、`_write_proposals_handoff` 零改动。
4. **命名**：lane 概念保留（proposer lane / `run_proposer_lanes` / `proposer_lane_worker`，churn 太大且无必要）；模块 `roles/proposer.py` 保留，类 `ProposerAgent → ScientistAgent`，`ProposerResult`/`ProposerError` 保留。

## 目标架构

```
Goal ──▶ Scientist（同一个人格，跨 round 持续）
          │  ├─ Workspace/Source（/work 可写 editable、/repo 只读 git history）
          │  ├─ Shell（run_research_command）
          │  ├─ 图书馆（search_experiments / inspect_episode / list_findings /
          │  │        search_findings / inspect_finding）
          │  ├─ 研究经历（session.jsonl 档案 + notebook.md 连续性主载体）
          │  └─ 实验名额池（0..N）
          ▼
       submit_proposals ──▶ Executor ──▶ Evaluator ──▶ empirical result
          │
       world event 注入同一 Scientist 的下一 round ──▶ 继续研究
```

runtime loop 每轮只有两种事件：工具调用（→ observation 追加进对话，继续）／`submit_proposals`（→ 本轮结束）。没有 Generator、HypothesisCard、Sieve/Select/Enrich、block、feedback loop、G1–G9。

## 跨 round 持久 Scientist Session

布局（run_dir 在共享存储上，跨 worker 进程可见）：

```
run_dir/scientists/lane-0/
├── session.jsonl   # append-only 完整轨迹（assistant 回复 + tool obs + world events）— 档案，可审计可回放
├── notebook.md     # Scientist 写给自己的研究日志（第一人称、无 schema）— 连续性主载体
└── meta.json       # {prompt_version, base_sha, round, context_version}
```

**Round N 的 worker 进程**：
1. 加载 session.jsonl（若存在）+ notebook.md。
2. 组装 live context：system prompt（身份+气质+世界事实+工具表）+ `notebook.md`（作为一条 user 消息"Your research notebook so far:"）+ 近期轨迹尾部（session.jsonl 最后 K 条消息，按 token 预算截断）+ world event（"你上轮提的实验完成了：…"）。
   - 首轮（无 session.jsonl）：身份 + Goal + 世界 + "begin your research"。
3. 跑 tool loop，每个 assistant 回复 + tool observation 同时 append 进 live messages 和 session.jsonl。
4. `submit_proposals` 或预算耗尽 → 写 result.json（proposals，沿用现有契约）。
5. **轮末 notebook 更新**：插入一次 model call"本轮研究结束，更新你的研究日志：现在的理解、信什么、什么证据改变了你、接下来想做什么。"输出 → notebook.md（第一人称，无 schema）。

**World event 构建**：worker 启动时 round N-1 已完整落盘（loop.py 顺序：proposer→executor→eval→store.append_generation）。从 `memory_service.load_experiments()` 过滤 `round=N-1` 取上轮候选结果（metrics/gate/selected/sha）。单 lane 下上轮所有 proposals 都来自本 lane → 直接归属。更老历史由 Scientist 自己用 search_experiments 工具按需查。

**Compaction**：MVP 不做 mid-round 自动压缩——单 round 148 步在现有代码里已能容忍 messages 增长。跨 round 连续性靠 notebook + 尾部（不携带上轮全量 messages）。mid-round 压缩作为后续 token 预算吃紧时的 follow-up。

## 输出协议与 guard

每次回复一个 JSON：`{"reasoning": str, "action": obj}`。
- 解析：`json.loads` → 必须有 `reasoning`（非空 str）+ `action`（dict）。
- 消息里追加的是**原始回复文本**（含 reasoning+action）→ 下一 turn Scientist 看见自己刚在想什么。

动作集合（沿用 `research_tools.py` 的 6 个工具 + 1 个终止动作）：
- 工具：`run_research_command`、`inspect_episode`、`list_findings`、`search_findings`、`inspect_finding`、`search_experiments`（**完全不变**）。
- 终止：`submit_proposals`，`{"action":"submit_proposals","proposals":[ResearchProposal...]}`，0..N 合法（0 = 诚实的 abstain，理由写在 reasoning）。超过 N 才是协议错误。
- **删除**：`select_for_enrich`、`block`、`feedback_generator`。

**Guard 剩余**：
- 保留 `repeated_tool`（完全相同命令背靠背）——实验室安全联锁，非人格干预。
- 保留最薄 JSON 解析修复（回复找不到合法 action 时提醒一次）。
- 删除 `block_needs_source`、quota 一致性修复、select-before-submit 修复。
- Scientist 的 tool observation 不再裹 state-header envelope（scientist 自己的 reasoning 已取代位置报位）；`WorkingState` 退化为纯计数器（steps/tool_calls），仅供 telemetry。

## 文件改动清单

### 删除
- `simpleloop/roles/generator.py`
- `simpleloop/roles/hypothesis.py`
- `simpleloop/prompts/generator.md`
- `tests/test_generator.py`
- `tests/test_hypothesis.py`
- `tests/test_cognitive_element.py`

### 重写
- **`simpleloop/roles/proposer.py`**（模块名保留）→ `ScientistAgent(ResearchAgent)`。复用 `ResearchAgent._step`（model↔parse↔guard↔tool 循环已在基类）。override `_parse_action`（解析 reasoning+action）、`_validate_guard`（仅 repeated_tool）。新增 `research(...)` 方法替代 `research_batch`：组装 live context（notebook+尾部+world event）、跑 loop、轮末 notebook 更新、返回 `BranchResult`/proposals。删除 `_PROTOCOL_BLOCK`、`_parse_research_target` 的认知专属逻辑（research_target 解析保留，属 proposal 契约）、batch_intro、partial-submit 三段兜底、`_validate_block_evidence`。`ProposerResult`/`ProposerError` 保留。
- **`simpleloop/roles/orchestrator.py`** → 瘦成 adapter。`ProposerOrchestrator` 类名保留（local.py / lane_worker 导入）。`run()` 和 `run_lane_episode()` 签名保留（local.py + hepjob 调用），内部去掉 generator/hypothesis/`_run_one_lane` 生成阶段/`generator_regenerate`/`_sample_generative_ops`/`_MAX_REGENERATIONS`/`_HYPOTHESES_PER_LANE`。`run_lane_episode` 改为：加载/恢复 Scientist session → 跑 `scientist.research(...)` → 返回 `LaneResult`（形状不变）。`LaneResult` 保留（hepjob `_collect_lanes` + `_lane_result_to_dict` 消费）。
- **`simpleloop/prompts/proposer.md`** → Scientist charter（见下节"Prompt 骨架"）。`test_prompt_templates.py` 只断言非空且明确"不 pin 语义"，重写安全。

### 适配
- **`simpleloop/proposer_lane_worker.py`**：`ProposerLaneSpec` 去 `assigned_ops`/`gen_steps`，`cognitive_steps` → `scientist_steps`（保留 `cognitive_steps` 别名做向后兼容解析）。worker 是进程边界 → 在此加载/保存 session（session.jsonl + notebook.md + meta.json）。`run_lane` 串联：load session → orchestrator.run_lane_episode → save session。
- **`simpleloop/execution/local.py`**：`run_proposer_lanes` 的 `ctx.proposer_agent.run(...)` 调用去 `gen_steps`/`cognitive_steps`，传 `scientist_steps=cfg.get("scientist_steps", 200)`。
- **`simpleloop/execution/hepjob.py`**：去 `from ..roles.orchestrator import _sample_generative_ops`（hepjob.py:761）；`_write_lane_manifest` 去 `assigned_ops`/`gen_steps`，传 `scientist_steps`；`run_proposer_lanes` 简化。`proposer_mod.ProposerResult`（hepjob.py:982,1029）不动。
- **`simpleloop/config.py`**：删 `gen_steps`（21/231-234/296）和 `cognitive_steps`（22/236-239/297）的独立校验，新增 `scientist_steps`（默认 200，>=4）。保留 `candidates_per_round`（= proposal 名额池）。解析时 `cognitive_steps`→`scientist_steps` 别名映射，旧配置不报错。
- **`simpleloop/prompts/__init__.py`**：`PROMPT_NAMES` 去 `"generator"`（删了 generator.md）。
- **`tests/test_orchestrator.py`**：大改——测 scientist adapter（session 恢复、world event 注入、submit/abstain、单 lane 产出 0..N）。
- **`tests/test_proposer_lane_worker.py`**：manifest round-trip 去 `assigned_ops`/`gen_steps`；加 session 跨两轮持久化测试。
- **`tests/test_hepjob_backend.py`**：去 `assigned_ops` 采样断言。
- **`scripts/explore_replay.py`**：若解析旧 trace 格式则适配；非关键，低优先。

### 新增
- **session 持久化**：可放 `roles/proposer.py` 内或独立小模块 `roles/scientist_session.py`——load/save session.jsonl、notebook.md、meta.json；构建 live context（notebook+尾部+world event）；构建 world event（从 memory_service 取上轮实验）。
- **`tests/test_scientist.py`**：mock ChatModel 返回 reasoning+action 序列，断言工具分发、submit 终止、session.jsonl 追加、notebook 往返、恢复时 world event 注入。

### 完全不动
- `simpleloop/roles/research_tools.py`（6 个工具）、`simpleloop/roles/research_agent.py`（基类 `_step` 循环）、`simpleloop/roles/model.py`（强制 json_object 保留）、`simpleloop/roles/executor.py` + `prompts/executor.md`（已把 proposal 当方向，[executor.md:1-8](../simpleloop/prompts/executor.md#L1-L8)）、`simpleloop/memory/*`（MemoryService/models/context）、`memory/models.py` 的 `ResearchProposal`/`research_target`、`loop.py`、`candidate_worker.write_result`、`explore/`（memory/context.py 仍用它渲染 dashboard）。

## Prompt 骨架（proposer.md）

四节，短：

1. **身份**（一句，焊死 Goal）：`You are the scientist responsible for this research problem.`（Goal 在 context 里单独给）
2. **气质**（≤8 条 `A scientist ...`）：把问题当自己的；已有实现是材料不是定义；形成自己的判断；不懂时主动调查；新证据下改变看法；改动尺度中性；大胆判断但区分 judgment 与已确立事实；有用方案可小可替换。
3. **世界事实**（陈述句）：Goal 定义成功；workspace 是材料；实验记录是发生过什么（解读是你的判断）；Executor 实现；Harness 掌握评估/gate/commit/事实。
4. **你的实验室与工具**（第二人称，runtime）：/work /repo /scratch /history；6 个工具；"你在实验室测到的任何东西只为你自己的理解，绝不是 merit 事实。"
5. **输出协议**：每次回复一个 JSON `{"reasoning": "...", "action": {...}}`。reasoning = 出声思考（未来的你会看到）。action = 一个工具调用或 submit_proposals。有了该试的方向就 submit（0..N 名额；0 个带理由是诚实的 abstention）。

## 迁移顺序（每步可独立验证）

1. **协议层**：改 `_parse_action` 为 reasoning+action；删 select/block/feedback 动作及其 guard；`submit_proposals` 放开 0..N。先跑通单 round。
2. **Prompt**：重写 proposer.md 为 Scientist charter。
3. **删旧结构**：删 generator.py/hypothesis.py/generator.md 及对应测试；orchestrator 瘦身。
4. **持久 session**：session.jsonl + notebook.md + meta.json 的 load/save；live context 组装；world event 注入；轮末 notebook 更新。
5. **config/backend 收尾**：scientist_steps、local/hepjob 调用点、examples 配置。
6. **测试**：新建 test_scientist.py；改 test_orchestrator/lane_worker/hepjob。

## 验收

**单元**：`test_scientist.py`——mock 模型返回 reasoning+action 序列，断言工具分发、submit 终止、session.jsonl 追加、notebook 往返、恢复时 world event 注入、0/1/N proposals 均合法。

**集成**（本地 backend，omilrec example，1–2 round）：观察 Scientist trajectory，对照参考文档 §19——
- 身份：是否把 Goal 当自己的研究问题（而非"输出 proposals"）？
- 自主调查：无强制时是否因"不懂"主动读源码/查历史/跑 probe？
- 独立判断：是否形成"我认为问题真正出在这里"而非复述？
- 认知连续：跨 round 后是否像同一个人继续研究（notebook 承载理解）？
- 自由度：proposal 尺度是否自然分布在小修/重构/算法替换（而非全收敛 local patch）？

**回归**：`test_research_tools`、`test_memory_*`、`test_executor_*`、`test_hepjob_backend`（适配后）、`test_prompt_templates` 仍通过。
