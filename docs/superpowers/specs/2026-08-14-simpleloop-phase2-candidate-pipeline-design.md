# SimpleLoop Phase 2 Candidate Pipeline 设计

**状态：** 已确认，待实施计划

**日期：** 2026-08-14

**分支：** `refactor/phase0-phase1-typed-contracts`

**上游规格：** `2026-08-14-simpleloop-typed-pipeline-refactor-design.md`

## 1. 目标

Phase 2 把一次 candidate 的业务流程收敛为唯一、可直接调用的 typed pipeline：

```text
CandidateRequest
→ Executor
→ inspect changes
→ commit artifact
→ Evaluator
→ Gate
→ CandidateResult
```

Local backend 与 HPC candidate worker 必须调用同一个 `run_candidate()`，而不是维护两份相似实现。`loop.py` 只调用 backend 的 candidate batch 接口，不再创建 candidate worktree、运行 Executor、执行 eval 或构造失败结果。

本阶段为未来树演化保留自然扩展点：每个 `CandidatePlan` 独立声明 `parent_sha`，因此同一 batch 可从不同父节点展开。但树的拓扑、展开、打分和剪枝不属于 Candidate Pipeline。

## 2. 必须保持的行为

- Local 与 HEPJob 执行路径保持可用。
- Agent、Apptainer、Git workspace、eval 命令和 hard gate 的现有运行语义不变。
- `CandidateResult` 继续是唯一内存业务结果。
- candidate worker、history、resume 和 RSI 的 Phase 0 durable fixtures 保持兼容。
- worker 继续原子写 `result.json`、`usage.json` 和 `_FINISHED`。
- candidate telemetry finalization 继续由当前 loop 层完成。
- candidate worktree 继续由 execution backend 创建和清理。
- baseline evaluation 的执行、Condor 提交、轮询和恢复保持原位。

## 3. 非目标

- 不实现 `WorldBuilder`、`SourceWorkspace` 或 `ExecutionSandbox`；它们属于 Phase 3。
- 不统一 Local/HEPJob Scheduler、retry、poll、resume 或 worker envelope；它们属于 Phase 4。
- 不建立最终 `run_round`、`run_loop` 或 Composition Root；它们属于 Phase 5。
- 不拆分 RSI；它属于 Phase 6。
- 不改变 selection、objective improvement、incumbent advancement 或 static proposal 语义。
- 不删除 `RunContext`；只禁止 Candidate Pipeline 接收它。
- 不加入 tree depth、child list、branch score、pruning policy 等树专用概念。
- 不创建通用插件系统、IOC 容器或事件总线。

## 4. 选择的方案

采用渐进式 Candidate Ports：现在建立窄而真实的 Executor、ArtifactWorkspace、Evaluator、Gate 和 Trace 边界，用 adapter 包装现有 Agent、Workspace、Apptainer eval 与 handoff。Phase 3 替换运行环境机制时，Candidate Pipeline 本身不再重写。

不采用纯搬迁方案，因为它会继续把 `cfg`、Agent、Workspace 和 Apptainer 捆成 `CandidateDeps`，Phase 3 必须再次拆解；也不合并 Phase 2 与 Phase 3，因为同时改变业务流程和隔离机制会扩大回归面。

## 5. 模块所有权

```text
simpleloop/
  candidate.py              Candidate contracts、ports、run_candidate
  candidate_worker.py       manifest/CLI、adapter 组装、结果落盘

  stages/
    executor.py             AgentExecutor 与 Executor contracts
    evaluator.py            HarnessEvaluator、baseline validation
    gate.py                 GateSpec 与 typed pure gate decision
    selector.py             保持当前 selection 责任

  execution/
    local.py                Local batch 并发与 worktree 生命周期
    hepjob.py               Condor prepare/submit/supervise/collect
    base.py                 backend 接口与共享 execution errors
```

`simpleloop/roles/executor.py` 的实现迁移到 `simpleloop/stages/executor.py`；生产代码和测试迁移后删除旧文件，避免两份 Executor 逻辑并存。旧 `harness/evals.py` 与 `harness/gate.py` 中被新 stage 接管的业务实现同样只保留一个 owner；与 reporting 共用的纯 metric helper 可暂时留在原处，直至对应消费者迁移。

## 6. Domain Requests

### 6.1 Candidate batch

```python
@dataclass(frozen=True)
class CandidatePlan:
    candidate_id: int
    parent_sha: str
    proposal: Proposal


@dataclass(frozen=True)
class CandidateBatchRequest:
    round_id: int
    candidates: tuple[CandidatePlan, ...]
```

当前 round 为所有 plan 填写相同 `parent_sha`。未来 tree orchestrator 可以提交不同父节点，而无需修改 Candidate Pipeline。

backend 的 live 接口变为：

```python
class ExecutionBackend:
    def run_candidates(
        self,
        request: CandidateBatchRequest,
        *,
        journal: RoundJournal | None = None,
    ) -> tuple[CandidateResult, ...]:
        ...
```

### 6.2 Single candidate

backend 打开 worktree 后，把 plan 转成可执行 request：

```python
@dataclass(frozen=True)
class CandidateRequest:
    round_id: int
    candidate_id: int
    parent_sha: str
    proposal: Proposal
    worktree: Path
```

`CandidateRequest` 不包含 `cfg`、`RunContext`、runtime、result directory、scheduler handle 或 retry state。

## 7. Stage Ports

### 7.1 Executor

```python
@dataclass(frozen=True)
class ExecutorConfig:
    goal: str
    gate_block: str = ""
    prompt_dir: Path | None = None


@dataclass(frozen=True)
class ExecutionRequest:
    round_id: int
    candidate_id: int
    proposal: Proposal
    worktree: Path


class Executor(Protocol):
    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        ...
```

`AgentExecutor(agent, config)` 组装现有 executor prompt、调用 Agent 并解析 `SELF_REPORT`。它不检查 Git、不 commit、不 eval、不 gate、不写 history。

`AgentError` 和当前明确视为 executor 业务失败的输入错误由 adapter 转成 `ExecutionResult(status="EXECUTOR_FAILED", reason=...)`，而不是把 Agent 细节泄漏给 pipeline。

### 7.2 Artifact workspace

```python
@dataclass(frozen=True)
class CommitRequest:
    round_id: int
    candidate_id: int
    parent_sha: str
    worktree: Path
    changed_paths: tuple[PurePosixPath, ...]


class ArtifactWorkspace(Protocol):
    def inspect(self, worktree: Path) -> tuple[PurePosixPath, ...]:
        ...

    def commit(self, request: CommitRequest) -> CandidateArtifact:
        ...
```

`GitArtifactWorkspace` 是当前 `harness.workspace.Workspace` 的窄 adapter。它不创建或清理 worktree；backend 保持生命周期所有权。Phase 3 的正式 `SourceWorkspace` 可替换该 adapter，而不改变 `run_candidate()` 的步骤。

### 7.3 Evaluator

```python
@dataclass(frozen=True)
class EvaluationConfig:
    commands: tuple[str, ...]
    objective_key: str
    gate_keys: tuple[str, ...]
    timeout_seconds: int = 600
    output_cap_chars: int = 16000


@dataclass(frozen=True)
class EvaluationRequest:
    worktree: Path


class Evaluator(Protocol):
    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        ...
```

`HarnessEvaluator(runtime, config)` 是现有 Apptainer eval 的 adapter。Pipeline 只看到 `EvaluationResult`，不构造 Apptainer argv、bind 或 subprocess environment。

Evaluator 无法启动、timeout 或 runtime failure 返回带 `error` 的 `EvaluationResult`；非零 eval command 通过 `returncodes` 表达。两者分别映射为 `EVAL_FAILED` 与 `GATE_REJECTED`，保持现有行为。

### 7.4 Gate

```python
@dataclass(frozen=True)
class GateSpec:
    objective_key: str
    gate_keys: tuple[str, ...]


def apply_gates(
    evaluation: EvaluationResult | None,
    spec: GateSpec,
    *,
    skip_reason: str | None = None,
) -> GateDecision:
    ...
```

`apply_gates()` 是纯函数。它生成保留现有 wire schema 的 `PATHS`、`EVAL_COMMANDS` 和配置 hard gates。

`GateDecision.eligible` 在 Phase 2 保留，避免改变 worker/history codec；它继续表示 artifact 存在、全部 gate 通过且 objective 为有限数值。Phase 5 重构 Round/Selector 时再决定是否将 eligibility 完全归入 Selector。

### 7.5 Trace

```python
class CandidateTrace(Protocol):
    def record_execution(
        self,
        request: CandidateRequest,
        execution: ExecutionResult,
        artifact: CandidateArtifact | None,
    ) -> None:
        ...

    def record_evaluation(
        self,
        request: CandidateRequest,
        evaluation: EvaluationResult | None,
        gate: GateDecision,
        status: CandidateStatus,
    ) -> None:
        ...
```

`HandoffCandidateTrace(run_dir)` 保留现有 executor/eval handoff 文件。Trace 只记录事实，不能修改或替换业务结果。为保持当前审计语义，trace write failure 不静默忽略，而由 guarded runner 归一为 `WORKER_FAILED`。

## 8. Candidate Pipeline

```python
def run_candidate(
    request: CandidateRequest,
    *,
    executor: Executor,
    artifacts: ArtifactWorkspace,
    evaluator: Evaluator,
    gate_spec: GateSpec,
    trace: CandidateTrace,
) -> CandidateResult:
    ...
```

顺序固定：

1. 调用 Executor。
2. Executor 失败则记录 execution trace，返回 `EXECUTOR_FAILED`。
3. inspect worktree。
4. 没有变化则记录 trace，返回 `NO_CHANGE`；Evaluator 不运行。
5. commit 精确 changed paths，得到 `CandidateArtifact`。
6. 调用 Evaluator。
7. Evaluator 有 `error` 则生成 failed gate、记录 trace，返回 `EVAL_FAILED` 并保留 artifact。
8. 对正常 evaluation 调用 `apply_gates()`。
9. gate 未通过则返回 `GATE_REJECTED`；通过则返回 `COMPLETED`。

共享 wrapper：

```python
def run_candidate_guarded(
    request: CandidateRequest,
    *,
    executor: Executor,
    artifacts: ArtifactWorkspace,
    evaluator: Evaluator,
    gate_spec: GateSpec,
    trace: CandidateTrace,
) -> CandidateResult:
    ...
```

它只捕获 pipeline 未预期异常并生成 `WORKER_FAILED`。Local backend 与 candidate worker 都调用该 wrapper，确保相同异常获得相同业务状态。manifest 无法读取、result path 不可用等 worker protocol/infrastructure failure 仍在 wrapper 外处理。

## 9. Backend 与 Worker 数据流

### 9.1 Local

```text
CandidateBatchRequest
→ LocalBackend ThreadPool
→ per CandidatePlan: add_worktree(parent_sha)
→ CandidateRequest
→ run_candidate_guarded
→ finally remove_worktree
→ ordered tuple[CandidateResult, ...]
```

并发结果必须按 `CandidatePlan` 输入顺序返回。一个 candidate failure 不能取消兄弟 candidate。

Local candidate batch 代码从 `loop.py` 迁入 `execution/local.py`。迁移完成后，`execution/local.py` 不得 import `simpleloop.loop`；时间戳 helper 和 `BaselineAcceptanceError` 迁到各自 owner，而不是用延迟 import 绕过依赖方向。

### 9.2 HEPJob

```text
CandidateBatchRequest
→ manifest transport rows
→ existing Condor submit/supervise/resume
→ candidate_worker
→ CandidateRequest
→ run_candidate_guarded
→ CandidateResult codec
→ existing collect
```

HEPJob 的 submit、query、retry、resume 和 collect 本阶段保持原结构。manifest 可以继续包含 `run_dir`、`worktree_path`、`result_dir`、`prompt_dir` 和 `attempt`，但这些字段只属于 transport `CandidateSpec`，不能进入领域 `CandidateRequest`。

### 9.3 Loop

Loop 从 `ProposalBatch` 构造 `CandidateBatchRequest`。当前线性 loop 为每个 `CandidatePlan` 使用同一 incumbent SHA，candidate id 按 proposal 顺序确定。

以下旧函数从 `loop.py` 删除：

- `_run_candidates`
- `_run_candidate_guarded`
- `_deps_from_ctx`
- `_run_one_candidate`
- `_candidate_failure`

Loop 仍负责 terminal candidate telemetry finalization、selection、RoundResult 和 history append；这些责任将在后续 Phase 5 迁移。

## 10. Baseline 边界

共享错误和纯校验移到 evaluator stage：

```python
class BaselineAcceptanceError(RuntimeError):
    ...


def validate_baseline(
    evaluation: EvaluationResult,
    gate_spec: GateSpec,
) -> None:
    ...
```

它校验：

- 所有 eval return code 为 0；
- objective 存在、是非 bool 的有限数值；
- 所有配置 hard gate 为 `True`。

Local 与 HEPJob baseline path 调用同一校验。它们的 worktree lifecycle、Condor job shape、poll 和 cleanup 本阶段不统一。这样可以消除 backend 对 `loop.py` 中错误类型的依赖，又不提前实施 Phase 4。

## 11. 错误分类

| 情况 | 表达方式 |
|---|---|
| Agent expected failure | `EXECUTOR_FAILED` |
| worktree 没有变化 | `NO_CHANGE` |
| evaluator 启动、timeout 或 runtime failure | `EVAL_FAILED` |
| eval command 非零 | `GATE_REJECTED` |
| hard gate false/unknown | `GATE_REJECTED` |
| gate 通过且 objective 有效 | `COMPLETED`，eligible |
| pipeline 未预期异常 | `WORKER_FAILED` |
| manifest/result codec 错误 | worker protocol boundary |
| scheduler/node/lost-result | backend infrastructure boundary |

Phase 2 不重新定义 retry policy。HEPJob 仍仅按当前 completion marker 语义区分 business terminal 与 infrastructure failure。

## 12. 测试

### 12.1 Pipeline unit tests

使用 fake Executor、ArtifactWorkspace、Evaluator 和 CandidateTrace 覆盖：

- completed；
- executor failure；
- no-change 且 evaluator 未调用；
- eval error 且 artifact 保留；
- nonzero eval command；
- hard gate false 与 unknown；
- non-finite objective 不 eligible；
- inspect、commit、trace 或其他未预期异常归一为 `WORKER_FAILED`。

每个测试同时断言 port 调用顺序和未调用的后续阶段。

### 12.2 Stage unit tests

- AgentExecutor prompt、SELF_REPORT parse 和 AgentError normalization。
- GitArtifactWorkspace inspect/commit request mapping。
- HarnessEvaluator typed result mapping。
- `apply_gates()` 的 reserved keys、missing metric 和 objective validity。
- `validate_baseline()` 的 command、objective、hard gate failures。

### 12.3 Backend tests

- Local serial 与 parallel path 都调用共享 guarded runner。
- 输出顺序等于输入 plan 顺序。
- 每个 plan 使用自己的 `parent_sha`。
- success 与 failure 都清理 worktree。
- 一个 candidate 异常不取消兄弟 candidate。
- HEPJob manifest 从 typed plan 投影，collect 仍返回 `CandidateResult`。

### 12.4 Compatibility and architecture tests

- Phase 0 candidate worker fixture 精确相等。
- history、inflight、proposer lane 和 RSI fixture 继续通过。
- AST guard：`execution/local.py` 不 import `simpleloop.loop`。
- AST guard：`loop.py` 不再定义 Candidate 执行函数。
- AST guard：Candidate Pipeline 不访问 `cfg` 或 `RunContext`。
- dependency guard：Host pipeline 继续不 import `proposer.*`。
- 完整 `python -m pytest -q` 与 `python -m compileall -q simpleloop tests` 通过。

## 13. 完成条件

Phase 2 完成时必须同时满足：

```text
run_candidate implementation                         1 套
Local 与 candidate worker 共用 guarded pipeline      是
execution/local.py import simpleloop.loop             0 处
loop.py 中 candidate execution helper                 0 个
Candidate Pipeline 读取 cfg / RunContext              0 处
Local/HPC/worker/history/resume/RSI 行为回归            0 个
Phase 0 durable fixture 变化                           0 个
```

每迁移一个责任就删除对应旧实现；禁止把新 stage 与旧 `candidate_worker.run_candidate` 长期并存。
