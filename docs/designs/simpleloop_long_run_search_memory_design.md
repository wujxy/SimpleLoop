# SimpleLoop Long-run Search Memory 设计方案

> 状态：MVP v1  
> 适用分支：`examples-reorg`  
> 目标：在现有稳定的 `Proposer → Executor → Judger` Loop 上，引入最简、可重建、可审计的长期搜索记忆，使 Proposer 能够从长 run 的历史试错中持续学习，而不是反复试错或重新提出已经落地、已被证伪的 Proposal。

---

## 1. 背景

SimpleLoop 当前已经具备三项基础能力：

1. Proposer 能根据任务目标和当前源码主动提出 Proposal；
2. Proposer 能根据最近若干 round 的结果进行短期反思；
3. Executor、Judger 和 accepted code lineage 已形成稳定交接。

当前反思链主要解决：

```text
最近发生了什么
    ↓
当前方向是否值得继续
    ↓
下一轮提出什么 Proposal
```

但在长 run 中仍存在明显风险：

- 十几个 round 后重新提出已经实现过的方案；
- 对同一方向反复尝试局部变体，但没有意识到该方向已经饱和；
- 一次失败只影响下一轮，没有沉淀为长期可复用认知；
- history 越来越长，但 Proposer 每轮仍需重新阅读和理解流水账；
- round 数增加不一定带来 Proposal 质量提升，甚至可能变成更有组织的随机试错。

因此需要在 `history.jsonl` 基础上增加 Long-run Search Memory。

---

## 2. 第一性原理

### 2.1 History 与 Search Memory 的区别

`history.jsonl` 保存：

> 每一次具体试验发生了什么。

Long-run Search Memory 保存：

> 多次试验共同说明了什么，以及当前搜索空间处于什么状态。

二者关系为：

```text
history.jsonl
完整、追加、不可变的实验事实
        │
        │ deterministic fold
        ▼
search_memory.json
可删除、可重建的长期搜索状态
        │
        │ compact projection
        ▼
Proposer Context
```

Search Memory 不是新的事实来源。

它必须满足：

- 可由 `history.jsonl` 完整重建；
- 删除后不影响 run 的事实完整性；
- 不保存 Executor 或 Judger 无法验证的自由推断；
- 不依赖增量状态恢复；
- 不引入新的独立 Agent。

---

### 2.2 Search Memory 的唯一产品目标

MVP 只解决三个问题：

1. **防止遗忘**  
   让 Proposer 知道哪些方向已经尝试、哪些机制已经落地。

2. **识别方向状态**  
   让 Proposer 知道一个方向总体是 promising、stalled、negative，还是尚未真正验证。

3. **让长期经验进入决策链**  
   让 Proposer 的 reflection 同时使用长期搜索状态和近期 round 证据，而不是只看最近一次结果。

---

## 3. MVP 非目标

第一版明确不实现：

- 向量数据库；
- embedding 检索；
- Memory Agent；
- Knowledge Graph；
- 自主遗忘；
- 跨 run 经验迁移；
- 自动生成自由形式 scientific lessons；
- 强化学习式 memory controller；
- 独立 novelty judge；
- 复杂语义重复检测；
- 自动 profiling 和新观测生成。

这些能力可能有长期价值，但不是当前问题的最小解。

---

## 4. 总体架构

```text
┌─────────────────────────────────────┐
│ history.jsonl                       │
│ proposal / diff / metrics / judge   │
│ selected / accepted / commit        │
└──────────────────┬──────────────────┘
                   │
                   │ 每轮重新扫描
                   ▼
┌─────────────────────────────────────┐
│ simpleloop/search_memory.py         │
│                                    │
│ 1. 按 family 聚合 candidate         │
│ 2. 计算 objective gain              │
│ 3. 分类 candidate outcome           │
│ 4. 计算 family state                │
└──────────────────┬──────────────────┘
                   │
                   ▼
┌─────────────────────────────────────┐
│ search_memory.json                  │
│ family 级长期搜索状态               │
└──────────────────┬──────────────────┘
                   │
                   │ render_for_proposer()
                   ▼
┌─────────────────────────────────────┐
│ Proposer Context                    │
│                                    │
│ Current accepted base              │
│ Long-run Search Memory             │
│ Recent round history               │
│ Source inspection                  │
└──────────────────┬──────────────────┘
                   ▼
          reflection → decision
                     → proposal
```

---

## 5. 最小记忆单位：Search Family

### 5.1 为什么使用 family

Long-run Search Memory 不按 round 聚合，而按“优化假设族”聚合。

一个 family 表示：

```text
target bottleneck + optimization mechanism
```

例如：

```text
qpdf_kloop.bin_search_hoist
time_pdf.last_bin_cache
cal_ltof.geometry_precompute
pmt_loop.control_flow_specialization
```

这使系统可以把分散在不同 round 的相似试验归并起来。

---

### 5.2 family 的稳定性约束

Proposer 必须遵守：

- 同一目标、同一核心机制再次尝试时，复用原 family；
- 目标不同或机制本质不同，才创建新 family；
- 不能仅通过换措辞创建新 family；
- family 是长期记忆主键，不只是当前 candidate 标签。

Prompt 中应加入：

```text
`family` is the stable memory identity of the target bottleneck and
optimization mechanism. Reuse the exact prior family when testing the
same underlying hypothesis. Create a new family only for a genuinely
different target or mechanism.
```

---

## 6. `search_memory.json` 最小结构

建议结构：

```json
{
  "version": 1,
  "base_sha": "current-accepted-sha",
  "objective": {
    "key": "SPEED_MS",
    "lower_is_better": true
  },
  "families": [
    {
      "memory_id": "M1",
      "family": "qpdf_kloop.bin_search_hoist",
      "state": "stalled",
      "attempts": 3,
      "outcomes": {
        "improved": 1,
        "not_improved": 2,
        "invalid": 0,
        "duplicate": 0
      },
      "best_gain_pct": 16.0,
      "last_gain_pct": -2.6,
      "consecutive_non_improving": 2,
      "landed_refs": [
        "r3c0@ec27070"
      ],
      "last_ref": "r5c0@b3e95ce",
      "last_proposal": "Hoist repeated QPDF bin lookup outside the k-loop.",
      "last_feedback": "The change passed correctness but regressed against the accepted parent."
    }
  ]
}
```

### 6.1 必要字段

| 字段 | 含义 |
|---|---|
| `memory_id` | Prompt 中使用的稳定引用，如 `[M1]` |
| `family` | 长期搜索主键 |
| `state` | family 当前搜索状态 |
| `attempts` | 总尝试次数 |
| `outcomes` | 各结果类型计数 |
| `best_gain_pct` | 该 family 最佳客观收益 |
| `last_gain_pct` | 最近一次有效试验收益 |
| `consecutive_non_improving` | 最近连续无改善次数 |
| `landed_refs` | 进入 accepted lineage 的实现 |
| `last_ref` | 最近 candidate 引用 |
| `last_proposal` | 最近一次 Proposal 的紧凑摘要 |
| `last_feedback` | 最近一次 Judger 反馈的截断摘要 |

### 6.2 不加入的字段

MVP 不加入：

- `lesson_id`
- `confidence`
- `evidence_for`
- `evidence_against`
- `ttl`
- `embedding`
- `created_at`
- `updated_at`
- `belief`
- `revisit_reason`
- `causal_graph`

因为这些要么可以从 history 重新计算，要么会引入不可验证的自由推断。

---

## 7. Candidate Outcome 分类

Candidate outcome 必须由确定性程序根据真实执行结果计算。

Judger feedback 只能解释原因，不能决定一个方向是否有效。

### 7.1 `duplicate`

满足：

```text
landing_state == already-implemented
```

含义：

- Proposal 对应机制已经存在；
- 该 candidate 没有形成新的有效实验；
- 后续默认禁止原样重复。

---

### 7.2 `invalid`

满足任一条件：

```text
没有 commit
hard gate 未通过
risk == high
objective metric 缺失
evaluation 执行失败
```

含义：

- 本次试验没有产生可信的性能结论；
- 不能被当作该方向已被证伪；
- family 可能只是 blocked，而不是 negative。

---

### 7.3 `improved`

满足：

```text
commit 存在
hard gates 全通过
risk != high
objective 优于该 round 的 accepted parent
```

注意：

- 即使 candidate 没有被 selected，只要它优于 parent，也属于正向证据；
- candidate 必须和该 round 的 parent 比较，而不是始终与初始 baseline 比较。

---

### 7.4 `not_improved`

Candidate 有效，但 objective 没有优于 parent：

```text
objective 与 parent 持平或回退
```

MVP 不再拆分 `neutral` 和 `regressed`。

收益幅度由 `gain_pct` 表达。

---

### 7.5 `gain_pct` 统一定义

统一规定：

```text
gain_pct > 0 代表改善
gain_pct = 0 代表持平
gain_pct < 0 代表回退
```

对于 lower-is-better 指标：

```text
gain_pct = (parent - candidate) / parent * 100
```

对于 higher-is-better 指标：

```text
gain_pct = (candidate - parent) / parent * 100
```

---

## 8. Family State

MVP 只保留六种状态。

| State | 确定性条件 | 默认决策含义 |
|---|---|---|
| `promising` | 最近一次有效试验 improved | 有正向证据，但仍需判断是否还有新机会 |
| `stalled` | 曾 improved，之后连续两次 not_improved | 局部收益可能已经耗尽 |
| `negative` | 至少两次有效试验，且从未 improved | 默认切换方向 |
| `blocked` | 没有有效测量，只有 invalid | 方向未被真正验证 |
| `duplicate` | 最近一次 outcome 为 duplicate | 不应原样重复 |
| `inconclusive` | 其他情况 | 证据不足 |

建议状态计算顺序：

```python
if latest_outcome == "duplicate":
    state = "duplicate"
elif valid_attempts == 0 and invalid_attempts > 0:
    state = "blocked"
elif improved_attempts == 0 and valid_attempts >= 2:
    state = "negative"
elif improved_attempts > 0 and consecutive_non_improving >= 2:
    state = "stalled"
elif latest_valid_outcome == "improved":
    state = "promising"
else:
    state = "inconclusive"
```

第一版将“连续两次”直接硬编码，不新增配置项。

---

## 9. Search Memory 构建方式

### 9.1 每轮全量重建

不实现增量 update。

每次 Proposer 调用前：

```python
memory = build_search_memory(
    history=store.history(),
    metrics_schema=metrics_schema,
    baseline_metrics=baseline_metrics,
    base_sha=parent_sha,
)
```

按 chronological order 扫描 history：

```text
baseline metric
    ↓
round 0 candidates 与 baseline 比较
    ↓
selected candidate 更新 accepted metric
    ↓
round 1 candidates 与新的 accepted parent 比较
    ↓
……
```

### 9.2 为什么不做增量更新

全量重建具有以下优势：

- history 始终是唯一事实来源；
- Search Memory 文件损坏可直接恢复；
- `--continue` 不需要恢复复杂状态；
- 修改分类规则后可重建旧 run；
- 不存在 history 与 memory 更新不一致；
- 数十到数百 round 的 O(n) 扫描成本可以忽略。

### 9.3 文件写入

使用临时文件原子替换：

```python
tmp_path.write_text(serialized)
tmp_path.replace(search_memory_path)
```

---

## 10. Search Memory 与短期反思链的结合

### 10.1 原反思链

```text
Recent history
      ↓
reflection
      ↓
decision
      ↓
proposal
```

### 10.2 新反思链

```text
Long-run Search Memory
      ↓ 长期方向先验
Recent round history
      ↓ 最新增量证据
Current accepted source
      ↓ 当前客观状态
reflection
      ↓
decision
      ↓
proposal
```

Search Memory 不能只是 Prompt 中一段可选参考资料。

必须让 `reflection` 的合法输出依赖它。

---

## 11. Reflection 的新职责

Reflection 不再只是近期历史总结，而是：

> 将长期搜索状态、近期 round 证据和当前源码状态融合成一个搜索判断。

保持现有输出字段：

```json
{
  "reflection": "...",
  "decision": "continue",
  "family": "...",
  "proposal": "..."
}
```

不新增：

```text
long_term_reflection
recent_reflection
memory_reason
revisit_reason
```

避免输出 Schema 膨胀，并确保长期和短期证据形成同一条链。

---

## 12. Reflection Prompt 合同

建议替换为：

```text
- `reflection` (mandatory when prior search memory or recent history exists;
  round 0 may leave it empty):

  At most 1–2 sentences forming one decision chain:

  1. cite the single long-run search-memory entry most relevant to this
     round's choice and state what it establishes about the current or
     proposed family;

  2. combine it with the most relevant recent-round or current-source
     evidence, noting whether that evidence confirms, weakens, or changes
     the long-run conclusion;

  3. conclude whether there remains one concrete worthwhile next experiment
     in the same direction or whether the search should switch.

  Do not separately summarize memory and recent history. The reflection must
  explain how they jointly determine the decision.
```

---

## 13. 强制 Memory 引用

`render_for_proposer()` 给每个 family 分配：

```text
[M1] qpdf_kloop.bin_search_hoist
[M2] pmt_loop.control_flow_specialization
[M3] time_pdf.pointer_resolution
```

当 Search Memory 非空时：

```text
reflection 必须引用一个有效的 [M#]
```

例如：

```json
{
  "reflection": "[M2] records three regressions for per-PMT control-flow specialization, and recent history/current source provides no changed premise or distinct implementation route; the direction is exhausted.",
  "decision": "switch",
  "family": "time_pdf.pointer_resolution",
  "proposal": "..."
}
```

程序进行轻量校验：

```python
if search_memory_exists and not contains_valid_memory_ref(reflection):
    retry_proposer_once()
```

这不是为了验证模型已经深刻理解 Memory，而是防止它完全跳过长期搜索状态。

---

## 14. Long-run Memory 是先验，不是禁令

Search Memory 不能硬性禁止重访旧方向。

状态规则应定义为：

```text
negative:
  默认 switch；
  只有出现 changed premise 或 materially different mechanism 时才重访。

stalled:
  只有存在具体、实质不同的下一次实验时才 continue。

promising:
  不能自动 continue；
  仍需判断是否还有新的可实施机会。

blocked:
  区分“没有验证成功”和“已经证伪”。

duplicate:
  默认不重复；
  除非当前 accepted source 中对应 landed mechanism 已不存在。
```

长期记忆提供 default prior。

近期证据和当前源码可以确认、削弱或推翻该 prior。

---

## 15. Continue / Switch 与 Proposal 的约束

### 15.1 `continue`

当 `decision == continue`：

- 必须与 reflection 中引用的 family 保持一致；
- 必须指出一个具体、实质不同的下一次实验；
- 不能只是把上一轮失败实现换一种措辞；
- 对 stalled/negative family，必须说明 changed premise 或新机制。

### 15.2 `switch`

当 `decision == switch`：

- Proposal 必须选择不同的 target bottleneck 或 optimization mechanism；
- 应说明为什么新方向比继续当前耗尽方向更有价值；
- 新 family 不能只是旧 family 的改名版本；
- 优先选择当前源码中有明确证据、且 Search Memory 尚未覆盖的机会。

Prompt 可加入：

```text
For `switch`, the proposal must identify the new target bottleneck or
optimization mechanism and explain why it is a higher-value unexplored
opportunity than continuing the exhausted family identified in reflection.
```

---

## 16. Proposer Context 顺序

建议最终 Prompt 顺序：

```text
1. Role boundaries
2. Task goal
3. Current accepted base and objective
4. Long-run Search Memory
5. Recent round history
6. Current source inspection instructions
7. Search decision policy
8. Output schema
```

逻辑是：

```text
任务约束
    ↓
当前 accepted 状态
    ↓
长期搜索先验
    ↓
近期证据修正
    ↓
源码检查
    ↓
reflection → decision → proposal
```

---

## 17. Recent History 的职责收缩

加入 Search Memory 后，不再给 Proposer 拼接全部历史。

建议：

```python
recent_history = history[-6:]
```

职责拆分为：

### Recent History

负责：

- 最近具体改了什么；
- 最近一次成功或失败的机制；
- 当前 parent 上还剩什么局部机会；
- 失败来自方向还是具体实现。

### Long-run Search Memory

负责：

- 一个方向总体试过几次；
- 是否成功过；
- 是否已经落地；
- 是否连续失败；
- 当前是 promising、stalled、negative 还是 blocked。

完整历史仍保存在 `history.jsonl`，需要时 Proposer 可通过 commit 引用和 `git show` 自查。

---

## 18. Memory Prompt 渲染格式

不直接把完整 JSON 放入 Prompt。

建议一行一个 family：

```text
[M1] family=qpdf_kloop.bin_search_hoist
state=stalled attempts=3 improved=1 not_improved=2 invalid=0 duplicate=0
best_gain=+16.0% last_gain=-2.6% consecutive_non_improving=2
landed=r3c0@ec27070 last=r5c0@b3e95ce
last_feedback="Passed correctness but regressed against the accepted parent."

[M2] family=pmt_loop.control_flow_specialization
state=negative attempts=3 improved=0 not_improved=3 invalid=0 duplicate=0
best_gain=-4.8% last_gain=-10.1%
last=r18c0@abc1234
last_feedback="Code duplication and instruction-cache cost outweighed branch removal."
```

每个 family 最多保留：

- 一条统计；
- 一条 landed reference；
- 一条 last feedback；
- 一条 last proposal。

文本字段截断至约 160～200 字符。

---

## 19. 无 Objective Metric 时的行为

如果任务没有 metrics schema：

Search Memory 仍可记录：

- attempts；
- landed refs；
- duplicate；
- invalid；
- last feedback；
- last proposal。

但不应使用 Judger score 代替 objective。

此时：

```text
state = unknown
best_gain_pct = null
last_gain_pct = null
```

没有客观指标时，不能可信地计算 promising、stalled 或 negative。

---

## 20. 代码改动范围

### 20.1 新增文件

```text
simpleloop/search_memory.py
simpleloop/tests/test_search_memory.py
runs/<run>/search_memory.json
```

### 20.2 `simpleloop/search_memory.py`

只提供三个纯函数：

```python
def build(
    history: list[dict],
    metrics_schema: dict | None,
    baseline_metrics: dict,
    base_sha: str,
) -> dict:
    ...

def render_for_proposer(memory: dict) -> str:
    ...

def write(path: Path, memory: dict) -> None:
    ...
```

不创建重型 `SearchMemory` class。

---

### 20.3 `loop.py`

Proposer 调用前：

```python
history = store.history()

memory = search_memory.build(
    history=history,
    metrics_schema=metrics_schema,
    baseline_metrics=baseline_metrics,
    base_sha=parent_sha,
)

search_memory.write(
    run_dir_path / "search_memory.json",
    memory,
)

proposal_obj = proposer_mod.propose(
    ...,
    history=history[-6:],
    search_memory=memory,
)
```

round 结束、history 写入后，可再刷新一次 `search_memory.json`，方便实时查看。

---

### 20.4 `proposer.py`

修改：

- 增加 `search_memory` 参数；
- 收紧 `family` 语义；
- Prompt 中增加 Long-run Search Memory；
- 修改 reflection 合同；
- 加入 `[M#]` 引用要求；
- 增加 stalled/negative/blocked/duplicate 的使用规则；
- 对 `switch` 增加新方向价值说明要求。

不新增 Proposal 输出字段。

---

### 20.5 `views.py`

修改：

```python
views.for_proposer(history[-6:])
```

只保留近期完整轨迹。

不再把所有旧 round 的压缩 Proposal 全部塞入 Prompt。

---

### 20.6 不修改

```text
store.py
executor.py
judger.py
gate.py
workspace.py
candidate 执行流程
candidate 选择流程
history.jsonl schema
```

---

## 21. 测试设计

`simpleloop/tests/test_search_memory.py` 至少覆盖：

### 21.1 Same family aggregation

相同 family 跨多个 round 被正确归并。

### 21.2 Direct-parent comparison

Candidate 与所在 round 的 accepted parent 比较，而不是始终与初始 baseline 比较。

### 21.3 Selected lineage

Selected candidate 被加入 `landed_refs`，并正确更新下一 round 的 parent metric。

### 21.4 Stalled state

一个 family 曾 improved，随后连续两次 not_improved，状态变为 `stalled`。

### 21.5 Negative state

一个 family 至少两次有效尝试且从未 improved，状态变为 `negative`。

### 21.6 Duplicate state

`already-implemented` 被归类为 `duplicate`。

### 21.7 Blocked state

只有 invalid 试验时状态为 `blocked`，而不是 `negative`。

### 21.8 Rebuild consistency

相同 history 多次 build 得到完全相同结果。

### 21.9 Reflection memory reference

Search Memory 非空时，reflection 必须包含一个有效 `[M#]`；否则触发一次 Proposer retry。

### 21.10 Recent/long-term separation

Proposer 只接收最近 6 轮详细 history，但 Search Memory 中仍包含更早 family 的聚合状态。

---

## 22. MVP 验收标准

第一版上线后，不要求 Proposal 每轮都提升。

应观察以下指标：

### 22.1 重复 Proposal 率

```text
already-implemented / duplicate candidate 比例
```

应显著下降。

### 22.2 连续无效探索长度

同一 family 连续 not_improved 的最大长度应下降。

### 22.3 Long-run Memory 使用率

当 Search Memory 非空时：

```text
reflection 有效引用 [M#] 的比例 = 100%
```

### 22.4 Reflection 质量

Reflection 应体现：

```text
长期状态
+
近期或当前源码证据
+
continue / switch 判断
```

而不是只复述上一轮。

### 22.5 新方向质量

`switch` 后的新 Proposal 应明确：

- 新 target 或 mechanism；
- 为什么比继续旧方向更值得尝试；
- 与历史 family 的区别。

### 22.6 可恢复性

删除 `search_memory.json` 后，可以仅通过 `history.jsonl` 无损重建。

---

## 23. 暂不实现但预留的后续能力

只有当 MVP 的真实长 run 暴露新问题时，再考虑：

1. **轻量 semantic novelty check**  
   解决 Proposer 通过改 family 名绕过旧记忆的问题。

2. **结构化 changed premise**  
   当重访 stalled/negative family 频繁出现时，再新增字段。

3. **自由形式 lesson consolidation**  
   当 family 统计不足以支持复杂跨方向归纳时引入。

4. **主动 observation / profiling**  
   当长期记忆只能整理旧猜测、无法发现新瓶颈时引入。

5. **跨 run memory transfer**  
   当单 run 内 Search Memory 稳定后再考虑。

6. **Memory evaluation framework**  
   对 write、state、render、reflection 使用分别做消融实验。

---

## 24. 最终设计原则

Long-run Search Memory 的核心不是保存更多历史，而是维护一个有界的搜索状态。

每一轮试验至少应产生一种价值：

```text
要么改善 objective；
要么确认一个有效机制；
要么证伪一个具体假设；
要么缩小下一轮搜索空间。
```

最终反思链为：

```text
Long-run Search Memory
        +
Recent round evidence
        +
Current accepted source
        ↓
reflection
        ↓
continue / switch
        ↓
next proposal
```

一句话总结：

> SimpleLoop 最简有效的 Long-run Search Memory，是以 `family` 为稳定假设主键，从 `history.jsonl` 中确定性聚合长期试错结果，并强制 reflection 将该长期先验与近期证据结合后，再导出 decision 和 proposal。
