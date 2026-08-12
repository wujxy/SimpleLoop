# 生成元-认知侧伙伴分工重构计划

## 目标

把 SimpleLoop 从 "生成 → 去重/过滤/选择 → 分支" 重构为 "生成元 ↔ 认知侧伙伴 1:1 lane"。

**核心原则：不是重构，是重新分工 + 接口调整。** 当前生成侧和认知侧的内部逻辑都很好，不乱碰。只做三件事：
1. 把生成侧从历史中解放（输入接口换掉，内部不动）
2. 给认知侧加一条 "历史审计 → 反馈给生成侧重新生成" 的路径
3. orchestrator 从 pool-filter 改为 1:1 lane

**关系定位：伙伴关系，不是 challenge。** 生成侧是自由的探索者，认知侧是它的伙伴——做最简审查 + 历史建议 + 增强为 proposal。认知侧替生成侧看它看不到的历史，把相关事实反馈给它，生成侧自己决定怎么调整。反馈有界（3 次），但不是否决权。

**在 `feature/proposer-branch-deepen` branch 上改。** 当前 HEAD = `56a665a`（independent hypothesis generation with G-op scheduling）。

---

## 现状（HEAD = 56a665a）

### orchestrator pipeline
```
explore → build_startup_pack(含历史) →
  _generate_independent_hypotheses(hypothesis_count=N calls × n=2) →
  dedup_by_signature → _split_frozen → cards[:candidates_per_round] →
  _run_branches(并行 research_branch) →
  filter eligible → cap by tool_calls → collect
```

### 生成侧
- `generator.run(n, frame_free_ratio, context, explore, prompt_dir, assigned_ops)`
- 收 `startup_pack`（含 dashboard/frontier/explore boundary）+ `explore`（exhausted families）
- free/guided slot 机制，一次产 N 张 card

### 认知侧
- `research_branch(hypothesis, ..., explore, max_steps)` — 收完整 startup_pack + 一张 card
- Sieve → Enrich → submit|block
- 有 `search_experiments`/`inspect_episode`/`list_findings`/`search_findings` 工具
- **没有 "查完历史 → 反馈给生成侧 → 重新生成" 的路径**

### config
- `hypothesis_count`（=8）：独立生成调用数
- `branch_steps`（=None）：per-branch step budget
- `candidates_per_round`（=1）：最终 proposal 数
- `frame_free_ratio`：硬编码 0.0

---

## 改动设计

### 改动 1：生成侧——从历史中解放

**文件：`generator.py`、`memory/context.py`、`memory/service.py`**

#### 1a. 新增 `build_generation_context()` in context.py + service.py facade

只含任务定义，不含历史：
```python
def build_generation_context(*, goal, editable, frozen, base_sha, gate_block) -> str:
    """History-free context: objective/gates/paths/base_sha only."""
```

#### 1b. 修改 `GeneratorAgent.run()` 签名

从 `(n, frame_free_ratio, context, explore, prompt_dir, assigned_ops)` 改为：
```python
def run(self, *, context, prompt_dir=None, assigned_ops=None) -> GenerationResult
```
- 删 `explore` → 不再渲染 `render_generation_boundary`
- 删 `frame_free_ratio` → free/guided slot 逻辑删
- 固定产 1 张 card（`_parse_hypotheses(reply.text, expected=1)`）
- 保留 `assigned_ops`（5-of-9 随机 G 子集）
- **保留不动：** G1-G9、`_replace_basis`、`_g_definition`、`_parse_hypotheses`

#### 1c. 新增 `GeneratorAgent.regenerate()`

```python
def regenerate(self, *, context, feedback, transcript, prompt_dir=None,
               assigned_ops=None) -> GenerationResult
```
- `feedback`：认知侧的历史证据（哪些方向做过、结果如何）
- `transcript`：这个 lane 之前的生成侧历史
- 产 1 张新 card
- prompt 告诉生成侧 "这是你看不到的历史，参考它重新生成"

### 改动 2：认知侧——加历史审计 + 反馈路径

**文件：`proposer.py`**

#### 2a. 新增 `feedback_generator` action in `_parse_action`

```json
{"action": "feedback_generator",
 "evidence_refs": ["experiment:r3c0"],
 "observation": "this region+mechanism was tried in r3c0, result neutral",
 "relation_to_seed": "same cache pattern",
 "request": "consider a materially different direction"}
```
非终止 action（和 tool call 一样不终止 loop）。

#### 2b. `research_branch` 加 `generator_regenerate` callback

```python
def research_branch(self, *, ..., generator_regenerate=None) -> BranchResult
```
action 循环里 `feedback_generator` 处理：
- 调 `generator_regenerate(action)` → 拿到新 hypothesis
- 把 feedback + 新 hypothesis 追加到 messages，继续 loop
- **认知侧 transcript 保持活跃**——source reads 不因 hypothesis 换了就丢

#### 2c. proposer prompt 加 `feedback_generator` 文档

### 改动 3：orchestrator——1:1 lane

**文件：`orchestrator.py`**

- 删 `dedup_by_signature`、`_split_frozen`、cap、list-wise selection
- 删 import：`fnmatch`、`dedup_by_signature`、`distinct_niches`
- 新增 `LaneState`（lane_id, assigned_ops, hypothesis_versions, regenerations, gen_transcript）和 `LaneResult`
- `run()`：N = `candidates_per_round` 个 lane，1:1，并行跑，收集所有 submit 的 proposal
- `_run_one_lane`：初始生成 → `research_branch(generator_regenerate=callback)` → feedback 循环（≤3 次）
- callback 里 `lane.regenerations >= 3` 时抛异常 → 认知侧 catch → submit 或 abstain

### 改动 4：config 清理

**文件：`config.py`、`loop.py`**

- 删 `hypothesis_count`（`candidates_per_round` 是唯一宽度）
- `loop.py` 构造不再传 `hypothesis_count`
- `branch_steps` 保留

### 改动 5：prompt 更新

**文件：`prompts/generator.md`、`prompts/proposer.md`**

- generator.md：删 exhausted-region/slot 相关，改 "Produce exactly one hypothesis"，加 regenerate 契约
- proposer.md：加 `feedback_generator` action，强调历史重叠不是 block 理由

### 改动 6：测试

删旧 pool-filter 测试，加 1:1 lane 测试 + history-free context 测试 + regenerate 测试 + feedback action 测试 + feedback 循环测试 + 3 次限制测试。

---

## 实施步骤

1. **改动 1a-1b**：history-free context + generator.run() 改签名
2. **改动 1c**：generator.regenerate()
3. **改动 2a-2b**：feedback_generator action + research_branch callback
4. **改动 3**：orchestrator 1:1 lane
5. **改动 4**：config 清理
6. **改动 5**：prompt 更新
7. **改动 6**：测试
8. **全量测试 + smoke round**

---

## 不碰的东西

- G1-G9 生成元基础、`_replace_basis`、`_g_definition`、`_parse_hypotheses`
- 认知侧 Sieve（block 三个客观理由）、Enrich（locate + constraint + realization freedom）
- 认知侧 `_step`、protocol repair、guard、`_partial_submit`
- `ResearchTools`、`WorkingState`、evidence tracking
- `ProposerResult` 接口（loop.py 不动）
- `build_startup_pack`（认知侧继续用，含历史）
