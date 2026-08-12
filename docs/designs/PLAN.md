# Proposer 重构计划：先生成，后深思

## 讨划的来源

四轮讨论收束的结论，不是拍脑门：

1. **当前病根**：proposer 在 generate 阶段 0 工具调用就声明方向（r0 第 1 步直接 generate→validate），因为 `generate` 动作无证据前置条件，evidence_basis 闸门只在 assess/submit 触发。模型走最小阻力路径：先验声明方向 → validate 里凑工具调用过闸 → submit。工具为闸门服务，不为推理服务。
2. **v1 认知元的副作用**：在分支产生前启动深度思考 → 第一个 frame 变成整个 episode 的坐标系 → reframe 只在同族内换措辞，搜索空间坍缩。
3. **文献共识**（Snell 2024 / Co-Scientist / CodeEvolve / DGM / BATS）：一条线性轨迹不能稳定兼顾宽度和深度；解法是 population 在外、deliberation 在内。固定策略被自适应支配。
4. **本轮决策**：生成元负责提大量 hypothesis（宽、轻、无证据）；认知元降级到 branch 内做深研（深、隔离、C1-C4）；Explore 从"submit 闸门"降为"调生成边界的信号源"；executor 接收方向性 proposal（不是 plan，不到行号）。

## 不做什么（边界）

- **不拆多进程 / 不引入 Supervisor agent**。文献要求轨迹分叉，不要求进程分叉。元调度（开几个、深挖哪个）由 harness 侧硬切（有 Explore 数据），branch 内判断留给认知元（保留 identity-spine 方向）。单 proposer 进程内维护 hypothesis pool + 隔离 episode 上下文。
- **不动 executor**。它已经接收方向性指令、在 worktree 自主实现（prompt 3054B / timeout 7200s）。契约不变。
- **不动 harness / eval / ledger**。实验记录、gate、metrics 通路不变。
- **不删 Explore**。角色变（刹车→方向盘），代码保留并改消费者。
- **不在第一版做树搜索 / MCTS / Evolution**。MVP 是宽生成 → 结构去重 → 浅探针 → per-branch 认知元 → portfolio。回路（深研发现回灌生成）留接口不实现。

## 架构

```
                    Run-level Project Context（现有 startup pack，不改）
                              │
              (0) 共享地图层 — 本期不单独建，沿用 startup pack + frontier
                              │
              (1) Hypothesis Expansion
                  生成元 G1-G9 产 N 条轻量 hypothesis card
                  每条：{region, mechanism, intervention_family,
                         why_plausible, critical_unknown}
                  纯先验，无证据，允许不成熟
                  一部分槽位 frame-free（不看 Explore），一部分受生成边界引导
                              │
              (2) 结构去重（diversity archive）
                  按 (region × mechanism × intervention_family) 分箱
                  每箱至多一个代表 → 保留 K 个不同 niche
                              │
              (3) 浅探针（probe）
                  每条 survivor 跑 1-2 条只读 shell 命令
                  确认 hypothesis 指向的机制在代码中存在
                  砍掉机制不存在的（这是"过滤"，但用证据过滤，非先验过滤）
                              │
              (4) Per-branch 认知元深研
                  每条 probed hypothesis 启动隔离 episode
                  现有 proposer.py 的 generate→validate→commit 状态机
                  降级为 branch 内部研究策略
                  各 episode 独立上下文，可 reframe/abandon
                  产出 0-1 个方向性 proposal
                              │
              (5) Proposal Portfolio
                  list-wise 比较各 branch 的成熟 proposal
                  保留 1..candidates_per_round 个
                  交 executor
                              │
              (回路，留接口) 深研发现的新方向 → 回灌 (1) 生成空间边界
```

## 落地步骤（按依赖序）

每步可独立测试。新分支 `feature/proposer-branch-deepen`，从当前 `feature/proposer-generator-explore` 的 HEAD 切。

### Step 1 — hypothesis card 数据模型 + 结构签名

**文件**：`simpleloop/roles/hypothesis.py`（新建）

- `HypothesisCard` dataclass：`region, mechanism, intervention_family, why_plausible, critical_unknown, generative_op`
- `hypothesis_signature(card) -> tuple`：`(bucket_path(region), canonical(mechanism), canonical(intervention_family))`，复用 `explore.families.normalize_region` / `_bucket_path`
- `dedup_by_signature(cards: list, per_bin=1) -> list`：按签名分箱，每箱保留第一个（或随机一个，但无 Math.random → 用 index 轮换避免总取第一个）
- 纯数据 + 纯函数，单测全覆盖

**测试**：`tests/test_hypothesis.py` — 签名稳定性、去重保留不同 niche、同族坍缩到 1

### Step 2 — 生成元 episode（轻量、无证据）

**文件**：`simpleloop/roles/generator.py`（新建）、`simpleloop/prompts/generator.md`（新建）

- `GeneratorAgent`：单次模型调用（非多步状态机），产 N 条 hypothesis card
- prompt 身份：九个生成元 G1-G9 作为**起点分配器**，每个 G 是一个入口角度；明确"你产的是 hypothesis 不是 proposal，允许不成熟，不需要证据链"
- 输入：startup pack（objective/gates/editable/frontier）+ 生成边界（Explore 的负反馈：哪些区域穷尽）+ frame-free 标志
- 输出：`list[HypothesisCard]`，长度 = 配置的 N（默认 8）
- 生成边界渲染：Explore family 中 `consecutive_no_improve >= 阈值` 的 (region, mechanism) → "已穷尽区域，不要产此族变体"。这是负反馈，只关区域不指方向
- frame-free 槽：N 中保留 `N // 3` 个不受边界引导（纯 G 采样），防御先验坍缩

**测试**：mock model 返回固定 JSON → 解析出 N 条 card；边界关闭某区域 → 该区域不出现在 frame-guided 槽（但可能出现在 frame-free 槽）

### Step 3 — 浅探针（probe）

**文件**：`simpleloop/roles/probe.py`（新建）

- `probe_hypothesis(card, research_tools) -> ProbeResult`
- 每条 card 跑 1-2 条只读命令（`run_research_command` cwd=source），确认机制存在
- 探针命令由 card.region / mechanism 派生（如 region 指向某文件 → `grep -l <mechanism_keyword> <file>`；通用 → `find source -name '*.cc' | head` + grep）
- `ProbeResult`：`confirmed: bool, evidence_ref: str | None, note: str`
- 预算：每条 card 最多 2 次工具调用，总探针预算 = K * 2
- 砍掉 `confirmed=False` 的；保留 confirmed 的，附 evidence_ref

**测试**：mock research_tools 返回 grep 命中 → confirmed=True；返回空 → confirmed=False；超预算 → 停

### Step 4 — 认知元降级为 branch 内研究策略

**文件**：`simpleloop/roles/proposer.py`（重构现有）

核心变化：现有 `ProposerAgent.run` 的 generate→validate→commit 状态机**保留**，但不再对整个搜索负责，而是成为**一条 hypothesis branch 的深研器**。

- 新入口：`ProposerAgent.research_branch(hypothesis: HypothesisCard, ...) -> BranchResult`
  - 以 hypothesis 作为初始 candidate_directions（跳过现在的"generate 0 工具调用就声明"——方向已由生成元给，认知元从 validate 开始）
  - 隔离上下文：startup pack + hypothesis + 相关源码引用，不共享其他 branch 的推理链
  - 跑现有状态机（validate↔commit，可 reject→重入 generate 在 branch 内 reframe）
  - 产出 0-1 个 `ResearchProposal`（方向性，不到行号）
- 保留 `run()` 作为兼容入口（static-proposal 模式仍用），内部调 `research_branch`
- 删除：submit 时的 `challenge_response` 闸门（Explore 执法角色移交生成边界，不再在 submit 挡）
- 保留：evidence_basis 闸门（branch 内仍要证据才能 submit）、taboo set（branch 内 reframe 仍需要）、near-duplicate 检测

**测试**：`tests/test_proposer_agent.py` 改造 — 给定 hypothesis card + mock tools，branch 产出 proposal 或 abandon；reframe 在 branch 内发生不影响其他 branch

### Step 5 — 编排：ProposerOrchestrator

**文件**：`simpleloop/roles/orchestrator.py`（新建）

- `ProposerOrchestrator.run(...) -> ProposerResult`：替代现有 `ProposerAgent.run` 成为 loop 的入口
- 流程：
  1. 调 `GeneratorAgent` 产 N 条 card
  2. `dedup_by_signature` 保留 K 个 niche
  3. 对 K 条并行/串行调 `probe_hypothesis`，保留 confirmed 的
  4. 对 confirmed 的每条调 `ProposerAgent.research_branch`（串行，各自隔离上下文）
  5. 收集 branch 产出的 proposals，list-wise 选 1..candidates_per_round 个
  6. 返回 `ProposerResult`（接口不变，loop 不改）
- 自适应切分（Snell）：第 1 轮 / frontier 全空 → N 小、深度大（先深一条探路再分支）；有历史 → N 大、深度浅（宽生成）。本期用简单启发式（`first_round or len(experiments)==0`），不引入难度估计器
- 预算拆分：`exploration_budget`（生成+探针）和 `exploitation_budget`（branch 深研），从现有 `max_steps=50` 按比例分（如 20% 探索 / 80% 深研）

**测试**：mock generator + probe + proposer，端到端产 portfolio；某 branch abandon 不影响其他 branch；第 1 轮走 depth-first 模式

### Step 6 — Explore 角色迁移

**文件**：`simpleloop/explore/render.py`（改）、`simpleloop/explore/monitor.py`（改）

- 新增 `render_generation_boundary(explore) -> str`：把 Explore 的 family stall / global stall 渲染成"已穷尽区域列表"给生成元，而非"challenge_required"给 submit 闸门
- 关闭判定加保守门槛：`consecutive_no_improve >= 5`（当刹车时是 4，当方向盘要更高，避免误关）
- 删除 `render_challenge_repair_message` 的调用点（proposer submit 闸门已删）
- `ExploreReport.challenge_required` 保留字段但不再触发 proposer 闸门；仅供 telemetry

**测试**：`tests/test_explore_monitor.py` — 边界渲染输出穷尽区域列表；阈值 5 vs 4

### Step 7 — 配置 + 端到端

**文件**：`simpleloop/config.py`（改）、`simpleloop/loop.py`（微改）

- 新配置项（`roles.researcher` 下）：
  - `hypothesis_count: int = 8`（生成元产多少条）
  - `branch_count: int = 3`（最多深研几条 branch）
  - `probe_budget_per_branch: int = 2`（浅探针每条最多几次工具调用）
  - `frame_free_ratio: float = 0.33`（frame-free 槽占比）
- `loop.py` 的 `_build_context` 把 `ProposerAgent` 包进 `ProposerOrchestrator`，loop 其余不变（`ProposerResult` 接口不变）
- 在 omilrec-v100 任务上跑 1 轮 dry-run，确认 portfolio 产出、trace 记录、executor 接收方向性指令

**测试**：config 解析新字段；端到端 1 轮（mock model）走通

## 测试策略

- 每步单测先写，mock model + mock research_tools
- 现有 `tests/test_proposer_agent.py` 改造为 branch 内测试
- 新增 `tests/test_orchestrator.py` 端到端编排测试
- 最后在 omilrec-v100-postv107-gated 上跑真实 1 轮（gpt-5.5 生成 + glm-5.2 执行），对比 r0 行为：生成元是否产了多个不同 niche、认知元是否在 branch 内深研、portfolio 是否多样

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 生成元仍产同质先验（G1-G9 都是缓存变体） | 结构去重砍伪多样；frame-free 槽持续注入；archive 让先验集中**可见** |
| 浅探针砍掉真方向（grep 没命中但机制存在） | 探针命令保守（宽匹配），confirmed=False 需明确空结果；保留 frame-free card 不经探针直接进 branch |
| branch 深研预算不够（50 步拆给 3 branch） | 自适应：第 1 轮只开 1-2 branch；有历史后宽生成浅深研 |
| Explore 误判穷尽 → 错关生成区域 | 关闭阈值从 4 提到 5；frame-free 槽不受边界影响 |
| 回路缺失（深研发现的新方向无回灌） | 本期留接口（`BranchResult.novel_directions`），不实现回灌；先验证开环漏斗是否已比单 proposer 强 |

## 验证标准

重构后 r0 行为应满足：
1. 生成元产出 ≥3 个**不同** niche（结构签名不同），而非 1 个方向的 3 个变体
2. 每条进入 branch 的 hypothesis 都经过浅探针确认机制存在（有 evidence_ref）
3. branch 内认知元从 validate 开始（方向已给），在 branch 内深研 ≥3 步才 submit
4. portfolio 里 proposal 的 (region, mechanism) 签名互不相同
5. Explore 的 stall 信号出现在生成边界（生成元不再产该族），而非出现在 submit 闸门
