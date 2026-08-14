# SimpleLoop Scientist Continuity and Reflection Design

**Status:** Design proposal  
**Scope:** Proposer / Scientist cognition semantics  
**Out of scope:** implementation steps, code-change instructions, migration plan, testing plan, engineering task decomposition

---

## 1. Problem

SimpleLoop 的 Scientist 面对的是长期、开放、由真实实验反馈驱动的研究问题。

在这样的任务中，单轮提出一个合理 proposal 并不够。真正决定长期研究能力的是：

> **跨越多个实验以后，Scientist 是否仍然能够延续自己的科学判断，并且让新的证据真正改变这些判断。**

当前最核心的矛盾有两个。

### 1.1 如果每个 round 都是一个新的 Scientist

新的 Scientist 可以读取历史，但它面对的是：

> “过去有人做过这些实验。”

而不是：

> “昨天的我是基于什么判断提出这个实验的，这个结果回来以后，我原来的判断应该怎样变化。”

这会造成认知断裂。

Experiment History 可以保存事实，但仅有事实不能保存研究推理中的语义关系：

- 为什么提出这个 hypothesis；
- 为什么这个 experiment 值得做；
- 实验前预期什么结果；
- 什么结果会支持或削弱原判断；
- 当前哪些问题仍未被解释。

因此，单纯让每轮重新读取历史，并不能形成真正的长期 Scientist。

### 1.2 如果 Scientist 过度连续

另一端同样有问题。

如果 Scientist 简单继承上一轮的计划、方向或 unfinished intention，它会自然形成 continuation inertia：

```text
早期实验显著成功
→ 强化对某方向的判断
→ 后续收益持续下降
→ 仍不断解释为“还没有做彻底”
→ 继续投入同一局部方向
```

这种失败并不是缺少历史。

恰恰相反，它可能是因为同一个 Scientist 太连续，以至于不断用旧的 worldview 解释新的 evidence。

因此，SimpleLoop 需要同时具备：

1. **跨 round 的认知连续性；**
2. **周期性打断这种连续性的批判性 Reflection。**

---

## 2. Core Mental Model

SimpleLoop 的 Scientist 可以被理解为：

> **一个极其健忘，但非常聪明的长期研究者。**

每次 `proposer_run` 可以拥有新的模型上下文。

它会忘记昨天具体的 shell 输出、代码阅读过程、长对话、临时分析和大量 working context。

但它不能变成一个完全陌生的人。

系统需要帮助它恢复：

> “昨天的我是如何理解这个问题的；我为什么提出那个实验；我原本认为不同实验结果意味着什么；现在现实给了什么反馈。”

因此，Scientist continuity 的目标不是：

> 保留完整 conversation。

也不是：

> 保留上一轮计划。

而是：

> **让一个健忘的 Scientist 恢复自己的 epistemic state。**

核心原则是：

> **Forget the episode; preserve the epistemic state.**

---

## 3. Scientist Is Continuous Across Rounds

Round 是 Host 的运行与记录单位。

它不应该成为 Scientist 的 cognitive boundary。

从 Scientist 的角度，研究过程应该被理解为：

```text
Scientist
│
├── form judgment
├── commission experiment
├── receive evidence
├── revise belief
├── investigate
├── commission another experiment
├── receive evidence
├── reflect
└── continue research
```

而不是：

```text
Round 1 → Scientist A
Round 2 → Scientist B
Round 3 → Scientist C
```

因此：

> **每一次 proposer_run 都是同一个 Scientist 再次回到自己的研究中。**

模型上下文可以重新开始，但 Scientist identity 与研究认知在语义上是连续的。

---

## 4. What Continuity Preserves

Scientist continuity 应保留的是 **epistemic continuity**。

也就是：

> **“我目前知道什么、相信什么、为什么相信，以及现实如何改变了这些判断。”**

应当连续的内容包括：

### 4.1 Problem understanding

Scientist 当前如何理解 Goal 与 Current World。

哪些结构、机制或约束被认为与问题有关。

### 4.2 Beliefs and hypotheses

Scientist 当前持有哪些 scientific judgments。

这些 judgment 必须被明确表示为 belief / hypothesis，而不是事实。

例如：

```text
Hypothesis:
Remaining lookup overhead may still be an important runtime bottleneck.
```

而不是：

```text
Fact:
Lookup is the dominant bottleneck.
```

### 4.3 Basis of belief

为什么形成这一判断。

包括：

- 已观察到的 evidence；
- 当前 world 中的事实；
- 已有实验结果；
- 仍然存在的不确定性。

### 4.4 Experiment intent

上一轮提交的实验到底在检验什么。

重要的不是：

> “我改了哪个文件。”

而是：

> “这个 intervention 是为了区分什么 scientific explanation。”

### 4.5 Prior expectation

实验开始之前，Scientist 对结果有怎样的预期。

例如：

> 如果 hypothesis A 是主要解释，那么 experiment E 应产生 material improvement。

以及：

> 如果结果接近 neutral，则 A 应被明显削弱。

这使实验结果能够真正作用于 prior judgment。

### 4.6 Returned evidence and epistemic consequence

实验实际上发生了什么。

以及这个 evidence 对已有 belief 的意义是什么。

### 4.7 Unresolved questions

现在仍然有哪些关键问题没有被解释或裁决。

---

## 5. What Continuity Must Not Preserve

Scientist continuity 不应该默认保存 **future action commitments**。

尤其不应把下面这些内容作为下一轮的 continuity anchor：

```text
我下一步想继续做 A
我目前倾向于继续这个方向
下一轮应该进一步优化 X
我已经准备继续完成某个 unfinished plan
```

这些内容会把上一轮的研究判断悄悄变成下一轮的行动指令。

因此核心原则是：

> **Preserve epistemic commitments, not future action commitments.**

过去的 Scientist 可以把自己的判断交给未来。

但不能把自己的决定交给未来。

---

## 6. Cognitive Handoff

普通 proposer round 之间需要一个 **Cognitive Handoff**。

它不是：

- conversation summary；
- history digest；
- next-step plan；
- TODO list；
- 对下一轮的行动建议。

它的作用是：

> **帮助未来那个健忘的自己恢复上一阶段的 epistemic state。**

一个 Cognitive Handoff 需要回答：

```text
What did I believe?

Why did I believe it?

What question was I trying to answer?

What experiment did I commission to answer it?

Before seeing the result, what did I expect?

What observations would strengthen or weaken the belief?

What actually happened?

What did that evidence do to the belief?

What remains unresolved?
```

例如：

```text
Hypothesis:
Remaining lookup overhead may still be a major bottleneck.

Basis:
Earlier restructuring produced a substantial gain, and profiling still
showed meaningful cost in this path.

Experiment:
Remove the remaining repeated lookup.

Prior expectation:
If this mechanism is still dominant, the experiment should produce a
material improvement. A neutral result would substantially weaken this
explanation.

Observed result:
The experiment produced only a marginal improvement.

Epistemic consequence:
The result weakens the hypothesis that remaining lookup is still a
dominant bottleneck.

Unresolved:
The larger source of runtime remains unexplained.
```

然后结束。

它不应该追加：

```text
Therefore next investigate data layout.
```

因为那属于下一轮 Scientist 看到最新世界以后应当重新形成的判断。

---

## 7. Continuity Does Not Mean Continuation

Scientist continuity 的含义是：

> **今天的我知道昨天的我为什么那么想。**

它不意味着：

> **今天的我应该继续昨天想做的事情。**

下一次 proposer_run 应重新面对：

```text
Goal
+
Current World
+
Accumulated evidence
+
Previous epistemic state
```

然后重新决定：

> **现在什么最值得研究？**

因此正常的认知链条应该是：

```text
previous belief
      ↓
experiment
      ↓
returned evidence
      ↓
new judgment
      ↓
new research decision
```

而不是：

```text
previous direction
      ↓
unfinished plan
      ↓
continue
```

这一区别是整个 continuity 设计的核心。

---

## 8. Why Reflection Is Necessary

Epistemic continuity 能让 Scientist 形成长期研究判断。

但同一个 Scientist 也会逐渐形成自己的认知惯性。

例如：

```text
E1: +10%
→ “A 看起来确实重要。”

E2: +2%
→ “A 可能还有 secondary opportunity。”

E3: +0.4%
→ “可能还没有做彻底。”

E4: neutral
→ “再试一个细节。”

E5: neutral
→ “也许实现方式不对。”
```

每一次局部解释都可能看起来合理。

但整个 trajectory 可能已经显示：

> Scientist 正在不断为早期形成的 hypothesis 找补。

问题不在于它不知道历史。

问题在于它正在使用自己的旧 worldview 解释历史。

因此，Scientist 需要周期性地暂时退出当前 research trajectory。

这就是 Reflection。

---

## 9. Reflection Is a Different Cognitive Mode

Reflection 是 Proposer / Scientist 自身的一种认知机制。

它不是新的 Executor stage。

也不是新的实验类型。

Reflection checkpoint 的语义是：

> **暂时停止推进研究，把最近这个 Scientist 在研究过程中的行为当作审查对象。**

Normal Scientist 的核心问题是：

> **How can I most effectively advance the Goal now?**

Reflection Scientist 的核心问题是：

> **How might the recent version of me have failed to advance the Goal effectively?**

Normal mode 的姿态是：

> **Build. Judge. Act.**

Reflection mode 的姿态是：

> **Doubt. Challenge. Detach.**

---

## 10. Reflection World

Reflection 仍然处于正常的 **Research World**。

它不进入 RSI 的 Self World。

它不需要 self-repo。

Reflection 可以看到：

- Goal；
- Current Work / current repository；
- authoritative Experiment History；
- 最近若干轮 Cognitive Handoffs；
- Scientist 当时的 hypotheses；
- prior expectations；
- experiment outcomes；
- Scientist 对 evidence 的 interpretation；
- previous Reflection，若存在。

Reflection 审查的是：

> **最近这个 Scientist 如何研究这个 task。**

而不是：

> “Scientist 的 prompt、runtime 或 self source code 哪里可以改。”

后者属于 RSI。

---

## 11. Reflection Charter

Reflection 的制度性任务是：

> **尽可能寻找最近这个 Scientist 的研究判断和研究行为中值得怀疑的地方。**

Reflection 不负责鼓励当前 Scientist。

也不负责维护过去方向的 momentum。

它尤其需要审查以下问题。

### 11.1 Goal alignment

最近的工作是否真的在有效接近 Goal。

Scientist 是否逐渐把：

- 容易优化的 proxy；
- 局部 metric；
- 当前最熟悉的问题；

误认为 Goal 本身。

### 11.2 Anchoring

某次早期成功是否过度决定了后续研究。

一个方向曾经有效，不意味着它现在仍然最值得投入。

### 11.3 Local exploitation

最近是否持续把实验资源投入同一个 mechanism / intervention family。

这种投入仍然由 current evidence 支持，还是已经成为惯性。

### 11.4 Confirmation and self-justification

Scientist 是否在客观响应 neutral / negative evidence。

还是不断生成新的解释，以避免放弃旧 hypothesis。

### 11.5 Stale beliefs

过去形成的判断是否已经因为 Current World 或 accumulated evidence 的变化而失效。

### 11.6 Unexamined alternatives

Scientist 是否已经太久没有重新看整个问题。

是否存在长期没有重新比较的、更大的 leverage 或 alternative explanation。

### 11.7 Attention allocation

Scientist 是否因为某件事情容易理解、容易实现、自己熟悉，就持续把它当成研究重点。

### 11.8 Blind spots

是否存在重要问题，因为 Scientist 缺乏知识、缺乏工具、无法理解或不擅长调查，而被长期避开。

Reflection 在这里可以识别：

> “我可能缺少某种能力。”

但 Reflection 本身不修改 Scientist。

---

## 12. Reflection Must Be Critical, Not Defensive

Reflection 的默认立场应是：

> **Do not defend the recent Scientist. Challenge it.**

它不应该输出：

```text
A 仍然是当前热点。
虽然最近收益下降，但继续深挖仍然值得。
下一轮建议继续 A。
```

因为这只是 Normal Scientist 为自己当前 trajectory 提供辩护。

Reflection 的价值恰恰在于抵消这种 tendency。

但批判性不意味着强制制造问题。

如果没有足够 evidence 证明当前核心 judgment 错误，Reflection 可以明确说明：

> 没有发现足以推翻当前判断的强证据。

但即使如此，它仍然应该指出：

> **当前最值得警惕的风险是什么。**

Reflection 提供的是 cognitive resistance，而不是 encouragement。

---

## 13. Reflection Does Not Choose the Next Direction

Reflection 不负责提出下一实验。

不调用 Executor。

不提交 proposal。

也不替下一轮 Scientist 选择新的方向。

例如下面这种 Reflection 是越界的：

```text
Stop cache optimization and investigate data layout next.
```

因为它仍然是在替未来 Scientist 做 action selection。

更合适的是：

```text
Recent experiments no longer justify treating cache optimization as
the highest-value direction. Do not treat the unfinished cache plan
as evidence that it deserves more experiments. Re-evaluate the
problem globally from the current evidence.
```

Reflection 的职责是：

> **揭示当前 cognition 中值得被打破的惯性。**

至于下一步研究什么，由下一次 Normal Scientist 自己判断。

---

## 14. Reflection Handoff

Reflection 最重要的输出是一条面向未来自己的：

> **Note to my next self**

它不是 action instruction。

它是一个 cognitive warning。

例如：

```text
I became anchored on this optimization family because its first
experiment succeeded. The last several experiments no longer justify
treating it as the highest-value direction.

Do not inherit the unfinished local plan merely because I left it
there. Re-evaluate the whole problem from the current evidence before
forming the next research decision.
```

Reflection Handoff 的意义是：

> “昨天的我专门停下来审查了自己，这是我认为自己最可能犯的错误。”

---

## 15. Continuity After Reflection

普通 proposer runs 之间：

```text
Normal Scientist
      ↓
Cognitive Handoff
      ↓
Normal Scientist
```

经过 Reflection：

```text
Normal
↓
Normal
↓
Normal
↓
Reflection
↓
Reflection Handoff
↓
Normal Scientist
```

下一次 Normal Scientist 的主要 continuity anchor 应当成为 Reflection Handoff。

这并不删除：

- 实验结果；
- source state；
- authoritative history；
- previous hypotheses；
- prior expectations。

这些仍然是研究事实与研究记录。

Reflection 真正打断的是：

- unfinished intention；
- local momentum；
- 对既有 trajectory 的默认延续；
- “昨天本来准备继续什么”。

因此：

> **Reflection does not erase research continuity. It breaks continuation inertia.**

---

## 16. Reflection Timing

Reflection 属于 Proposer / Scientist 的认知机制。

它不要求把整个 Loop 改造成 Scientist。

外部 Loop 仍然负责正常的运行与实验生命周期。

Scientist 内部需要一个 **guaranteed periodic reflection**：

> Scientist 不能无限连续进行 normal research episodes，而始终不重新审查自己。

Reflection 的固定周期不是在宣称：

> “每 N 轮一定是最科学的反思频率。”

它只是一个 anti-inertia guarantee：

> **无论 Scientist 当前多么相信自己的方向，它最终都必须停下来审查自己。**

因此第一版 Reflection 可以被理解为：

```text
N normal research episodes
↓
Reflection checkpoint
↓
N normal research episodes
↓
Reflection checkpoint
```

这里 N 是认知节奏参数，而不是研究语义本身。

未来 Scientist 可以主动更早反思，但固定 checkpoint 仍然承担兜底作用。

---

## 17. Reflection and RSI Are Different

Reflection 与 RSI 必须明确区分。

### Reflection

Reflection 问：

> **“最近的我是不是哪里想错了？”**

它改变的是 Scientist 的 cognitive state。

例如：

```text
Before reflection:
我仍然把 A 当成默认研究中心。

After reflection:
我认识到自己可能因为早期成功而对 A 产生了锚定。
下一轮必须重新依据 current evidence 判断。
```

Scientist body 没有变化。

仍然是同一个 Scientist。

### RSI

RSI 问：

> **“为什么我反复出现这种问题？是不是当前这个 Scientist 本身已经成为 Goal 的瓶颈？”**

只有当问题被理解为一个稳定的 self limitation 时，才进入真正的 self modification。

例如：

- 反复缺乏关键领域知识；
- 反复无法判断某类实验；
- 反复陷入同一种 exploration failure；
- 缺少某种研究工具；
- 缺少有效的 history capability；
- 缺少必要的 specialist；
- 当前 runtime 限制了 Scientist 能够采取的研究行为。

此时：

```text
Scientist S0
↓
RSI
↓
Scientist S1
```

因此三者的关系是：

```text
Continuity
= 让我还是昨天那个我

Reflection
= 让我质疑昨天那个我

RSI
= 如果问题确实在我，就改变我
```

Reflection 可以为 RSI 积累 self-diagnosis evidence。

但 Reflection 本身不是 RSI。

---

## 18. Design Principle: Fact, Judgment, and Action Must Remain Separate

整个 continuity / reflection 设计需要始终保持三层区分：

### Fact

真实世界发生了什么。

例如：

```text
Experiment E17 produced +0.4%.
Gate passed.
```

### Judgment

Scientist 如何解释这些事实。

例如：

```text
This weakens the hypothesis that lookup remains the dominant bottleneck.
```

### Action

基于当前 Goal、World 和 Judgment，下一步选择研究什么。

例如：

```text
Commission experiment E18.
```

跨 round continuity 可以保留 Fact 和 Judgment 的关系。

但不应该把过去的 Action preference 直接继承给未来。

因为：

> **Scientific judgment needs continuity; research action needs to be re-decided.**

---

## 19. Final Mental Model

SimpleLoop 的长期 Scientist 不是一个无限上下文的 persistent chat agent。

它更像：

> **一个每天都会忘记昨天具体思考过程，但可以恢复自己科学判断的研究者。**

普通 Cognitive Handoff 告诉它：

> “昨天的你为什么相信这些东西，以及现实后来怎样回应了你。”

它因此能够继续同一条 epistemic trajectory。

但它不能从 Handoff 获得：

> “今天你应该继续做什么。”

今天做什么必须重新判断。

同时，这个 Scientist 每隔一段时间必须停下来。

那一刻它不再问：

> “下一步怎么推进这个研究？”

而是问：

> **“最近这个我，真的还在有效接近 Goal 吗？”**

它主动寻找：

- 自己是否被过去的成功锚定；
- 是否陷入局部探索；
- 是否在替旧 hypothesis 找补；
- 是否忽略了更大的问题；
- 是否因为自己的能力边界而形成 blind spot。

然后给未来的自己留下一条批判性的 warning。

因此整个机制可以概括为：

```text
Epistemic Continuity
        ↓
same Scientist continues judging
        ↓
experiment
        ↓
evidence
        ↓
belief revision
        ↓
more research
        ↓
Periodic Reflection
        ↓
challenge the recent Scientist
        ↓
Reflection Handoff
        ↓
same Scientist,
but without automatic continuation
        ↓
new judgment from current evidence
```

最终原则是：

> **过去的 Scientist 可以把自己的判断交给未来，但不能把自己的决定交给未来。**

以及：

> **Continuity preserves scientific reasoning; Reflection prevents that reasoning from becoming inertia.**
