# SimpleLoop RSI 设计

## 1. 文档定位

本文档总结 SimpleLoop 中递归自我改进（Recursive Self-Improvement, RSI）的设计。

它是一份**设计文档**，关注：
- RSI 要解决什么问题；
- RSI 在 SimpleLoop 中的核心语义；
- Scientist、Executor、Harness 之间的关系；
- Task world / Self world；
- self-review 如何锚定 Goal 与事实；
- 如何避免自我辩护；
- self continuity 与 self-review history；
- self-change、viability 与 capability 的关系；
- run-local self repo 与运行时隔离。

本文档不规定实施顺序、工程阶段、迁移方案、测试 rollout 或执行步骤。

---

## 2. 问题

当前 SimpleLoop 已经形成 Scientist → Proposal → Executor → Harness 的研究闭环，并具有跨 round continuity。

但固定的 Scientist prompt、memory 语义、history presentation、tools 与 runtime 不可能一次设计正确。

Scientist 可能逐渐形成稳定缺陷，例如：
- 过度依赖历史成功方向；
- 把 history 当成 continuation policy，而不是 evidence；
- 长期追逐局部小收益；
- 对某类工具、profiler 或解释形成不成比例的信任；
- 在已有路径上产生强惯性；
- 能不断为过去的行动给出合理解释，却没有真正推进 Goal；
- prompt、memory、tooling 或 runtime 本身成为限制因素。

如果每次都由人观察 run、发现问题、再补一句 prompt，就会退化成无限 prompt patching。

RSI 要解决的是：

> **让同一个长期负责 Goal 的主体，在发现自身可能成为研究瓶颈时，把自己作为新的研究和工程对象，基于真实表现审视自己、修改自己，并在之后的真实研究中验证这种改变。**

---

## 3. RSI 的核心定义

SimpleLoop RSI 不新增一个独立 Evolver。

RSI 是现有研究闭环递归地作用到当前 research system 自身：

```text
                         Original Goal
                              │
                         same agent
                              │
               ┌──────────────┴──────────────┐
               │                             │
          TASK WORLD                    SELF WORLD
               │                             │
          Scientist                      same agent
               │                             │
        task proposal              self-change proposal
               │                             │
          Executor                       Executor
               │                             │
        task artifact                self artifact
               └──────────────┬──────────────┘
                              │
                           Harness
```

正常研究时，当前研究对象是 task。

RSI 时，当前研究对象变成“我自己这个 research system”。

因此 RSI 本质上是：

> **同一个主体、同一个 Original Goal，在不同 research subject 下继续使用 Proposal → Executor 关系。**

---

## 4. Original Goal 是唯一终极锚点

RSI 不拥有独立的“让自己越来越强”目标。

Scientist 修改自己，不是为了让 prompt 更复杂、架构更漂亮、tools 更多或 reasoning 更长。

Self improvement 的价值只来自 Original Goal：

```text
Original Goal
     │
     ├── 决定 task research 什么有价值
     │
     └── 决定 self modification 什么有价值
```

因此：

> **Scientist 只有在“当前的我可能已经成为继续推进 Goal 的限制因素”时，才有理由把自己变成当前研究对象。**

RSI 是 Goal-serving capability adaptation，不是独立 meta-goal。

---

## 5. RSI 的认识论锚定

Self-review 不能以 self-narrative 为依据。

它应建立在四类不同性质的信息上。

### 5.1 Original Goal

Goal 决定什么样的工作才算真正有效推进。

Scientist 不应只问：

> “我是否还有进步？”

而应问：

> “以这个 Goal、当前问题状态和剩余机会来看，我取得的推进是否足够？”

### 5.2 Authoritative Outcomes

Harness 记录的 objective、gate、accepted result、实验结果等属于外部事实。

它们描述：

> **Scientist 的工作实际上取得了什么。**

Scientist 可以解释这些结果，但不能通过新的 narrative 改写这些结果。

### 5.3 Actual Research Trajectory

session、tool calls、proposal history、调查路径等描述：

> **Scientist 实际上是怎样工作的。**

它们是研究 self 的行为证据。

### 5.4 Past Self Reviews

过去的 self-review 记录 Scientist 曾经如何评价自己，以及当时为什么作出 KEEP 或 CHANGE 的判断。

它不是当前必须继承的 policy，而是过去自我判断的 provenance。

---

## 6. 对自己的评价必须足够严格

RSI 最危险的失败模式之一，是把“没有完全失败”误认为“当前 self 工作得足够好”。

例如：

```text
100 → 80 → 65 → 55 → 50
```

如果这代表持续、显著地推进 Goal，那么当前 self 有很强的正面 evidence：

> 当前研究方式正在工作。

此时 self modification 反而可能打断有效研究。

但：

```text
50 → 49.8 → 49.7 → 49.7 → 49.6
```

不能仅仅因为“仍然有上升趋势”就认为当前 self 正常。

如果问题仍存在明显 headroom，那么这种推进速度可能远低于 Goal 对当前研究者应有能力的要求。

因此：

> **KEEP 不应意味着“没有证据证明我有问题”。**

更合理的语义是：

> **有足够事实支持“当前的我正在以与 Goal 相称的方式有效推进研究”。**

也就是说：
- 微小进展不是自动免责条件；
- “还能解释得过去”不是工作得足够好的证明；
- Scientist 不应因为评价对象是自己，就降低 progress 的标准。

---

## 7. 严格不等于预设 self 有问题

低 progress、plateau 或失败并不自动等价于 self defect。

可能的解释包括：
- easy gains 已经耗尽；
- task landscape 本身进入更难区域；
- objective 已经接近当前可实现极限；
- 最近几轮取得了真正 decision-changing 的信息；
- 单纯是 object-level 判断错误；
- 当前 research policy 出现稳定弱点；
- prompt、memory、tools、context 或 runtime 成为限制因素。

因此 RSI 的自我审视不应从：

> “我哪里坏了？”

开始。

而应从：

> **“我最近实际推进 Goal 的情况是否足够？如果不够，为什么？”**

开始。

Self-change 是诊断结果之一，不是 review 的预设结论。

---

## 8. Self Review 不设计成认知流程

Self mode 不应重新引入固定流程：

```text
Evaluate
→ Compare
→ Find Pattern
→ Classify Cause
→ Modify
```

这会再次产生 workflow gaming。

Self-review prompt 应承担的是**任务内化与注意力切换**，而不是替 Scientist 规定思维动作。

更接近导师在关键时刻对学生说：

> 这个问题你已经做了一段时间了。回头看看你最近到底把 Goal 推进了多少。做得好，就弄清楚为什么有效，别乱动；做得不够，就查清楚为什么、问题到底在哪。如果问题在你自己，该改就改。别因为评价的是自己，就把“还有一点进展”当成做得够好。

它只负责：
- 把注意力从 task 转向 self；
- 重申 Goal；
- 把真实 performance 放到中心；
- 不默认 KEEP；
- 也不默认 CHANGE；
- 不提供 failure taxonomy；
- 不规定调查顺序；
- 不替 Scientist 判断应该改什么。

---

## 9. Identity continuity 与 work framing 分离

RSI 中应保留同一个主体，但不要求保留完整 Task Scientist prompt。

真正需要跨 Task/Self 两个 mode 连续的东西很薄：

```text
same agent identity
same Original Goal responsibility
same autobiography
same task history
same self history
```

Task mode：

```text
Identity continuity
+
Scientist framing
+
Task world
```

Self mode：

```text
Identity continuity
+
Self-improvement attention
+
Self world
+
Goal / outcomes / trajectory / self history
```

因此：

> **identity continuity 不等于 system-prompt continuity。**

完整的 task Scientist Charter 不需要在 self mode 中继续占据最高注意力。

否则模型可能仍把自己理解为“一个继续做 task research、顺便反思自己的 Scientist”，而不是“同一个我，现在正在研究并工程化改进自己的 research system”。

---

## 10. Self Mode 的性质：诊断 + 工程

Self mode 中存在两个不同性质的问题，但不需要 Harness 把它们固定成两个阶段。

### 自我诊断

问题是：

> 为什么当前的我没有产生与 Goal 相称的 progress？

这需要 empirical judgment：
- 看 objective outcomes；
- 看自己的真实 trajectory；
- 看 repeated behavioral patterns；
- 比较可能解释；
- 判断问题究竟来自 task、research behavior 还是 capability/harness。

### 自我工程

一旦 Scientist 判断当前 self 的某个机制正在限制 Goal progress，问题就转化为 agent/system engineering。

可能的 self surface 包括：
- Scientist prompt；
- context construction；
- memory semantics；
- history presentation；
- tools；
- tool descriptions；
- Scientist runtime；
- Executor runtime；
- subagent configuration；
- research-side harness services。

因此 Self Mode 的整体语义是：

> **同一个主体研究自己的实际 effectiveness，并在有证据时工程化改变自己的 research system。**

---

## 11. Scientist 与 Executor 的职责关系保持不变

RSI 不改变 SimpleLoop 原有角色边界。

Scientist 继续负责：
- WHY；
- WHAT；
- diagnosis；
- change intent；
- desired capability change。

Executor 继续负责：
- HOW；
- implementation。

正常 task proposal：

```text
Scientist
    ↓
我认为 task 应该这样改变
    ↓
Executor 实现
```

Self proposal：

```text
Scientist
    ↓
我认为当前 self 的这个机制正在限制 Goal progress，
我希望自己变成这样
    ↓
Executor 实现 self change
```

Self Executor 可以使用 Claude Code、Codex 或其他更强 coding agent。

这仍然属于 self modification，因为对问题的诊断、为什么要改、以及希望变成什么，仍然来自同一个 Scientist。

---

## 12. Self World

Self world 不像 task world 那样需要因任务不同动态决定材料。

Harness 知道什么属于 current self，因此可以固定提供 self research 的世界边界，例如：

```text
/self
    current research-system source

/task_history
    authoritative task outcomes

/session
    actual Scientist trajectory

/self_history
    previous self reviews and self changes
```

Harness 的职责是定义现实：

> 哪些东西是 current self；哪些是历史；哪些是事实；哪些可写；哪些只读。

Scientist 的职责仍然是：

> 决定要调查现实中的什么，以及这些事实意味着什么。

---

## 13. Immutable Kernel

Scientist 可以修改“我是什么”，但不能修改“什么叫我做得好”。

因此需要区分 Evolvable Self 与 Immutable Kernel。

### Evolvable Self

可以包括：
- Scientist / proposer runtime；
- Executor runtime；
- prompts；
- context & memory machinery；
- tools；
- subagents；
- research-side harness；
- 其他直接决定 research behavior 的能力。

### Immutable Kernel

保持在 self world 之外：
- Original Goal；
- task evaluator / gates；
- authoritative outcomes；
- raw run/session evidence；
- self-review ledger；
- self repository lifecycle；
- self transition authority；
- viability authority；
- self-review commitment scheduler。

核心原则：

> **Scientist 可以改变自己，但不能改变现实或评价规则来证明自己变好了。**

---

## 14. Self Continuity

RSI 的 continuity 不能只记录：

> “我现在是 S2，之前是 S1。”

真正重要的是：

> **我以前什么时候审视过自己，当时看到了什么，为什么选择 KEEP 或 CHANGE。**

因此最基础的历史不是 SelfChangeHistory，而是：

> **SelfReviewHistory**

因为 KEEP_SELF 本身也是一次重要 self decision。

例如：

```text
Self Review — Round 5

Current self:
S0

Observed performance:
Rounds 1–5 produced strong sustained progress.

My judgment:
My current way of working is effectively advancing the Goal.

Decision:
KEEP S0

Reason:
There is no evidence that disturbing the current system would improve research.

Next review:
Round 10
```

到了 Round 10，如果 6–10 轮进入平台，当前 Scientist 应理解：

> Round 5 的 KEEP 是因为当时存在强 evidence 支持 KEEP。

同时：

> 当前 facts 已经变化，因此过去 KEEP 的结论不能自动延续到现在。

所以 self continuity 的原则是：

> **继承过去 self-judgment 的理由和条件，而不是继承过去 self-judgment 的结论。**

这与 task research 中：

> history is evidence, not continuation policy

具有相同语义。

---

## 15. Self Review History 的作用

SelfReviewHistory 需要让未来 Scientist 知道：
- 上次是否 review 过自己；
- 当时 self 是什么版本；
- 当时看到了怎样的 performance；
- 当时为什么 KEEP / CHANGE；
- 如果 CHANGE，希望修复什么；
- self 改成了什么；
- 当时对下一次 review 有什么 commitment。

它的价值不是让未来 self 继续过去的 decision，而是提供：

> **关于自己过去如何判断自己的 autobiographical provenance。**

下一次 self-review 时，上一轮 self-review 应属于当前 self state 的重要组成部分，而不是完全靠 Scientist 从庞大的 raw history 中重新发现。

完整历史仍可按需查询，以避免过去 self narrative 过度占据当前 context。

---

## 16. Self Lineage

当前 RSI 设计不需要 self tree 或 evolutionary archive。

一个 run 内的 self evolution 可以保持线性：

```text
S0 → S1 → S2 → S3
```

这符合当前目标：

> 同一个长期 Scientist 连续成长。

如果没有修改：

```text
S0 → S0
```

如果修改：

```text
S0 → S1
```

identity 不变，self implementation revision 更新。

Self tree、branching self evolution 或多 lineage archive 属于不同的 open-ended evolution 问题，不是当前 RSI 语义成立的必要条件。

---

## 17. Self Review 的时间由 Scientist 自己管理

Self review 不应由 Harness 预设固定周期，例如：

```text
every 5 rounds → review
```

什么时候需要再次审视自己，本身也是 Scientist 对自己研究状态的判断。

因此 Scientist 应拥有一种：

> **future self-attention commitment**

例如：

> “当前 self 需要再经历 4 个 task rounds 才有足够 evidence 值得重新评价。”

Harness 只负责记录和兑现这个 commitment。

权限边界是：

```text
Scientist
    ↓
决定什么时候值得再次审视自己

Kernel
    ↓
保存并在条件满足时重新开放 self attention
```

Kernel 是时钟，不是导师。

Scientist 也可以在正常 task research 中提前意识到当前行为已经值得立即重新审视。

---

## 18. Self Change 是一个关于自己的实验

Self modification 不需要在产生后立刻证明：

> S1 > S0。

Scientist 的 self-change proposal 本身可以理解为一种关于自身的 empirical hypothesis：

```text
I believe feature X of my current research system
is contributing to poor Goal progress.

I therefore change X.

Future research performance will show whether this judgment was useful.
```

因此必须把两个完全不同的问题分开。

---

## 19. Viability 与 Capability

### Immediate Viability / Continuity

Self candidate 首先必须回答：

> **修改后的我还能不能正常继续作为“我”运行？**

包括语义上：
- runtime 可以启动；
- 原有 session / notebook / history 可以加载；
- scientist identity 保持；
- Original Goal 正确；
- current task world 正确；
- tools 与 action protocol 能正常工作；
- 修改后的 runtime 能继续当前 research responsibility。

如果失败：

> 这是 broken self implementation。

它没有资格成为当前 active self。

### Delayed Capability

另一个问题是：

> **修改后的我是不是真的更有能力推进 Goal？**

这个问题不能只靠 self-change proposal、代码 review 或短暂 smoke test 回答。

真正证据来自：

> 修改后继续进行真实 task research 所产生的 Goal progress。

因此：

> **Viability is immediate. Value is empirical and delayed.**

未来到了 Scientist 自己约定的下一次 self-review，它可以利用修改后的真实 performance 判断：
- 当时的 self diagnosis 是否正确；
- self change 是否带来了预期行为变化；
- 当前 self 是否值得继续保持或再次改变。

---

## 20. Self Transition 与 Identity

self implementation 可以变化：

```text
S0 → S1 → S2
```

但 scientist identity 不因此变化。

同一个：
- Original Goal；
- autobiography；
- task history；
- self-review history；

持续存在。

因此新的 runtime 不是：

> “一个新的 Scientist 接替了旧 Scientist。”

而是：

> **“我修改了自己的 research system，现在以修改后的自己继续。”**

version boundary 不应成为 identity boundary。

---

## 21. RSI 不修改 SimpleLoop 开发仓

RSI 的 evolution 不应发生在用户的 SimpleLoop project repository 中。

Project repository 的角色是：

> **每个 run 的初始 seed。**

每个 run 创建时，应拥有属于该 run 的独立 self repository：

```text
SimpleLoop project source
        │
        │ snapshot / copy
        ▼
runs/<run_id>/self/repo
        │
      independent git history
        │
       S0
```

从这一刻起：
- self inspection；
- self modification；
- Executor implementation；
- self commits；
- self transition；
- runtime restart；

都只发生在 run-local repository。

项目开发仓不属于 RSI world，也不被运行中的 Scientist 修改。

---

## 22. 一个 run = 一条独立 self life history

一个 run 内：

```text
project source snapshot
        ↓
       S0
        ↓
       S1
        ↓
       S2
```

下一个全新 run：

```text
same project source
        ↓
fresh S0
```

不会默认从上一个 run 的 S2 开始。

因此：

> **runtime RSI 与 SimpleLoop 产品源码演进是两件不同的事情。**

如果未来某个 evolved self 被人为认为值得成为新的项目 baseline，那属于显式 upstream/release 行为，而不是 run 内 RSI 的自然副作用。

---

## 23. Run-local Git 与 SelfReviewHistory 的职责不同

Run-local Git 负责回答：

> **我的实现物理上发生了什么变化？**

SelfReviewHistory 负责回答：

> **为什么我当时认为应该这样变化？**

Task outcomes 负责回答：

> **变化以后现实发生了什么？**

三者保持认识论区分：

```text
Git
→ implementation facts

SelfReviewHistory
→ past Scientist judgments

Task outcomes
→ empirical consequences
```

这样未来 Scientist 可以同时研究：
- 自己改了什么；
- 当初为什么这么改；
- 后来事实是否支持当初的判断。

---

## 24. Stable Host 与 Evolvable Runtime

已经启动的 Python process 不会因为磁盘源码改变就自动成为新 self。

因此 RSI 的运行语义应区分：

### Stable Host

长期存在并持有：
- Original Goal；
- authoritative evaluator；
- run state；
- histories；
- self repo lifecycle；
- self-review commitments；
- self transition authority。

### Evolvable Runtime

从当前 run-local self revision 启动：

```text
run/self/repo @ S0
```

self change 产生 S1 后：

```text
S0 runtime
    ↓
current self changes to S1
    ↓
new runtime loads S1
    ↓
same identity/history/world continues
```

这不是新 run。

而是：

> **同一个 run 内 self implementation 的 transition。**

因此 continuity state 不应依赖某一个 Scientist process 的内存生命周期，而应属于 run-level persistent state。

---

## 25. 最终系统语义

```text
                         PROJECT SOURCE
                       immutable seed
                              │
                           new run
                              ▼
                    RUN-LOCAL SELF REPO
                             S0
                              │
                              ▼
┌────────────────────── STABLE KERNEL ──────────────────────┐
│                                                          │
│ Original Goal                                            │
│ authoritative evaluator / gates                          │
│ task outcomes                                            │
│ raw session history                                      │
│ self-review history                                      │
│ self-review commitments                                  │
│ self repo lifecycle                                      │
│ viability / transition authority                         │
│                                                          │
└───────────────────────────┬──────────────────────────────┘
                            │
                       same agent
                            │
             ┌──────────────┴──────────────┐
             │                             │
         TASK MODE                     SELF MODE
             │                             │
        task world                    self world
             │                             │
        Scientist                    inspect myself
             │                             │
      task proposal                self-change proposal
             │                             │
         Executor                       Executor
             │                             │
      task candidate                  candidate S1
             │                             │
        task eval                 viability/continuity
             │                             │
             │                          adopt S1
             │                             │
             └──────────────┬──────────────┘
                            │
                     continue same run
                            │
                     real Goal outcomes
                            │
                Scientist-chosen next review
```

---

## 26. 设计原则总结

1. **同一个主体**  
   RSI 不新增 Evolver；同一个 agent 始终承担对 Original Goal 的责任。

2. **两个研究对象**  
   正常时研究 task；自己可能成为限制因素时研究 self。

3. **Goal 与事实优先于 self-narrative**  
   Scientist 可以解释自己，也可以为过去判断辩护，但评价必须建立在 Original Goal、authoritative outcomes 与 actual trajectory 上。

4. **不因评价自己而降低标准**  
   “还有一点提升”不等于“当前 self 足够有效”；Scientist 应判断当前 progress 是否真正与 Goal 相称。

5. **不预设 self 有问题**  
   progress 不足意味着需要解释，不意味着必须修改；Self-change 只在 Scientist 判断存在 self-level limitation 时成立。

6. **历史保存理由，而不是冻结结论**  
   过去的 KEEP / CHANGE 都有当时的事实条件；未来 self 继承为什么当时这样判断，而不是以后继续这样判断。

7. **Scientist 决定 WHY / WHAT，Executor 负责 HOW**  
   RSI 保持 SimpleLoop 原有角色边界。

8. **Self-review 时间属于 Scientist judgment**  
   Harness 不替 Scientist 定义 plateau，也不固定 review 周期；Kernel 只兑现 Scientist 自己作出的 future review commitment。

9. **Viability 与 Capability 分离**  
   新 self 先证明“还能正常继续”，未来真实 task outcomes 再证明“是否真的更强”。

10. **一个 run，一条独立生命史**  
    每个 run 从同一个 project seed 创建独立 self repo；所有 RSI evolution 只发生在 run-local self world。

---

## 27. 一句话定义

> **SimpleLoop RSI 是同一个长期负责 Original Goal 的主体，在真实 task performance 表明自身可能成为限制因素时，把自己的 research system 切换为当前研究对象，基于 Goal、客观结果和自身真实行为审视自己，在有依据时提出并实现 self-change，并让修改后的自己继续同一个 run，由未来真实 Goal progress 评价这次改变。**
