# Proposer CLI 搬运清单（施工图）

> 配套 [proposer_cli_contract.md](proposer_cli_contract.md)。本文是 step 1 产物：把"提取 proposer 成独立 CLI"落成**符号级搬运表 + 依赖切断点 + Host 侧新增**，可直接据施工。所有行号基于当前 working tree。

---

## 1. 执行模型（从代码确认的 ground truth）

盘库得到三个决定性事实，修正了之前契约里的假设：

1. **proposer 不是 `claude -p`。** Scientist 的 LLM 调用是 [research_agent.py:260](../simpleloop/roles/research_agent.py#L260) `self.model.complete(...)`，走 [model.py](../simpleloop/roles/model.py) 的 `HepAIChatModel`/`ZhipuChatModel`（OpenAI 兼容 HTTP API，调 GLM）。只有它的**研究 shell probe** 才进 Apptainer（离线，[research_tools.py:159](../simpleloop/roles/research_tools.py#L159)）。所以 proposer-CLI = **一个 Python 程序**（HTTP 调模型 + 离线 Apptainer 跑 probe）。"like claude -p" 只对**调用形态**（Host spawn 子进程、传输入、收 JSON），不对机制。

2. **HEPJob 路径已经完成了子进程化。** [hepjob.py:811](../simpleloop/execution/hepjob.py#L811) 以 `python -m simpleloop.proposer_lane_worker --manifest ...` 提交 condor job；[proposer_lane_worker.py:247](../simpleloop/proposer_lane_worker.py#L247) `main()` 读 manifest → 从 resolved config 重建 model/runtime/MemoryService → `run_lane_episode` → 写 `result.json`。**这就是目标 CLI 的原型。** 只有 LOCAL 路径是 in-process（[local.py:36](../simpleloop/execution/local.py#L36) 直接调 `ctx.proposer_agent.run`）。

3. **Host↔proposer 的契约面已经收敛成一个方法**：[base.py:71](../simpleloop/execution/base.py#L71) `run_proposer_lanes(round_id, base_sha) -> ProposerResult`。两个后端都只实现它，loop 只调它。两个入口（`ProposerOrchestrator.run` / `run_lane_episode`）的 **16 个 kwargs 形状一致**——这已经是 CLI 输入规范。

> **含义**：提取不是"重新发明"，而是 (a) 让 LOCAL 路径也走子进程，(b) 把 `proposer_lane_worker` 泛化成两个后端共用的 proposer-CLI，(c) 把 proposer+memory 源码从 `simpleloop` 包里迁出成独立包，(d) 切断 5 个 Host 依赖。HEPJob 已经替我们验证了 (a)/(b) 的形态。

---

## 2. Kernel 数据边界（盘库确认）

| 磁盘 artifact | 当前位置 | 谁写 | 去向 | 备注 |
|---|---|---|---|---|
| `history.jsonl` | `run_dir/history.jsonl` | **Host**（[store.py `append_generation`](../simpleloop/harness/store.py)） | **留 Host** | 唯一权威任务事实；proposer 只读 |
| `findings.jsonl` | `run_dir/memory/findings.jsonl` | proposer（[finding_store.py](../simpleloop/memory/finding_store.py)） | **迁 proposer**（→ `run_dir/proposer/findings.jsonl`） | Scientist 自己的笔记 |
| `session.jsonl`/`notebook.md`/`meta.json` | `run_dir/scientists/lane-<N>/`（[scientist_session.py:75](../simpleloop/roles/scientist_session.py#L75)） | proposer | **迁 proposer**（→ `run_dir/proposer/`） | 单 lane ⇒ 就是 `scientists/lane-0/`，v0 直接整体搬到 `proposer/` |
| `experiment_index` | （非独立文件） | — | **迁 proposer** | [experiment_index.py](../simpleloop/memory/experiment_index.py) 是对 `list[dict]` 的**纯投影**，无 I/O；proposer 读 history 后自己投影 |
| `proposer_traces/r{id}.json`、`proposals.json` | `run_dir/...` | Host（[loop.py:587/607](../simpleloop/loop.py)） | 留 Host（handoff/离线分析） | Host 侧簿记，不进 proposer |

**`finding_id` 从 history.jsonl 淘汰**：现状 [store.py:89](../simpleloop/harness/store.py) 把 `cand["finding_id"]` 写进 history（Host 解析自 `resolve_targets`）。提取后 Host 不再 resolve findings ⇒ history 的 `finding_id` 变 null/缺省。proposer 用 **(round_id, candidate index)** 作 join key 把自己的 findings 关联到 history 结果（候选顺序 = 提案顺序，稳定）。

---

## 3. 符号级搬运表（核心施工图）

### 3.1 🟢 迁入 proposer-CLI body（整包搬）

| 当前文件 | 关键符号（行） | 去向 | 切断的 Host 依赖 |
|---|---|---|---|
| [roles/proposer.py](../simpleloop/roles/proposer.py) | `ScientistAgent`(823) `.research`(891) `._suspension_checkpoint`(1094)；`_build_system_prompt`(413) `_build_world_event`(471) `_fmt_metrics`(459)；`_SUSPEND_PROMPT`(391) `_PROTOCOL_BLOCK`(323) `_TOOL_BLOCK`(318) `_RUNTIME_BOUNDARIES`(355) `_COLD_START`(375)；`parse_response`(783) `_dispatch`(680) `_parse_proposal`(651)；`ProposerResult`(71) `ScientistRound`(90) `ProposerError`(67)；`ContextPolicy`(142) `SCIENTIST_PROMPT_VERSION`(116, 现为 **scientist-v3**)；compact 系 `_compact_live_messages`(279) `_cap_tail`(233) | proposer 核心 | `..container.runtime`(类型提示)、`..memory.context`、`..memory.models`、`..prompts.load_semantic`（随迁） |
| [roles/orchestrator.py](../simpleloop/roles/orchestrator.py) | `ProposerOrchestrator`(56) `.run`(86) `.run_lane_episode`(159) `._run_lane`(190)；`LaneResult`(33) | **溶解进 CLI main()**（单 lane ⇒ CLI 即一个 lane；session 生命周期归 scientist_session） | `..container.runtime` |
| [roles/research_agent.py](../simpleloop/roles/research_agent.py) | `ResearchAgent`(208) `._step`(247, **model 调用点**)；`WorkingState`(32)；`AgentError`(26)；helpers `_bump` `_fingerprint` `_register_evidence` `_source_path_exists` `_build_telemetry` `_build_trace` | proposer 基类 | `..container.runtime`(类型提示) |
| [roles/research_tools.py](../simpleloop/roles/research_tools.py) | `ResearchTools`(316) `.execute`(353)；`ResearchCommandRunner`(133) `.run`(159, 离线 Apptainer probe)；`RESEARCH_TOOL_SPECS`(36) `MEMORY_TOOL_ACTIONS`(117) `render_research_tool_prompt`(126) | proposer 工具层 | `..container.runtime.MountMap`(类型提示)、**`..processes.CHILD_PROCESSES`**(运行时) |
| [roles/scientist_session.py](../simpleloop/roles/scientist_session.py) | `ScientistSession`(37) `.load_or_create`(65) `.append_message`(143) `.write_notebook`(158) `.save_meta`(164) `.tail_turns`(175) | proposer 连续性（**最易搬**，纯 stdlib） | 无 |
| [roles/model.py](../simpleloop/roles/model.py) | `ChatModel`(27) `OpenAICompatChatModel`(70) `.complete`(91)；`HepAIChatModel`(147) `ZhipuChatModel`(167) `build_chat_model`(200) | proposer model 层（**易搬**，stdlib + 懒加载 SDK） | 无（懒加载 SDK） |
| [memory/](../simpleloop/memory/) **整包** | `finding_store.py` `FindingStore`(22)；`frontier.py` `compute_frontier`(12)；`retrieval.py` `BM25Index`(40) `rank_*`；`context.py` `build_generation_context`(19) `build_startup_pack`(46)；`experiment_index.py` `Experiment`(11) `build_experiments`(49)；`models.py` 全部 dataclass；`service.py` `MemoryService`(47) | proposer 研究记忆（除 service.py 的 harness 依赖，见 §4） | `service.py` 唯一 Host 依赖：`..harness.memory.{read_history, resolve_episode}` |
| [proposer_lane_worker.py](../simpleloop/proposer_lane_worker.py) | `main`(247)、`build_lane_deps`(113)、`run_lane`(219)、序列化 helpers(157-216) | **演化为 proposer-CLI 的 main()**（两个后端共用入口） | — |

### 3.2 🔒 留 Host（Kernel / 编排 / executor 侧）

| 当前文件 | 关键符号 | 留 Host 原因 |
|---|---|---|
| [harness/store.py](../simpleloop/harness/store.py) | `Store.append_generation`(65)（写 history）；`eligible` `best_candidate` | 唯一权威任务事实的写入者 |
| [harness/memory.py](../simpleloop/harness/memory.py) | `read_history`(17) `resolve_episode`(58) | Host 读 history（Store.history、export、cli plot 都用）。proposer 另持一份 reader（同冻结格式） |
| [harness/gate.py](../simpleloop/harness/gate.py) [evals.py](../simpleloop/harness/evals.py) [views.py](../simpleloop/harness/views.py) | gate/eval | 评价规则（Kernel） |
| [harness/workspace.py](../simpleloop/harness/workspace.py) [container/runtime.py](../simpleloop/container/runtime.py) | clone/worktree/mount/exec_argv | workspace 构造 + 容器机制（Kernel；proposer 另建自己的 runtime 跑 probe） |
| [execution/local.py](../simpleloop/execution/local.py) [hepjob.py](../simpleloop/execution/hepjob.py) [base.py](../simpleloop/execution/base.py) | `run_proposer_lanes` `run_candidates` | 后端：**改写 local 的 `run_proposer_lanes` 改为 spawn CLI**；hepjob 的 payload 从 `proposer_lane_worker` 改为 spawn proposer-CLI |
| [roles/agent.py](../simpleloop/roles/agent.py) [roles/executor.py](../simpleloop/roles/executor.py) | `Agent`(claude-p adapter) `execute` | executor 侧（task + self executor 都用它） |
| [loop.py](../simpleloop/loop.py) [config.py](../simpleloop/config.py) [processes.py](../simpleloop/processes.py) [cli.py](../simpleloop/cli.py) | round 编排、config、信号 | Host 骨架 |

### 3.3 ❌ 取消（被新模型取代）

| 符号 | 行 | 原因 |
|---|---|---|
| `MemoryService.link_completed_experiments` | [service.py:200](../simpleloop/memory/service.py#L200) | join-from-history 不变量（契约 §2.5）：归因改由 proposer 读 history 时现算 |
| Host 侧 `resolve_targets` 调用 | [loop.py:348](../simpleloop/loop.py#L348) | finding 创建/去重全归 proposer 内部；Host 不再 resolve |
| Host 侧 `link_completed_experiments` 调用 | [loop.py:433](../simpleloop/loop.py#L433) | 同上 |
| history.jsonl 的 `finding_id` 字段写入 | [store.py:89](../simpleloop/harness/store.py#L89) | Host 不再持有 finding 概念；proposer 用 (round, candidate idx) join |
| LOCAL 路径 in-process `ctx.proposer_agent.run` | [local.py:36](../simpleloop/execution/local.py#L36) | 改为 spawn proposer-CLI（与 hepjob 对称） |

---

## 4. 必须切断的 5 个 Host↔proposer 依赖（及替代）

盘库确认 proposer 当前**不直接 import harness.\***，Host 耦合全是**注入对象**。这 5 个注入点就是切断面：

| # | 当前依赖 | 切点 | 替代（让 proposer 自给自足） | 先例 |
|---|---|---|---|---|
| 1 | `memory_service` 注入 | [local.py:41](../simpleloop/execution/local.py#L41) / hepjob manifest | proposer 自己 `MemoryService(run_dir, ...)`：读 `history.jsonl`（冻结格式，自带 reader）+ 拥有 `findings.jsonl` | hepjob worker [proposer_lane_worker.py:146](../simpleloop/proposer_lane_worker.py#L146) 已自建 |
| 2 | `runtime`(ApptainerRuntime) 注入 | [local.py:40](../simpleloop/execution/local.py#L40) / orchestrator ctor | proposer 从 resolved config 自建 `ApptainerRuntime`（仅用于离线 probe） | hepjob worker [proposer_lane_worker.py:124](../simpleloop/proposer_lane_worker.py#L124) 已自建 |
| 3 | `processes.CHILD_PROCESSES` | [research_tools.py:212/246](../simpleloop/roles/research_tools.py#L212) | proposer 自带子进程登记（复制小 registry，或靠 process-group 清理） | — |
| 4 | `prompts.load_semantic` | [proposer.py 内](../simpleloop/roles/proposer.py) | charter/prompt 随迁进 proposer-CLI，从自带 prompt 目录加载 | — |
| 5 | `model` 注入 | orchestrator ctor | proposer 从 resolved config `build_chat_model`（HTTP 端点/凭证经 env） | hepjob worker [proposer_lane_worker.py:131](../simpleloop/proposer_lane_worker.py#L131) 已自建 |

**切断后**：proposer-CLI 给定 `(resolved config + run_dir + base_sha + round_id + workspace + goal)` 即**完全自给**——自建 model/runtime/memory、自载 prompt、读 history、写自己的 findings/notebook。Host 只 spawn 它 + 读回 proposals。**16 kwargs 收缩到契约 §3.2 的 ~6 个 args + resolved config。**

---

## 5. Host 侧新增（step 2 预览）

| 新增 | 职责 | 模板 |
|---|---|---|
| **proposer-CLI 调用 adapter** | spawn proposer-CLI 子进程（local: Popen；hepjob: condor）、传 args/env/mounts、读回 stdout JSON envelope 或 `result.json`、回传 `ProposerResult`-equivalent | 镜像 [roles/agent.py `Agent`](../simpleloop/roles/agent.py)，但 payload 是 `proposer-run` 而非 `claude -p` |
| **LOCAL 后端改写** | `run_proposer_lanes` 从 in-process 改为 spawn adapter | — |
| **（RSI 期）self-repo lifecycle** | snapshot proposer 源码 → `run_dir/self/repo`；transition/viability authority；reviews.jsonl writer；mode/commitment scheduler | 见契约 §9；**v0 先不做** |

> 注意 proposer-CLI **需要网络**（HTTP 调模型），其 probe **离线**。所以 CLI 本体跑在 host（同当前 local 后端）或**联网**容器；probe 才进离线 Apptainer。这与 executor（claude-p 进容器）不同——adapter 要照搬 hepjob worker 的部署形态，不是照搬 agent.py 的容器化。

---

## 6. 分阶段切片（step 1 → step 2）

把"提取"拆成可独立 review 的薄片，降低风险：

- **S1 ✅ 搬运清单**（本文）——确认切的边界。
- **S2a** LOCAL 子进程化：把 `proposer_lane_worker.main` 抽成 `proposer-run` CLI；改写 [local.py](../simpleloop/execution/local.py) 的 `run_proposer_lanes` 用 adapter spawn 它（payload 仍 `python -m simpleloop.proposer_lane_worker`，但走子进程）。**此时 Host 不再 in-process 调 proposer**——进程边界成立，可独立验证（一轮 run 跑通 = 通过）。
- **S2b** 源码迁出：把 `roles/{proposer,orchestrator,research_agent,research_tools,scientist_session,model}.py` + `memory/` 整包迁成独立包（如 `proposer/`），切断 §4 的 5 个依赖，Host 侧删除 `ctx.proposer_agent` / `ctx.memory_service` 注入、删 `resolve_targets`/`link` 调用。
- **S2c** findings/session 搬家：`findings.jsonl` → `run_dir/proposer/`；`scientists/lane-0/` → `run_dir/proposer/`；history 的 `finding_id` 置空；proposer 改用 (round, candidate idx) join。
- **（RSI 期 S3+）** self-repo snapshot + viability `--check` + self-executor + mode switch。

> 建议先 **S2a**（最小、可独立验证、不动源码归属）——它单独成立价值：哪怕不迁源码、不做 RSI，LOCAL 路径子进程化也让 proposer crash 不再拖垮 frontend，且与 hepjob 路径统一。**S2a 通过后再做 S2b/S2c。**

---

## 7. 对契约文档的勘误（下一步修）

盘库发现的 3 处契约假设偏差，需回头改 [proposer_cli_contract.md](proposer_cli_contract.md)：

1. **§3.1 spawn 机制**：契约写"Host 通过 Apptainer runtime 在容器内 exec proposer-CLI"——**不准确**。proposer-CLI 是 HTTP 调模型的 Python 程序（需网络），本体跑 host 或联网容器；只有 probe 进离线 Apptainer。原型是 `proposer_lane_worker.py`，不是 claude-p。
2. **§4.2 prompt 版本**：示例写 `scientist-v2`，实际 [proposer.py:116](../simpleloop/roles/proposer.py#L116) 已是 **`scientist-v3`**。
3. **§5 session 位置**：现状在 `run_dir/scientists/lane-<N>/`（非 `run_dir/proposer/`）。契约的 `run_dir/proposer/` 是**目标**搬家位置（S2c），需标注"当前 vs 目标"。
