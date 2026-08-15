# SimpleLoop 异常路径状态机与角色信息义务设计

**Status:** Design proposal（仅设计，未实施）
**Scope:** Proposer lane / Executor session / Harness 簿记的异常路径；各角色的信息陈述义务
**Out of scope:** 实施步骤、代码改动、迁移计划
**Evidence base:** runs/omilrec-v100-001（r11c0/r15c1/r21c0 executor 死亡三案；r22–r28 协议失败空转段；r25 RSI 误诊）

---

## 1. 问题

omilrec-v100-001 的尸检揭示了一个系统性缺口：**正常路径上职责已经分明
（proposer 出假设、executor 做实验并客观汇报、harness 拥有评测），但异常路径
上"谁负责陈述发生了什么"没有人负责。**

三个具体形态：

1. **executor 中途死亡，尸体冒充实验。** r11c0/r15c1/r21c0 三案的
   `executor_response` 都冻结在实现中途（"Now I'll insert the QTMLE cache
   builder…"），`self_report: null`，但 harness 将半成品 worktree 照常提交，
   status 记 COMMITTED，随后 eval 挂 gates——于是在实验记录里，一次**从未被
   执行完的干预**伪装成了一次**被测过且失败**的实验。后果：proposer 只能自
   己 git diff 做法医分析（认知税）；expectation ledger 把"没测到"按"测败了"
   参与信念削弱（账本污染）。
2. **协议层故障不进 RSI 的视野。** r22/r23 的 lane 结果明确写着
   `action protocol failed after 2 repairs`，但 self_review 的 evidence 只有
   轮次编号——r25 对着 retrieval 层动了刀，而病灶在协议层。自改进系统"知道
   该改哪"的那一半失败，根因是 evidence pack 里没有故障层信息。
3. **空转期的时间感侵蚀。** r22–r28 期间 resume 消息反复注入 "Outcomes from
   round 21" 的陈旧世界状态，proposer 在 r29 写下 "Resuming after round 22"
   ——对自己处在第几轮产生漂移。簿记同样漂移（inflight 停在 26、meta
   last_round: 26，实际在 r29）。

一个重要的既有事实：**executor 的遗言没有丢**。`handoffs/r{R}/{R}-c{i}.
executor.json` 的 `executor_response` 完整保留了最后的输出原文。丢失的不是
字节，是**分类**（死亡 ≠ 正常结束）与**义务**（死亡后谁来说明）。

已修复、本设计不再覆盖的部分：proposer 协议失败 fail-fast（不消耗 round、
不写 history、错误消息带 last reply）；executor 正常结束但缺报告的
`no_report` 显式化；实验员报告路由进 world event 与 inspect_episode。

---

## 2. 设计原则

1. **每个进入实验记录的事件必须有一个陈述责任人。** 责任人只能是 executor
   （活着）或 harness（executor 死亡时）。harness 代写的报告必须署名为
   harness，绝不伪造 executor 的话。
2. **停机原因是一等数据。** "说完了"和"没说完"必须可区分；死亡必须可检测、
   可分类（耗尽 | 超时 | 崩溃）、可追溯。
3. **未完成的干预不是实验失败。** 它不参与科学判定（不触发 expectation 的
   削弱语义），但计入执行成本证据。混淆这两者是账本污染之源。
4. **每一层只看它能行动的层的故障信息。** 协议/基础设施故障给 RSI 和人类；
   执行成本（含死亡率）给 reflection；时间感给 proposer。
5. **簿记与现实的最终一致是 harness 的义务**，包括异常路径上的每一次状态
   迁移（omilrec-v100-001 中 inflight 残留导致的 stage/round mismatch 崩溃
   即违反本条）。
6. **词汇域中立。** executor 死亡统一表述为 "experimenter session ended
   without completing the intervention"；不引入任何具体工程任务的词汇。
7. **信息与机制优先，不规训行为。** harness 的职责边界是准备好工具、准备好
   环境、把信息递到；不通过给 agent 加章程/纪律来教它们干活。本设计的一切
   修复手段限于：状态分类（harness 侧）、信息持久化、信息路由。任何角色判
   断质量的提升应该是"看到了以前看不到的信息"的自然结果。

---

## 3. 状态机

### 3.1 Executor session（一次候选实现）

```
                 ┌────────────────────────────────────────────┐
                 │                RUNNING                     │
                 └──────┬───────────┬───────────┬─────────────┘
        正常结束带报告   │           │           │  非正常停机（新增分类）
                       ▼           ▼           ▼
              ENDED_WITH_REPORT  ENDED_SILENT  DIED_{EXHAUSTION|TIMEOUT|CRASH}
                       │           │           │
                       ▼           ▼           ▼
              status=COMMITTED  status=COMMITTED   status=IMPLEMENTATION_INCOMPLETE
              self_report=<报告> self_report=no_report
              照常进入 eval     照常进入 eval      不进入 eval（见 §5 决策）
                                                  harness 代写 post-mortem（署名）
```

关键改动点：

- **死亡检测**在 agent 层：预算/步数耗尽、超时、进程崩溃当前表现为"返回最
  后一段输出文本"，上层无法区分说完与没说完。agent 层必须把停机分成三类
  终态并随结果上抛：`ended`（模型自己停了）、`exhausted`、`crashed`。
- **ENDED_SILENT** 是现有 `no_report` 语义的正式化：executor 活着结束但没交
  报告——实验照常评估（工作可能是完整的），缺报告被显式记录。
- **DIED_*** 三类的区分靠 harness 可观测的事实（步数计数 vs 时钟 vs 进程退
  出码），不靠猜测。

### 3.2 候选（candidate）状态

候选的对外 status 集合从现在的 {COMPLETED, GATE_REJECTED, NO_CHANGE, …}
增加一个正交维度**"干预是否被执行"**：

| status                    | 干预执行了？ | 参与科学判定？ | 进入 expectation 削弱？ |
|---------------------------|------------|--------------|----------------------|
| SELECTED / PASSED_NOT_IMPROVED / FAILED_GATES | 是 | 是 | 是（按预注册） |
| IMPLEMENTATION_INCOMPLETE | 否（死亡）  | 否            | **否**——渲染为"该干预未被执行"，期望行标注"未检验"而非"未通过" |
| EVAL_INFRA_FAILURE（eval 自身崩溃，目前与 GATE_REJECTED 混淆） | 是 | — | 否 |

### 3.3 Round 级别的派生规则

- 一轮中**部分**候选 IMPLEMENTATION_INCOMPLETE：轮照常提交（已执行的其他候
  选是真实实验）；未完成候选进 execution-cost 账。
- 一轮中**全部**候选 IMPLEMENTATION_INCOMPLETE：该轮没有产生任何实验。
  此为开放决策（§6.1）。
- **连续死亡告警**：连续 N 个候选死于同一停机原因 → run 级 WARNING（阈值见
  §6.3）。这与 proposer fail-fast 对称：单个失败是成本，连续同因失败是系统
  性信号，必须浮出而不是靠人翻日志。

### 3.4 Proposer lane（已实施，此处仅入册）

```
RUNNING → SUBMITTED | ABSTAINED          （研究终态，正常入账）
        → PROTOCOL_DEAD | WORKER_CRASH    （基础设施终态）
```
基础设施终态：fail-fast、不消耗 round id、不写 history、错误消息携带原始
last reply。**补充义务（新增）**：last reply 原文必须持久化到 lane 工件
（当前只活在错误消息里，日志轮转即丢失）。

### 3.5 Harness 簿记不变量

每一次状态迁移（含异常终态）之后必须成立：

- `inflight.json` 反映当前未完成阶段，终态即清或推进；
- 空文件/撕裂文件可容忍（读侧容错），不再出现零字节 inflight 使 resume 崩溃；
- `meta.json` 的 last_* 字段与真实推进一致；
- world event 的轮号标注与实际 round id 一致。

---

## 4. 各角色信息义务清单

义务的格式：**谁，在什么事件下，向谁，以什么形式，陈述什么。**

### 4.1 Executor（实验员）

| 事件 | 义务 | 形式 |
|---|---|---|
| 正常结束 | 客观报告：做了什么、跑了什么、观察到什么、偏离在哪 | SELF_REPORT（outcome/summary/fidelity/local_runs） |
| 正常结束但未交报告 | 无（已死不了但可能忘）——harness 标注 no_report | — |
| 死亡 | **无。死人无义务。** harness 接管 | — |

### 4.2 Harness

| 事件 | 义务 | 形式 | 落点 |
|---|---|---|---|
| executor 死亡 | 代写验尸报告（署名 harness）：diff-stat、executor 最后动作、停机原因、显式声明"无实验员报告" | post-mortem 块 | candidate 工件 + history 行 |
| executor 死亡 | 尸体保留可查（commit 或 worktree 快照），不冒充实验 | status=IMPLEMENTATION_INCOMPLETE | history |
| proposer 协议死亡 | 原始 last reply 持久化 | lane 工件 | rounds/rX/lanes/l0/ |
| resume | 时间感：当前轮号、最近完成实验轮号、之间轮次的类别（机制轮/未完成轮） | world event 头部 | session 注入 |
| 任何异常终态 | 簿记一致性（§3.5 不变量） | journal/meta | run 目录 |
| 持续 | 故障聚合：实验员会话死亡率（按停机原因分布）、proposer 协议失败数 | 聚合视图字段 | reflection pack / self_review evidence |

### 4.3 Proposer（scientist）

| 事件 | 义务 | 说明 |
|---|---|---|
| 收到 IMPLEMENTATION_INCOMPLETE | 无强制义务；可选择 inspect 尸体（inspect_episode 应返回 post-mortem） | 信念如何更新是它的科学判断；本设计只保证它看到的是"未执行"而非伪装的"失败" |
| 预注册 | 现状维持（不强制绑定未执行结局） | "未执行"不再伪装成"失败"，账本语义已自洽；是否进一步要求预注册覆盖执行风险，留待证据 |

### 4.4 Reflection

- 消费：execution-cost 聚合（含死亡率与停机原因分布）——这是"该方向是否还
  值得委托执行"的证据，属于它的审计范围。
- 新义务：无（章程不变）。

### 4.5 RSI / self-review

- 消费：lane 层故障原因（协议失败原文、停机原因分布）——修"知道该改哪"。
- **不加任何章程约束。** 如何解读故障层信息是它自己的判断；harness 的义务
  止于把信息递到（治理原则见 §2.7）。r25 式误诊的修复手段是信息补全，
  不是行为规训。

---

## 5. 关键决策（带倾向，待批准）

**D1：尸体是否还跑 eval？** 倾向**不跑**。一次未执行完的干预没有科学信息
量，eval 白耗约 5 分钟；死因与 diff-stat 已足够陈述。反方观点：eval 可能有
意外的间接信息——认为不抵成本。

**D2：尸体 commit 还是丢弃？** 倾向 **commit 并保留**（可追溯性优先），
只是 status 与语义改变。proposer 可通过 inspect_episode 查看尸体与
post-mortem。

**D3：IMPLEMENTATION_INCOMPLETE 的期望行渲染。** 削弱条款不触发；渲染为
"该预注册期望未被检验（干预未被执行）"。proposer 自己如何解读执行成本是它
的判断。

---

## 6. 开放问题（需拍板）

1. **全候选死亡轮是否消耗 round id？** 消耗派：干预被委托且执行失败是真实
   研究成本；不消耗派：与"只有真实研究轮消耗 id"的既定哲学一致。倾向：**消
   耗**（它是被记录的执行成本，不是基础设施故障），但连续死亡告警必须存在。
2. **连续死亡告警阈值**（N 个候选 / N 轮）与告警后行为（停 run vs 仅警
   告）。倾向：连续 3 个候选同因死亡 → WARNING；连续 2 轮全灭 → 停。
3. **EVAL_INFRA_FAILURE 的拆分**：eval 自身崩溃与候选挂 gates 目前共用
   GATE_REJECTED 语义，是否本轮一并拆出。（低成本，倾向做。）
4. **post-mortem 的 diff-stat 粒度**：文件级（changed_paths + 行数）即够，
   还是需要函数级。倾向文件级 + executor_response 尾部原文。

---

## 7. 与既有机制的接缝

- **fail-fast 语义不变**：proposer 的基础设施失败停 run；executor 死亡不停
  run（候选级成本），两者通过 §3.3 的连续性告警衔接。
- **expectation ledger**：`expectation_ledger` 视图需按 §3.2 的正交维度分类
  渲染，未执行行不进削弱统计。
- **reflection_views**：`mechanism_family_distribution` 等聚合视图对
  IMPLEMENTATION_INCOMPLETE 行的计数口径 = 执行成本维度，与科学维度分开统计。
- **history 行不可变原则**：新 status 是新增枚举，不改写旧行。omilrec-
  v100-001 的历史三案（r11c0/r15c1/r21c0）**保持原样**——该 run 作为反例
  归档存在，不做事后标注，不篡改。
