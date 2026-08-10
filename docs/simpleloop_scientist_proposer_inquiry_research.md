# 从 Inquiry 到 Scientist-Proposer：SimpleLoop 下一代 Proposer 的科研方法论与架构设计

> **文档性质**：研究方法论与架构设计文档  
> **目标**：从专业问题求解与科学研究方法出发，解释我们为什么需要重构 SimpleLoop Proposer；用“太阳中微子流强测量”和“哥德巴赫猜想”两个性质完全不同的问题验证方法论的普适性；最后推导出一套适合当前 SimpleLoop、但不提前跨入完整 autonomous researcher 的 Proposer vNext 设计。

---

## 摘要

SimpleLoop 当前的 Proposer 已经通过“生成侧 + 认知侧”、多 lane、生成元基、历史记忆与候选实验闭环取得了比过去更好的 OMILREC 优化结果。但进一步观察表明，它仍然容易把科研任务理解为“从局部现象中寻找可尝试的 intervention”：在 OMILREC 中，它能找到热点、缓存、hoist、allocation 等有效局部方向，却很难先从系统整体出发理解 reconstruction、optimizer、FCN、数据生命周期、状态 ownership 与成本形成机制，再提出跨组件、跨文件的结构性重构。

这暴露的不是单纯的 idea-generation 问题，而是更前置的 **problem construction / problem representation / model construction** 问题。

本文重新从 John Dewey 的 reflective inquiry、Herbert Simon 的 ill-structured problem 与 problem representation、Donald Schön 的 framing/reframing、Charles S. Peirce 的 abduction–deduction–test、Nancy Nersessian 的 model-based reasoning，以及 Value of Information 式“何时停止调查”的思想出发，提出一个统一观点：

> **研究的核心不是从给定问题搜索答案，而是把一个最初模糊、结构不完整的情境逐渐转化成一个足以解释、推理、区分假设并支持行动的问题模型。**

在这个观点下，“生成 proposal”不再是第一性能力。一个专业研究者首先需要抑制 premature solution，调查问题全貌，构造有生产力的问题表示与 Working Model，解释为什么当前结果会发生，形成多个竞争解释，再从解释空间扩展干预路线，最后才投入深度调查形成 proposal。

用太阳中微子流强测量和哥德巴赫猜想两个极端案例可以看到：二者的知识对象、证据形式和实验手段完全不同，但都自然出现同一条认知主线：

**UNDERSTAND → MODEL → EXPLAIN → EXPLORE → NARROW → DEEPEN → PROPOSE/TEST**

因此，SimpleLoop 下一版不应继续强化“Generator Agent + Cognitive Agent”的角色边界，而应改成 **单个 Proposer Scientist 在不同研究阶段切换注意力和信息权限**。当前 Loop、Candidate Worker、Gate、Experiment Ledger 和多 lane 并行可以继续保留；主要重构集中在 Proposer 的单轮研究过程。

关键设计为：

1. **单 Proposer Scientist**：取消生成侧/认知侧的角色分裂，保留由浅入深、由宽到窄的认知漏斗。
2. **历史延迟注入**：UNDERSTAND / MODEL / EXPLAIN / 首次 EXPLORE 不看搜索历史；在形成独立 Working Model、Explanation Set 和 Hypothesis Portfolio 后首次注入历史。
3. **历史可见性单调**：同一 LLM context 中只允许 `history 0 → 1`，不能假装 `1 → 0`。
4. **认知层级回退**：历史或新证据若只否定方向，回 EXPLORE；若否定解释，回 EXPLAIN；若否定 Working Model，回 MODEL；只有怀疑整个 frame 被锚定时才开启新的 `fresh_reframe` clean context。
5. **Artifact 不是填表**：Working Model、Explanation、Hypothesis 等必须成为后续 proposal derivation 的真实上游节点，并由 evidence linkage、counterfactual usefulness 和 downstream dependency 防止“先想 proposal、再倒填模型”。
6. **Harness 不替 Scientist 思考**：Harness 负责信息权限、可用 action、阶段 commitment 和最低研究完整性；Scientist 自己决定调查什么、如何建模以及哪些证据足以改变判断。

本文最后给出可一次性落地到当前 SimpleLoop 的状态机、action、artifact、prompt identity、phase attention、回退机制、OMILREC 映射及 A/B 实证评估方案。

---

# 1. 问题是怎样被发现的：从“更好的 idea generator”到“研究方法”

## 1.1 当前 Proposer 已经解决了什么

SimpleLoop 当前的 Proposer 已经不再是一次 prompt 直接吐 proposal。现有设计通过：

- history-blind 的广泛生成；
- 对源码的 survey；
- lever map；
- 5/9 生成元基；
- 多个 lane 并行；
- history-aware 的 cognitive enrichment；
- Candidate Worker 执行；
- Gate / metric 实验验证；
- Experiment Ledger / Finding Archive / Frontier 记录历史；

已经明显增强了 candidate 多样性和实验产出。

这套设计解决的是一个真实矛盾：

> **过早看历史会压缩搜索空间；完全不做认知验证又会导致浅层、随意的 proposal。**

因此此前“生成侧负责宽、认知侧负责深”的分离是合理的工程解。

但 OMILREC 的后续表现暴露出更深一层的问题：即使生成侧能产生很多不同 idea，它构造搜索空间的方式仍然过于贴近源码局部结构。它会从“函数/热点/局部工作”直接形成 lever，而不是先回答：

- OMILREC 作为一个 reconstruction system 到底怎样从 event 走到 reconstruction result？
- SPEED_MS 是怎样由调用频率、状态生命周期、数据流、计算流和算法结构共同产生的？
- charge/time likelihood、optimizer 与 reconstruction 层分别承担什么职责？
- 哪些工作真正依赖 FCN-call-level 参数，哪些只依赖 event？
- 当前性能瓶颈是算术本身、重复工作、数据 ownership、representation，还是调用 topology？
- 一个局部热点究竟是 root mechanism，还是更高层设计的表象？

于是问题从“如何让 Generator 提更宏观的 idea”进一步上升为：

> **一个专业思考者面对粗糙、困难、定义不完整的问题时，究竟如何把它转化成一个可推理、可研究的问题？**

这一步之后，AI Agent 架构本身退居第二位；更重要的是科研方法。

---

# 2. 方法论主线：专业研究不是从 solution 开始

## 2.1 Dewey：先悬置判断，再调查“问题究竟是什么”

John Dewey 在 *How We Think* 中把 reflective inquiry 描述为从 felt difficulty 出发，经历 difficulty 的定位与定义、可能 solution 的 suggestion、对 suggestion 后果的 reasoning，以及进一步 observation/experiment 的检验。更关键的是，他强调 critical thinking 的核心之一是 **suspended judgment**：不要被第一个看似合理的 suggestion 迅速终止 inquiry，而应先调查问题的 nature。

这给本项目最直接的启示不是“五阶段流程”，而是一条认知纪律：

> **局部机会首先是关于问题的证据，而不是问题本身，更不是 solution commitment。**

当 Proposer 看到一个 hotspot、一个异常、一段复杂代码、一个统计误差项，它的第一反应不应是“这里可以改”，而应是：

> “这个现象在整个问题中意味着什么？它反映了哪一个更高层机制？”

Dewey 的意义因此不是提供一个 agent state machine，而是说明为什么 **premature proposal 必须被结构性抑制**。

## 2.2 Simon：ill-structured problem 要先被构造成可搜索的问题

Herbert Simon 对 ill-structured problems 的研究指出，现实问题往往并没有一开始就给定清晰的 state、operator、constraint 和完整 objective structure。解决问题的一部分工作，就是逐渐把问题本身结构化。

这意味着在科研中：

> **搜索空间不是原本就存在、只等着算法去搜索；研究者通过 problem representation 构造搜索空间。**

对于 OMILREC，如果问题表示是：

> “一组 C++ 热点代码的性能优化”

那么搜索空间自然是：

> cache / hoist / inline / allocation / loop / data layout。

如果问题表示变成：

> “一个 iterative inference system，其中不同状态具有不同生命周期，而 optimizer 反复触发 likelihood evaluation”

那么搜索空间会自然出现：

> state lifetime、ownership、shared context、evaluation topology、functional decomposition、information flow。

因此，Proposer 的宏观能力不能靠一句“请从架构角度思考”实现，而必须让它真正完成 **problem representation construction**。

## 2.3 Schön：问题不是只定义一次，而是在行动中不断 framing / reframing

Donald Schön 对工程师、建筑师、医生等专业人士的研究进一步说明：真实专业问题往往不是“给定一个 well-defined problem，再应用正确技术”。专业人士会选择当前 situation 中哪些因素值得关注，把 messy situation frame 成某一种问题，然后通过行动观察 situation 如何“反馈”，必要时重设 frame。

因此：

- Working Model 不是最终真理；
- 初始 problem representation 只是 provisional frame；
- 新证据如果持续与现有模型冲突，不应只修 proposal，而应允许回到更高层 **reframe**。

这也为后面历史注入后的回退规则提供了理论依据：新 evidence 应该修复被推翻的认知层，而不是统一“regenerate”。

## 2.4 Peirce：EXPLAIN 不是 optional，科学推理需要 abduction → deduction → test

Charles S. Peirce 把科学 inquiry 中的推理区分为：

- **Abduction**：针对令人困惑的现象提出可能解释；
- **Deduction**：如果解释是真的，推导还应当出现什么后果；
- **Induction / experiment**：检验这些后果。

这说明 `MODEL → EXPLORE` 之间不能省略 EXPLAIN。

Working Model 回答：

> **这个研究对象是怎样组织和运行的？**

Explanation 回答：

> **为什么当前出现了我要改变/解释的现象？**

二者不相同。

如果缺少 EXPLAIN，Proposer 即使构造了系统模型，也仍然可能直接问“模型中的哪里可以改”，于是退化为更宏观一点的 lever search。

真正需要的是：

**Working Model → Competing Explanations → Intervention Space**

例如：

> `optimizer repeatedly evaluates FCNs` 是模型事实；  
> `event-invariant work 被错误绑定到 FCN-call lifetime` 才是 explanation；  
> `把 invariant state ownership 上移到 event-level` 才是 intervention。

## 2.5 Nersessian：模型不是 summary，而是研究者进行推理的载体

Nancy Nersessian 的 model-based reasoning 强调科学研究中模型构造、模型操作、类比和 thought experiment 对概念创新的重要性。

这给 `WorkingModel` 一个非常重要的质量标准：

> **模型必须能承担推理，而不是复述资料。**

一个 repository summary：

> file A 做 X，file B 做 Y，class C 调用 D。

不是 Working Model。

一个有用的模型应当能够回答 counterfactual：

> 如果 optimizer FCN 调用次数减半，总成本如何变化？  
> 如果某类 state 从 FCN-call lifetime 提升到 event lifetime，哪些 consumer 和成本会改变？  
> 如果某背景完全被独立约束，solar flux uncertainty 是否仍然受限？  
> 如果某个 Goldbach lemma 成立，剩余 proof gap 是否闭合？

不能产生解释、预测或 counterfactual 的“模型”，只是 summary。

## 2.6 调查何时足够：不是 completeness，而是 decision-changing unknown

科研调查不可能无限进行。一个研究者不需要“把所有东西都懂完”才行动。

Value of Information 的思想给出了一种非常适合 agent 的 stopping principle：额外信息之所以有价值，不是因为“知识更多”，而是因为它可能减少对后续决策有意义的不确定性。

因此，不问：

> “我是不是已经调查了足够多文件/论文？”

而问：

> **当前还有没有一个关键未知，一旦知道答案，很可能改变我的 problem model、competing explanations、方向排序或下一步 research move？**

如果有：继续调查。  
如果没有：当前理解已经足以进入下一层。

这是比固定 tool-call quota、文件数、token 数更接近专业研究者的停止标准。

---

# 3. 统一的研究认知结构

将上面的思想压缩后，可以得到一条非常稳定的主线：

```text
模糊情境 / 粗糙要求
        │
        ▼
   UNDERSTAND
  问题到底是什么？
        │
        ▼
      MODEL
  它是怎样工作的？
        │
        ▼
     EXPLAIN
 为什么会出现当前结果？
        │
        ▼
     EXPLORE
  有哪些不同解释/干预路线？
        │
        ▼
      NARROW
  哪些路线值得实验成本？
        │
        ▼
      DEEPEN
  关键前提真的成立吗？
  实验应看到什么？
        │
        ▼
     PROPOSE / TEST
```

这不是宣称所有科研都按固定顺序机械执行。

真正的认知原则是：

1. **由浅入深**：先整体 representation，后局部证据；
2. **由宽到窄**：先保持多种解释/方向，后投入深度研究；
3. **证据驱动回退**：新证据推翻哪一层，就回哪一层修；
4. **问题模型先于 solution space**；
5. **解释先于 intervention**；
6. **prediction/test 使 hypothesis 成为科研 hypothesis，而不是 idea。**

接下来用两个极端问题检验这套结构。

---

# 4. 案例一：从“请测量太阳中微子流强并尽可能压低误差”到可研究问题

这个要求看起来明确，实际上包含大量未定义内容。“太阳中微子流强”并不是一个单一、直接可观测的量；“误差尽量低”也没有说明究竟受统计、背景、探测器、理论转换还是参数退化限制。

Borexino 的实际工作很好地说明了这一点：pp、7Be、pep 等成分可以通过扩展能区的 global fit 同时抽取 interaction rates；不同分量的 precision 与 limitation 不同。Borexino 后续的 CNO 分析又利用了与传统 spectral information 独立的 directionality information，说明当主要限制来自 signal/background degeneracy 时，“增加新的正交信息轴”可能比单纯增加曝光更有价值。

## 4.1 UNDERSTAND：先把“flux”定义清楚

研究者首先不能直接讨论“怎么减误差”，而应先问：

- 测的是哪种 solar component？
- 是 detector interaction rate、Earth flux、electron-neutrino flux 还是 total active flux？
- 哪些物理转换与探测响应位于 measurement chain 中？
- success criterion 是单一 flux precision，还是多个分量联合测量？

原始要求被改写成类似：

> 在指定探测器、能区、曝光与物理模型下，对目标 solar-neutrino component 的 flux 参数进行估计，并最小化统计与系统联合不确定度。

这一步已经把含混目标变成了可建模对象。

## 4.2 MODEL：把问题表示成 inverse inference

可以建立：

```text
solar production fluxes
        ↓
flavor conversion
        ↓
interaction cross sections
        ↓
true event distributions
        ↓
detector response
        ↓
observed energy / position / time / direction / topology
        +
background populations
        ↓
likelihood / posterior over flux and nuisance parameters
```

Working Model 不再把“更多事件”当成唯一信息，而是开始描述：

- flux 参数；
- nuisance 参数；
- observables；
- detector response；
- backgrounds；
- 参数相关性；
- 哪些 observable 对哪个 nuisance 提供区分力。

这才使后续误差分析具有逻辑基础。

## 4.3 EXPLAIN：为什么当前 flux precision 下不去？

此时提出 competing explanations：

- E1：纯统计量主导；
- E2：signal/background spectral degeneracy 主导；
- E3：energy scale / response uncertainty 主导；
- E4：现有 observables 的 Fisher / likelihood information 不足；
- E5：从 interaction rate 到 physical flux 的理论 nuisance 主导。

这些 explanation 不是 solution。

它们是关于“误差为什么大”的 competing models。

## 4.4 EXPLORE：从解释空间自然生成 intervention space

如果 E1 成立，方向可能是：

- 增大 exposure；
- 提高 efficiency；
- 增大有效质量。

如果 E2/E4 成立，方向可能是：

- 找一个与 energy spectrum 更独立的 observable；
- 使用方向、时间、空间或 topology 信息；
- 联合 fit 以打破 nuisance correlation。

如果 E3 成立，方向可能是：

- calibration；
- detector response model；
- fiducial-volume determination。

这时 proposal 不是“brainstorm 出来的”，而是从 explanation 推出来的。

Borexino CNO directionality 的真实结果正好展示了这种逻辑：方向信息与原来的 spectral information 独立，因此能够提供新的 signal/background discrimination，并减少对某些外部 background constraint 的依赖。

## 4.5 NARROW / DEEPEN：调查真正会改变设计选择的未知

假设关键未知是：

> 当前究竟 statistical-limited，还是 nuisance-degeneracy-limited？

这个未知会直接改变实验设计，因此必须继续调查。

而：

> 一个极小次级 background 是 0.7% 还是 0.8%

如果不会改变路线排序，就可以暂时不调查。

选定 directionality 路线后，DEEPEN 才进入具体问题：

- Cherenkov component 的可提取信息量；
- 与 scintillation timing 的关系；
- direction estimator；
- signal/background angular model；
- calibration；
- systematic closure；
- expected posterior improvement。

由此产生实验级 proposal。

---

# 5. 案例二：从“如何解决哥德巴赫猜想”到可研究问题

哥德巴赫猜想与太阳中微子相反：命题本身极其清楚，但“如何解决”几乎没有给出任何 research representation。

如果一个 proposer 直接输出：

> 尝试归纳法、筛法、解析数论、计算验证……

这不是 research proposal，而只是方法名罗列。

## 5.1 UNDERSTAND：把自然语言命题变成 proof obligation

定义 representation count：

\[
R(N)=\#\{(p,q):p+q=N,\;p,q\text{ prime}\}.
\]

强哥德巴赫猜想变成：

\[
R(N)>0
\quad
\text{for every even }N>2.
\]

问题从“找两个素数”转化为：

> 证明一个 additive representation function 对所有目标 N 严格为正。

这一步开始暴露 analytic / sieve / exceptional-set 等研究表示。

## 5.2 MODEL：建立 proof landscape，而不是“整数系统”

数学中的 Working Model 可以是 proof landscape：

```text
Goldbach
  │
  ├── Fourier / circle-method representation
  │       └── main term vs error / major-minor arcs
  │
  ├── sieve representation
  │       └── prime + almost-prime / parity obstruction
  │
  └── exceptional-set representation
          └── density-one control vs zero exceptions
```

Helfgott 对 ternary Goldbach 的证明展示了 circle method 中 major/minor arcs 的精细控制；Chen-type 结果则说明 sieve route 可以逼近 `prime + prime`，但仍存在结构性缺口。

Working Model 的作用不是证明猜想，而是明确：

> **现有方法为什么还差最后一步？**

## 5.3 EXPLAIN：为什么当前路线没有闭合证明？

不同 representation 下 explanation 不同。

例如：

- E1：binary circle method 缺少足够强的 averaging / exponential-sum control；
- E2：sieve 路线受 parity phenomenon 限制，无法把 almost-prime 收紧成 prime；
- E3：exceptional-set 结果无法从 density control 推到 zero exceptions；
- E4：当前问题表示可能没有暴露足够可控的新结构。

这与太阳中微子中的“误差受什么机制限制”完全同构。

## 5.4 EXPLORE：不是“多想几个技巧”，而是攻击 proof bottleneck

例如：

- 若 E1 成立：寻找 qualitatively stronger prime-correlation / exponential-sum control；
- 若 E2 成立：寻找独立的 parity-sensitive information，与 sieve 组合；
- 若 E3 成立：寻找从 averaged result 到 pointwise positivity 的新桥梁；
- 若 E4 成立：改变 representation，寻找新的中间对象或 conditional lemma。

于是“idea generation”发生在已经建模的 proof landscape 上。

## 5.5 DEEPEN：把 research direction 压缩成可验证 lemma

一个高价值数学 research move 常常不是“直接证明 Goldbach”，而是：

> 如果我能证明 Lemma L，Goldbach 是否真的随之成立？

先把：

```text
Lemma L
   ↓
existing machinery
   ↓
Goldbach
```

闭合。

然后真正的问题缩小为：

> 如何证明 L？

这就是数学研究中的 discriminating / diagnostic experiment。

如果一个新 weighted sieve 根本没有突破 parity bottleneck，那么它即使在数值上很好看，也不是决定性 research direction。

---

# 6. 两个案例共同揭示的结构

| 认知层 | 太阳中微子流强 | 哥德巴赫 |
|---|---|---|
| 原始要求 | 测 flux、压误差 | 证明猜想 |
| UNDERSTAND | 定义测什么 flux / precision | 把命题写成 positivity proof obligation |
| MODEL | signal+nuisance+response inference model | proof landscape / representation count |
| EXPLAIN | 为什么 uncertainty 大 | 为什么现有 proof route 卡住 |
| EXPLORE | exposure / calibration / new observable | stronger estimate / parity-sensitive info / new representation |
| NARROW | 哪个 limitation 真正主导 | 哪个 proof bottleneck 是关键 |
| DEEPEN | 定量验证信息增益和 systematic | conditional lemma / toy theorem / bound |
| TEST | 数据、simulation、fit | proof、counterexample、conditional derivation |

因此，一套通用科研方法不需要规定“必须画 dataflow”“必须有 causal graph”“必须有 profiler”。

真正稳定的是：

> **先构造有生产力的问题表示与 Working Model，再解释当前 gap，最后才构造 intervention / proof / experiment space。**

“宏观”也因此有了更严格定义：

> **宏观理解不是 overview 很长，而是能够说明目标结果由哪些关系和机制产生、真正限制目标的关键结构是什么，以及局部行动如何通过这个模型最终影响目标。**

---

# 7. 从方法论推导 SimpleLoop Proposer vNext

## 7.1 设计边界

当前版本不直接升级成跨 round autonomous Researcher。

暂时保留现有外环：

```text
Round
  │
  ▼
Proposer Scientist
  │
  ├── proposal 1
  ├── proposal 2
  └── ...
  │
  ▼
Candidate Workers
  │
  ▼
Gate / Eval
  │
  ▼
Experiment Ledger / Findings
  │
  ▼
next Round
```

当前升级只验证一个核心命题：

> **单个 round 内，如果 Proposer 被迫先完成独立的问题表示、Working Model 与 explanation-space construction，再由宽到窄进行研究，它是否能稳定发现更高层、更机制化、更跨组件的 proposal？**

若这一点有效，再考虑下一阶段让 Scientist 跨 round 掌控 research agenda 和 Loop。

## 7.2 取消“生成侧 / 认知侧”角色分裂

下一版采用一个 lane-local **Proposer Scientist**。

原结构：

```text
Generator Agent
      ↓
Cognitive Agent
```

改为：

```text
One Proposer Scientist

UNDERSTAND
   ↓
MODEL
   ↓
EXPLAIN
   ↓
EXPLORE
   ↓
[HISTORY INJECTION]
   ↓
NARROW
   ↓
DEEPEN
   ↓
PROPOSE
```

生成与批判仍然存在，但不再是两个身份：

- 前半段重点是构造 representation、explanation 和可能性空间；
- 后半段重点是利用历史和深度证据收敛、证伪和实验化。

这保留了当前设计最有价值的 **宽→窄、浅→深**，同时让所有阶段属于同一个研究主体。

---

# 8. 各阶段的认知目标与注意力

## 8.1 UNDERSTAND — 从 task 进入 problem

**核心问题**：

> “我究竟面对什么问题？目标结果是怎样被定义和产生的？”

关注：

- research goal；
- observables / objective；
- system/problem boundary；
- hard constraints；
- major entities / quantities / processes；
- 当前真正未知的是什么。

禁止 attention 过早锁定到：

- 某个文件；
- 某个局部 theorem；
- 某个 implementation idea；
- 第一个 plausible solution。

UNDERSTAND 的本质是 Dewey 意义上的 **suspended judgment**。

## 8.2 MODEL — 构造可推理的 Working Model

**核心问题**：

> “如果我要解释和预测这个任务，我应该怎样表示它？”

Working Model 应当表达：

```yaml
working_model:
  representation:
    # 当前把问题看成什么

  explanatory_structure:
    # 对象、过程、关系、层级、依赖、约束

  important_unknowns:
    # 哪些未知会改变后续判断

  evidence:
    # 主要 claim 来自哪里
```

不要求所有领域使用同一内部 schema。

代码任务可以是 process / state / cost model；实验物理可以是 physical / detector / inference model；数学可以是 proof landscape。

**Model 完成标准**不是“字段填满”，而是：

1. 能解释目标结果如何产生；
2. 能支持 counterfactual；
3. 能指出 decision-changing unknown。

## 8.3 EXPLAIN — 建立竞争机制解释

**核心问题**：

> “为什么当前现象/性能/误差/proof gap 会是现在这样？”

输出少量 competing explanations：

```yaml
explanation:
  id:
  phenomenon:
  claim:
  model_basis:
  expected_if_true:
  evidence_needed:
  alternatives:
```

此阶段仍然不直接设计 intervention。

EXPLAIN 的意义是把 diagnosis 与 treatment 分开。

## 8.4 EXPLORE — 在模型与解释上扩展可能性空间

**核心问题**：

> “如果这些 model/explanations 暂时成立，有哪些 materially different mechanism families 能改变结果？”

这里才使用：

- 5/9 generative basis；
- mechanism-level lever map；
- analogy；
- inversion；
- decomposition；
- scale analysis；
- anomaly；
- algorithmic alternatives。

输出的是 **Hypothesis Portfolio / Research Directions**，不是 implementation-ready proposal。

## 8.5 NARROW — 首次使用历史进行证据化收敛

在 Fresh Explore 完成后首次注入：

- Experiment Ledger；
- Finding Archive；
- Frontier；
- previous proposals / failed families；
- previous implementation evidence。

当前问题变成：

> “哪些独立形成的 explanations/directions 在已有证据下仍值得实验成本？”

历史在这里是 **evidence**，不是最初的问题表示 prior。

可能结果：

- `SURVIVE` → DEEPEN；
- `DIRECTION_REFUTED` → 继续 evidence-aware EXPLORE；
- `EXPLANATION_CONFLICT` → reopen EXPLAIN；
- `MODEL_CONFLICT` → reopen MODEL；
- `FRAME_ANCHORED` → fresh_reframe。

## 8.6 DEEPEN — 让 macro hypothesis 决定 micro investigation

**核心问题**：

> “这个 selected hypothesis 的关键前提真的成立吗？如果成立，实验应该出现什么？”

此时才允许大量微观调查：

- exact files/functions；
- exact formulas；
- specific profiler traces；
- specific literature；
- call-site / data structure；
- exact proof lemma；
- exact dataset analysis。

顺序必须是：

```text
macro hypothesis
      ↓
critical premise
      ↓
targeted investigation
      ↓
evidence
      ↓
prediction
      ↓
intervention
```

而不是：

```text
micro inspection
      ↓
local idea
```

## 8.7 PROPOSE — Proposal 是研究推理链的末端

最终 proposal 应保持紧凑，因为上游研究状态已经存在：

```yaml
proposal:
  derived_from:
    model_claims: [...]
    explanations: [...]
    hypothesis: ...
    evidence: [...]

  intervention:
  mechanism:
  affected_scope:
  prediction:
  constraints:
  verification:
```

Candidate Worker 得到的是 **experiment specification**，而不是一个未经解释的优化 idea。

---

# 9. 历史何时注入：信息访问策略

## 9.1 区分 world evidence 和 search-trajectory evidence

### World evidence：从一开始可见

- task / goal；
- 当前 accepted source/artifact；
- hard constraints / gate；
- baseline measurements；
- domain knowledge；
- raw data；
- profiler；
- literature；
- repo structure / call graph；
- 当前 observable。

这些属于“世界现在是什么样”的证据。

### Search-trajectory evidence：延迟注入

- previous proposals；
- past candidate results；
- Experiment Ledger；
- Findings；
- Frontier；
- failed families；
- previous interpretations。

这些属于“过去我们怎样搜索”的轨迹，容易造成 anchoring。

## 9.2 Visibility policy

| Phase | Current artifact / raw evidence | Generative basis | Search history |
|---|---:|---:|---:|
| UNDERSTAND | ✓ | × | × |
| MODEL | ✓ | × | × |
| EXPLAIN | ✓ | 可选 | × |
| FRESH EXPLORE | ✓ | ✓ | × |
| NARROW | ✓ | ✓ | ✓ |
| DEEPEN | ✓ | 按需 | ✓ |
| PROPOSE | ✓ | 按需 | ✓ |

## 9.3 历史可见性必须单调

同一个模型 context 内：

```text
history_visibility: 0 → 1
```

合法。

```text
history_visibility: 1 → 0
```

不合法。

一旦 Scientist 在 NARROW 读过历史，它就不可能通过 prompt “忘记历史”。

因此，历史后回 EXPLORE 的语义是：

> **EVIDENCE-AWARE EXPLORE**

而不再是 Fresh Explore。

真正需要重新获得 history-independent breadth 时，只能：

> **fresh_reframe → 新 context**

---

# 10. 不再使用统一 regenerate：根据被推翻的认知层回退

旧的：

```text
Cognitive
   ↓
feedback_generator
   ↓
Generator regenerate
```

在新架构下不再足够精确。

新 evidence 应首先回答：

> **它推翻的是哪一层认知？**

### Direction 被否定

```text
NARROW / DEEPEN
      ↓
continue_explore
```

保持 Model / Explanation，不再 history-blind。

### Explanation 被否定

```text
NARROW / DEEPEN
      ↓
reopen_explain
```

例如历史已经证明 removing repeated preparation 几乎不影响 SPEED_MS，那么“repeated preparation 是主要瓶颈”的 explanation 应被修订。

### Working Model 被否定

```text
NARROW / DEEPEN
      ↓
reopen_model
```

不是继续生成附近 proposal。

### Frame / representation 被严重锚定

```text
fresh_reframe
      ↓
new clean context
      ↓
UNDERSTAND → MODEL → EXPLAIN → FRESH EXPLORE
```

fresh_reframe 只传：

- goal；
- current artifact；
- hard constraints；
- raw domain facts / current-world evidence。

不传：

- old WorkingModel；
- old explanations；
- previous directions；
- experiment search trajectory。

这是真正意义上的去锚定。

---

# 11. 如何避免 Artifact 变成“填词游戏”

只要求交付 `WorkingModel.yaml` 没有意义。LLM 很可能先产生 proposal，再倒填模型。

因此 Artifact 必须成为后续推理的真实 dependency。

## 11.1 Evidence linkage

主要 model claim 必须引用实际 evidence：

```text
M2:
state X is produced outside optimizer loop
and consumed repeatedly inside charge/time evaluation.

evidence:
source:A
source:B
profile:C
```

## 11.2 Downstream lineage

Hypothesis 必须引用 model/explanation：

```text
M2 + E3
    ↓
H7
    ↓
deep evidence
    ↓
P3
```

没有上游 lineage 的 proposal 不能提交。

## 11.3 Counterfactual usefulness check

离开 MODEL 前，要求当前模型承担一次真实推理：

> 如果模型中的关键机制发生改变，目标应怎样变化？

回答不了，说明只是 summary。

## 11.4 Decision-changing unknown check

模型提交前必须二选一：

```text
continue_investigation(
  question,
  why_answer_could_change_model_or_search
)
```

或者：

```text
commit_working_model(
  why_remaining_unknowns_do_not_block_broad_exploration
)
```

Harness 不判断答案本身是否“聪明”，但迫使模型把调查停止理由外显。

## 11.5 Proposal lineage

最终 proposal 需要能被机械追溯到：

```text
Working Model
     ↓
Explanation
     ↓
Hypothesis
     ↓
Evidence
     ↓
Intervention
     ↓
Prediction
```

这样 artifact 才不是装饰。

---

# 12. Harness：限制注意力，不替代思考

设计原则：

> **Phase 定义当前认知任务；Action 定义当前允许做的外部行为和 commitment；Scientist 自己决定如何研究。**

不要把科研方法写成：

```text
Step 1 call A
Step 2 call B
Step 3 call C
```

这会把 Scientist 变成 workflow executor。

更合适的是：

```text
UNDERSTAND
  ├ research
  ├ research
  ├ inspect
  ├ realize gap
  ├ research
  └ commit understanding

MODEL
  ├ construct model
  ├ test counterfactual
  ├ discover unknown
  ├ research
  └ commit model
```

`max_steps` 只是保险丝。

如果预算耗尽而认知 artifact 不合格，应允许：

> partial / abstain

而不是强制交 proposal。

---

# 13. Action 设计

保持 action 数量少，只把外部研究和认知 commitment 结构化。

## 13.1 通用研究

```text
run_research_command
```

允许根据领域接入：

- source inspection；
- literature；
- profiler；
- data analysis；
- simulation；
- theorem/proof tooling；
- specialized subagent。

## 13.2 UNDERSTAND / MODEL

```text
propose_working_model
continue_investigation
commit_working_model
```

`propose_working_model` 不等于 phase transition。

Scientist 可以：

```text
propose → check → discover gap → investigate → revise → commit
```

## 13.3 EXPLAIN

```text
submit_explanation
commit_explanation_set
```

必须允许 competing explanations。

## 13.4 EXPLORE

```text
emit_lever_map
submit_hypothesis
commit_hypothesis_portfolio
```

5/9 generator basis 保留，但作用对象改成：

> Working Model + Explanation Set

而不是原始 source locality。

## 13.5 NARROW

首次开放：

```text
search_experiments
inspect_episode
search_findings
```

以及：

```text
select_for_deepen
continue_explore
reopen_explain
reopen_model
fresh_reframe
```

## 13.6 DEEPEN

```text
run_research_command
search_*
submit_proposals
return_to_narrow
continue_explore
reopen_explain
reopen_model
fresh_reframe
abandon_direction
```

旧的 `feedback_generator` 可以被上述认知层级回退替代。

---

# 14. Prompt 设计：身份内化、Attention Policy 与 Harness 分工

## 14.1 Identity Prompt 不写 checklist

Prompt 的核心不是告诉 Scientist “先 1 再 2 再 3”，而是内化什么叫专业研究。

参考风格：

> **You are the scientist responsible for deciding which interventions are worth an experiment.**  
> A plausible local opportunity is evidence about the problem, not yet the problem definition. Treat observations as clues about a larger structure or mechanism until you understand how the target outcome is produced.  
>   
> Build working models that support explanation, counterfactual reasoning, and prediction—not summaries of available facts. Resist premature commitment. Before spending detailed effort on one direction, understand the problem broadly enough to keep materially different explanations alive.  
>   
> Your models are provisional. When evidence contradicts a direction, repair the level of understanding that failed rather than defending the proposal. A proposal is justified only when it follows from a mechanism you understand and makes a prediction that an experiment can test.

这里内化的是：

- suspended judgment；
- model-based reasoning；
- competing explanations；
- shallow→deep；
- broad→narrow；
- evidence-driven revision。

不是 API manual。

## 14.2 Phase Prompt 只控制当前 attention

### UNDERSTAND

> **Current mode: UNDERSTAND.**  
> Do not search for modifications yet. Investigate the problem broadly enough to understand what is being changed or explained, how the target outcome arises, and which unknowns could change your later model.

### MODEL

> **Current mode: MODEL.**  
> Construct a working representation that can explain the target outcome and support counterfactual reasoning. A list of components or facts is not sufficient.

### EXPLAIN

> **Current mode: EXPLAIN.**  
> Explain why the current gap or phenomenon occurs. Keep materially different explanations alive until evidence can distinguish them. Do not design the intervention yet.

### EXPLORE

> **Current mode: EXPLORE.**  
> Using the working model and competing explanations, search broadly across different mechanism families before investing deeply in any one direction.

### NARROW

> **Current mode: NARROW.**  
> Past experiments are now available as evidence. Use them to support, refute, or revise the independently formed model and hypotheses. Historical vocabulary must not replace your own problem representation.

### DEEPEN

> **Current mode: DEEPEN.**  
> Detailed investigation is now justified. Test the selected hypothesis's critical premises, trace its full impact, derive observable consequences, and submit only if the mechanism survives.

## 14.3 三者职责

```text
Identity Prompt:
  你是什么样的研究者

Phase Attention:
  你现在应该关注什么

Harness:
  你现在能看什么、能 commit 什么
```

三者必须分开。

---

# 15. OMILREC 任务中的具体实例化

下一版 Scientist 面对 OMILREC 不应先问：

> 哪个函数能优化？

而应经过以下结构。

## 15.1 UNDERSTAND / MODEL

建议第一轮 Working Model 至少自然覆盖三个视图，但不需要三个独立 schema。

### Process Model

```text
event
  ↓
event preparation
  ↓
reconstruction / optimizer
  ↓
repeated FCN evaluations
  ├ charge likelihood
  └ time likelihood
  ↓
fit result
```

### State / Dependency Model

```text
run-level
event-level
fit-level
FCN-call-level
hit-level
```

追踪：

- state 在哪里产生；
- 谁消费；
- 真正生命周期；
- 是否跨 charge/time 共享；
- 是否被重复 transform / fetch。

### Cost Model

```text
SPEED_MS
≈ outside_FCN
 + N_FCN × cost_per_FCN

cost_per_FCN
≈ required arithmetic
 + repeated preparation
 + memory/data access
 + state construction
```

这比 profiler “某函数 55%”多了一层 mechanism model。

## 15.2 EXPLAIN

形成 competing bottleneck explanations：

- arithmetic is intrinsically dominant；
- event-invariant work is coupled to FCN-call lifetime；
- duplicated charge/time state preparation；
- data ownership/layout drives memory cost；
- FCN call topology dominates total runtime。

## 15.3 EXPLORE

生成元基在 mechanism space 上工作。

例如：

- **Decomposition**：FCN 是否承担了不属于 FCN-call lifecycle 的职责？
- **Inversion**：consumer pull state 是否可以改成 producer-owned shared view？
- **Scale analysis**：哪些工作现在是 `O(N_FCN × N_hits)`，理论上只需 `O(N_event × N_hits)`？
- **Cross-domain analogy**：是否存在 event-context / query-plan / staged-computation 同构？
- **Cost attack**：最大成本项由哪个上层设计决定，而不只是在哪个函数执行？

由此得到：

- EventContext centralization；
- state ownership restructuring；
- cross-charge/time shared immutable views；
- FCN call-count reduction；
- data-oriented likelihood state；
- algorithmic decomposition change；

等 mechanism-level directions。

## 15.4 NARROW

此时第一次看过去 OMILREC 实验：

- 某 caching 失败是否证伪整个 lifetime hypothesis？
- 还是仅仅某个局部 cache implementation 不够？
- 某 layout change 没收益是否推翻 memory-cost explanation？
- 是否有历史 evidence 表明 call topology 才是更强解释？

历史改变的是 belief，不是原始 representation。

## 15.5 DEEPEN

选中 `event-level shared likelihood context` 后，才具体调查：

- 哪些 event state 在 optimizer loop 外产生？
- charge/time 谁读取？
- 哪些 transform 重复？
- 哪些 FCN arithmetic 路径不能重排？
- 需要哪些 interface / ownership 变化？
- 预计消除多少 invocation-level work？

最终自然形成跨文件 proposal，而不是因为 Harness 要求“多改几个文件”。

---

# 16. 多 lane 的保留方式

当前多 lane 仍然保留。

每个 lane 独立执行：

```text
UNDERSTAND
→ MODEL
→ EXPLAIN
→ FRESH EXPLORE
→ history
→ NARROW
→ DEEPEN
```

第一版不强制每 lane 使用不同 representation。

如果不同 lane 自然形成：

- state-lifetime representation；
- optimizer topology representation；
- dataflow / ownership representation；

这本身就是有价值的宏观多样性。

如果实验表明多个 lane 仍高度同质化，再单独考虑 representation diversification；不在本次重构中提前增加额外 Harness。

---

# 17. 一次性重构计划

这不是跨多个产品阶段的长期 roadmap，而是下一版 Proposer 的一次性设计范围。

## Work Package A — 单 Scientist Runtime

- 删除 production path 中 GeneratorAgent → CognitiveAgent 的角色边界；
- 每 lane 使用一个 Proposer Scientist；
- state 至少包含：
  - phase；
  - WorkingModel；
  - ExplanationSet；
  - LeverMap；
  - HypothesisPortfolio；
  - selected hypotheses；
  - evidence links；
  - history visibility；
  - phase transitions。

## Work Package B — Information Views

实现两类 context：

### Fresh Inquiry View

包含：

- goal；
- current accepted artifact；
- gate / hard constraints；
- raw current-world evidence；
- domain/source research tools。

不含：

- Experiment Ledger；
- Findings；
- Frontier；
- previous proposals。

### History-Aware View

在前者基础上新增历史。

确保同一 context visibility 只单调增加。

## Work Package C — Working Model / Explanation Artifacts

实现：

- propose/revise/commit WorkingModel；
- evidence linkage；
- counterfactual check；
- decision-changing-unknown check；
- ExplanationSet；
- competing explanation support。

## Work Package D — Explore / Narrow / Deepen

- 把现有 5/9 generator basis 接到 WorkingModel + ExplanationSet；
- LeverMap 升级成 mechanism-level map；
- history injection 移到 portfolio commitment 后；
- 删除统一 feedback_generator；
- 增加层级回退 action；
- DEEPEN 只针对 selected hypotheses。

## Work Package E — Prompt

- 一个 Scientist identity prompt；
- 每 phase 一个短 attention block；
- action schema 与 identity prompt 分离；
- 不把科研方法写成 checklist。

## Work Package F — Trace / Telemetry

记录但不把它们写进 authoritative Experiment Ledger：

```json
{
  "phase_transitions": [],
  "history_injected_at": "...",
  "working_model_claims": [],
  "explanation_ids": [],
  "hypothesis_lineage": [],
  "reopen_counts": {
    "explore": 0,
    "explain": 0,
    "model": 0,
    "fresh_reframe": 0
  }
}
```

用于后续验证认知流程是否真实发生。

---

# 18. 实证评估：如何判断这不是“更会写科研作文”

必须做与当前 Proposer 的严格 A/B。

## 18.1 控制变量

固定：

- 同一 base model；
- 同一 OMILREC baseline；
- 同一 candidate 数；
- 同一 Worker / Gate；
- 尽量接近的总 token / tool budget；
- 同样的 run length。

比较：

### A — Current

```text
survey
→ lever map
→ generator basis
→ hypothesis
→ cognitive enrich
```

### B — Scientist-Proposer vNext

```text
UNDERSTAND
→ MODEL
→ EXPLAIN
→ EXPLORE
→ history
→ NARROW
→ DEEPEN
→ proposal
```

## 18.2 不只看 SPEED_MS

### 1. Problem-model accuracy

Working Model 中主要 architecture/process/state/cost claims 是否被源码与 profiling 支持。

### 2. Explanatory depth

proposal 是否只说“这里贵”，还是明确：

```text
observation
→ mechanism explanation
→ intervention
→ prediction
```

### 3. Proposal abstraction level

建议采用语义 rubric，而不是“改几个文件”：

- **L0**：expression / loop / tiny local edit；
- **L1**：function-level mechanism；
- **L2**：跨组件 ownership/interface/lifecycle/dataflow change；
- **L3**：重组主要 computation / architecture / algorithmic decomposition。

目标不是强制 L2/L3，而是比较 vNext 是否在没有“必须跨文件”的提示下自然提升 L2/L3 discovery rate。

### 4. Mechanism diversity

比较的是不同 causal/architectural mechanism，而不是文本 Jaccard。

### 5. Proposal lineage quality

能否追溯：

```text
model
→ explanation
→ hypothesis
→ evidence
→ intervention
→ prediction
```

### 6. Experimental yield

- Gate PASS rate；
- measurable improvement rate；
- significant improvement rate；
- best-of-N SPEED_MS；
- accepted improvement。

### 7. Search efficiency

- tool calls；
- tokens；
- wall time；
- 每个成功 proposal 的 research cost。

## 18.3 Retrospective discovery test

OMILREC 特别适合做一个强验证：

- 给 Scientist 较早版本源码；
- 不给它未来版本 diff；
- 把后来人类真正做过的结构性优化 family 当成 hidden retrospective references；
- 看它是否能独立发现相同或等价的 mechanism。

这比让另一个 LLM 打“架构思维 8/10”更有实证意义。

---

# 19. 风险与反模式

## 19.1 WorkingModel 变成长篇 repository summary

症状：

> file A 做 X、file B 做 Y……

修复：

- counterfactual test；
- explanation dependency；
- 要求说明 target outcome 如何由模型产生。

## 19.2 先想 proposal 再倒填 explanation

修复：

- proposal action 在前期不可用；
- lineage 强制；
- Explanation 中不能包含具体 intervention commitment。

## 19.3 EXPLAIN 变成“只有一个喜欢的原因”

修复：

- 在不确定问题上保留 competing explanations；
- 要求每个 explanation 给出 `expected_if_true` 与 discriminating evidence。

## 19.4 history injection 太晚导致重复浪费

这是刻意 tradeoff。

Fresh Inquiry 的价值是获得 independent representation；历史后 NARROW 会快速淘汰重复方向。

如果成本过大，可以以后让 **raw world facts** 跨 lane 共享，但不共享 search trajectory interpretation。

## 19.5 history-aware 回退假装 history-blind

禁止。

同一 context 一旦读历史，后续 EXPLORE / EXPLAIN / MODEL 都明确是 evidence-aware revision。

只有 fresh_reframe 可以恢复 clean context。

## 19.6 Harness 逐渐替 Scientist 思考

危险信号：

- 强制读 N 个文件；
- 强制执行固定工具序列；
- Harness 判断具体 causal explanation；
- 自动把“跨文件”当 architecture；
- 因 step budget 到点强制 proposal。

Harness 应只控制：

- information access；
- action availability；
- commitment gates；
- minimum lineage。

---

# 20. 最终设计原则

本研究最终不是得到了一套“更复杂的 Proposer workflow”，而是得到几条更根本的原则。

### 原则 1：问题模型先于方案空间

**不要优化你尚未正确表示的问题。**

### 原则 2：解释先于干预

**先问为什么，再问改什么。**

### 原则 3：宽度应发生在机制层，而不是文本 idea 层

**不同 wording 不是不同 research direction。**

### 原则 4：深度调查由 macro hypothesis 驱动

**不是看得越细越科学，而是细节必须服务于一个已经形成的研究问题。**

### 原则 5：历史是 evidence，不是第一次问题表示的生成器

**先独立构造世界模型，再让历史挑战它。**

### 原则 6：新证据应修复被推翻的认知层

**direction 错就换 direction；explanation 错就改 explanation；model 错就改 model；frame 错才 reframe。**

### 原则 7：调查停止由 decision-changing uncertainty 决定

**不是知道够多，而是剩余未知是否还会改变下一步决定。**

### 原则 8：Harness 的作用是让 shallow shortcut 变难，而不是替模型完成研究

**Identity 教科研方式，Phase 控注意力，Harness 控信息与 commitment。**

---

# 结论

SimpleLoop 最初的问题可以表述为：

> 如何让 Proposer 产生更多、更好的 proposal？

随着生成元基、多 lane、生成/认知双侧、研究历史逐渐完善，这个问题最终暴露出更深的层次：

> **一个研究者是怎样从粗糙问题构造出值得搜索的问题空间的？**

Dewey 告诉我们为什么必须悬置第一个 solution；Simon 解释了 ill-structured problem 为什么需要 problem representation；Schön说明 representation 必须在证据冲突时重新 framing；Peirce提供 explanation → prediction → test 的科学推理骨架；Nersessian说明 Working Model 必须成为真正的推理对象；Value of Information 则给出了调查停止的 decision-theoretic 直觉。

太阳中微子与哥德巴赫两个案例说明，这些原则并不依赖“代码优化”这一领域：

- 一个是测量与统计推断问题；
- 一个是纯数学证明问题；
- 二者都需要先从 raw requirement 构造 representation 和 Working Model；
- 都需要解释当前 gap；
- 都需要在多个 competing possibilities 中由宽到窄；
- 都需要让下一步研究动作具有区分力，而不是直接产生 solution。

因此，SimpleLoop 下一版最合适的升级不是立即构造一个复杂的跨 round autonomous Researcher，而是先把当前单轮 Proposer 改造成真正遵循 inquiry 方法的 **Scientist-Proposer**：

```text
UNDERSTAND
   ↓
MODEL
   ↓
EXPLAIN
   ↓
FRESH EXPLORE
   ↓
──── HISTORY INJECTION ────
   ↓
NARROW
   ↓
DEEPEN
   ↓
PROPOSE
   ↓
Candidate Worker
```

Worker、Gate、Experiment Ledger 和多 lane 并行继续存在。

这使下一次实验真正检验一个清楚的科学假设：

> **显式构造 whole-problem representation、Working Model 和 competing explanations，能否让同一个基础模型在 OMILREC 中从局部 hotspot optimization，自发跃迁到机制级、跨组件和 architecture-level intervention？**

如果答案是肯定的，那么 SimpleLoop 才有了把 Proposer 进一步升级为跨 round persistent Researcher 的方法论基础。

如果答案是否定的，我们也获得了非常有价值的结论：问题可能不再主要是 Harness 没给正确的认知结构，而是当前模型本身在 scientific problem construction / model-based reasoning 上存在能力上限。

无论结果怎样，这都比继续为 idea generator 叠加更多 prompt 或更多 generator operator 更接近真正的问题。

---

# 参考资料

1. John Dewey, *How We Think* (1910), Chapter VI.  
   [Brock University / Mead Project](https://brocku.ca/MeadProject/Dewey/Dewey_1910a/Dewey_1910_f.html)

2. Herbert A. Simon, “The Structure of Ill Structured Problems,” *Artificial Intelligence* 4 (1973).  
   [ScienceDirect](https://www.sciencedirect.com/science/article/pii/0004370273900118)

3. Donald A. Schön, *The Reflective Practitioner: How Professionals Think in Action* (1983).  
   [Open University hosted PDF](https://studenthublive.open.ac.uk/sites/studenthublive.open.ac.uk/files/reflective%20practitioner%20-%20schon.pdf)

4. Charles S. Peirce scientific inquiry overview: abduction, deduction, induction.  
   [Stanford Encyclopedia of Philosophy](https://plato.stanford.edu/entries/peirce/)

5. Nancy J. Nersessian, “Model-Based Reasoning in Conceptual Change” (1999).  
   [Georgia Tech PDF](https://sites.cc.gatech.edu/aimosaic/faculty/nersessian/papers/model-based-reasoning-in-conceptual-change.pdf)

6. Fenwick et al., “Value of Information Analysis for Research Decisions,” *Value in Health* (2020).  
   [Value in Health](https://www.valueinhealthjournal.com/article/S1098-3015%2820%2930027-9/fulltext)

7. Anthropic, Claude Code permissions / Plan mode documentation.  
   [Claude Code Docs](https://docs.anthropic.com/en/docs/claude-code/permissions)

8. OpenAI, Codex Best Practices — Plan first for difficult tasks.  
   [OpenAI Developer Docs](https://developers.openai.com/codex/learn/best-practices)

9. Gottweis et al., “Towards an AI co-scientist” (2025).  
   [arXiv:2502.18864](https://arxiv.org/abs/2502.18864)

10. Yamada et al., “The AI Scientist-v2: Workshop-Level Automated Scientific Discovery via Agentic Tree Search” (2025).  
    [arXiv:2504.08066](https://arxiv.org/abs/2504.08066)

11. Borexino Collaboration, “Simultaneous Precision Spectroscopy of pp, 7Be, and pep Solar Neutrinos with Borexino Phase-II.”  
    [arXiv:1707.09279](https://arxiv.org/abs/1707.09279)

12. Borexino Collaboration, “Final results of Borexino on CNO solar neutrinos.”  
    [arXiv:2307.14636](https://arxiv.org/abs/2307.14636)

13. H. A. Helfgott, “Major arcs for Goldbach's problem.”  
    [arXiv:1305.2897](https://arxiv.org/abs/1305.2897)

14. H. A. Helfgott, “Minor arcs for Goldbach's problem.”  
    [arXiv:1205.5252](https://arxiv.org/abs/1205.5252)

15. D. R. Heath-Brown, *Lectures on Sieve Methods* (parity phenomenon and sieve limitations).  
    Related lecture literature and modern Chen-theorem discussions provide the number-theoretic background for the proof-landscape example.

---

## 版本说明

本文刻意不设计跨-round Scientist agenda、Research Tree 自主管理、diagnostic-experiment reward 或完整 autonomous laboratory。它只定义下一版单轮 Proposer 的研究认知骨架。

这是一个有意的边界：先证明 **科学问题建模能力** 本身能够提升 proposal 质量，再让 Scientist 接管更长时间尺度的研究循环。
