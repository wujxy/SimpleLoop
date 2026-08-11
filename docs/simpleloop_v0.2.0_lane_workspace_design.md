# SimpleLoop v0.2.0 Lane + Proposer Workspace 重构设计

## 1. 目的

本设计面向 SimpleLoop `v0.2.0`，目标是在**不改动现有核心科研逻辑**的前提下，完成两项基础设施重构：

1. 将 `ProposerOrchestrator` 现有"进程内并发 lane"（frontend 每轮一次 proposer 调用内部的线程池 fan-out）**升格为跨进程 / 跨 HEPJob 的独立 Lane Proposer research episode**（lane 本身不是新概念，见 §2/§4.2）。
2. 为每个 Lane 的 Proposer 提供一个**独立、可写、带完整 Git 历史、挂靠 `run/repo` 的研究工作空间**（理解辅助型，merit 仍归 Harness，见 §10）。

本设计只解决 **Lane 化 + Proposer Workspace + HEPJob 两阶段调度**。

以下组件不在本次重构范围内：

- Proposer 的科学研究方法论与内部状态机
- History / Experiment Ledger
- Finding Archive / Frontier / Memory
- 历史何时可见、何时注入
- Gate
- Eval
- CandidateWorker 的核心执行逻辑
- Selection / accepted-best 逻辑
- Tree evolution
- Proposer prompt 重构

设计原则：

> Harness 负责定义 Agent 能进入的文件世界；Agent 在该世界内自由工作。  
> Workspace 不负责定义 Proposer 应该知道什么，也不负责替代现有历史系统。

---

## 2. 当前 v0.2.0 的执行拓扑

> 前置事实：代码里**已经有 lane**。`ProposerOrchestrator`（`simpleloop/roles/orchestrator.py`）现在就在一个 frontend proposer 进程**内部**跑 `n_lanes` 条并发 lane——lane 数由 `candidates_per_round` 推导：`n_lanes = ceil(candidates_per_round / _SELECT_PER_LANE)`，`_SELECT_PER_LANE = 2`；这些 lane 是进程内 `ThreadPoolExecutor` 的线程（≤8 worker），不是进程、不是 job、也不拥有独立 workspace。

当前真实执行拓扑：

```text
Round
  ↓
_next_proposals:  add_worktree("proposer-{round}", parent_sha)
  ↓                          ← 全局唯一一个 proposer worktree，
ProposerOrchestrator.run (单进程，frontend 内)        进容器时挂成只读
  ↓ ThreadPoolExecutor (≤8)        /source:ro  /repo:ro  /scratch:rw(tempdir)
  lane 0: Generator → Cognitive  ┐
  lane 1: Generator → Cognitive  ├ 全部共享同一个只读 source worktree
  lane 2: Generator → Cognitive  ┘   + 各自一个 per-batch /scratch tempdir
  ↓ flatten proposals（按 lane 稳定顺序）
[P0, P1, P2, P3, ...]
  ↓
CandidateWorker 0/1/2/...   ← 每条 proposal 从同一 base_sha 建干净 worktree
  ↓
Executor → changed_paths/gate → commit → eval → result → Selection
```

两个关键现状：

1. **并发 lane 共享同一个只读 worktree。** 因为 worktree 进容器时是 `:ro`，共享安全；每条 lane 各有一个 per-batch `/scratch` tempdir，也互不干扰。
2. **Proposer 的文件世界被锁成"只读 + 离线 + 无 credential"。** source / repo / history 全部 `:ro`，容器 `--net --network none`、不转发任何 token，prompt 再二次确认（`_RUNTIME_BOUNDARIES`："You cannot ... edit candidates"）。Proposer 能 `git show` / `git diff` 历史实验 SHA，但**不能写源码、不能编译、不能跑 benchmark、不能做 toy experiment**——它只能靠读来推理。

CandidateWorker 已经具备相对清晰的职责：

```text
proposal
  ↓
prepare candidate worktree
  ↓
Executor
  ↓
changed paths / gate
  ↓
commit
  ↓
eval
  ↓
result
```

本次改造不重新设计 CandidateWorker，而是：**把 orchestrator 现有的"进程内并发 lane"升格为"跨进程 / 跨 job 的独立 lane episode"，并顺势让每条 lane 拥有自己的可写 workspace。**（具体因果见 §3。）

---

## 3. 目标执行拓扑

改造后：

```text
                         Round r
                    accepted_sha = A
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
        Lane 0           Lane 1           Lane 2
          │                │                │
   fresh Git WS @ A  fresh Git WS @ A  fresh Git WS @ A
          │                │                │
     Proposer Job     Proposer Job     Proposer Job
          │                │                │
      P0  P1           P2 P3 P4            P5
      │   │            │  │  │             │
      ▼   ▼            ▼  ▼  ▼             ▼
     CW  CW            CW CW CW             CW
      │   │            │  │  │             │
      └────────────────┴──┴──┴─────────────┘
                           │
                     existing results
                           │
                     existing select
                           │
                     new accepted_sha
```

定义：

> **一个 Lane = 一个独立的 Proposer research episode，以及该 Proposer 产生的一组 proposals。**

一个 Lane 的 Proposer 可以返回 `1..N` 个 proposals。这些 proposals 继续 fan-out 到现有 CandidateWorker。

### 3.1 为什么这个改造是"自然外推"而不是"新加盖"

不要把 Lane Workspace 当成给 proposer 的加分项——它是现有结构的逻辑必然：

```text
orchestrator 已经在并发多 lane（线程，共享一个 worktree）
        ×
workspace 要从只读变成可写（解决"被锁死、没法理解任务"）
        ↓
并发 lane 不能再共享同一个可写 worktree（否则 lane 0 的 git checkout
会踩烂 lane 1 的工作树）
        ↓
每条 lane 必须拥有独立 workspace
        ↓
workspace 独立后，in-process 的 ThreadPoolExecutor 自然演化成
跨进程 / 跨 HEPJob 的 fan-out
```

换言之：**不是"先有 Lane 化、再给 workspace"，而是"workspace 一旦可写，现有并发 lane 就被迫分家，于是 Lane 顺理成章升格为跨 job 单元"。** 这也解释了为什么 lane 数、proposal 配额都可以沿用 orchestrator 现有推导（见 §4.2、§5）。

---

## 4. Lane 的语义

### 4.1 Lane 是逻辑单元

Lane 不是：

- 一个 CandidateWorker
- 一个 Container
- 一个 HEPJob
- 一个长期运行进程
- 一个 Git branch

Lane 是一个稳定的逻辑 research slot。

例如：

```text
lane-0
lane-1
lane-2
lane-3
```

在当前串行 round / accepted-best 模式中，每轮所有 Lane 都拿到相同的：

```text
base_sha = current accepted_sha
```

未来如果进入 Tree Evolution，只需改变 Lane 的 `base_sha` 分配：

```text
当前：
lane-0 -> accepted_sha
lane-1 -> accepted_sha
lane-2 -> accepted_sha

未来：
lane-0 -> node_A.sha
lane-1 -> node_F.sha
lane-2 -> node_K.sha
```

Lane Workspace 语义无需改变。

### 4.2 一个 Lane ≡ 一个 orchestrator-lane（必须明确的映射）

代码里已经存在的 orchestrator lane（`_run_one_lane`：1 个 Generator + 1 个 Cognitive，产 `select_quota` 个 proposal）和本文档要新增的 Lane，**是同一个东西的两种执行形态**：

| 维度 | 现在（v0.2.0 现状） | 本设计之后 |
| --- | --- | --- |
| 执行单元 | 进程内线程（`ThreadPoolExecutor`） | 独立进程 / HEPJob |
| 文件世界 | 共享一个只读 worktree | 独立可写 workspace |
| 一条 lane 产出的 proposal 数 | `select_quota`（=2） | 不变，仍 `select_quota` |
| lane 数推导 | `ceil(candidates_per_round / K)` | **不变** |

因此本设计的映射关系固定为：

> **一个 doc-Lane ≡ 一个 orchestrator-lane**（即一次 `_run_one_lane`：1 Generator + 1 Cognitive，`select_quota` 个 proposal）。

`ProposerOrchestrator._run_lanes` 里那个 `ThreadPoolExecutor` 的并发 fan-out，被替换成 backend 的 HEPJob 提交；`_run_one_lane` 的内部逻辑（generate → cognitive → feedback）**整体保留**。

⚠️ **必须避免的陷阱：嵌套 fan-out。** 如果实现者在一个 HEPJob-Lane 里又跑一整个 `ProposerOrchestrator`（内部再 N 条 lane），就会得到 `N × M` 个 proposal，`candidates_per_round` 和预算全部失控。一条 HEPJob-Lane **只跑一条** `_run_one_lane`。

---

## 5. LaneAssignment

建议新增最小数据结构：

```python
@dataclass
class LaneAssignment:
    lane_id: int
    round_id: int
    base_sha: str
```

当前不需要加入 Tree / parent node 等额外字段。

如果现有执行链需要其它 run metadata，可继续通过现有 context 传递，不应把历史系统重新塞进 `LaneAssignment`。

**lane 数与配额**：v0.2.0 不新增配置项，继续沿用 orchestrator 现有推导——`n_lanes = ceil(candidates_per_round / _SELECT_PER_LANE)`，每条 lane 的 `select_quota` 由 `_lane_quotas` 分配。本设计只改变 lane 的执行形态（线程 → job），不改 lane 数的来源。未来 Tree Evolution 若要按 node 分配不同 lane 数，再单独引入配置。

---

## 6. HEPJob：一个 Lane 两阶段执行

### 6.1 Stage A：Proposer HEPJob

Frontend / Backend 为每个 Lane 提交一次 Proposer job：

```text
Lane 0 -> Proposer HEPJob
Lane 1 -> Proposer HEPJob
Lane 2 -> Proposer HEPJob
```

输入：

```text
lane_id
round_id
base_sha
Proposer 所需的现有输入
```

输出：

```text
Lane 0 -> [P0, P1]
Lane 1 -> [P2, P3, P4]
Lane 2 -> [P5]
```

### 6.2 Stage B：CandidateWorker HEPJob

Frontend 收集所有 Lane proposals 后，继续使用现有 CandidateWorker：

```text
P0 -> CandidateWorker
P1 -> CandidateWorker
P2 -> CandidateWorker
...
```

每个 CandidateWorker 独立并行。

不要让远端 Proposer HEPJob 自己再提交 Condor 子任务。

调度仍由 frontend / HEPJobBackend 控制。

### 6.3 抽象边界

Loop 层应该逐渐趋向：

```python
lane_proposals = backend.run_proposer_lanes(assignments)
candidate_results = backend.run_candidates(flatten(lane_proposals))
```

而不是把 Proposer / HEPJob / Candidate 的底层细节全部散落在 loop 中。

第一版可以保持改动最小，不要求一次完成完整 backend 抽象重构。

---

## 7. Proposer Workspace 的核心语义

### 7.1 Workspace 必须是挂靠 `run/repo` 的可写 Git 工作树

Proposer Workspace 不能只是：

```text
copy(parent_sha 文件树)
```

必须是：

> **一个挂靠在 per-run canonical repository `run/repo` 上、可写、checkout 到本轮 `base_sha` 的 Git 工作树。** 主路径用 linked worktree（与现有 CandidateWorker 同机制），独立 clone 仅作可选降级。

例如：

```text
run/repo   canonical per-run Git repo（共享 object store）
       │
       │ git worktree add（linked，零拷贝）
       ▼
lane-0/workspace/
├── .git   → /run/repo/.git/worktrees/lane-0   (gitfile)
├── src/
├── ...
└── HEAD = base_sha（detached）
```

**为什么默认用 linked worktree 而不是独立 clone：**

1. **成本。** 对 junosw 这类大仓，每 lane 每轮做一次 full clone + checkout 是可观的磁盘与时间开销；linked worktree 共享 object store，近乎零成本。现有 `Workspace` 对 `run/repo` 本身就用 `--local` 硬链接，同理。
2. **下方 4 条语义 worktree 全满足。** Git worktree 本就是为"共享 objects、各自工作树、互不污染"设计的；proposer 在 detached HEAD 工作树里 commit / reset / checkout 不动 `run/repo` 的任何 ref。
3. **兼容现有 research 命令的 git 环境（硬约束）。** `ResearchCommandRunner`（`simpleloop/roles/research_tools.py`）现在按 linked-worktree 写死——`_worktree_git_dir` 读 `source/.git` 期待 `gitdir: /repo/.git/worktrees/<id>`，并设 `GIT_COMMON_DIR=/repo/.git`。**独立 clone 会直接打穿这两处**（`.git` 是真目录而非 gitfile → 抛 "research source has no worktree metadata"；objects 也不在 `/repo/.git`）。用 linked worktree 则现有 git 环境处理原样可用，无需重写。

唯一真风险是 proposer 跑 `git gc --prune=now` 误删 object——这由 §24 的 reachability 不变量挡住（仍被实验历史引用的 commit 必须可达，gc 不会回收可达对象）。

示意（主路径，linked worktree）：

```bash
git -C <run_repo> worktree add --detach <lane_workspace> <base_sha>
# 用完：
git -C <run_repo> worktree remove --force <lane_workspace>
```

可选降级（独立 clone，仅当需要把 proposer 的 git 操作与 run/repo 彻底隔离时）：

```bash
git clone --no-checkout --no-hardlinks <run_repo> <lane_workspace>
git -C <lane_workspace> checkout --detach <base_sha>
```

⚠️ 选独立 clone 时，**必须同时重写 `ResearchCommandRunner` 的 git env 处理**（不再假设 `GIT_COMMON_DIR=/repo/.git`、不再期待 gitfile），并把它列为 §26 Step 2 的显式工作项。

无论哪条路径，必须保持以下语义：

1. Workspace 能被 Git 当作一个完整工作树操作（有可写的 index / HEAD）。
2. Workspace 能访问该 run 已经产生的历史 SHA（`git show <sha>` 可用）。
3. Workspace 的 Git 操作不会污染 canonical `run/repo` 的 refs / 默认分支。
4. Workspace 当前 working tree 精确对应 `base_sha`。

---

## 8. 为什么必须从 `run/repo` 派生

不能每轮从用户最原始的 source repo 派生。

原因：

SimpleLoop 运行过程中 CandidateWorker 会产生新的 commit：

```text
A
├── B
├── C
└── D
```

这些 SHA 属于当前 run 的研究历史。

现有实验记录可能引用：

```text
sha = B
sha = C
sha = D
```

Proposer 的认知侧可能根据历史记录进一步执行：

```bash
git show B:path/to/file
git diff B..D
git diff B..HEAD
```

因此：

> `run/repo` 是本次 run 的 Git authority。

Lane Workspace 必须从 `run/repo` 派生。

---

## 9. Workspace 必须支持历史 SHA 回查

这是本设计的硬要求。

例如历史系统告诉 Proposer：

```text
historical experiment sha = abc123
```

新的 Workspace 中必须能直接：

```bash
git show abc123
git show abc123:path/to/file.cc
git diff abc123..HEAD
```

这保证本次 Workspace 重构不会破坏现有认知侧通过实验 SHA 检查过去源码的能力。

注意：

- 本次不改变历史何时暴露给 Proposer。
- 本次不改变历史查询工具。
- Workspace 只保证：一旦 Proposer 获得某个 SHA，它有能力在自己的 Git repo 中检查它。

---

## 10. Workspace 是"理解辅助型"自由实验室

Proposer 对自己的 Workspace 拥有完整读写权限，**目的是更自由地理解代码与任务目标，而不是变成一个私有测量通道**。这一点必须在设计层钉死，因为它直接关系到现有 proposer 的 epistemic 契约。

### 10.1 现有契约（不能动摇）

`simpleloop/roles/proposer.py` 开头写明：

> "Merit — will the change preserve correctness, will it be faster — is structurally unknowable by an LLM in this domain and is reserved for the Harness, which is the only source of truth."

整套只读 + `--network none` + 无 credential 的研究边界，就是为了让 proposer **无法靠"试一下"来知道答案**，否则它会用自己跑的 benchmark 当事实去预筛选 proposal。本设计给 proposer 可写 workspace，**不改这条契约**。

### 10.2 允许（理解辅助）

实现采用 linked worktree + `/repo:ro`（见 §7.1、§18.1），所以"可写"= **写文件 + 只读 git**，**不包括 commit/branch/reset**（写 object/index 要落到只读的 `/repo`，mount 层兜底挡住）——这正好对：产出 git artifact 是候选的职责。

- 阅读任何 workspace 内源码
- 写文件、改文件、删除文件（实验台 `/workspace` 是可写的 lane worktree）
- 写临时分析脚本、scratch 代码
- 编译，以**搞清楚结构 / 依赖 / 调用关系**（不是为了拿一个速度数）
- 插临时 instrumentation 观察 control flow / 数据形状
- 只读 Git 操作：`git show <sha>:<path>` 读单个历史文件、`git diff <sha>..HEAD`、`git log` 查任意历史实验 SHA（object 经 worktree 共享 store 可达）
- 甚至可以把 Workspace 搞乱——这是允许的，因为 Workspace 不属于正式 artifact 路径，episode 结束即弃

> 注：若未来要让 proposer 真能 `commit/branch/reset`（empirical oracle 路线需要可保存实验检查点），必须改成独立 clone（自带可写 object store，重写 `_worktree_git_dir` 的 git env）——不在 v0.2.0 范围。

### 10.3 禁止（merit 裁定仍归 Harness）

两条硬规则，须在 prompt 与运行时同时落实：

1. **Lab 必须保持离线、无 credential。** 可写 workspace 不等于放开 `--network none`、不等于转发 token。Proposer **不得**调用 canonical eval、不得联网、不得接触任何带 credential 的服务。一旦放开，proposer 就有了私有测量通道，"Harness 是唯一事实来源"立刻瓦解。
2. **Lab 里测到的任何数字不作 merit 事实。** Proposer 在 workspace 里编译/插桩观察到的现象，**仅供自己理解结构**；是否更快、是否保持正确，仍由 Harness 的 eval/gate 裁定。Proposal schema 维持现状（`instruction` / `research_target` / `evidence_refs` / `material_difference`），不引入 "lab_measured_speed" 之类的字段。

这两条把"可写 lab"明确收敛为**理解辅助（comprehension aid）**，而非**经验主义裁决（empirical oracle）**。后者（让 proposer 真能用 toy benchmark 决策）是更大的方法论变更，留给后续单独设计，不在 v0.2.0 范围（见 §28 non-goal）。

原因：

> Proposer Workspace 不属于正式 artifact 路径；它的可写性是为了理解，不是为了产出事实。

---

## 11. Workspace 不跨 episode 继承状态

Lane 是长期逻辑槽位，但 Workspace 的内容不是长期记忆。

例如：

```text
Lane 0

Round 0:
  fresh workspace @ SHA A
      ↓
  proposer experiments
      ↓
  dispose

Round 1:
  fresh workspace @ SHA D
      ↓
  proposer experiments
      ↓
  dispose
```

每次新的 Proposer episode：

```text
delete previous workspace
      ↓
clone current run/repo
      ↓
checkout --detach base_sha
      ↓
start proposer
```

不要跨轮保留：

- dirty source
- build/
- cache
- scratch
- generated files
- private proposer commits
- profiling output
- temporary scripts

硬不变量：

> **每个 Proposer episode 的 filesystem 初始状态必须只由当前 `run/repo` + `base_sha` 决定，不得继承上一轮 Workspace 文件状态。**

这样 Workspace 不会成为一套隐式 memory system。

---

## 12. Lane 是持久的，但实验台每轮重建

更准确的比喻：

> Lane 是长期存在的实验室席位，每次 assignment 开始时换上一张全新的实验台。

因此目录可以稳定：

```text
run/
└── lanes/
    ├── lane-0/
    │   └── workspace/
    ├── lane-1/
    │   └── workspace/
    └── lane-2/
        └── workspace/
```

但每次执行 `LaneWorkspace.create(base_sha)` 时，旧 workspace 应被删除并重建。

不要求保留目录 inode 或 clone。

---

## 13. Proposer Workspace 与 Candidate Workspace 严格隔离

这是本设计最重要的不变量之一。

禁止：

```text
Proposer dirty workspace
        ↓
CandidateWorker
```

正确：

```text
                    run/repo
                       │
                   base_sha A
                    /       \
                   /         \
                  ▼           ▼
         Proposer Workspace  Candidate Worktree
                  │             ▲
                  │             │
                  └─ Proposal ──┘
```

二者唯一正式 crossing boundary：

```text
Proposal
```

不能传：

- dirty tree
- patch
- private proposer commit
- build artifact
- temporary experiment files

原则：

> **Proposer modifications are research activity. CandidateWorker modifications are formal artifacts.**

---

## 14. Candidate Workspace 保持现有机制

当前 CandidateWorker 的 Git worktree 模型继续保留。

每个 proposal 从同一 `base_sha` 创建干净 candidate worktree：

```text
Lane 1 Proposer
      ↓
  [P0, P1, P2]
   │   │   │
   ▼   ▼   ▼
  WT0 WT1 WT2
   │   │   │
   ▼   ▼   ▼
   CW  CW  CW
```

建议 candidate/worktree ID 加入 Lane：

```text
r3-l0-c0
r3-l0-c1
r3-l1-c0
r3-l1-c1
```

但 CandidateWorker 的以下逻辑尽量原样保留：

```text
Executor
  ↓
changed_paths
  ↓
gate
  ↓
commit
  ↓
eval
  ↓
result
```

本次重构不要顺便重新设计 CandidateWorker。

---

## 15. Workspace 与现有 History / Memory 解耦

本设计不引入：

```text
workspace/context/
workspace/history/
workspace/priors/
workspace/memory/
```

等新的信息体系。

也不改变现有历史系统。

以下逻辑保持当前实现：

- 实验记录如何保存
- Experiment Ledger
- Finding Archive
- Frontier / Memory
- Proposer 何时能读历史
- 哪个 phase 能看哪些信息
- 历史如何插入上下文
- Proposer 如何查询历史 SHA

Workspace 模块只做：

> **materialize 一个可写、隔离、带完整 Git 历史的源码世界。**

它不做：

> **决定 Proposer 应该知道什么。**

---

## 16. “源码是先验”的含义

本设计不再把源码人为建模成：

```text
/source      read-only
/repo        read-only
/workspace   writable
```

对于 Proposer 来说：

```text
/workspace
```

就是本轮开始时 Harness 给它的源码世界。

该世界：

- 初始状态由 `base_sha` 决定；
- 含完整 Git 历史；
- 完全可写。

源码本身只是 Proposer 本轮初始先验的一部分。

至于其它先验和历史如何交付，继续使用现有 Proposer 机制，本次不修改。

---

## 17. Container / Apptainer 边界

Lane 不等于 Container。

推荐：

```text
HEPJob
  ↓
Proposer worker
  ↓
apptainer exec
  ↓
/workspace -> lane-X/workspace : rw
```

Container 是一次性运行环境。

Lane、Workspace、Container、Job 生命周期应分别理解：

```text
Lane        persistent logical identity
Workspace   one proposer episode
Container   one execution invocation
HEPJob      one scheduler job
```

不要做长期运行的 Lane Container。

---

## 18. Proposer Container 应只看到自己的 Workspace

如果条件允许，新的 Proposer execution boundary 应避免继续暴露整个 `run_dir`。

目标是：

```text
host:
run/lanes/lane-0/workspace

     │ bind RW
     ▼

container:
/workspace
```

Proposer 不应该因为运行时 convenience 而自动看到：

```text
run/repo
other lanes
candidate worktrees
SimpleLoop internal directories
```

是否还需要挂载现有历史相关资源，应严格沿用当前 Proposer runtime 逻辑，本次不要重新设计历史可见性。

### 18.1 `/workspace` 必须是整 episode 持久的 RW 挂载（实现硬要求）

这是一个容易被漏掉、但 Step 3 会直接撞上的要求。现状：`run_research_command` 每次都是一次独立 `apptainer exec`，可写区只有一个 `research_batch` 生命周期内的 `/scratch` tempdir，且 `cwd` 枚举只有 `source | scratch`（`simpleloop/roles/research_tools.py`）。如果 proposer 要"写代码 → 下一步编译 → 再下一步跑"，**可写区必须是整个 episode 稳定的同一个路径**，不能每条命令换 scratch。

落地要求：

- `research_exec_argv` 新增一个稳定 RW 挂载：`/workspace` ← `run/lanes/lane-N/workspace:rw`。
- `run_research_command` 的 `cwd` 枚举从 `{source, scratch}` 扩展为含 `workspace`；workspace 是默认且唯一的持久可写区。
- 对应地，`ResearchCommandRunner` 的 git env（`GIT_WORK_TREE` / `GIT_DIR` / `GIT_COMMON_DIR`）指向 `/workspace` 及其挂靠的 `run/repo/.git`。
- `/source`（只读快照，= base_sha）、`/repo`（只读，= run/repo）、`/scratch`（临时）可保留，但持久可写主战场是 `/workspace`。

核心原则：

> Harness 通过 mount namespace 定义 Proposer 的文件世界，而不是依靠 prompt 描述 filesystem policy。

---

## 19. 建议新增 `LaneWorkspace`

职责应保持极小。

```python
class LaneWorkspace:
    def __init__(self, run_repo: Path, lane_root: Path):
        ...

    def create(self, base_sha: str) -> Path:
        # Remove any previous proposer workspace.
        # Create an independent full Git clone from run_repo.
        # Checkout base_sha in detached HEAD state.
        # Return workspace path.
        ...

    def dispose(self) -> None:
        # Remove the proposer workspace.
        ...

    @property
    def path(self) -> Path:
        ...
```

`LaneWorkspace` 不负责：

- History
- Memory
- Proposal generation
- Candidate creation
- Gate
- Eval
- Selection
- Tree scheduling

---

## 20. 建议新增 Proposer Lane Worker

为了支持 HEPJob Stage A，可以新增类似：

```text
simpleloop/proposer_worker.py
```

或：

```text
simpleloop/lane_proposer_worker.py
```

职责：

```text
load LaneAssignment
      ↓
create LaneWorkspace(base_sha)
      ↓
reconstruct existing Proposer runtime/dependencies
      ↓
run existing Proposer logic
      ↓
write proposal result
      ↓
dispose workspace if appropriate
```

注意：

> 该 worker 只是把现有 Proposer 调用搬到可调度执行单元中，不应重构 Proposer 科研逻辑。

---

## 21. 建议的数据输出

一个 Lane Proposer job 输出可以很简单：

```python
@dataclass
class LaneProposalResult:
    lane_id: int
    round_id: int
    base_sha: str
    proposals: list[Proposal]
```

具体 `Proposal` schema 沿用现有实现。

不要在本次引入新的 scientific metadata schema。

---

## 22. LocalBackend 与 HEPJobBackend

两种 Backend 应最终具有相同语义。

### Local

```text
for each Lane:
    create workspace
    run proposer locally

collect proposals

run existing candidate workers
```

### HEPJob

```text
submit proposer jobs per lane
      ↓
collect proposal result files
      ↓
flatten proposals
      ↓
submit existing candidate HEPJobs
```

本次如果时间有限，可以优先保持现有 Backend 结构，仅新增对应函数，不要求做大规模接口统一。

---

## 23. Retry / Failure 语义

### 23.1 Proposer Job Retry

Proposer Workspace 本身是 disposable。

基础设施 retry 时应重新：

```text
delete workspace
  ↓
clone run/repo
  ↓
checkout base_sha
  ↓
rerun proposer
```

是否需要保证 retry 时 proposal 完全一致，不在本次设计中强制。

如果未来需要严格的 episode checkpoint / deterministic infra retry，可另行设计。

### 23.2 Candidate Retry

继续沿用现有 CandidateWorker / Backend retry 语义。

本次不改。

### 23.3 部分 Lane 失败

多 lane 并行后必然出现：lane-1 的 job 挂了、lane-0/2 正常。处理原则：

> **Lane 只是 proposal 来源；少几个 proposal 不是 round 失败。**

Frontend 收集所有 lane 结果时，**用成功 lane 的 proposal 继续 flatten 进 CandidateWorker**；失败 lane 记一条 error trace（沿用现有 `LaneResult(outcome="error")` 语义），不阻断本轮。

这与现有 `InfraRoundError` + `inflight_round.json` 的"整轮要么成功要么整轮重提"语义有交集，需明确：**部分 lane 失败不触发 `InfraRoundError`**——只有当全部 lane 都失败（本轮 0 个 proposal）时，才走现有的 abstain / 失败路径。基础设施级失败（schedd 不可达、共享文件系统挂掉）仍按 `InfraRoundError` 处理。

---

## 24. `run/repo` 的 Git 历史不变量

为了让历史 SHA 可回查，必须保证：

> 实验记录中仍被引用的 Candidate SHA 必须继续存在于 canonical `run/repo` 中。

即：

```text
history records SHA X
        ↓
git -C run/repo cat-file -e X
must succeed
```

不要让 worktree 清理、branch 清理或 Git GC 意外删除仍被实验历史引用的 commit。

这不是 History schema 修改，而是 Git storage invariant。

---

## 25. 推荐目录结构

第一版不需要复杂：

```text
run/
├── repo/                         # canonical per-run Git repo
├── history / memory / ...        # existing structure, unchanged
│
├── lanes/
│   ├── lane-0/
│   │   └── workspace/            # proposer episode workspace
│   ├── lane-1/
│   │   └── workspace/
│   └── lane-2/
│       └── workspace/
│
└── worktrees/                    # existing candidate worktrees
    ├── r0-l0-c0/
    ├── r0-l0-c1/
    ├── r0-l1-c0/
    └── ...
```

不要为了“整齐”把现有 history / memory / candidate storage 全部迁移到 Lane 目录。

本次保持最小侵入。

---

## 26. 推荐实施顺序

### Step 1：实现 `LaneAssignment`

只增加逻辑标识：

```text
lane_id
round_id
base_sha
```

不改变现有执行行为。

### Step 2：实现 `LaneWorkspace`

完成：

```text
run/repo
  ↓
independent clone
  ↓
checkout base_sha
  ↓
RW workspace
```

增加最基本测试：

- HEAD == base_sha
- historical candidate SHA 可 `git show`
- workspace modification 不影响 run/repo
- recreate 后上一 episode dirty state 消失

### Step 3：让 Proposer 能在 Lane Workspace 中运行

把当前 Proposer 的 source 工作目录替换为 Lane Workspace。此阶段先保证本地执行成功，不修改 Proposer 的 history / memory / reasoning logic。

本步包含两个容易被漏的子任务：

1. **持久 RW `/workspace` 挂载 + `cwd` 枚举扩展**（见 §18.1）。改 `research_exec_argv` 增加稳定 `/workspace:rw` 挂载，`run_research_command` 的 `cwd` 枚举增加 `workspace`，`ResearchCommandRunner` 的 git env 指向 `/workspace`。否则 proposer 无法跨步骤"写→编译→跑"。
2. **收敛 prompt 的 fs policy 描述**（见 §10.2/10.3）。`_RUNTIME_BOUNDARIES` 从"/source 只读、/scratch 可写"改为"/workspace 是你的可写实验台"，并补"lab 离线、测得数字不作 merit 事实"两条约束。

同时把 `simpleloop/loop.py` 里 `_next_proposals` 现有的 `add_worktree(f"proposer-{round_id}")` / `remove_worktree` 调用替换为 `LaneWorkspace.create(base_sha)` / `dispose()`（旧 proposer worktree 路径的清理见 Step 7）。

### Step 4：增加多 Lane Proposer

当前一轮：

```text
1 Proposer -> N proposals
```

改为：

```text
N Lane Proposers -> each 1..N proposals
```

然后 flatten proposals，继续进入现有 CandidateWorker。

### Step 5：增加 HEPJob Proposer Worker

将每个 Lane 的 Proposer episode 提交为 HEPJob。

Frontend 收集 proposal result。

### Step 6：接回现有 Candidate HEPJob

```text
Lane proposals
   ↓
flatten
   ↓
existing CandidateWorker submission
```

验证最终 round behavior 与现有 gate / eval / selection 正常衔接。

### Step 7：清理旧 Proposer temporary worktree 逻辑

确认 Lane Workspace 成为唯一 Proposer source workspace 后，删除旧的临时 proposer worktree 路径。

---

## 27. 必须测试的行为

### Git isolation

在 Proposer Workspace：

```bash
echo test >> file
git commit ...
git reset ...
```

确认：

```text
run/repo
```

完全不受影响。

### Historical SHA visibility

创建至少一个历史 Candidate commit `B`，当前 base 为 `D`。

在 Lane Workspace @ D：

```bash
git show B
git diff B..D
```

必须成功。

### Fresh episode

Round r：

```text
workspace @ A
modify foo
create temp files
private commit P
```

Round r+1：

```text
workspace @ D
```

确认：

- foo 的 dirty changes 消失
- temp files 消失
- private commit P 不作为新 workspace 初始状态存在
- HEAD == D

### Candidate isolation

Proposer 修改 `foo.cc` 后提出 proposal。

Candidate worktree 必须仍从 canonical `base_sha` 创建。

Candidate 初始源码不能包含 Proposer 的 dirty change。

### Multiple Lane isolation

Lane 0 和 Lane 1 同时 @ A：

```text
Lane 0 modifies X
Lane 1 modifies Y
```

二者互相不可见。

### HEPJob

Proposer HEPJob 能在共享 filesystem 上：

1. 创建/获得自己的 Lane Workspace；
2. 执行 Proposer；
3. 写回 proposals；
4. frontend 收集；
5. 后续 Candidate HEPJobs 正常运行。

---

## 28. 本次明确 Non-Goals

执行 Agent 不应借本次重构顺手修改以下内容：

### 不做 Tree Evolution

暂时：

```text
all lanes -> accepted_sha
```

Tree 以后只改变 assignment 的 `base_sha`。

### 不改 History

不要修改：

- history schema
- Experiment Ledger
- Finding Archive
- Frontier
- history query logic
- history visibility phases
- historical prompt injection

### 不改 Proposer 方法论

不要修改：

- Proposer internal state machine
- scientific reasoning flow
- generator/cognition logic
- proposal verification logic

可写 workspace 不算方法论变更——它只扩大 proposer 的**理解**手段（§10.2），不改它的 epistemic 契约（§10.3：merit 仍归 Harness）。让 proposer 用 toy benchmark 做 merit 裁定（empirical oracle 路线）属于本 non-goal，不在 v0.2.0 范围。

### 不改 CandidateWorker 核心流程

不要重写：

```text
Executor -> Gate -> Commit -> Eval -> Result
```

除非为了 Lane identity 传递做最小参数调整。

### 不改 Selection

仍然使用当前 v0.2.0 的 accepted/best selection。

### 不做 Workspace Memory

不要加入：

```text
persistent cache
persistent scratch
workspace notes
workspace memory
```

跨 episode 文件继承。

### 不把 Proposer patch 传给 Executor

Proposal 是唯一 formal handoff。

### 不做常驻 Container

Lane 是逻辑 identity，不是常驻 Apptainer instance。

---

## 29. 最终硬不变量

实现完成后必须满足以下 6 条：

1. **Lane 是 Proposer research episode 的逻辑槽位，不是 CandidateWorker、Container 或 HEPJob。**
2. **每个 Proposer episode 的 Workspace 都是挂靠 canonical `run/repo` 的可写 Git 工作树（默认 linked worktree；独立 clone 仅作可选降级，见 §7.1），并 checkout 到指定 `base_sha`。**
3. **Workspace 不继承上一 episode 的任何 filesystem 状态。**
4. **Workspace 包含完整 run Git 历史，因此现有认知侧可以继续通过历史实验 SHA 检查和比较源码。**
5. **Proposer Workspace 和 Candidate Workspace 没有 filesystem inheritance；二者只通过 Proposal 通信。**
6. **除 Lane 调度与 Proposer Workspace 外，v0.2.0 现有 History、Memory、CandidateWorker、Gate、Eval、Experiment Record、Selection 等核心组件保持原样。**

---

## 30. 一句话设计总结

> **把 SimpleLoop v0.2.0 已有的"进程内并发 lane"（`ProposerOrchestrator` 的线程池）升格为"跨进程 / 跨 HEPJob 的独立 lane episode"：因为 workspace 要从只读变可写，并发 lane 必须分家，于是每条 lane 拥有一个挂靠 `run/repo`、从当前 `base_sha` 初始化、可写、含完整 run Git 历史、episode 间不继承状态的临时实验室（理解辅助型，merit 仍归 Harness）；lane 数与配额沿用现有推导，每条 lane 产 `select_quota` 个 proposal，flatten 后交给现有 CandidateWorker 并行验证。**

这套设计只改变 lane 的执行形态与源码工作空间，不改变 SimpleLoop 当前的科学研究核心逻辑，并为后续：

```text
node -> lane -> proposals -> child candidates
```

的 Tree Evolution 保留自然接口。
