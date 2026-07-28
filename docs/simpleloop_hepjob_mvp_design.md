# SimpleLoop CandidateWorker + HEPJobBackend MVP 设计

## 1. 目标

在不改变 SimpleLoop 当前业务语义的前提下，将现有单 candidate 执行逻辑从前台进程中抽离为可独立启动的 `CandidateWorker`，并增加 `HEPJobBackend`，使每个 candidate 能作为独立 HEPJob 作业运行。

MVP 只解决两件事：

1. 一个 candidate 可以在独立进程或计算节点中完整执行：`Executor → Gate/Eval → Judger`。
2. HEPJobBackend 能管理作业的提交、等待、Hold、超时、丢失、重试和结果回收，保证每个 candidate 最终进入明确终态，而不是让 Harness 无限等待。

---

## 2. 当前状态

当前 `structure_opt` 分支中，一个 candidate 的完整执行逻辑仍集中在 `loop.py` 的单 candidate 执行函数中：

```text
创建 candidate worktree
→ Executor 修改代码
→ Harness 检查修改并创建 commit
→ 运行 Gate/Eval
→ Judger 评价
→ 返回 candidate result
→ 清理 worktree
```

多个 candidate 可以通过本地线程池并发，但仍存在以下限制：

- 运行依赖前台 SimpleLoop 进程中的对象；
- 没有独立 CLI；
- 输入无法单独序列化；
- 输出主要通过函数返回；
- 前台进程退出后 candidate 无法继续；
- 不能直接作为 `hep_sub` 作业提交。

因此，当前已经存在 CandidateWorker 的业务逻辑，但还没有形成独立的 CandidateWorker 执行单元。

---

## 3. MVP 总体结构

```text
Proposer
   ↓
生成一批 proposals
   ↓
Harness 创建 CandidateSpec
   ↓
ExecutionBackend
   ├── LocalBackend
   │      └── 本地运行 CandidateWorker
   │
   └── HEPJobBackend
          └── hep_sub 提交 CandidateWorker
                         ↓
             Executor → Gate → Judger
                         ↓
                  CandidateResult
   ↓
Harness 汇总结果
   ↓
跨 candidate find best
   ↓
更新 accepted base 和历史
```

基本计算单元定义为：

> 一个 HEP job = 一个 candidate 从 proposal 到 judgment 的完整生命周期。

单个 candidate 内部仍然串行执行：

```text
Executor → Gate/Eval → Judger
```

不同 candidate 之间由 HEPJob 并行。

---

## 4. CandidateWorker

### 4.1 职责

`CandidateWorker` 负责一个 candidate 的完整业务执行：

1. 读取 CandidateManifest；
2. 打开指定 candidate worktree；
3. 重建运行所需的 runtime、Executor 和 Judger；
4. 调用 Executor 修改代码；
5. 执行 changed-path 检查和 Harness commit；
6. 运行 Gate/Eval 并解析 metrics；
7. 调用 Judger；
8. 原子写入 CandidateResult；
9. 创建完成标记。

CandidateWorker 不负责：

- 生成 proposal；
- 提交或管理 HEPJob；
- 比较同一 round 中的其他 candidate；
- 选择 winner；
- 更新 accepted base；
- 写入全局历史或 Search Memory。

### 4.2 独立启动形式

新增独立入口：

```bash
python -m simpleloop.candidate_worker \
  --manifest /path/to/candidate_manifest.json
```

HEPJob 的 Bash 脚本只负责加载必要环境并启动该入口：

```bash
#!/usr/bin/env bash
set -uo pipefail

source ~/.claude_batch_env

exec python -m simpleloop.candidate_worker \
  --manifest "$1"
```

业务逻辑不应重新写入 Bash。

### 4.3 内部执行顺序

```text
读取 manifest
→ 加载 resolved config
→ 打开已有 worktree
→ Executor
→ 检查修改路径
→ 创建 candidate commit
→ Gate/Eval
→ Judger
→ 写 result.json.tmp
→ os.replace(result.json.tmp, result.json)
→ touch _FINISHED
```

Judger 应进入 candidate job，因为 Judger 是逐 candidate 的工作。前台只保留跨 candidate 的 selection。

---

## 5. CandidateManifest

MVP 中，前台 Harness 在提交作业前为每个 candidate 写一个 manifest。

```json
{
  "run_id": "run_001",
  "round_id": 3,
  "candidate_id": 7,
  "attempt": 1,

  "run_dir": "/lustrefs/.../runs/run_001",
  "config_path": "/lustrefs/.../runs/run_001/config.resolved.json",

  "parent_sha": "abc123",
  "worktree_path": "/lustrefs/.../runs/run_001/worktrees/r3-c7",

  "proposal": {
    "family": "layout",
    "decision": "switch",
    "proposal": "..."
  },

  "prior_metrics": {
    "SPEED_MS": 706.69
  },

  "result_dir": "/lustrefs/.../runs/run_001/rounds/r3/candidates/c7"
}
```

Manifest 只保存可序列化数据，不保存前台 Python 对象。

---

## 6. CandidateResult

CandidateWorker 最终输出：

```text
rounds/r3/candidates/c7/
├── manifest.json
├── job.json
├── job.out
├── job.err
├── result.json
└── _FINISHED
```

`result.json` 尽量保持当前 candidate dict 的语义，并增加 execution 信息：

```json
{
  "candidate": 7,
  "family": "layout",
  "decision": "switch",
  "proposal": "...",

  "sha": "def456",
  "changed_paths": [
    "OMILRECV2/src/example.cc"
  ],

  "metrics": {
    "SPEED_MS": 680.10,
    "FCN": true,
    "CONSISTENCY": true,
    "EVAL_RESULT": true
  },

  "score": 0.88,
  "risk": "low",
  "feedback": "...",
  "feedback_for_proposer": "...",
  "accepted": true,

  "candidate_status": "COMPLETED",

  "execution": {
    "backend": "hepjob",
    "job_id": "123456.7",
    "attempt": 1,
    "host": "jnws082.ihep.ac.cn"
  }
}
```

结果必须原子写入：

```text
写 result.json.tmp
→ rename 为 result.json
→ 最后创建 _FINISHED
```

HEPJobBackend 只有看到 `_FINISHED` 后才读取结果。

---

## 7. HEPJobBackend

### 7.1 核心职责

`HEPJobBackend` 是 candidate 远程执行的监管器，负责：

1. 为每个 candidate 准备 manifest 和 worktree；
2. 调用 `hep_sub`；
3. 保存 JobID；
4. 周期查询 `hep_q`；
5. 处理 Idle、Running、Held、Timeout 和 Lost；
6. 必要时调用 `hep_rm`；
7. 重试基础设施失败；
8. 收集 CandidateResult；
9. 清理 candidate worktree；
10. 保证每个 candidate 最终进入明确终态。

其核心目标不是保证每个作业成功，而是：

> 保证每个提交的 candidate 最终得到可判定的终态，Harness 不会无限等待。

### 7.2 不属于 Backend 的职责

HEPJobBackend 不负责：

- Executor、Gate 或 Judger 的内部逻辑；
- proposal 的生成；
- candidate 的业务评分；
- 跨 candidate find best；
- accepted base 更新；
- Search Memory 更新。

---

## 8. 作业状态与 Candidate 状态

必须区分两个层次。

### 8.1 Scheduler 状态

由 HEPJobBackend 管理：

```text
SUBMITTED
IDLE
RUNNING
HELD
DISAPPEARED
CANCELLED
TIMEOUT
INFRA_FAILED
COMPLETED
```

### 8.2 Candidate 业务状态

由 CandidateWorker 输出：

```text
COMPLETED
NO_CHANGE
PATH_GATE_REJECTED
EXECUTOR_FAILED
EVAL_FAILED
GATE_REJECTED
JUDGER_FAILED
```

例如：

```text
scheduler = COMPLETED
candidate = GATE_REJECTED
```

表示作业正常完成，只是 candidate 没通过 Gate。

而：

```text
scheduler = HELD
candidate = 尚无结果
```

表示基础设施失败，不能作为 proposal 的负面实验结果进入 proposer 历史。

---

## 9. HEPJob 生命周期管理

### 9.1 Harness 等待条件

Harness 不等待“所有 candidate 都成功”，而等待：

> 所有 candidate 都进入终态。

终态包括：

```text
COMPLETED
INFRA_FAILED
TIMEOUT
CANCELLED
```

例如一轮 20 个 candidate：

```text
12 COMPLETED
3 GATE_REJECTED
2 EXECUTOR_FAILED
2 INFRA_FAILED
1 TIMEOUT
```

这一轮已经可以结束，并在有效 candidate 中 find best。

### 9.2 Held

不能无限等待 Held 作业。

MVP 策略：

```text
发现 HELD
→ hep_q -i JOB_ID -hold
→ 保存 hold reason
→ hep_rm JOB_ID
→ 清理旧 worktree
→ 从 parent_sha 重建干净 worktree
→ 若 attempt 未耗尽则重新提交
→ 否则标记 INFRA_FAILED
```

第一版不自动 `release`，避免对不可恢复原因反复 release。

### 9.3 Idle 超时

```text
IDLE 时间超过 idle_timeout
→ hep_rm
→ 若仍有重试次数则重新提交
→ 否则 INFRA_FAILED
```

### 9.4 Running 超时

```text
RUNNING 时间超过 run_timeout
→ hep_rm
→ 标记 TIMEOUT
```

MVP 中运行超时默认不重试，避免重复消耗大量算力；之后可根据任务类型配置。

### 9.5 作业从队列中消失

作业完成后可能无法继续从 `hep_q` 查到，因此：

```text
队列中消失
→ 检查 _FINISHED
```

若存在 `_FINISHED`：

```text
读取 result.json
→ COMPLETED
```

若不存在：

```text
等待 disappearance_grace_seconds
→ 再检查一次
→ 仍无结果则 LOST
→ 有重试次数则重新提交
→ 否则 INFRA_FAILED
```

### 9.6 全部 candidate 都基础设施失败

若一轮中至少有一个 candidate 产生业务结果，则正常结束该 round。

若所有 candidate 均为：

```text
HELD / LOST / TIMEOUT / INFRA_FAILED
```

则不应：

- 消耗一个有效 round；
- 写入 proposer 失败历史；
- 更新 Search Memory；
- 让 proposer 根据基础设施故障反思。

应保留 in-flight round，并暂停或等待人工恢复。

---

## 10. Worktree 所有权

MVP 继续复用当前共享 Git repo + 独立 worktree 模型。

### 前台 HEPJobBackend 负责

```text
顺序创建 candidate worktree
→ 写 manifest
→ 提交 job
→ 作业完成后删除 worktree
```

### CandidateWorker 负责

```text
只操作 manifest 指定的 worktree
→ Executor 修改
→ Harness 逻辑检查路径
→ Harness 逻辑创建 commit
→ Gate
→ Judger
```

不要让多个 worker 自己调用公共 repo 的 `add_worktree` 或 `remove_worktree`。

重试时不能复用可能已经变脏的 worktree：

```text
hep_rm
→ 删除旧 worktree
→ 从 parent_sha 新建 worktree
→ attempt + 1
→ 重新提交
```

MVP 暂不引入 clone + git bundle。只有在共享 Lustre worktree 并发 commit 测试失败时，再切换到 bundle 方案。

---

## 11. In-flight Round 持久化

当前前台进程可能在等待 HEP jobs 时退出，因此必须保存尚未结束的 round。

新增：

```text
run_dir/inflight_round.json
```

示例：

```json
{
  "round_id": 3,
  "parent_sha": "abc123",

  "reflection": "...",
  "proposals": [],

  "jobs": {
    "0": {
      "job_id": "123456.0",
      "attempt": 1,
      "state": "RUNNING",
      "manifest_path": ".../c0/manifest.json"
    },
    "1": {
      "job_id": "123456.1",
      "attempt": 2,
      "state": "IDLE",
      "manifest_path": ".../c1/manifest.json"
    }
  }
}
```

恢复逻辑：

```text
启动或 --continue
→ 检查 inflight_round.json
→ 若存在，不重新调用 Proposer
→ 继续 poll/reconcile 已提交 jobs
→ 必要时重试
→ 所有 candidate 终态后完成 selection
→ 写入正式 history
→ 删除 inflight_round.json
```

状态更新必须原子写入，避免进程中断留下半个 JSON。

---

## 12. Telemetry

当前线程级 telemetry 不能直接由多个 HEP worker 并发写同一个文件。

MVP 规则：

```text
CandidateWorker
→ 在 result.json 中保存本 candidate 的 token/usage

前台 Harness
→ collect 后串行合并到全局 telemetry
```

远端 worker 不直接写：

```text
history.jsonl
insights.jsonl
telemetry.json
summary.json
```

这些全局文件仍由前台 Harness 单写。

---

## 13. 配置

新增最小配置：

```yaml
execution:
  backend: hepjob

  hepjob:
    group: juno
    poll_seconds: 30
    max_attempts: 2
    idle_timeout_seconds: 7200
    run_timeout_seconds: 21600
    disappearance_grace_seconds: 60
    memory_mb: 8000
```

本地模式：

```yaml
execution:
  backend: local
```

现有 `loop.max_workers` 只用于 LocalBackend。

HEPJobBackend 中，提交数量由 `candidates_per_round` 决定，实际排队与并发交给 HEPJob。

---

## 14. 源码改动

建议新增：

```text
simpleloop/
├── candidate_worker.py
├── execution/
│   ├── __init__.py
│   ├── base.py
│   ├── local.py
│   ├── hepjob.py
│   └── models.py
```

### `candidate_worker.py`

包含：

```python
def run_candidate(spec, dependencies) -> dict:
    ...
```

以及：

```python
def main() -> int:
    ...
```

### `execution/base.py`

定义统一 Backend 接口：

```python
class ExecutionBackend:
    def run_candidates(
        self,
        *,
        proposals,
        round_id,
        parent_sha,
        prior_metrics,
    ) -> list[dict]:
        raise NotImplementedError
```

### `execution/local.py`

保留当前行为：

```text
ThreadPoolExecutor
→ 调用 run_candidate()
→ 直接返回 candidate dict
```

### `execution/hepjob.py`

实现：

```text
prepare
submit
poll
reconcile
retry
collect
cleanup
```

### `loop.py`

原 `_run_candidates()` 改为：

```python
candidates = execution_backend.run_candidates(
    proposals=proposals,
    round_id=round_id,
    parent_sha=parent_sha,
    prior_metrics=prior_metrics,
)
```

原 `_run_one_candidate()` 的业务逻辑迁入 `candidate_worker.run_candidate()`。

---

## 15. 实施顺序

### 阶段 1：抽出 CandidateWorker

```text
把 _run_one_candidate() 迁为 run_candidate()
```

仍使用本地线程池，保证行为完全不变。

验收：

- 当前测试通过；
- 当前 Local 模式输出不变；
- candidate schema 不变；
- accepted chain 不变。

### 阶段 2：本地独立进程验证

新增：

```bash
python -m simpleloop.candidate_worker --manifest ...
```

先在本地通过 subprocess 启动。

验证：

- manifest 可独立重建依赖；
- worker 能独立运行；
- result.json 可被前台回收；
- 前台退出后 worker 仍能继续。

### 阶段 3：单 candidate HEPJob

```text
固定 proposal
→ 创建 worktree
→ hep_sub
→ Executor → Gate → Judger
→ result.json
→ 前台 collect
```

### 阶段 4：作业异常管理

验证：

```text
HELD
IDLE timeout
RUNNING timeout
作业消失无结果
重试
重试耗尽
```

### 阶段 5：多 candidate round

```text
一轮提交 N 个 candidate
→ 混合成功和失败
→ 所有 candidate 进入终态
→ find best
```

### 阶段 6：恢复

```text
前台提交后退出
→ --continue
→ 不重新调用 Proposer
→ 恢复作业状态
→ 完成 round
```

---

## 16. MVP 验收标准

MVP 完成必须满足：

1. LocalBackend 行为与当前版本一致；
2. CandidateWorker 可通过 CLI 独立启动；
3. 一个 HEP job 能完成 `Executor → Gate → Judger`；
4. HEPJobBackend 能提交并记录 JobID；
5. Held 作业不会无限等待；
6. Idle 和 Running 有明确超时；
7. 作业消失后通过结果文件判断完成或 Lost；
8. 基础设施失败不会被当成 proposal 失败；
9. 所有 candidate 进入终态后 round 能结束；
10. 前台退出后可从 in-flight 状态恢复；
11. Judger 在 HEP job 内运行；
12. 前台只负责 proposal、作业管理、结果汇总和 find best。

---

## 17. MVP 暂不实现

以下能力不属于本次最小版本：

- 大规模 candidate 的 Top-K 淘汰；
- GEPA 式 candidate lineage；
- 多 proposer ensemble；
- candidate family 配额；
- early stopping；
- 动态资源申请；
- 自动分析 Hold reason 并修改资源；
- heartbeat；
- Web dashboard；
- SQLite 作业数据库；
- clone + git bundle 结果传输；
- 同节点 parent/candidate 配对性能测量；
- 跨节点 benchmark 归一化；
- 自动压缩和清理失败 candidate artifacts。

这些能力应在 CandidateWorker 和 HEPJobBackend 稳定后逐步加入。

---

## 18. 最终边界

### CandidateWorker

```text
一个 candidate 的业务执行者：
Executor → Gate → Judger → CandidateResult
```

### HEPJobBackend

```text
一个 candidate job 的生命周期管理者：
submit → poll → hold/timeout/lost/retry → collect
```

### Harness

```text
round 级协调者：
Proposer → 批量提交 → 等待全部终态 → find best → 更新 accepted chain
```

MVP 的核心不是把 `hep_sub` 塞进现有循环，而是形成一个稳定边界：

> 当前 `_run_one_candidate()` 的业务语义保持不变，但它不再依赖前台线程，而是成为可独立提交、可持久化输入输出、可被远程作业管理的 CandidateWorker。
