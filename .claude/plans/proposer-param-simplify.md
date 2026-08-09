# Plan: 简化 proposer 生成侧参数

## 问题诊断

当前 proposer 生成侧有 **6 个**步预算相关参数,但其中 3 个是死参数,2 个冗余拆分,1 个硬编码不可配:

| 参数 | 位置 | 默认值 | 实际效果 |
|------|------|--------|----------|
| `loop.gen_steps_base` | config.py | 36 | ✅ 生效 — 参与 `gen_steps = base + per_hyp * 10` |
| `loop.gen_steps_per_hyp` | config.py | 18 | ✅ 生效 — 同上,但与 base 拆分冗余 |
| `roles.researcher.max_steps` | config.py | 150 | ❌ **死参数** — 传到 orchestrator 后算 `max_branch_steps`,但该值从未被使用 |
| `roles.researcher.branch_steps` | config.py | None | ❌ **死参数** — 同上,是 `max_branch_steps` 的另一来源,也未被使用 |
| `_GEN_STEPS = 36` | orchestrator.py | — | ❌ **死代码** — 模块常量,零引用 |
| `_COGNITIVE_STEPS = 108` | orchestrator.py | — | ⚠️ 硬编码生效,但不可配 |

**实际运行时的步预算:**
- generator: `gen_steps = 36 + 18 * 10 = 216` 步
- cognitive: `cognitive_steps = 108 + 4 * 10 = 148` 步
- 用户配的 `max_steps: 50` 完全不参与以上任何计算

## 改动方案

删除 3 个死参数/死代码,将 `gen_steps_base + gen_steps_per_hyp` 合并为单个 `loop.gen_steps`,将硬编码的 cognitive 步预算提升为可配 `loop.cognitive_steps`。

### 改动后用户可见的生成侧参数

```yaml
loop:
  max_rounds: 30
  candidates_per_round: 4
  max_workers: 3
  gen_steps: 216            # generator 步预算(原 base+per_hyp*10 的合并)
  cognitive_steps: 148      # cognitive 步预算(原硬编码 108+40)
  agent_timeout_seconds: 7200

roles:
  researcher:
    api: hepai
    model: gpt-5.5
    base_url: https://...
    # max_steps 已删除(死参数)
    # branch_steps 已删除(死参数)
    command_timeout_seconds: 120
    command_output_cap_chars: 12000
```

### 向后兼容

- `gen_steps_base` / `gen_steps_per_hyp`: 无用户在 task.yaml 中使用过(grep examples/ docs/ 确认),可直接替换
- `max_steps` / `branch_steps`: 所有 example task.yaml 都配了 `max_steps: 20` 或 `50`,但这些值是死参数,删除后对实际运行零影响。config.py 对旧 key 报清晰错误提示
- `_GEN_STEPS` / `_COGNITIVE_STEPS`: 内部常量,`_COGNITIVE_STEPS` 改为从参数读入,`_GEN_STEPS` 直接删除

## 实现步骤

### 1. config.py — 替换 loop 步预算参数

- 删除 `gen_steps_base` / `gen_steps_per_hyp` 的解析和验证
- 新增 `gen_steps` (默认 216, int >= 4) 和 `cognitive_steps` (默认 148, int >= 4) 的解析和验证
- 删除 `_RESEARCHER_DEFAULTS` 中的 `max_steps` 和 `branch_steps`
- 删除 `_resolve_researcher` 中对 `max_steps` 和 `branch_steps` 的验证
- 更新 docstring(schema 说明)
- 更新返回的 resolved dict

### 2. orchestrator.py — 删死参数,用新参数

- 删除 `_GEN_STEPS = 36` (死代码)
- 删除 `_COGNITIVE_STEPS = 108` (改为从参数读入)
- `ProposerOrchestrator.__init__`: 删除 `max_steps` 和 `branch_steps` 参数及 `self.max_steps` / `self.branch_steps` 存储
- `run()`: 删除 `gen_steps_base` / `gen_steps_per_hyp` 参数,新增 `gen_steps` / `cognitive_steps` 参数;删除 `max_branch_steps` 计算(死代码)
- `_run_lanes()` / `_run_one_lane()`: 同步参数签名变更
- `_run_one_lane()` 内: `gen_steps` 直接用传入值(不再算 `base + per_hyp * 10`);`cognitive_steps` 直接用传入值(不再算 `_COGNITIVE_STEPS + 4 * _HYPOTHESES_PER_LANE`)
- `GeneratorAgent` / `ProposerAgent` 的 `__init__` 仍保留 `max_steps` 参数(它们是 ResearchAgent 基类接口,orchestrator 传入即可)

### 3. loop.py — 更新调用

- `ProposerOrchestrator(...)` 构造: 删除 `max_steps=researcher["max_steps"]` 和 `branch_steps=researcher.get("branch_steps")`
- `ctx.proposer_agent.run(...)`: 删除 `gen_steps_base` / `gen_steps_per_hyp`,改为 `gen_steps=cfg.get("gen_steps", 216)` 和 `cognitive_steps=cfg.get("cognitive_steps", 148)`

### 4. examples/ + docs/ — 更新 task.yaml 模板

- 所有 task.yaml: 删除 `roles.researcher.max_steps` 行
- `examples/task.yaml` (模板): 在 loop 块注释中加 `gen_steps` / `cognitive_steps` 说明
- `docs/README.md`: 删除 `max_steps: 50` 行

### 5. tests/ — 更新测试

- `tests/test_config_execution.py`: 
  - 更新 `test_researcher_defaults` 的 assert(去掉 `max_steps` / `branch_steps`)
  - 更新 `test_researcher_rejects_invalid_values` 的 parametrize(去掉 `max_steps: 0` 用例,可加 `gen_steps: 3` / `cognitive_steps: 3` 用例)
- `tests/test_orchestrator.py`:
  - `_orchestrator()` helper: 删除 `max_steps` / `branch_steps` 参数
  - 更新所有 `_orchestrator(...)` 调用
- 其他 test 文件中对 `max_steps` 的引用: 区分两类
  - 直接构造 `ProposerAgent` / `GeneratorAgent` 的(如 test_cognitive_element.py, test_generator.py): 这些是 ResearchAgent 基类的 `max_steps`,**保留不动**(基类接口不变)
  - 构造 `ProposerOrchestrator` 的: 删除 `max_steps` / `branch_steps`

### 6. 验证

- `python -m pytest tests/test_config_execution.py tests/test_orchestrator.py -x`
- `python -m pytest tests/ -x` (全量)
- `python -c "from simpleloop.config import load; load('examples/omilrec-v100-opt/task.yaml')"` 确认配置加载正常
