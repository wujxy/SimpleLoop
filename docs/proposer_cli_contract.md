# Proposer CLI Contract（草案 v0）

## 0. 文档定位

本文档规定 **Proposer CLI** 与 **Harness（Host）** 之间的接口契约。

它是 [simpleloop_rsi_design.md](simpleloop_rsi_design.md) 的工程落点：把 RSI 设计里的
"Stable Host / Evolvable Runtime 边界"（§13、§24）和 "Immutable Kernel / Evolvable Self"
（§13）落成一份**可执行、可冻结的进程间契约**。

核心思路（来自讨论结论）：把 proposer 从 `simpleloop` 包里**提取成一个独立 CLI**，
效果类似 `claude -p`——Harness 负责 *输入 goal、创建 workspace、调用 proposer、接收 proposal*；
proposer 自身（它的源码、prompt、工具、context 构造、连续性语义）就是**唯一可被自修改的对象**。
goal（输入）和 proposals（输出）不动，模块化天然成立。

> **状态：草案。** 标 `🔒 FROZEN` 的是契约本身（属 Immutable Kernel，proposer 自修改**不可触碰**）；
> 标 `🟢 EVOLVABLE` 的是 proposer 自修改**可改**的部分。本文档自身是 Kernel。

---

## 1. 一句话模型

```
   Harness (Host)                          Proposer CLI (Evolvable Self)
   ─────────────                           ─────────────────────────────
   goal ──┐                                (读 run_dir 冻结格式)
   workspace ─┐                            (写 run_dir/proposer/ 自己的历史)
   round ctx ─┤── proposer-run ──┐  ┌──▶  agentic loop (自研控制环)
              │                  └──┤       │
              │                     │       └─▶ 多轮研究 (tools, probes)
              │                     │       └─▶ 连续性 (session.jsonl + notebook)
              │                     │       └─▶ 调用 model (外部)
              │                     │
              ◀── stdout JSON ───────┘   structured_output = proposals | self_decision
   resolve finding_id, run executor,
   eval, gate, select, record
```

- Host 是**长生命周期进程**，永不需要因 self-change 而重启——每次 spawn proposer 都从磁盘 fresh load。
- self transition = "下次 spawn 时从 self repo 读到新 revision"。进程边界免费送了 late-binding。
- Viability = 一次黑盒子 subprocess 测试（`proposer-run --check`）。

---

## 2. 所有权边界（契约的心脏）

### 2.1 🔒 Host 拥有（Immutable Kernel —— proposer 不可改）

Host 拥有**现实与评价规则**，proposer 不能改、不能重写：

| 类别 | 内容 | 当前代码 / 落点 |
|---|---|---|
| 终极锚点 | Original Goal | config |
| 评价规则 | evaluator / gates / metrics / objective | [harness/gate.py](../simpleloop/harness/gate.py), [evals.py](../simpleloop/harness/evals.py) |
| **权威任务事实** | **任务 outcome 账本——proposer 唯一不能重写的任务事实（RSI §5.2）** | [harness/store.py](../simpleloop/harness/store.py) → `run_dir/history.jsonl` |
| self 判断账本 | self-review ledger（Host 用 proposer 的 decision + viability 结果 append；proposer 只读） | (待建) `run_dir/self/reviews.jsonl` |
| 生命周期与权威 | self repo snapshot/transition、viability authority、mode switch、commitment scheduler | (待建，入 loop.py) |
| workspace 构造 | clone/worktree/mount | [harness/workspace.py](../simpleloop/harness/workspace.py), [container/runtime.py](../simpleloop/container/runtime.py) |
| **本契约** | args、stdout schema、磁盘布局、protocol version | 本文档 |

> **Host 不再拥有 findings / notebook / session / 检索**——这些是 Scientist 自己的研究笔记与认知机制（见 2.2）。Host 的任务数据权威面**收缩到只有 `history.jsonl`**。

### 2.2 🟢 Proposer 拥有（Evolvable Self —— 可自修改）

proposer 拥有**自己的全部认知与研究记忆**——不仅 prompt/工具，连它怎么记笔记、怎么检索、怎么把实验结果归因到自己的研究问题，都是它自己的能力：

| 类别 | 内容 | 当前代码（迁入 self repo） |
|---|---|---|
| 控制环 | agentic loop、action 解析/分发、guard、compact、suspension | [roles/proposer.py](../simpleloop/roles/proposer.py), [research_agent.py](../simpleloop/roles/research_agent.py) |
| 连续性机制 | session/notebook 的读写语义 | [roles/scientist_session.py](../simpleloop/roles/scientist_session.py) |
| **研究记忆（全部）** | **findings ledger、frontier、检索策略、context 构造、experiment-index 投影** | [memory/finding_store.py](../simpleloop/memory/finding_store.py), [frontier.py](../simpleloop/memory/frontier.py), [retrieval.py](../simpleloop/memory/retrieval.py), [context.py](../simpleloop/memory/context.py), [experiment_index.py](../simpleloop/memory/experiment_index.py) |
| **finding 生命周期** | **finding 的创建/去重/与实验的归因——全部 proposer 内部账** | [memory/service.py](../simpleloop/memory/service.py) `resolve_targets`/`link_completed_experiments` → 迁入 |
| 身份内容 | charter、suspend prompt、protocol 文本 | [prompts/proposer.md](../simpleloop/prompts/proposer.md) + 内联文本 |
| 工具 | research tools、memory tools、descriptions | [roles/research_tools.py](../simpleloop/roles/research_tools.py) |
| model 调用 | 如何调 model、用哪个 model（策略） | proposer 内部 |

### 2.3 `memory/` 整体迁出 Host

讨论结论：**写账本/备忘录是 proposer 的能力**。proposer 与 memory 交互的那一整块从 Host 拉出来打包进 proposer。

- Host 的存储面**只剩 `harness/store.py`（`history.jsonl`）**——权威任务事实。
- `simpleloop/memory/` 整包（findings/frontier/retrieval/context/experiment_index/service/models）**迁入 proposer**；`experiment_index` 是对 `history.jsonl` 的**只读投影**（proposer 读 Kernel 数据、自己投影）。

### 2.4 三层模型（由此变干净）

| 层 | 是什么 | 谁写 | 跨 self revision |
|---|---|---|---|
| **Reality（现实）** | `history.jsonl` + goal/gates/evaluator + 契约 | Host | 永久（Kernel） |
| **Autobiography（自传）** | `run_dir/proposer/{findings,notebook,session,traces}`——Scientist 的笔记与经历 | proposer | **持久累积、跨 S0→S1 不重置**（这即"身份"，RSI §20） |
| **Body（躯体）** | `run_dir/self/repo @ S(n)`——proposer 的源码实现 | self-executor（Host 编排） | 每次 self-change 切换 revision |

### 2.5 关键不变量：归因从现实派生（join-from-history）

findings 里"某 finding 在 r3 被试、r5 被选、best_objective=0.42"这类**实验归因必须每轮从 `history.jsonl` 重新派生（只读 join），不得作为权威存进 findings**。

- 现状 [loop.py `link_completed_experiments`](../simpleloop/loop.py)（Host 把归因写进 findings）**取消**——proposer 读 history 自己 join。
- 这样 Scientist 的笔记**永远无法与权威结果漂移**（RSI §5.2：Scientist 可解释结果，但不能用 narrative 改写结果）。
- `history.jsonl` 是**不可摧毁的脊柱**：即便 findings 被自改破坏，笔记可从现实 + workspace 重建。

---

## 3. 调用契约（🔒 FROZEN）

### 3.1 入口点与 spawn 机制

- **机制澄清（盘库确认）**：proposer-CLI **不是** `claude -p`。它是一个 Python 程序——HTTP 调模型（[model.py](../simpleloop/roles/model.py) HepAI/Zhipu，OpenAI 兼容）+ 离线 Apptainer 跑研究 probe（[research_tools.py](../simpleloop/roles/research_tools.py)）。所以 CLI 本体**需要网络**（跑 host 或联网容器），只有它的 probe 进离线 Apptainer。这与 executor（claude-p 进容器）**不同**——adapter 要照搬 [proposer_lane_worker.py](../simpleloop/proposer_lane_worker.py) 的部署形态，不是照搬 [agent.py](../simpleloop/roles/agent.py) 的容器化。
- "like claude -p" 只对**调用形态**：Host spawn 子进程、传输入、收 stdout JSON / `result.json`。HEPJob 路径已子进程化，**LOCAL 路径待转换**。
- **入口路径冻结**：v0/S2 = `python -m simpleloop.proposer_run`（从 `proposer_lane_worker.main` 泛化）；RSI 期 = `run_dir/self/repo` 内固定相对入口。Host 只认"入口 + args + 输出 schema"。
- **没有 installed launcher**：launcher 本身也是 body，整体可演化。

### 3.2 参数（🔒 FROZEN）

```
proposer-run \
  --contract-version proposer-cli-v0     # 契约版本，Host 与 proposer 必须一致
  --run-dir <path>        # Host 的 run 目录（读 outcomes、写自己的历史）
  --workspace <path>      # 研究工作目录（cwd）：task=lane worktree；self=self 源码 scratch
  --round-id <int>        # 当前轮次
  --base-sha <sha>        # 当前 accepted task revision
  --mode <task|self>      # 研究对象
  --goal <text|->         # Original Goal（主指令）；`-` 表示从 stdin 读（大 goal 时）
  [--self-repo <path>]    # proposer 自身源码根（self 审视/自我研究用）
  [--goal-file <path>]    # goal 过大时替代 --goal
  [--max-proposals <int>] # 可选 proposal 上限（缺省从 resolved_config 读）
  [--check]               # viability 自检模式（不做研究，见 §8）
```

- `--goal` 是**主输入**，刻意显式（对应"harness 输入 goal"的心智模型）。
- gates / editable / metrics / objective：proposer **从 `run_dir/resolved_config.json` 读**（Host 已持久化，见 [loop.py `_write_config_snapshot`](../simpleloop/loop.py)）。config 的 schema 是冻结的 Kernel。
- 其余研究对象信息（上一轮 outcomes、findings、self-review 史）proposer 从 run_dir 读（见 §5）。

### 3.3 环境变量（🔒 机制 FROZEN；🟢 model 选择策略 EVOLVABLE）

- model 端点与凭证由 Host 经 runtime 注入 env（与 executor 同）：
  `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY`。
- `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 等由 Host 按配置注入。
- **用哪个 model**：v0 由 config 决定（Host 经 env / `--model` 传）；proposer 在多 model 配置下**可选**（策略可演化，v0 单 model）。
- 认证/端点本身永远 Host 控制（Kernel）—— proposer 不能绕过去连别的端点。

### 3.4 文件世界（由 Host 经 mount map 构造，🔒）

> 与 executor 完全同构："proposer 不该碰的东西根本不在它的容器里"（见 [executor.py:86-90](../simpleloop/roles/executor.py)）。

**task mode**：
| 路径 | 权限 | 用途 |
|---|---|---|
| `--workspace`（lane worktree @ base_sha） | 读写（scratch） | 探查/临时改 task 代码、跑 probe（不提交，Host 不 commit 这里） |
| `--run-dir/resolved_config.json` | 读 | goal/gates/editable/metrics |
| `--run-dir/history.jsonl` | 读 | 任务 outcomes（构造 world event） |
| `--run-dir/memory/findings.jsonl` | 读 | 研究记忆 |
| `--run-dir/proposer/` | 读写 | proposer 自己的连续性 + trace |
| `--self-repo` | 读 | 自我源码（task mode 下一般不研究，但可读） |

**self mode**：
| 路径 | 权限 | 用途 |
|---|---|---|
| `--workspace`（self-repo @ incumbent SHA 的 scratch checkout） | 读写（scratch） | 探查/跑 probe 自己的源码（这里的改动≠self-change） |
| `--run-dir/history.jsonl` | 读 | **任务 outcomes——判断自己是否瓶颈的依据** |
| `--run-dir/self/reviews.jsonl` | 读 | 自我判断的 provenance（§15） |
| `--run-dir/proposer/` | 读写 | 连续性 |
| `--self-repo` | 读 | incumbent self 源码（研究对象） |

> self mode 下真正的 self-change 由 **self-executor**（另一个 `claude`/codex 调用，Host 驱动）在 self-repo 的 worktree 里完成，不在本契约内（见 §9）。

### 3.5 stdin

- 一般**不用**。proposer 是自驱动（自己从 goal+charter+world+notebook 拼指令）。
- 保留：`--goal -` 时从 stdin 读 goal 文本。

---

## 4. 输出契约（🔒 FROZEN）

### 4.1 stdout JSON envelope（与 `claude --output-format json` 同构）

Host 用现有 [`_decode_output`](../simpleloop/roles/agent.py) 机制解析：

```json
{
  "result": "<proposer 的可选自然文本（message），仅供离线查看>",
  "structured_output": { ... 见 4.2/4.3 ... },
  "usage": { ... token 计费，回传 telemetry ... }
}
```

- 非 0 退出码 = proposer 崩溃 → Host 记为该轮 proposer 失败（与现状一致）。
- `structured_output` 不在 / 不合法 → 视为协议违规（round 失败，不静默）。

### 4.2 task mode `structured_output`（🔒）

```json
{
  "contract_version": "proposer-cli-v0",
  "round_id": 5,
  "base_sha": "abc12345",
  "mode": "task",
  "scientist_prompt_version": "scientist-v3",
  "abstained": false,
  "abstain_reason": null,
  "abstain_blocking_unknown": null,
  "proposals": [
    {
      "instruction": "尝试 X，因为 ……（WHAT + WHY，不是行级计划）",
      "evidence_refs": ["src/foo.cc:120", "experiment r3-c1"],
      "material_difference": "相对 incumbent 的实质变化"
    }
  ]
}
```

- `abstained=true` 时 `proposals=[]` + 填 `abstain_reason`（Scientist 判断没有值得其执行成本的实验）。
- **没有 `research_target` / `finding_id`**：finding 的创建/去重/归因是 proposer 自己的内部账（见 §2.2/§2.5），不进 Host 契约。Host 只消费 `instruction`（给 executor）；`evidence_refs`/`material_difference` 仅作 handoff provenance 记录。Host 侧的 `resolve_targets`/`link_completed_experiments` 取消。
- `scientist_prompt_version` 由 proposer 盖戳（可观测当前 self 用的 prompt 版本）。

### 4.3 self mode `structured_output`（🔒）

```json
{
  "contract_version": "proposer-cli-v0",
  "round_id": 12,
  "base_sha": "abc12345",
  "mode": "self",
  "incumbent_self_sha": "<self-repo git sha>",
  "self_decision": "CHANGE",
  "diagnosis": "为什么当前 self 限制了 goal progress（或为什么不）",
  "keep_reason": null,
  "next_review_after_rounds": null,
  "self_change": {
    "target": "prompt | context | tools | runtime | retrieval | model-policy",
    "intent": "希望的能力变化（WHY/WHAT，给 self-executor 的方向，非行级）",
    "instruction": "改哪里、为什么、希望变成什么",
    "evidence_refs": ["proposer/cli.py:910", "trace r8-r12"]
  }
}
```

- `self_decision ∈ {KEEP, CHANGE}`：
  - **KEEP**：填 `keep_reason` + `next_review_after_rounds`（commitment，"再过 N 轮 task 才值得重评"）。
  - **CHANGE**：填 `self_change`（diagnosis 来自 RSI §6/§7：先问"progress 是否足够"，不预设 self 有问题）。
- viability 结果、是否 adopt、candidate sha、最终 next_review_round —— **由 Host 在跑完 self-executor + viability 后写入账本**（见 §9），不在此输出里。

---

## 5. 磁盘契约

### 5.1 proposer 读（🔒 格式 FROZEN —— 这些是 Kernel 数据）

| 路径 | 含义 | 冻结的 schema 来源 |
|---|---|---|
| `run_dir/resolved_config.json` | goal/gates/editable/metrics/roles | [config.py](../simpleloop/config.py) 的 resolved config |
| `run_dir/history.jsonl` | 任务 outcome 账本（每行一代，proposer 唯一只读的权威任务事实） | [store.py `append_generation`](../simpleloop/harness/store.py) 的 record |
| `run_dir/self/reviews.jsonl` | self-review 账本（self mode） | §9.2 |

> `experiment_index` 是 `history.jsonl` 的**投影**（[experiment_index.py:3](../simpleloop/memory/experiment_index.py)），不是独立存储——proposer 可自行投影。

### 5.2 proposer 写（🔒 位置 FROZEN；🟢 语义 EVOLVABLE）

proposer 把自己的历史写到 **proposer 拥有的子目录**（"历史由它自己写到 run 目录"）：

| 路径 | 含义 | 备注 |
|---|---|---|
| `run_dir/proposer/findings.jsonl` | 研究记忆（Scientist 自己的结构化笔记 + frontier） | 位置冻结；schema v0 冻结骨架、可演化；归因从 history join（§2.5） |
| `run_dir/proposer/session.jsonl` | 轨迹归档（append-only） | 位置冻结；写入/读取/tail 语义可演化 |
| `run_dir/proposer/notebook.md` | 自传式备忘（suspension 时重写） | 位置冻结；语义可演化 |
| `run_dir/proposer/meta.json` | scientist_id、prompt_version、contract_version | 位置冻结 |
| `run_dir/proposer/traces/r{round_id}.json` | 审议轨迹（离线分析） | 位置冻结；内容可演化 |

> proposer **不写** `history.jsonl`（Kernel 权威事实）与 `reviews.jsonl`（Host append）。它写自己的 `run_dir/proposer/`（findings/notebook/session/traces）——这是它自己的笔记与经历，自己拥有。

---

## 6. 连续性 & 身份契约

- **身份 = 磁盘 state**（§20：version 边界不是身份边界）：`run_dir/proposer/{session.jsonl, notebook.md, meta.json}`。
- 每次 spawn = 一次冷启，proposer 从这些文件**重建 trajectory**（§24："continuity 属 run-level persistent state 而非进程内存"）。CLI 化把这条不变量从纪律变成编译期事实。
- **v0 schema 冻结**（§14）：session.jsonl 的行 shape、notebook 是 utf-8 文本——这些在 v0 不可改，否则旧 notebook 在 evolved proposer 里读不出来（backward-compat）。语义（怎么用 notebook、tail 几轮）可演化。
- scientist_id 在 run 内稳定（跨 self revision 不变）；self revision（S0→S1）由 Host 按 self-repo git sha 追踪，proposer 不需要知道自己的"S 编号"。

---

## 7. 模式契约

- **同一份调用契约**，mode 只影响：cwd/mount（§3.4）、proposer 内部加载哪份 charter（proposer 自己按 `--mode` 从自己源码加载，charter 本身可演化）。
- **mode switch 由 Host 拥有**（RSI §17）：Host 持 `next_review_at_round` commitment，到期则该轮以 self mode 调 proposer。proposer 不主动请求进入 self mode（防止逃避 self-review）。
- task mode 里 Scientist 仍可在 charter 下"意识到自己值得立即重审"——但**兑现成 self mode 仍是 Host 的时钟**（proposer 只能影响 commitment，不能直接切换）。

---

## 8. Viability 检查契约（🔒）

每次 self-change 产出候选 S(n+1) 后，Host 在**候选源码**上跑：

```
proposer-run --check --contract-version proposer-cli-v0 \
             --self-repo <candidate_checkout> --run-dir <run_dir>
```

必须断言（退出 0 = viable）：
1. **入口能 boot**：import/语法正确，entrypoint 可启动。
2. **能加载既有连续性**：读 `run_dir/proposer/session.jsonl` + `notebook.md` 不崩（backward-compat）。
3. **协议可产出**：能在**不调 model** 的前提下 emit 一个合法的 `structured_output` 骨架（空 proposals 或合法 abstain）——证明控制环 + 输出 schema 没坏。
4. **契约版本一致**：`contract_version == proposer-cli-v0`。
5.（可选）一次极小 model smoke：发一个 canned goal，断言返回合法结构。

- 退出非 0 + stderr = broken self，**没有资格成为 active self**（RSI §19 immediate viability）。
- `--check` 必须快、尽量确定性（核心断言不依赖网络/model）。
- capability（"是否真的更强"）**不在 viability 范围**——那是后续真实 task progress 的事（RSI §19 delayed）。

---

## 9. Self-review 输出与账本

### 9.1 流程（Host 编排，🔒）

```
self mode spawn (proposer 诊断)
   └─▶ self_decision
         ├─ KEEP  ─▶ Host 写账本 {decision:KEEP, keep_reason, next_review_round} ─▶ 继续 task
         └─ CHANGE ─▶ Host 在 self-repo@incumbent 开 worktree
                      └─▶ self-executor (claude/codex) 按 self_change.instruction 改源码
                           └─▶ 候选 S(n+1) sha
                                └─▶ proposer-run --check (viability)
                                      ├─ 失败 ─▶ 拒绝，账本记 {viable:false, adopted:false}，保留 incumbent
                                      └─ 通过 ─▶ adopt：下次 spawn 用 S(n+1)；账本记 {viable:true, adopted:true, next_review_round}
```

- **self-executor 是独立的 `claude`/codex 调用**（像 task executor），只改 self-repo 的 **🟢 Evolvable 路径**（§2.2）；🔒 Kernel 路径不在它的可写世界（与 executor 的 mount containment 同构）。
- **WHY/WHAT 来自 proposer；HOW 来自 self-executor**（RSI §11 角色边界不变）。

### 9.2 self-review 账本（🔒 Host 拥有；proposer 只读）

`run_dir/self/reviews.jsonl`，每行一条：

```json
{
  "round": 12,
  "incumbent_self_sha": "...",
  "decision": "CHANGE",
  "diagnosis": "...",
  "change": {"target","intent","instruction"},
  "candidate_self_sha": "...",
  "viable": true,
  "adopted": true,
  "next_review_round": 17
}
```

- **proposer 在 self mode 读它**（RSI §15：autobiographical provenance——继承"过去为何这么判断"的理由，不继承结论）。
- **proposer 不能写它**——Host 用 proposer 的 `self_decision` + viability 结果**组装并 append**（防止 proposer 改写自己的历史，RSI §5.2/§13）。

---

## 10. 遥测

- `usage` 在 stdout envelope 里回传（与 claude/executor 一致），Host 经 `telemetry.record_usage` 入账。
- proposer 的 `traces/r{id}.json` 是它自己的审议轨迹（离线分析），Host 不注入未来轮（与现状 `_write_proposer_trace` 语义一致）。

---

## 11. 契约版本与演化（🔒）

- `PROPOSER_CLI_CONTRACT_VERSION = "proposer-cli-v0"`：Host 与 proposer 必须一致。
- proposer 在 `structured_output` 回显 `contract_version`；viability 断言一致。
- **改契约 = 改 Kernel**：是人工/upstream 行为，**不是** proposer 自修改的自然副作用。一个产出"我需要新 contract 字段"的 self-change → viability 失败（Host 只讲 v0）。
- 这就是 RSI §13"能改自己，不能改评价规则"在**接口层**的落实。

---

## 12. 明确不在自修改范围内

proposer 自修改**触碰不到**（无论怎么演化）：
- Goal、evaluator、gates、metrics；
- `history.jsonl` / `findings.jsonl` / `reviews.jsonl` 的**格式与写入权**；
- 本契约（args、stdout schema、磁盘布局、contract version）；
- run_dir 的目录布局；
- Host 的 round 编排、mode switch、commitment scheduler、transition/viability authority；
- executor 侧（task executor 与 self-executor 的调用）。

> 如果 Scientist 诊断出的瓶颈落在以上任何一处，**self-modification 修不了**——那是人/upstream 的事。这是预期的 Kernel 边界，不是缺陷。

---

## 13. 与当前代码的映射

**迁入 `run_dir/self/repo`（成为 proposer-CLI body，🟢 可演化）：**
- [roles/proposer.py](../simpleloop/roles/proposer.py)（ScientistAgent / 控制环 / `_build_*` / `_SUSPEND_PROMPT` / parse/dispatch/guard/compact/suspension）
- [roles/research_agent.py](../simpleloop/roles/research_agent.py)、[roles/scientist_session.py](../simpleloop/roles/scientist_session.py)、[roles/research_tools.py](../simpleloop/roles/research_tools.py)
- [prompts/proposer.md](../simpleloop/prompts/proposer.md) + 内联 prompt 文本
- [memory/](../simpleloop/memory/) **整包**（context/retrieval/finding_store/frontier/experiment_index/service/models）——proposer 的全部研究记忆与检索
- 新增一个薄 `proposer/cli.py` 入口（解析 §3 args、读 history.jsonl 做归因 join、跑控制环、emit §4 envelope）

**留在 Host（🔒 Kernel）：**
- [loop.py](../simpleloop/loop.py)（round 编排）+ 新增：proposer-CLI 调用 adapter（镜像 [`Agent`](../simpleloop/roles/agent.py)）、self-repo lifecycle（snapshot proposer 源码→`run_dir/self/repo`）、viability runner、reviews.jsonl writer、mode/commitment scheduler、transition authority
- [harness/*](../simpleloop/harness/)（store/gate/evals/workspace/handoff/export/views）、[container/*](../simpleloop/container/)、[execution/*](../simpleloop/execution/)、[roles/agent.py](../simpleloop/roles/agent.py)、[roles/executor.py](../simpleloop/roles/executor.py)、[config.py](../simpleloop/config.py)、[processes.py](../simpleloop/processes.py)、[cli.py](../simpleloop/cli.py)
- Host 的存储面**只剩 [harness/store.py](../simpleloop/harness/store.py)（`history.jsonl`）**；`memory/` 整包迁入 proposer（见上）

---

## 14. v0 冻结清单（首批 self-revision 的安全围栏）

为以最小风险验证整个 RSI 环路（trigger→self world→诊断→change→viability→adopt→真实 task 评估），**v0 额外冻结**：

1. **认知基底（连续性 + 研究记忆）**：`session.jsonl` 行 schema、`notebook.md`=utf-8 文本、`findings.jsonl` 骨架 schema、world-event 的磁盘来源——v0 不可改。注意：现在 **所有** Scientist 记忆都 proposer-owned，一个坏 self-change 可能破坏它的全部笔记；`history.jsonl` 是不可摧毁脊柱（§2.5，可重建），`--check` 必须断言能加载旧 notes（backward-compat），self-repo git 提供**代码**回滚。
2. **内部 action 协议可演化，但输出 schema 冻结**：proposer 内部多轮用的 message/action JSON 是它自己的事（Host 看不见）；只有 §4 的 `structured_output` 冻结。
3. **建议首批 self-revision 只动 prompt + context 构造旋钮**（charter / suspend / 怎么读 run_dir / 怎么拼 system prompt），不动 `proposer/cli.py` 的可执行控制环—— viability 平凡安全（runtime 不变，只是加载的文本变）。验证环路成立后再放宽到可执行代码。

> v0 验证目标不是"有没有改进 goal"（那是 delayed capability，多轮才读得出），而是**反自我辩护的 falsification**：健康的 RSI 必须产出真实比例的 `KEEP`。若每次 self-review 都 CHANGE，即 self-review 已塌缩成"总得改点什么"的 churn——从第 1 个 run 就监控 KEEP/CHANGE 比。

---

## 15. 开放问题（实现前需定）

1. **self-repo 的 seed 范围**：snapshot 整个 `simpleloop/roles+prompts+memory(context,retrieval)` 还是更小集？影响 self-executor 的可改面与 viability 复杂度。
2. **多 lane**：现状 proposer 跑在 lane workspace（[workspace.py `add_lane_workspace`](../simpleloop/harness/workspace.py)）。CLI 化后多 lane = 多个 proposer-run spawn？session/notebook 是否 per-lane？v0 建议**单 lane** 先跑通。
3. **`--check` 的 model 依赖度**：核心断言（boot+连续性加载+无 model 协议产出）能否完全离线？决定 viability 的成本与确定性。
4. **goal 过大**：`--goal-file` 是否进 v0 契约，还是先只支持 `--goal` arg。
5. **memory 检索迁移**：proposer 侧重写 retrieval（读 findings.jsonl）还是 Host 暴露一个只读检索服务？倾向前者（thin contract + 可演化），但要确认 retrieval 的依赖（embedding 等）能进容器。
6. **contract version 协商**：v0 直接"必须相等"，还是允许 proposer 声明它支持的版本集合？
7. **数据跨 self-revision 的向后兼容**：代码回滚容易（self-repo git SHA）。但若 S2 把 findings 写成新格式、再回滚到 S1，S1 读得回 S2 的 notes 吗？v0 缓解：findings/notebook/session **骨架 schema 冻结**（演化"怎么用"，不演化 schema）；或 proposer 自己拥有带冻结 v0 baseline 的迁移逻辑。需定。
