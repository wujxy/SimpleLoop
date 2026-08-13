# SimpleLoop RSI 实现设计

## 0. 文档定位

本文是 RSI 的**实现设计**：把 [simpleloop_rsi_design.md](simpleloop_rsi_design.md)（语义/认识论——"是什么/为什么"）落成**工程机制 + 分片计划**（"怎么做"）。

它假设读者已读过：
- [simpleloop_rsi_design.md](simpleloop_rsi_design.md) — RSI 语义（同一个 agent、Goal 锚定、Immutable Kernel、Self World、viability vs capability、self-review history、严格不预设 self 有问题…）。
- [proposer_cli_contract.md](proposer_cli_contract.md) — Host↔proposer CLI 契约（🔒 FROZEN vs 🟢 EVOLVABLE、三层模型 Reality/Autobiography/Body、join-from-history、self-mode 调用）。
- [proposer_cli_migration_map.md](proposer_cli_migration_map.md) — 符号级搬运表 + 切片 S2a–S2c。

> **现状（S2 已完成）**：`proposer/` 已是**顶层独立包，零 `simpleloop` 导入**——这就是 RSI 的 self-repo 基底。Host 通过子进程跑 proposer，从不在进程内跑它。本文从此状态出发，设计 S3（RSI 机制本身）。

---

## 1. RSI 的实现架构（物理切分）

```text
SimpleLoop project source (immutable seed)
        │ new run: snapshot
        ▼
run_dir/self/repo/          ← Body：proposer/ 包的 per-run 副本，独立 git history
        │ 当前 active self = 某 git SHA (S0, S1, …)
        │
        ▼
┌──────────── STABLE HOST (simpleloop/, 永不被 self 修改) ────────────┐
│ loop.py round 编排                                                  │
│ self-repo lifecycle（snapshot / transition authority）              │
│ mode switch（task / self）+ self-review commitment scheduler         │
│ viability authority（跑 proposer-run --check）                       │
│ self-review ledger writer（reviews.jsonl，Host 拥有）                │
│ evaluator / gates / history.jsonl（Immutable Kernel）               │
│ proposer_lane_worker（稳定 launcher，见 §3 的 fork）                 │
└────────────────────────────┬───────────────────────────────────────┘
                             │ spawn
                             ▼
                   proposer-run（从 self/repo @ active SHA 加载）
                   ├── task mode：研究 task world → task proposals
                   └── self mode：研究 self world → self_decision (KEEP/CHANGE)
```

**三个物理层（对应契约 §2.4）：**
- **Reality（Kernel）**：`history.jsonl` + goal/gates/evaluator + 契约。Host 写，永不被 self 改。
- **Autobiography**：`run_dir/proposer/{findings,notebook,session,traces}`。proposer 写，跨 self revision 持久累积 = 身份（S2c 落地）。
- **Body**：`run_dir/self/repo @ S(n)`。self-executor 改，每次 self-change 切换 revision。

---

## 2. Self-repo 生命周期

- **snapshot（run 开始）**：把 project source 里的 `proposer/` 包复制到 `run_dir/self/repo/`，`git init` + 初始 commit = **S0**。记录 `active_self_sha = S0`。
- **transition（self-change 被采纳）**：`S(n) → S(n+1)`。Host 在 `self/repo` 开 worktree @ `active_self_sha`，self-executor 改它 → candidate SHA → viability `--check` → 通过则 `active_self_sha = candidate`，写 reviews.jsonl；失败则保留 incumbent。
- **per-run 独立**：每个 run 从同一 project seed 重建 fresh S0（RSI 设计 §22）。不跨 run 累积（upstream promote 是显式人工行为）。
- **continuity 跨 revision 不重置**：findings/notebook/session（Autobiography）在 `run_dir/proposer/`，**不**在 `self/repo` 里——所以 S0→S1 切换不丢 Scientist 的笔记（契约 §2.4、§14）。代码（Body）换；经历（Autobiography）留。

---

## 3. Worker placement fork（需先定的开放决策）

当前 `proposer_lane_worker` 是 Host 侧稳定 glue（`python -m simpleloop.proposer_lane_worker`），import 已安装的 `proposer` 包。RSI 要让它跑 **self-repo 的副本**。两条路：

- **(A) 稳定 launcher（推荐 v0）**：worker 留 Host，加 `--self-repo <path>`；worker 在 import proposer 前 `sys.path.insert(0, self_repo)`，于是 `proposer.*` 解析到 self-repo。**launcher 稳定不被 self 改；只有 proposer 包（认知机器）是 self。**
- **(B) self-repo entry**：把 worker 也搬进 self-repo，入口变成 `python -m <self_repo>.run`，入口本身可被 self 改（契约 §3.1 原倾向）。

**推荐 (A) 起步**：launcher 是薄管道（解析 manifest + 起子进程），不是认知；保持它稳定能最小化 RSI 风险，而 Scientist 真正的研究行为（charter / context 构造 / 工具 / 记忆语义 / 控制环）全在 proposer 包里、仍可自改。(B) 留作后期（若 Scientist 需要改自己的启动逻辑）。**契约 §3.1 的"launcher is body"应据此修订为"v0 = 稳定 launcher"。**

---

## 4. Mode switch + self-review 调度

- **Host 拥有 mode switch**（契约 §7、语义 §17）：`run_dir/self/state.json` 持 `{active_self_sha, next_self_review_round, mode}`。每轮 loop 开头，Host 读它决定该轮是 task 还是 self mode。
- **触发**：`round_id >= next_self_review_round` → 该轮以 **self mode** 调 proposer。否则 task mode。
- **commitment scheduler（语义 §17）**：self-review 不固定周期；由 Scientist 上一次 review 时自己承诺 `next_review_after_rounds`（KEEP 时填，CHANGE 时在采纳后由 Host 写入）。Host 只是"时钟不是导师"——只兑现承诺。
- **提早重审**：task mode 里 Scientist 若在 charter 下意识到自己值得立即重审，可影响 commitment，但**兑现成 self mode 仍是 Host 的时钟**（防止逃避 self-review）。

---

## 5. Self World（self mode 下 Scientist 看到什么）

self mode 的 `proposer-run` 调用，cwd/mount 与 task mode 不同（契约 §3.4）。Scientist 看到：
- `/self`：当前 self-repo 源码（incumbent @ active SHA）——研究对象是"我自己"。read/probe。
- `/task_history`：`history.jsonl`——判断"我是否瓶颈"的**唯一权威依据**（语义 §5.2、§6）。
- `/session`：自己的 trajectory（行为证据，语义 §5.3）。
- `/self_history`：`reviews.jsonl`——过去 self-judgment 的 provenance（语义 §5.4、§14/§15）。

**Self-review prompt**（语义 §8）：只做**注意力切换 + 重申 Goal + 把真实 performance 放中心**——不规定认知流程（Evaluate→Compare→…），不提供 failure taxonomy，不替 Scientist 判断改什么。是"导师在关键时刻的一句话"，不是 workflow。输出（契约 §4.3）：`self_decision ∈ {KEEP, CHANGE}` + diagnosis；CHANGE 带 `self_change{target, intent, instruction}`；KEEP 带 `keep_reason` + `next_review_after_rounds`。

---

## 6. Viability 检查（`proposer-run --check`）

每次 self-change 候选 S(n+1) 采纳前，Host 在候选源码上跑（契约 §8）：
1. 入口能 boot（import/语法）；
2. 能加载既有连续性（`run_dir/proposer/{session,notebook}` 不崩——backward-compat）；
3. **不调 model** 也能 emit 合法 `structured_output` 骨架（空 proposals 或合法 abstain）——证明控制环 + 输出 schema 没坏；
4. 契约版本一致。

退出 0 = viable；非 0 = broken self，**没资格成为 active self**（语义 §19：viability is immediate）。**capability（"是否真的更强"）不在 viability 范围**——那是后续真实 task progress 的事（delayed）。

---

## 7. Self-review ledger（reviews.jsonl，Host 拥有）

`run_dir/self/reviews.jsonl`，每行一条（契约 §9.2）：
```json
{"round":12,"incumbent_self_sha":"...","decision":"CHANGE","diagnosis":"...",
 "change":{"target","intent","instruction"},"candidate_self_sha":"...",
 "viable":true,"adopted":true,"next_review_round":17}
```
- self mode 下 **proposer 只读它**（语义 §15：autobiographical provenance——继承"为何这么判断"的理由，不继承结论）。
- **proposer 不能写它**——Host 用 proposer 的 `self_decision` + viability 结果**组装并 append**（防止改写自己的历史，语义 §5.2/§13）。

---

## 8. Self-executor

- self mode 产出 CHANGE 后，Host 在 `self/repo @ active_self_sha` 开 worktree，调 **self-executor**（独立 `claude`/codex 调用，像 task executor），按 `self_change.instruction` 改 **🟢 Evolvable 路径**（契约 §2.2：charter/context/tools/memory 语义/control loop）；🔒 Kernel 路径不在它的可写世界。
- **WHY/WHAT 来自 proposer（Scientist）；HOW 来自 self-executor**（语义 §11 角色边界不变）。
- 产出 candidate SHA → §6 viability → §7 ledger。

---

## 9. 认识论保障 → 机制映射

把语义文档的每条原则落成一个**可观测/可执行的机制**（这是 RSI 不塌缩成自我辩护的关键）：

| 语义原则 | 落地机制 |
|---|---|
| Goal 是唯一锚点（§4） | self mode 仍传 `--goal`；self Decision 必须引用 Goal progress 证据 |
| 权威结果不可改写（§5.2） | reviews.jsonl Host-append；history.jsonl Host-only 写；findings 归因 **join-from-history**（每轮重算，不存权威） |
| 不因评价自己降标准（§6） | self-review prompt 明示"微小进展≠足够"；**KEEP-rate 监控**（见下） |
| 不预设 self 有问题（§7） | self-review 从"progress 是否足够"开始，不从"我哪里坏了"开始；KEEP 是合法输出 |
| 历史保存理由非结论（§14/§15） | reviews.jsonl 带 diagnosis + 条件；下一次 review 读它作 provenance |
| viability vs capability 分离（§19） | `--check`（immediate）vs 真实 task outcomes（delayed） |
| 一个 run 一条生命史（§22） | 每 run fresh S0；self 只在 run-local repo 演化 |

**KEEP-rate falsification（最重要的早期信号）**：健康的 RSI 必须产出**真实比例的 KEEP_SELF**。若每次 self-review 都 CHANGE = self-review 已塌缩成"总得改点什么"的 churn（自我辩护驱动）。从第 1 个 run 就监控 KEEP/CHANGE 比——这是比"有没有改进 goal"更便宜、更早的 falsification（语义 §18 思路用于 self-review 自身）。

---

## 10. 分片计划（S3 + S2c）

每个切片可独立验证。建议顺序 **S3a → S2c → S3b → S3c → S3d**（先把 self-repo 跑通为活代码，再修记忆归属让 Kernel 干净，再加 viability/mode/ledger，最后才真正 self-modify）。

- **S3a — self-repo lifecycle（"活代码"）**：run 开始 snapshot `proposer/` → `run_dir/self/repo`（git init, S0）；worker 加 `--self-repo` → `sys.path.insert`，使 `proposer.*` 解析到 self-repo；记录 `active_self_sha`。**此时无 self-modification**——只证明 run 实际跑的是 self-repo 的代码（改 self-repo 里一行 print → 看到生效 = 通过）。
- **S2c — 记忆归属迁移（Kernel 收缩）**：`findings.jsonl` + `scientists/lane-0/` → `run_dir/proposer/`；history 的 `finding_id` 置空；proposer 用 (round, candidate idx) join history；**删 Host 的 `resolve_targets`/`link_completed_experiments` + `ctx.memory_service`**。落地契约 §2.5 join-from-history。这让 Kernel 在 RSI 评价 self 之前完全就位。
- **S3b — viability `--check`**：在 proposer-CLI 加 `--check`（boot + 连续性加载 + 无 model 协议产出 + 契约版本断言）；Host viability runner 在候选 self 上跑它。先不接 self-change，单独验证（对一个故意改坏的 self-repo，`--check` 应失败）。
- **S3c — self mode + self-review（只诊断，不执行）**：Host mode switch + commitment scheduler；Self World 构造；self-review prompt；reviews.jsonl writer。Scientist 能产出 KEEP/CHANGE self_decision 并被记账，但 **CHANGE 暂不触发 self-executor**（只记 intent）。验证：KEEP-rate 监控上线；一轮 self-review 产出合法 decision + ledger。
- **S3d — self-executor + adoption（真正自改）**：CHANGE → self-executor 改 self-repo worktree → viability → adopt（推进 active_self_sha）+ ledger。**v0 围栏：首批 self-revision 只动 prompt + context 文本（charter/suspend/怎么读 run_dir），不动 `proposer/*.py` 可执行控制环**——viability 平凡安全，先验证整个环路（trigger→self world→诊断→change→viability→adopt→真实 task 评估）成立，再放宽到可执行代码。

---

## 11. v0 围栏（首个 RSI 验证的安全栏杆）

1. **认知基底冻结**：session/notebook/findings 的骨架 schema v0 不改（防自改破坏自己的记忆基底）。history.jsonl 是不可摧脊柱（§2.5）。
2. **首批 self-revision 只动 prompt + context 旋钮**（S3d）：runtime 不变，只是加载的文本变 → viability 平凡。验证环路成立后再放宽到可执行代码。
3. **KEEP-rate 监控从 round 1 接上**（§9）：作为反自我辩护的 falsification 信号。
4. **单 lane**（已定，S2a）：v0 单 lane 跑通。
5. **per-run 隔离**：self 只在 run-local repo 演化，不碰 project source（§2）。

**v0 验证目标不是"有没有改进 goal"**（那是 delayed capability，多轮才读得出），而是**反自我辩护的 falsification**：KEEP/CHANGE 比健康 + self-change 后 viability 通过 + 修改后的自己在真实 task 里**行为可观测地不同**（不是"同一研究配更精致 self-narrative"）。

---

## 12. 开放决策（实现前/中需定）

1. **Worker placement**（§3）：推荐 (A) 稳定 launcher 起步。需确认。
2. **self-repo 的 seed 范围**：snapshot 整个 `proposer/`（含 vendored runtime/history/child_processes）还是更小？含 vendored 的好处是 self-repo 真正自洽；坏处是 self-executor 可改面更大（但 vendored 那几个是冻结基础设施，不该动——可在 self-executor 的可写边界里排除它们，或允许改但 viability 兜底）。
3. **`--check` 的 model 依赖度**（§6）：核心断言能否完全离线（不调 model）？决定 viability 的成本与确定性。建议能离线就离线。
4. **self-review 触发是否需要"最低 task 轮数"**（防首轮就 self-review 无 evidence）：commitment 默认值。
5. **self-executor 用什么模型**：可能与 task executor 不同（更强？）；config 增加 `roles.self_executor`。

---

## 13. 与现有文档/状态的关系

- 本文 = RSI **实现**设计（机制 + 分片）。
- [simpleloop_rsi_design.md](simpleloop_rsi_design.md) = RSI **语义**（不变，是本文的上游约束）。
- [proposer_cli_contract.md](proposer_cli_contract.md) = Host↔proposer 契约（S3 在其上叠加 self mode/reviews.jsonl/--check；本文若与契约 §3.1 冲突，以本文 §3 的"v0 稳定 launcher"为准并回写契约）。
- [proposer_cli_migration_map.md](proposer_cli_migration_map.md) = 搬运表（S2a–S2b(ii) ✅ 已完成；S2c + S3 见本文 §10）。
- **当前代码状态**：`proposer/` 独立包（零 simpleloop 导入）；S2a/S2a.5/S2b(i)/S2b(ii) 在 working tree（未提交）；3 个 `test_scientist.py` 失败是 proposer-repair 的遗留（`_FakeExp.parent_sha`），与本提取无关。

> **接续指引**：新会话从本文 §10 的 **S3a** 起步——按 S2a/S2b 的节奏（研究→plan→实现→验证）推进。每切片仍走 EnterPlanMode→ExitPlanMode 审批。
