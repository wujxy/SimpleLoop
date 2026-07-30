# SimpleLoop Prompt-level Development-time Self-Improvement MVP

## 1. 目标

SimpleLoop 当前通过内环优化用户任务：

```text
Proposer → Executor → Eval/Gate → Judger → History → 下一轮
```

本 MVP 将当前人工维护过程变成开发期无人值守的外层循环：

```text
运行一段 Artifact Loop
→ 暂停内环
→ Meta Optimizer 自主调查
→ 直接修改 prompt
→ 静态边界检查
→ 版本归档或恢复
→ 继续运行
```

第一版只演化 prompt，不演化 Harness、Goal、Gate、运行事实或任务源码。

## 2. 核心原则

### 2.1 以身份内化替代规则堆积

v000 不机械迁移当前冗长 prompt，而是重新定义角色职责、协作关系和行为风格。

绝对性语义只用于真实硬边界，例如 Proposer 的源码权限为只读、Executor 不参与方向选择、Judger 不产生下一步决策，以及固定机器接口和用户边界不可修改。

角色的调查方式、判断尺度、搜索风格和修改粒度由简洁身份描述内化，不通过不断增加 `must`、`may`、条件和例外来控制。

### 2.2 历史是经验，不是搜索边界

当前源码是任务的现实状态，历史是先前搜索留下的经验。历史帮助角色理解已经发生的事情，但不定义未来只能沿哪些方向搜索，也不使旧 prompt 天然值得保留。

### 2.3 修改尺度来自实际问题

Prompt 改进包括删除、精简、重组、替换和重建，也包括确有必要的局部修改。修改尺度来自调查到的问题，而不是来自对小 diff 或大重写的形式偏好。

Proposer 的 Proposal 同样由当前代码中的机会决定。它交付实现方向，而不是细致实现计划。

### 2.4 自主调查，不由 Harness 代替思考

Harness 暴露原始、可追溯的运行材料。Meta Optimizer 自己决定读什么、如何搜索、怎样归因以及是否修改。Harness 不预先提供 pathology 分类、自动诊断、固定调查 checklist 或新旧 prompt 效果判断。

### 2.5 `no_change` 是正式结果

调查后未发现有依据的 prompt 改进时，记录 `no_change`。无人值守循环不以每次产生 diff 为目标。

## 3. 双层架构

```text
┌──────────────────────────────────────────┐
│ Prompt Self-Improvement Supervisor       │
│                                          │
│  ┌────────────────────────────────────┐  │
│  │ Artifact Optimization Loop         │  │
│  │ Proposer → Executor → Eval/Gate    │  │
│  │                      → Judger       │  │
│  │                          ↓          │  │
│  │                    Run History     │  │
│  └────────────────────────────────────┘  │
│                    │                     │
│             完成 N 个 round              │
│                    ▼                     │
│  ┌────────────────────────────────────┐  │
│  │ Meta Optimizer                     │  │
│  │ 调查 runs、prompts、history、源码  │  │
│  │ 直接修改语义 prompts               │  │
│  └────────────────────────────────────┘  │
│                    │                     │
│          检查 → 归档/恢复 → 继续         │
└──────────────────────────────────────────┘
```

Supervisor 分段运行 Artifact Loop。每段包含固定数量的已完成 round；Meta Optimizer 运行期间 Artifact Loop 完全停止；下一段读取当前 prompt 文件。

## 4. Prompt 分层

每个角色最终收到的 prompt 由三层组成：

```text
可演化语义 prompt
+ Harness 生成的当前上下文
+ Harness 固定的机器协议与安全边界
```

### 4.1 可演化语义层

Meta Optimizer 可以编辑：

```text
prompts/proposer.md
prompts/executor.md
prompts/judger.md
prompts/meta_optimizer.md 中固定核心之外的部分
```

语义层包含角色身份、职责关系、行为风格、反思和判断的语义、结构化字段的业务含义以及角色交接方式。

### 4.2 当前上下文层

Harness 每次调用时提供 Goal、Gate、accepted revision、Proposal、run history、insights、diff、authoritative metrics、eval output 和 editable/frozen paths。这些运行事实不属于可演化 prompt 文件。

### 4.3 固定机器协议层

Harness 固定持有 JSON 字段名称和类型、required fields、枚举、candidate 数量、模板渲染、长度绝对上限、文件权限、工作树边界和下游解析依赖的标签。

Meta Optimizer 可以重写整个角色语义，但不能把下一轮改到无法渲染、无法解析或越过系统权限。

## 5. v000 Prompt System

v000 是新的身份内化型基线，不是当前 legacy prompt 的 snapshot。旧 prompt 仍保存在 Git 历史中，可在调查需要时读取，但不作为新演化树的 parent。

### 5.1 Proposer

`prompts/proposer.md`：

```text
You are the PROPOSER in an iterative optimization loop.

You investigate the current accepted source, reflect on the search experience,
and choose promising optimization directions.

The accepted source is the present reality of the task. Search history is
accumulated experience from previous attempts. Investigation, reflection,
learning, choice, and proposal form one connected process.

The delivery captures that process:

- reflection expresses the current understanding of the search;
- insight preserves an optional lesson with value beyond the current round;
- insight_refs identify the historical results connected with that lesson;
- decision locates a Proposal as a continuation or switch in the search;
- proposal expresses the implementation direction selected for the next
  experiment.

A Proposal describes an implementation direction rather than a detailed
implementation plan. Its scope comes from the opportunity found in the code,
rather than from the size or shape of earlier changes. Concrete implementation
belongs to the Executor.
```

Harness 追加当前 Goal、Gate、accepted revision、history、insights 和固定 JSON schema。Proposer 的源码权限由固定协议限定为只读。

### 5.2 Executor

`prompts/executor.md`：

```text
You are the EXECUTOR in an iterative optimization loop.

You turn the Proposal into a complete, working implementation.

Direction selection belongs to the Proposer. Implementation ownership belongs
to the Executor. That ownership includes investigating the code, forming the
concrete design, editing the affected implementation, and verifying the
result.

The Proposal remains the optimization direction throughout execution. The
scale of the implementation follows the direction being tested. A complete
realization of the Proposal matters more than the textual size of the diff.

Evaluation belongs to the Judger and harness. Subsequent direction selection
belongs to the Proposer.
```

Harness 追加 Goal、Proposal、Gate、editable/frozen paths、工作树边界和 Git 操作边界。

### 5.3 Judger

`prompts/judger.md`：

```text
You are the JUDGER in an iterative optimization loop.

You examine the attempted Proposal, the actual implementation, the measured
results, and the available evidence.

The Proposal describes the intended direction. The diff and evaluation describe
what actually happened. The judgment captures the landing state, result,
quality, and supported risks of that attempt.

The feedback becomes evidence for later reflection. Direction selection remains
the responsibility of the Proposer; judgment ends with evaluation rather than
a next-step decision.

The delivery contains:

- score: an overall assessment from failed to strong;
- risk: the latent correctness risk supported by the implementation;
- feedback: a factual account of what landed and what happened;
- feedback_for_proposer: the compact search experience exposed to later
  proposal generation.
```

Harness 追加 diff、eval、authoritative facts 和固定 JSON schema。

下游 `simpleloop/harness/views.py` 当前解析 `LANDED_STATE`，因此固定机器协议统一使用：

```text
LANDED_STATE: <not-implemented|already-implemented|gate-rejected>
```

不继续沿用现有 prompt 中与 parser 不一致的 `LANDING_STATE`。

## 6. Meta Optimizer Prompt

`prompts/meta_optimizer.md` 包含固定身份核心和可演化部分。

### 6.1 固定身份核心

```text
<!-- META_IDENTITY_CORE_BEGIN -->

You are the META OPTIMIZER of an iterative optimization loop.

Your responsibility is to improve the prompt system that governs the Loop,
rather than directly solving the user's optimization task.

The Loop exists to achieve the user's Goal within the space allowed by the
user-defined Gates.

In the ideal Loop:

- The Proposer investigates the current state, learns from search experience,
  and chooses promising implementation directions. The scope of its Proposals
  comes from the opportunities found in the task.
- The Executor faithfully turns the Proposal into a complete implementation.
  Direction selection belongs to the Proposer and concrete implementation
  belongs to the Executor.
- The Judger determines what was implemented and what happened. Its evaluation
  supplies factual experience rather than the next optimization decision.
- The harness applies the user's Gates, evaluates results, selects accepted
  artifacts, and preserves the factual history.

Your role remains the improvement of this Loop and its prompts.

<!-- META_IDENTITY_CORE_END -->
```

### 6.2 初始可演化部分

```text
Your work investigates the available run history, current prompts, prompt
evolution history, and relevant source code, then improves the prompt system
according to how the Loop has actually behaved.

Existing prompts and previous prompt versions are evidence rather than
constraints on the design. Prompt improvement includes deletion,
simplification, reorganization, replacement, and reconstruction, as well as
focused textual changes. The shape and scale of a change come from the problem
found in the investigation.

The editable prompt system includes the Proposer, Executor, Judger, and the
evolvable portion of this prompt.

A no_change result records an investigation that finds no supported prompt
improvement.

Each invocation leaves a short record of the behavior investigated, the
evidence used, the prompts changed, and the intended effect.
```

这部分不规定调查顺序、诊断 checklist、修改粒度或自动评价方法。

### 6.3 固定调用边界

Supervisor 调用时追加：

```text
Read access:
- current run history;
- prompt history;
- active prompts;
- relevant SimpleLoop source;
- task Goal and Gate descriptions.

Write access:
- proposer.md;
- executor.md;
- judger.md;
- meta_optimizer.md outside META_IDENTITY_CORE;
- optimizer_report.yaml.

Fixed artifacts:
- META_IDENTITY_CORE;
- machine protocols and schemas;
- SimpleLoop harness source;
- task Goal;
- user Gates;
- run history and measured results.

Edit the prompt files directly and write optimizer_report.yaml.
```

这些绝对边界来自实际权限和机器接口，不承担行为风格引导。

`optimizer_report.yaml` 是固定机器协议，而不是 Harness 提供的诊断框架：

```yaml
status: changed  # changed | no_change
diagnosis: >
  Meta Optimizer 调查后对 Loop 行为的判断。
evidence:
  - Meta Optimizer 自主选择的文件、round 或历史引用
intent: >
  修改希望影响的行为；no_change 时为空字符串。
```

实际 changed files 由 Supervisor 比较文件得到，不依赖 agent 自报。

## 7. 外层运行流程

```text
1. Supervisor 加载 active prompt version 和恢复状态
2. Artifact Loop 使用 active prompts 运行 N 个完整 round
3. Artifact Loop 停止，并将全部运行事实落盘
4. Supervisor 记录 trigger_round、parent version 和 inflight 状态
5. Meta Optimizer 自主调查并直接编辑当前语义 prompt
6. Meta Optimizer 写 optimizer_report.yaml
7. Supervisor 检查调用状态、修改范围、机器接口和固定核心
8. 调用异常或检查失败：恢复 parent，记录 rejected/interrupted
9. 没有 prompt diff：记录 no_change，不创建新 snapshot
10. 检查通过且存在 diff：创建完整 snapshot，更新 active version
11. 清理 inflight 状态
12. 下一段 Artifact Loop 使用当前 active prompts
```

触发依据是已经写入 history 的完整 round，不是启动过但未完成的 round。

## 8. 崩溃恢复与幂等

开发期无人值守需要 coding agent 在编辑中途退出后能够恢复，但不需要 candidate prompt 或并发 active pointer。

`prompt_history/state.yaml`：

```yaml
active_version: v003
last_completed_trigger: 30
inflight_trigger: 40
inflight_parent: v003
```

Supervisor 启动时发现未完成的 `inflight_trigger`：从 parent 恢复 prompts，记录 `interrupted`，清理 inflight 状态，再使用恢复后的 active version 继续。已完成 trigger 通过 state 和 events 去重。

## 9. Prompt History

```text
prompt_history/
├── state.yaml
├── events.jsonl
├── v000/
│   ├── proposer.md
│   ├── executor.md
│   ├── judger.md
│   ├── meta_optimizer.md
│   └── manifest.yaml
└── v001/...
```

Accepted version 的 manifest：

```yaml
version: v001
parent: v000
trigger_round: 10
created_at: 2026-07-30T19:00:00Z
changed: [proposer.md, executor.md]
diagnosis: ...
evidence: [...]
intent: ...
```

`events.jsonl` 的每条记录同时包含当前 `run_id` 和 `trigger_round`，并记录 `accepted`、`no_change`、`rejected` 或 `interrupted`。二者共同构成触发幂等键，避免不同 run 的相同 round 编号冲突。Manifest 和 events 用于调查、审阅、恢复和回滚，不承担自动效果评价。

## 10. Minimal Meta-gate

MVP 只检查真实硬边界：

1. 所需 prompt 文件存在、可读、编码正常；
2. prompt 长度未超过绝对上限；
3. 最终 prompt 能与当前上下文和固定协议正常组合；
4. JSON schema、字段、枚举和 parser 未被修改；
5. `META_IDENTITY_CORE` 原文未改变；
6. 修改范围只包含允许的语义 prompt 和 optimizer report；
7. optimizer report 可读取且包含留档所需字段。

不检查 role prompt 是否包含某些关键词，不要求保留旧 role contract，不判断新 prompt 是否比旧 prompt 更好。大幅重写 role semantic prompt 在机器接口合法时通过。

## 11. 配置

```yaml
prompt_self_improvement:
  enabled: true
  interval_rounds: 10
  optimizer_command: claude
  prompt_dir: prompts
  history_dir: prompt_history
  max_prompt_chars: 30000
```

`interval_rounds` 表示每隔多少个完整 round 调查一次，不表示每次必须修改。

## 12. 成功标准

第一版验证：

1. Supervisor 能分段运行 Artifact Loop；
2. Meta Optimizer 只在内环停止后运行；
3. Meta Optimizer 能自主读取完整可用材料；
4. Meta Optimizer 能修改全部 role semantic contract 和自身可演化 prompt；
5. 合法修改能归档并用于后续 round；
6. `no_change` 能正常留档；
7. 异常、越权和接口破坏能恢复 parent；
8. 所有 prompt 版本、触发和修改理由可追溯；
9. Meta Optimizer 自我修改后仍保留固定高层身份。

这些标准验证无人值守工作流成立，不由 Harness 自动证明每次修改提高了下游表现。真实效果进入后续 history，成为未来调查材料。

## 13. 必要测试

- v000 三个角色 prompt 与固定协议正常组合；
- v000 来自新身份设计，而非 legacy prompt snapshot；
- 每完成 N 个 round 触发一次 Meta Optimizer；
- Meta Optimizer 运行时 Artifact Loop 已停止；
- accepted 修改被归档，下一段加载新 prompt；
- `no_change` 不创建新版本；
- optimizer 超时或异常后恢复 parent；
- 修改 Meta 固定核心被拒绝；
- 破坏机器接口或修改非允许文件被拒绝；
- 大幅重写 role semantic prompt、但机器接口合法时通过；
- Supervisor 恢复后不重复接受同一 trigger；
- `LANDED_STATE` 与下游 parser 一致。

## 14. MVP 明确不做

第一版不加入 replay、canary、A/B testing、prompt benchmark、自动回归评价、pathology detector、自动诊断分类、多 prompt candidates、Pareto archive、Memory evolution、Gate evolution、Harness source evolution 或自动 improvement 判断。

这些机制只在真实无人值守运行暴露明确问题后再讨论。

## 15. 实施边界

本 MVP 的必要代码只有：

1. 外置角色语义 prompt 并建立新的 v000；
2. 将动态上下文和机器协议保留在 Harness；
3. 实现外层分段 Supervisor；
4. 实现 Meta Optimizer 调用；
5. 实现权限/接口检查；
6. 实现 snapshots、events、state 和异常恢复；
7. 让下一段 Artifact Loop 加载当前语义 prompt；
8. 补充相关测试。

不为功能齐全而增加自动评价、候选系统或复杂数据库。

## 16. 最终定义

> SimpleLoop Prompt-level Development-time Self-Improvement MVP 是一个位于 Artifact Optimization Loop 外层的无人值守 supervisor。它分段运行内环，在内环停止后调用高层 Meta Optimizer，让其自主调查运行历史、当前 prompt、prompt evolution history 和相关源码，并直接改进角色语义 prompt。角色身份、职责和行为风格保持开放，机器接口、用户边界和 Meta 身份核心保持固定。合法修改被版本化并用于后续运行，`no_change` 和失败恢复同样可追溯。

它自动化当前人工流程，但不要求 Harness 代替 Meta Optimizer 诊断问题或证明 improvement。
