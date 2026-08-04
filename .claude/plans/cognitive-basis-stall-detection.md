# 认知元基停滞检测与自适应深度修复计划

## 问题根因

17 轮 omilrec run 暴露三个结构问题，根因是同一个：

**停滞信号按 per-finding 计算，proposer 每轮开新 finding → 每个 finding 只有 1 次实验 → `mechanism_challenge`（需要 `eligible_neutral >= 2`）永远不触发。**

这导致：
1. r6–r10 连续 5 轮微变体全 no_improve，无 reframe/abandon
2. r13–r17 连续 5 轮回归，仅 r13 触发 1 次 reframe（且 reframe 后仍 submit 同族 proposal）
3. 17 轮 0 次 abandon，即使连续停滞/回归也总是提交

## 修复策略：三层递进

### 修复 1：跨 finding 全局停滞检测（signals.py）

在 `compute_deliberation_signals` 中新增 **cross-finding global signal**，不替代 per-finding 信号，而是叠加一层全局视图。

**核心思路：** 按时间窗口看最近 N 轮（跨所有 finding）的 eligible 结果序列，如果连续 M 轮没有 improvement（全是 neutral 或 regression），触发 `global_stall` 信号。

新增字段到返回的 signals dict：

```python
{
    "first_round": False,
    "hints_present": ...,
    "findings": [...],  # 不变
    "global": {          # 新增
        "recent_window": 5,
        "recent_eligible": 8,          # 最近5轮的eligible实验数
        "recent_improvements": 0,      # 其中improvement数
        "recent_neutral": 6,
        "recent_regressions": 2,
        "recent_mechanisms": ["invariant-hoisting", "hot-path-micro-optimization", ...],
        "policy_signals": {
            "global_stall": {
                "active": True/False,
                "rule": "recent_improvements == 0 and recent_eligible >= 3",
            },
            "regression_run": {
                "active": True/False,
                "rule": "recent_regressions >= 3 and recent_improvements == 0",
            },
        },
    },
}
```

**实现细节：**

1. 在 `signals.py` 中新增 `_compute_global_signal(experiments, sha_obj, *, window, objective_key, lower_is_better, findings)` 函数
2. 从 experiments 按 round 分组，取最近 `window` 轮（默认 5）
3. 对这些轮的 eligible 实验，用 `_classify_objective` 分类（复用已有逻辑）
4. 统计 improvement/neutral/regression 数
5. 收集这些实验关联 finding 的 mechanisms（通过 `findings` dict 查找）
6. `global_stall` active 当 `recent_improvements == 0 and recent_eligible >= 3`
7. `regression_run` active 当 `recent_regressions >= 3 and recent_improvements == 0`
8. 在 `compute_deliberation_signals` 末尾调用，合并到返回 dict

**阈值常量：**
```python
_GLOBAL_STALL_WINDOW = 5        # 看最近5轮
_GLOBAL_STALL_MIN_ELIGIBLE = 3  # 至少3个可分类实验才有意义
_GLOBAL_STALL_MIN_REGRESSIONS = 3  # regression_run 阈值
```

### 修复 2：停滞期重 frame（context.py + proposer.py）

当 global signal active 时，在 startup pack 和 frame 路径中注入更重的认知要求。

**2a. context.py `_render_signals` 扩展：**

在 per-finding 信号后渲染 global block。当 `global_stall` 或 `regression_run` active 时，追加一段显式指令：

```
Global stagnation signal: recent 5 rounds produced 0 improvements (6 neutral,
2 regression). Mechanisms tried: invariant-hoisting, hot-path-micro-optimization,
parameter-cache, ...
POLICY: The evidence shows your recent direction is not advancing the objective.
You must either (a) reframe to a genuinely different mechanism family and
explain why it is not a variant of what you have tried, or (b) abandon this
round if no direction clears the bar. Do not submit another small variant of
the same mechanism family.
```

这段文字是 **policy nudge**，不是科学结论（与现有 signals 的设计哲学一致）。它告诉 proposer "最近 N 轮没改进，你试过的机制族是这些"。

**2b. proposer.py `_maybe_nudge` 扩展：**

当前 `_maybe_nudge` 只检查 per-finding `mechanism_challenge`。新增对 global signal 的检查：

```python
if signals and not signals.get("first_round"):
    g = signals.get("global") or {}
    gps = g.get("policy_signals") or {}
    if (gps.get("global_stall") or {}).get("active"):
        notes.append(
            f"GLOBAL STALL: {g.get('recent_improvements',0)} improvements in "
            f"last {g.get('recent_window',0)} rounds. Mechanisms tried: "
            f"{', '.join(g.get('recent_mechanisms',[])[:5])}. "
            "Reframe to a different mechanism family or abandon — do not "
            "submit another variant of the same approach."
        )
```

这个 nudge 在每次 tool result 后注入，让 proposer 在 research 阶段也能看到（不只 startup pack）。

**2c. frame_research 在停滞期变重（proposer.py prompt 层）：**

不改状态机（frame_research 仍然一步完成），但通过 startup pack 中的 policy 文字让 proposer 自己在 frame_research 中展开更深的认知。

当 global stall active 时，startup pack 已经告诉了 proposer"必须 reframe 或 abandon"。proposer 在 frame_research 中自然会写更深的 research_question（因为 startup pack 明确要求它解释为什么新方向不是同族变体）。

这保持了"认知元基不是每步必填 checklist"的设计原则（§4.2）——不是强制 proposer 在 frame 中输出特定字段，而是通过 evidence 让它自己判断需要更深的 frame。

### 修复 3：abandon 合法化与显式鼓励（proposer.py prompt 层）

**3a. proposer.md prompt 强化：**

在 `## How you think` 的 Decide 段落中，强化 abandon 的地位。当前文案：

```
and `abandon_direction`, honestly, when nothing clears the bar — an honest
zero-proposal round beats a forced weak bet.
```

改为更主动的表述，并在 global stall 上下文中明确 abandon 是推荐选择之一。

**3b. _BUDGET_REMINDER 扩展：**

当前 budget reminder 只在 80% 步数时触发。新增：当 global stall active 时，在 startup pack 的 policy block 中已经包含"reframe 或 abandon"的要求。这不需要改 `_BUDGET_REMINDER`，因为 startup pack 已经在最前面告诉了 proposer。

**3c. 不加 runtime 级强制 abandon：**

不在 `_validate_action_guard` 中加"global stall 时禁止 submit"的硬 gate。原因：
- 设计原则是"policy signals 是 nudge，不是 verdict"（signals.py 文档注释明确说了）
- 硬 gate 会把 harness 启发式变成科学结论，违反设计哲学
- proposer 应该自己判断，而不是被 runtime 强制

但 `_maybe_nudge` 的 nudge 文字要足够强，让 proposer 很难忽略。

## 文件修改清单

| 文件 | 修改 | 测试 |
|------|------|------|
| `simpleloop/memory/signals.py` | 新增 `_compute_global_signal` + 阈值常量 + 在 `compute_deliberation_signals` 末尾调用 | `tests/test_deliberation_signals.py` 新增 3-4 个测试 |
| `simpleloop/memory/context.py` | `_render_signals` 扩展渲染 global block + policy 文字 | 在 `test_memory_service.py` 或新文件中测试 startup pack 包含 global block |
| `simpleloop/roles/proposer.py` | `_maybe_nudge` 扩展 global stall nudge | `test_proposer_agent.py` 新增 nudge 测试 |
| `simpleloop/prompts/proposer.md` | 强化 abandon 文案 | 无（行为测试在 proposer_agent 中） |

## 不修改的部分

- **状态机不变**：FRAME→RESEARCH→DECIDE 三阶段、`frame_research` 一步完成的设计保持
- **per-finding 信号不删**：仍然计算和渲染，只是叠加了 global 层
- **`_validate_action_guard` 不加硬 gate**：保持 policy nudge vs scientific verdict 的分离
- **`_compute_signals` 调用时机不变**：仍在 startup 时计算一次（signals 本身是 ledger-derived，一轮内不变）
- **Finding 数据模型不变**：不增加新字段到 Finding
- **frontier.py 不变**：coverage.mechanisms 已经提供了全局机制视图

## 实现顺序

1. `signals.py`：新增 `_compute_global_signal` + 常量 + 测试
2. `context.py`：`_render_signals` 渲染 global block + 测试
3. `proposer.py`：`_maybe_nudge` 扩展 + 测试
4. `proposer.md`：abandon 文案强化

每步独立可测，不依赖后续步骤。先改 signals（纯函数，最安全），再改渲染层，最后改 prompt。

## 验证方式

1. **单元测试**：`pytest tests/test_deliberation_signals.py` — 新增测试覆盖 global stall / regression_run 触发和不触发
2. **回归测试**：`pytest tests/` — 确保现有测试不破
3. **trace 验证**：用现有 run 的 history.jsonl 模拟，验证 r8/r9 会触发 global_stall（r6-r10 连续 5 轮 0 improvement），r13 会触发 regression_run
4. **行为验证**：只有实际重跑 run 才能验证 proposer 是否真的 reframe/abandon，但 prompt 层的 nudge 是否注入可以通过 startup pack 文本检查

## 风险

1. **window=5 可能太短**：r6-r10 正好 5 轮，如果 proposer 在 r5 就看到信号可能过早。但 r5 是 +1.71% improvement，所以 global_stall 在 r6 结束后才触发（r6 是第一个 neutral），r7 才看到信号。这是合理的——给一个轮的容错。
2. **recent_mechanisms 可能过长**：需要截断到 top-5（按频率排序），避免 startup pack 膨胀。
3. **proposer 仍可能忽略 nudge**：这是 prompt 层修复的固有风险。如果重跑后 proposer 仍然不 reframe/abandon，则需要考虑 runtime 级硬 gate（但那是后续决策，不在本次修复范围）。
