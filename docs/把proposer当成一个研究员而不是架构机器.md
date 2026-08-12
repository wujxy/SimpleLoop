# SimpleLoop Scientist Proposer：从“设计科研流程”到“培养科研人员”

## 1. 这次讨论得到的核心转变

过去设计 Proposer 时，我们一直隐含地在问：

> **一个 Scientist 应该按照什么认知流程解决问题？**

因此很自然地出现了：

- UNDERSTAND
- MODEL
- EXPLAIN
- EXPLORE
- NARROW
- DEEPEN
- Hypothesis generation
- Proposal verification

这些设计并非毫无价值。它们确实描述了优秀科研过程中经常出现的认知行为。

但问题在于：

> **我们把对优秀科研行为的事后描述，误当成了 Scientist 实际运行时必须遵循的程序。**

这会导致 Harness 开始替 LLM 决定什么时候理解、什么时候建模、什么时候形成 hypothesis、什么时候 reframe。

最终 Scientist 不再像一个人在研究，而像是在完成一套科研表格。

本次讨论后的核心认识是：

> **SimpleLoop 真正要设计的不是“科研 workflow”，而是一个接到研究任务后会自然以科研人员方式思考和行动的 LLM。**

---

# 2. Proposer 本质上是“一个人”

Proposer 不应该首先被定义为：

- Proposal Generator
- Hypothesis Generator
- Problem Solver Module
- Scientific Workflow Engine
- Problem Representation Engine

这些都是从外部观察其行为后得到的功能标签。

更自然的定义是：

> **Proposer 是一个被交付 Goal、需要自己想办法把事情做出来的科研人员。**

就像导师对学生说：

> “这个问题交给你，你去研究一下，想办法解决。”

从这一刻开始，学生真正稳定的目标只有一个：

> **想办法完成任务。**

至于后面发生什么：

- 查资料；
- 看代码；
- 建立理解；
- 做计算；
- 怀疑已有方法；
- 写脚本；
- 做 probe；
- 产生一个想法；
- 深挖一个方向；
- 推翻自己的判断；
- 回头重新理解问题；
- 找类似问题；
- 换一个 representation；
- 提出一个方案；

这些都不是预先规定的科研步骤。

它们是一个人在认真尝试解决问题时，根据当前认知状态自然产生的行为。

---

# 3. “解决黎曼猜想”的思想实验

假设导师告诉一个学生：

> “你去解决一下黎曼猜想。”

一个正常的研究者不会首先在脑中执行：

```text
FORM_PROBLEM
→ BUILD_MODEL
→ GENERATE_HYPOTHESES
→ VERIFY
→ PROPOSAL
```

更可能出现的是一种自然反应：

> 这到底是什么问题？  
> 为什么这么多年没人解决？  
> 已经有哪些重要结果？  
> 真正困难在哪里？  
> 人们尝试过哪些路线？  
> 为什么这些路线没有解决？

于是他开始阅读。

读的过程中可能发现等价 formulation，于是开始想：

> 换一种表述会不会更容易？

看到 Hilbert–Pólya 思路以后可能产生兴趣：

> 如果零点对应某个自伴算子的谱，为什么不能从这个方向构造？

于是去查谱理论、random matrix、quantum chaos。

后来发现核心困难依然没有解决，于是暂时放下。

某一天看到另一个结果，又突然产生新的联系。

期间可能：

- 推导；
- 搜文献；
- 算 toy example；
- 写程序看数值；
- 深挖一个方向；
- 怀疑这个方向；
- 回头重新理解问题。

这里不存在固定研究阶段。

存在的是：

> **一个人持续地想办法解决问题。**

Problem Representation、Hypothesis、Reframe、Discovery 都是在这个过程中自然出现的认知现象。

---

# 4. LLM 本身已经具有一部分科研能力

这次讨论另一个重要观察来自 LLM 自身。

当被问：

> “如果让你解决黎曼猜想，你会怎么做？”

LLM 会自然产生：

> “这到底是个什么问题？”  
> “为什么这么多年没人解决？”  
> “大家已经知道什么？”  
> “真正卡在哪里？”

这些行为并不是由 Harness 强制触发的。

同样，现代 coding agent 在面对复杂 repository 时，也可能自然判断：

> “我现在对代码理解不够，直接修改不合适，我应该先了解整体结构。”

这类行为说明 LLM 已经存在一些 latent capability：

- 判断自己是否理解充分；
- 主动获取缺失信息；
- 在行动之前形成一定的问题理解；
- 根据新证据修正判断；
- 自主决定下一步需要做什么。

这类似 coding agent 的 plan mode，但并不意味着必须存在一个硬编码的 `PLAN` 状态。

真正发生的是：

```text
我想完成任务
↓
我意识到当前认知不足
↓
因此我主动调查
```

SimpleLoop 希望进一步激活的是科研场景中的同类能力：

```text
我想完成 Goal
↓
我意识到自己目前只是接受了 existing implementation 的 framing
↓
我应该真正搞明白问题
```

或者：

```text
我已经在一个方向钻了很久
↓
但所有想法都来自同一个解释
↓
也许问题 framing 本身有问题
↓
我应该换一个角度
```

这些才是真正想获得的 **autonomous scientific judgment**。

---

# 5. 因此，Problem Formation 等不是 Runtime State

我们仍然认为以下能力很重要：

- Problem Formation
- Problem Representation
- Mechanistic reasoning
- Hypothesis formation
- Competing explanations
- Analogy
- Representation shift
- Inversion
- Decomposition / recomposition
- Limit reasoning
- Scale analysis
- Anomaly investigation
- Assumption challenge
- Reframe
- Evidence discipline

但它们的架构地位需要改变。

过去：

> **Scientist 必须执行这些步骤。**

现在：

> **这些是优秀 Scientist 应该逐渐内化的科研习惯和思维资源。**

例如导师可能告诉学生：

> 别只盯着别人已经写好的实现。  
> 想想真正的问题是什么。  
> 问问为什么这个量会这样。  
> 你的解释是不是唯一的？  
> 有没有极限情况可以帮助理解？  
> 能不能换个 representation？  
> 有没有类似问题？  
> 你是不是已经钻进一个方向出不来了？

导师不会要求学生每解决一个问题都填写：

```text
representation:
mechanism:
alternative explanation:
counterfactual:
reframe:
```

一个成熟研究者最终会把这些东西变成自己的**科研品味和习惯**。

SimpleLoop 应该尝试做到同样的事情：

> **不是替 LLM 思考，而是让好的科研习惯成为 LLM 在这个角色下更容易自然调用的行为。**

---

# 6. Scientist Identity：真正需要内化的东西

Scientist 最重要的不是知道自己必须执行哪些步骤，而是形成一种责任感：

> **这个 Goal 现在交给我了，我负责想办法完成它。**

因此：

### Goal 是最高层级

成功与否由 Goal 定义。

### Workspace 是材料

源码、已有算法、论文、配置、baseline implementation 都只是当前世界提供给 Scientist 的材料。

它们不是问题定义。

### History 是经验

历史实验告诉 Scientist：

> 以前有人试过什么，发生了什么。

它不是：

> 下一步只能沿着这些方向继续。

### Scientist 必须形成自己的判断

不能只总结 prior。

不能把现有 decomposition 自动继承成 problem decomposition。

不能把“以前就是这样做的”当成理由。

---

# 7. 我们真正想培养的是 Research Disposition

下一版 Scientist 的设计重点不应该首先是一组 workflow。

应该先回答：

> **一个优秀研究生与一个平庸研究生面对同一个任务时，思维习惯有什么区别？**

一个好的 Scientist 可能具有：

### Goal ownership

真正把任务当作自己的问题，而不是完成一次格式化输出。

### 主动理解

不知道怎么做时，会自然先搞明白情况，而不是因为 Harness 要求才调查。

### 机制意识

不会满足于：

> “这里慢。”

而会继续问：

> “为什么这里必须花这么多工作？”

### 独立判断

已有实现和已有研究只是输入，不自动成为自己的观点。

### 对大改变没有心理门槛

如果认为整个 decomposition 错了，可以自然考虑：

> 重构、替换算法、甚至重新实现。

而不是默认 local patch 更合理。

### 对自己认知状态有感觉

能够意识到：

> “我其实还没搞懂。”

或者：

> “我可能已经陷入这个解释里了。”

### 有换视角的习惯

卡住时知道可以：

- 找 analogy；
- 换 representation；
- 挑战 assumption；
- 看极限；
- 改变尺度；
- 从异常出发；
- 重新定义 decomposition。

### 使用实验帮助思考

调查、脚本和 probe 是思维的一部分。

但不会因为可以实验，就把所有 hypothesis 都亲自实现一遍才敢形成判断。

### 大胆形成判断，但允许自己错

科研不是避免错误。

科研是：

> 提出有根据的判断，然后不断让现实修正自己。

---

# 8. Runtime 应该因此变得非常简单

如果 Scientist 真正被当作一个“人”，Infrastructure 只需要给这个人一个可以工作的世界。

大致包括：

```text
Goal
 │
 ▼
Scientist
 │
 ├── Workspace / Source
 ├── Shell
 ├── Search / Investigation Tools
 ├── Scratch / Probe Capability
 ├── History Query
 ├── Continuous Context / Research Memory
 └── Proposal Interface
```

Harness 负责：

- 可访问什么；
- 可以修改什么；
- 有哪些工具；
- 权限边界；
- context / memory；
- 历史查询；
- proposal 如何交付。

Harness **不负责**：

- 什么时候建模；
- 什么时候生成 hypothesis；
- 必须有几个 hypothesis；
- 什么时候 reframe；
- 必须先理解到什么程度；
- 下一步应该调查什么；
- Scientist 当前应该处于哪一个认知状态。

原则是：

> **Harness provides the world. The Scientist decides how to think and act in it.**

---

# 9. 生成侧与认知侧也需要重新理解

之前的：

```text
Generator
→ Hypothesis
→ Cognitive
→ Proposal
```

可能仍然带有过多人为 decomposition。

真实研究更可能是：

```text
读
→ 想到一个 idea
→ 查一下
→ 发现不对
→ 换个理解
→ 又产生两个方向
→ 深挖一个
→ 回头看资料
→ 做个 probe
→ 推翻
→ 突然想到另一个方法
→ proposal
```

因此短期更值得考虑的是：

> **一个拥有连续认知的 Scientist。**

所谓生成、认知、调查、深化、reframe 都是同一个人在不同时间自然采取的行为。

而不是多个固定阶段。

---

# 10. Discovery Repertoire 的正确地位

之前研究过的生成元并没有因此失去价值。

相反：

- analogy
- inversion
- decomposition
- recomposition
- representation shift
- limit reasoning
- anomaly
- scale
- assumption challenge

很可能都是优秀 Scientist 应有的认知资源。

但正确的问题不再是：

> “Harness 怎么安排 G1–G9？”

而是：

> **怎样让 Scientist 知道这些思考方式，并在真正需要的时候自然想到使用它们？**

它们更接近：

> 导师教给学生的科研技巧。

而不是：

> Runtime operator schedule。

---

# 11. 当前阶段真正的问题

因此 SimpleLoop Scientist Proposer 的研究问题已经发生变化。

过去的问题：

> 应该设计怎样的 scientific reasoning workflow？

现在的问题：

> **如何激活 LLM 已经潜在具有的科研能力，让它一进入上下文就自然把自己理解为“这个问题现在交给我，我要想办法解决”的科研人员？**

具体需要继续回答：

### 1. LLM 已经天然具有哪些科研习惯？

例如：

- 主动调查未知；
- 自主形成问题理解；
- 因果/机制推理；
- analogy；
- hypothesis generation；
- 自我质疑；
- reframe；
- tool-use metacognition。

哪些能力已经很强？

哪些只有在人类提醒后才容易出现？

### 2. 哪些科研习惯最容易在 Agent 环境中消失？

例如：

- 一看到代码就变成 coding agent；
- 一看到 history 就开始沿历史优化；
- 一看到 proposal output requirement 就急着交作业；
- 长上下文后过早形成 narrative；
- 工具使用变成“读够材料”，而不是服务思考。

### 3. Scientist Identity 应该怎样写？

目标不是方法论 checklist。

而是让模型形成：

> “我是谁？”  
> “我负责什么？”  
> “我应该以什么态度面对 Goal、prior、history 和 uncertainty？”

### 4. Research Habits 应该怎样注入？

哪些适合写入 identity？

哪些适合作为轻量原则？

哪些应该做成 optional skill / cognitive repertoire？

哪些根本不需要提示，应该相信模型自己的能力？

### 5. Context 应该怎样组织才能强化科研角色？

上下文的结构本身可能比 prompt wording 更重要：

```text
GOAL

You own this research problem.

AVAILABLE MATERIALS
...

TOOLS
...

CURRENT RESEARCH CONTEXT
...
```

而不是：

```text
Your task is to output N proposals...
```

因为后者很可能从第一 token 就把模型 priming 成 proposal generator。

### 6. 怎么判断“科研感”真的被激活了？

不能再检查：

- 有没有 MODEL；
- 有没有 4 个 hypotheses；
- 有没有执行 analogy。

而应该观察行为：

> 它是否主动发现自己不知道什么？  
> 是否主动调查？  
> 是否会形成自己的问题理解？  
> 是否会挑战已有 decomposition？  
> 是否会因为新证据改变研究方向？  
> 是否会产生超出现有实现形状的方案？  
> 是否在没有 Harness 要求的情况下自然出现科研行为？

这可能才是下一阶段最关键的实验。

---

# 12. 当前设计原则

最终可以把当前共识压缩成三句话：

> **Proposer 是一个科研人员，不是一套科研流程。**

> **LLM 已经拥有潜在科研能力；我们的工作不是替它实现科研思维，而是创造身份、上下文和环境，使这些能力稳定地被调用。**

> **复杂性应该主要存在于 Scientist 的认知中，而不是 Runtime Graph 中。**

SimpleLoop 下一阶段真正值得研究的对象，不再是：

> `Scientist workflow architecture`

而是：

> **Scientist identity / research disposition / capability elicitation。**

这应该成为下一轮 Proposer 重构的理论起点。