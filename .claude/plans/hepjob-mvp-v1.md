# SimpleLoop HEPJob 骨架版(v1)实施计划

## 目标与范围

把当前 `_run_one_candidate()` 从前台线程中抽离为可独立启动的 CandidateWorker,新增 HEPJobBackend 管理 hep_sub 作业生命周期,形成三层边界:

- **CandidateWorker**(业务):Executor → Gate/Commit → Eval → Judger → result.json
- **HEPJobBackend**(作业生命周期):submit → poll → Held/超时/消失 reconcile → collect
- **Harness**(round 协调):Proposer → backend.run_candidates → find best → history

v1 只要骨架跑通,明确**不做**:baseline 迁移到集群(本地跑)、节点配对测量、Idle 重提交(只警告)、API 并发封顶、自动 hold 原因分析。

## 已确认的设计决策(讨论结论)

1. Idle 超时只打警告,无动作;reconcile 只保留三个动作:HELD→rm+重试、RUNNING 超时→rm+TIMEOUT(不重试)、队列消失→查 `_FINISHED`→LOST→重试。attempt 耗尽→INFRA_FAILED。
2. Manifest 必须含 `baseline_metrics`(judger 的 vs-baseline 轴,run-state 不在 config 里)。
3. worker catch-all 不变量:**任何业务失败都落 result.json + `_FINISHED`;`_FINISHED` 缺席 ⇔ 基础设施死亡**(被 rm/OOM/节点宕)。
4. collect 层过滤:infra 失败的 candidate 不进 `append_generation`、不参与 `_select_winner`。
5. 作业环境由前台物化成 `run_dir/job_env.sh`(不依赖 `~/.claude_batch_env`),token 不进 job ad。
6. 共享 Lustre:不需要 HTCondor 文件传输,job out/err 直写 candidate 目录;不用 git bundle。
7. result.json 记录 `execution.host`,供后验节点方差分析。

## 阶段 0:集群冒烟测试(写代码前)

手工 hep_sub 一个作业验证四项,失败任何一项先调整再动工:

1. 计算节点 apptainer 可用且 `--userns` 能起(否则 worker 环境要设 `SIMPLELOOP_APPTAINER_USERNS=0`,见 runtime.py:82);
2. 计算节点 host python3 可 `import yaml`(worker 进程跑在 host 上,只依赖 stdlib+pyyaml;container 只用于 claude/eval payload);
3. Lustre run_dir 在计算节点可写;
4. `hep_q -autoformat`/`-af` 能拿到 JobStatus 数字码(1=Idle 2=Running 5=Held),确认 hep_sub/hep_q/hep_rm 的实际 flag 语法(group、mem、out/err),据此定稿 `_hep_sub_argv`/`_query_jobs` 两个函数。

## 源码改动

### 新增 `simpleloop/candidate_worker.py`

从 `loop.py:_run_one_candidate()`(loop.py:465-553)迁入业务逻辑,**不含 worktree 生命周期**(改由 backend 负责):

- `CandidateSpec` dataclass:round_id, candidate_id, parent_sha, proposal(family/decision/proposal), prior_metrics, worktree_path, result_dir, attempt;`to_dict/from_dict` 序列化。
- `CandidateDeps` dataclass:cfg, run_dir, runtime, workspace, executor_agent, judger_agent, gate_lines, baseline_metrics + usage_sink(list,收集 raw usage)。
- `build_deps(cfg, run_dir, usage_observer)`:重建 ApptainerRuntime/Workspace(不调 setup(),不 clone)/executor/judger Agent。LocalBackend 与 worker CLI 共用,保证两种 backend 业务行为一致。
- `run_candidate(deps, spec) -> dict`:现 `_run_one_candidate` 函数体基本原样(executor→commit→eval→judger→dict),差异:catch `Exception`(不只 AgentError/ValueError),按失败阶段写 `candidate_status`(COMPLETED/NO_CHANGE/GATE_REJECTED/EXECUTOR_FAILED/EVAL_FAILED/JUDGER_FAILED/WORKER_FAILED);不 add/remove worktree;usage 进 sink。
- `main()`:`python -m simpleloop.candidate_worker --manifest <path>` → 读 manifest → `config.load_resolved(run_dir)`(纯 JSON,不依赖 yaml 配置文件)→ build_deps → run_candidate → 写 `result.json.tmp` → `os.replace` → touch `_FINISHED`。catch-all 兜底:任何异常都尽力写失败 result + `_FINISHED`;只有 manifest 不可读才非零退出。result.json 附带 `execution:{backend, job_id, attempt, host}` 和 `usage:[...]`。
- 注意不 import loop.py(它会拉 matplotlib);worker 依赖链 = stdlib + pyyaml。

### 新增 `simpleloop/execution/`

- `base.py`:`ExecutionBackend.run_candidates(*, proposals, round_id, parent_sha, prior_metrics) -> list[dict]`;返回**仅业务终态**的 candidate dict。
- `local.py`:LocalBackend = 现 `_run_candidates`(loop.py:426-462)原逻辑(serial + ThreadPool 两路、telemetry snapshot attach),加每 candidate 的 worktree add/remove(从 run_candidate 上提)。
- `hepjob.py`:HEPJobBackend:
  - **prepare/submit**:顺序 add_worktree → 写 manifest.json(含 baseline_metrics)/job.sh → 首次提交前生成 `run_dir/job_env.sh`(mode 600;内容 = runtime.py `_FORWARDED_ENV` 集合中存在的变量 + `PYTHONPATH=<simpleloop 父目录>` + python 用 `sys.executable`)→ hep_sub → 解析 cluster id 写 job.json → 原子写 `inflight_round.json`(round_id, parent_sha, reflection, proposals, jobs[])。
  - **poll/reconcile**:周期 `hep_q`(只信成功查询;查询失败记日志继续等,不误判 LOST);Held→记录 HoldReason→hep_rm→重建干净 worktree→attempt+1 重提或 INFRA_FAILED;Running 超时→hep_rm→TIMEOUT;Idle 超阈值→一次性警告;队列消失→查 `_FINISHED`,有→读 result.json(malformed 按 INFRA),无→grace 后复查→LOST→重试或 INFRA_FAILED。每次状态迁移原子更新 inflight_round.json。
  - **collect**:全部终态后,业务 candidate 的 usage 逐个 `telemetry.record_usage()` 合并、attach telemetry snapshot、remove_worktree、删 inflight_round.json;infra 失败只记日志不进返回列表。
  - **resume(inflight)**:从 inflight_round.json + job.json + `_FINISHED` 重建状态,进入同一 poll 循环(与正常路径同一代码)。
  - hep_sub/hep_q/hep_rm 调用集中在三个小函数,cmd 模板可被 config 覆盖(冒烟测试后定稿默认 flag)。
- `__init__.py`:`build_backend(ctx)` 按 `cfg["execution_backend"]` 返回 Local/HEPJob。

### 修改 `simpleloop/config.py`

- `TASK_TOP_KEYS` 加 `"execution"`;解析 `execution.backend`(默认 `local`)和 `execution.hepjob` 子块(strict,unknown key 报错):group(可选)、poll_seconds(默认 30)、max_attempts(默认 2)、idle_warn_seconds(默认 7200)、run_timeout_seconds(默认 21600)、disappearance_grace_seconds(默认 120)、memory_mb(可选)。
- resolved dict 加 `execution_backend`、`hepjob` 两个 key。

### 修改 `simpleloop/loop.py`

- RunContext 加 `execution_backend`;`_build_context` 末尾 `build_backend(ctx)`。
- `_run_candidates` 删除,round 循环改为 `candidates = ctx.execution_backend.run_candidates(...)`;`_run_one_candidate`/`_candidate_failure` 迁出(loop 保留 thin re-export 以免破坏现有测试 import,或同步改测试)。
- `--continue` 恢复:在 `_starting_state` 之后、`_next_proposals` 之前检查 `inflight_round.json`;存在且 round_id == start_round 时跳过 proposer,走 `backend.resume()`,reflection 从 inflight 文件取回。
- 全 candidate infra 失败(返回空列表)时不写 history、不消耗 round,报错退出并提示:修复后 `--continue`(inflight 保留)或删除 inflight_round.json 重新生成 proposals。
- `_candidate_failure` 迁到 candidate_worker 后,`base_sha` 字段等保持现状语义。

### 测试

- 新 `tests/test_candidate_worker.py`:fake agent 下 run_candidate 产出 dict;CLI 端到端(假 manifest + monkeypatch agent)原子写 result.json + `_FINISHED`;catch-all:注入非 AgentError 异常仍写失败 result。
- 新 `tests/test_hepjob_backend.py`:tmp PATH 里放假 hep_sub/hep_q/hep_rm shell 桩,驱动:正常完成、Held→重试成功、Held→attempt 耗尽→INFRA_FAILED、Running 超时→TIMEOUT、消失有结果→COMPLETED、消失无结果→LOST→重试、collect 过滤 infra、inflight 写入与 resume、malformed result→INFRA。
- 现有 `test_parallel_candidates.py`/`test_telemetry.py`/`test_static_mode.py` 保持通过(它们 patch `loop_mod._run_one_candidate`/`_run_candidates`,迁移时保留同名薄封装或改测试 import,倾向改测试指向新模块,语义不变)。
- config 测试:execution 块默认值、unknown key 报错。

## 关键实现注意点

- **worktree 所有权**:只有 backend(前端)调 `add_worktree`/`remove_worktree`;worker 只用 manifest 给定路径。重试必须先 hep_rm 再删建 worktree。
- **worker 内 preflight**:跑 ApptainerRuntime.preflight()(60s),失败写 WORKER_FAILED result(不自作主张判 infra)。
- **telemetry 合并**:result.json 带 raw usage 列表,前端 collect 时串行 `record_usage`;全局文件(history/insights/telemetry/summary)仍只有前端单写。
- **Ctrl-C 语义**:poll 循环被中断时作业继续跑,inflight_round.json 在盘上,`--continue` 可恢复——不捕获 KeyboardInterrupt 做清理。
- **result dict 兼容**:store.append_generation 只挑已知 key,candidate dict 新增 `candidate_status`/`execution`/`usage` 不会进 history.jsonl,无 schema 迁移问题。
- **集群命令封装**:`_submit_job()/_query_jobs()/_remove_job()` 三处是唯一直接碰 hep_* 的地方,默认 hep_sub/hep_q/hep_rm,config 可覆盖 cmd 前缀(冒烟后若需 condor_submit/condor_q 直接换)。

## 验证

1. `python -m pytest tests/ -q` 全绿(重点:LocalBackend 行为与现状一致)。
2. 本地独立进程:tiny_algo_opt 手动构造 manifest,`python3 -m simpleloop.candidate_worker --manifest ...` 直接跑通,result.json + `_FINISHED` 正确。
3. 集群单 candidate:tiny_algo_opt 固定 proposal,hepjob backend 提交 1 个 job 走完整 Executor→Eval→Judger,前端 collect 成功。
4. 集群多 candidate 一轮(candidates_per_round=3),混合成功/失败,全终态后 find best 正常,history 无 infra 污染。
5. 恢复:提交后 kill 前端,`--continue` 不调 proposer 直接恢复 poll 并完成 round。
6. 异常注入:手工 hep_rm 一个作业(→LOST 重试)、构造 Held(→重试),观察状态迁移与 inflight 更新。

## 实施顺序

阶段 0 冒烟 → 1. 抽 candidate_worker + LocalBackend(本地行为不变,测试全绿)→ 2. worker CLI 本地独立验证 → 3. config + HEPJobBackend 单 candidate 上集群 → 4. reconcile 三动作 + 多 candidate → 5. inflight 持久化与 --continue 恢复。
