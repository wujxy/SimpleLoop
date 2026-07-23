# OMILREC v1.0.0 SimpleLoop run012 多方向并行探索总结

## 1. Run 概览

| 项目 | 结果 |
|---|---|
| 日志 | `omilrec-v100-selfloop-post-v107-run012.log` + `omilrec-v100-selfloop-post-v107-run012-continue.log` |
| 优化对象 | `omilrec-v100-postv107-gated`，baseline commit `4e7c823440` |
| 目标 | 验证单 round 内并行探索多个优化方向，并从多个候选中稳定选择下一轮 parent |
| 初始 baseline | **944.096 ms/evt** |
| 续跑时重测 baseline | **942.102 ms/evt** |
| 最终已接受结果 | **559.415 ms/evt**，commit `580c84beb5` |
| 累计提升 | 相对首段 baseline **-40.7%**，约 **1.69×**；按续跑 baseline 口径为 **-40.6%** |
| 实际完成情况 | R1–R14 完成；R15 已启动，但 proposer 结构化输出重试耗尽，未产生候选 |
| 候选规模 | 14 个完成 round × 3 candidates = **42 个候选** |
| 方向构成 | `continue` **22** 个，`switch` **20** 个 |
| 选择结果 | **13** 轮接受新 incumbent，**1** 轮（R5）保留原 incumbent |
| 胜出方向构成 | `continue` **7** 次，`switch` **6** 次 |

> 严格地说，这是一项“计划/启动到第 15 轮”的任务，而不是 15 个完整优化 round：R15 停在 proposer 阶段，因此只有 14 轮具备三方向实现、评测和选择结果。

## 2. 单 round 的多方向探索与选择流程

| 阶段 | 本次 run 中的行为 | 对稳定性的作用 |
|---|---|---|
| 1. 确定 parent | 每轮从当前 accepted base 出发；首段为 baseline，续跑从 R10 winner `357dee30f6` 恢复 | 所有方向共享同一比较起点，避免候选间基线不一致 |
| 2. Proposer 生成方向 | 每轮固定生成 3 个 candidate，并给出 `family` 与 `decision=continue/switch` | 同时覆盖沿已有证据深挖与切换机制两类探索 |
| 3. 隔离并行实现 | 3 个 executor 分别在 `worktrees/rX-c0..2` 中同时启动 | 候选代码互不覆盖；慢候选不会污染快候选 |
| 4. 独立评判 | 某候选实现完成后即可启动自己的 judger；executor 与 judger 在候选间流水并行 | 不必等待所有实现结束才开始评测，且保留每个候选独立反馈 |
| 5. Gate + objective | Judger 给出 correctness/landing 状态、score、risk，并在可评测时输出 `SPEED_MS`、vs prior、vs baseline | 先过滤不可落地/高风险候选，再用统一性能口径比较 |
| 6. 方向选择 | 等 3 个方向收敛后，从 eligible 且优于 incumbent 的候选中选 `SPEED_MS` 最低者；若没有则保持原 base | 选择与完成顺序、candidate 编号及 judge score 高低解耦 |
| 7. 反馈进入下一轮 | Winner 成为下一轮 parent；未选方向的收益、回退和风险继续进入 proposer 上下文 | 支持“未赢但有潜力”的方向以后改形态重试 |
| 8. Continue 恢复 | 首段 R1–R10 结束后，续跑识别已完成 10 轮及 parent `357dee30f6`，从 R11 接续 | 验证跨进程恢复没有丢失 accepted chain |

选择逻辑在日志中的典型证据：

- **不是先完成先赢**：R1 的 c0 最早完成，但最后完成的 c2 更快并被选中；R13 的 c2 耗时约 3027 秒才完成，系统仍等待其评判后再选择 c1。
- **不是最高 judge score 直接赢**：R4 c0 的 score 为 0.90，高于 c1 的 0.83，但 c1 的 718.173 ms 更快，因此选择 c1；R8 同样选择 641.439 ms 的 c1，而不是 score 更高但稍慢的 c0。
- **允许整轮不更新**：R5 三个方向均没有形成 eligible 的 incumbent 改进，base 保持 R4 的 `f88f8a7e0b`。
- **慢方向仍有机会成为 winner**：R12 c2 是该轮最慢完成的实现，却以 **-9.8%** 的明显优势获选，说明选择没有被到达顺序截断。

## 3. 15-round 流程明细

表内变化均为相对该轮 incumbent；“—”表示日志没有给出可比较的 `SPEED_MS`。

| Round | 并行方向 c0 / c1 / c2（decision） | 三方向结果：`ms, Δ, score/risk` | 方向选择与新 incumbent |
|---|---|---|---|
| R1 | charge-loop invariant hoist（续） / isDyn contiguous mirror（续） / thread npe1 partition into tmle（切） | c0 929.751, -1.5%, .75/L；c1 —, .25/H；c2 **909.775, -3.6%, .40/M** | 选 c2 `ad771b8233`；性能优先于更高 score |
| R2 | event-invariant fetch hoist to SoA（续） / charge-PDF bin precompute（切） / time-PDF pointer hoist（切） | c0 852.596, -6.3%, .78/M；c1 **815.882, -10.3%, .82/M**；c2 916.215, +0.7%, .40/L | 选 c1 `daef32bd5e` |
| R3 | NPE-map r-bin hoist（续） / immutable PMT geometry SoA（切） / CalLTOF EvtR hoist（切） | c0 **760.054, -6.8%, .84/L**；c1 825.760, +1.2%, .58/L；c2 834.150, +2.2%, .50/L | 选 c0 `4c21649fc9` |
| R4 | calibration scalar SoA（切） / time-PDF per-PMT bin hoist（续） / lazy guarded dstn sqrt（切） | c0 729.128, -4.1%, .90/L；c1 **718.173, -5.5%, .83/L**；c2 781.301, +2.8%, .55/L | 选 c1 `f88f8a7e0b`；再次体现 speed 优先于 score |
| R5 | calibration scalar SoA retry（续） / event-hit SoA generation guard（续） / isDyn index partition（切） | c0 728.347, +1.4%, .42/L；c1 748.189, +4.2%, .38/M；c2 —, .20/H | **无候选改善**；保持 `f88f8a7e0b` / 718.173 |
| R6 | charge-kloop PDF pointer table（续） / lazy tres guard（切） / calibration packed AoS（切） | c0 —, .28/H，gate-rejected；c1 —, .40/M；c2 **684.298, -4.7%, .82/L** | 选 c2 `a8b9a0f40a`；从 SoA 转向 packed AoS 成功 |
| R7 | hit-fetch zero-copy alias（续） / charge AvgQPdf base pointer（续） / time-PDF kk=0 pointer hoist（续） | c0 704.360, +2.9%, .35/M；c1 696.835, +1.8%, .50/L；c2 **682.305, -0.3%, .60/L** | 选 c2 `2a2c748cb0` |
| R8 | charge proTemp per-event cache（续） / PMTR fold into PmtCalib（续） / dstn dead-path specialization（切） | c0 645.621, -5.4%, .88/L；c1 **641.439, -6.0%, .85/L**；c2 677.212, -0.7%, .75/L | 选 c1 `7f7487e070`；三方向全改善，选最快 |
| R9 | charge proTemp cache retry（续） / CalLTOF geometry dedup（切） / NPE-map GetArray hoist（切） | c0 **636.317, -0.8%, .72/L**；c1 680.239, +6.0%, .32/L；c2 660.642, +3.0%, .40/L | 选 c0 `100ce9370d`；R8 未胜方向改进后落地 |
| R10 | NPE-map GrMapId memo（续） / fdmu-derived Poisson precompute（切） / active-PMT index list（切） | c0 641.131, +0.8%, .45/L；c1 **632.238, -0.6%, .62/L**；c2 652.023, +2.5%, .38/L | 选 c1 `357dee30f6`；首段结束 |
| R11 | NPE-map eager theta table（切） / dmu+fdmu cache extension（续） / expected-PE denominator precompute（切） | c0 **629.544, -0.4%, .74/L**；c1 642.096, +1.6%, .38/L；c2 633.439, +0.2%, .48/M | 续跑正确恢复后选 c0 `f0e8a9b61d` |
| R12 | PMT position fold into PmtCalib（续） / defer time1d bins（续） / sparse Poisson cache fill（续） | c0 617.032, -2.0%, .76/L；c1 603.109, -4.2%, .80/L；c2 **567.792, -9.8%, .85/L** | 选最晚完成但收益最大的 c2 `f5b43df42b` |
| R13 | defer time1d bins retry（续） / guard dead IProb eval（切） / acos theta LUT（切） | c0 608.190, +7.1%, .35/L；c1 **564.680, -0.5%, .76/L**；c2 —, .25/H | 选 c1 `a46393ed41` |
| R14 | PMT position fold retry（续） / time-PDF kk>=2 pointer hoist（续） / CalLTOF redundant pos read（切） | c0 590.543, +4.6%, .32/L；c1 **559.415, -0.9%, .76/L**；c2 577.492, +2.3%, .30/L | 选 c1 `580c84beb5` |
| R15 | proposer 未能返回合法结构化 proposals | 约 999 秒、21 turns、5 次结构化输出重试后失败 | **无候选、无选择**；run 终止在 559.415 |

## 4. Accepted 方案演化

| 阶段 | Accepted path | 性能演化 | 方案含义 |
|---|---|---|---|
| 起点 | baseline `4e7c823440` | 944.096 | 原始 post-v107 gated 基线 |
| 结构裁剪与插值前移 | R1 npe1 partition → R2 charge-PDF bins → R3 NPE-map bins → R4 time-PDF bins | 909.775 → 815.882 → 760.054 → 718.173 | 前四轮连续把热循环内的分支/插值解析移到更外层 |
| 无收益保护 | R5 不接受任何候选 | 718.173 保持 | 三方向并行失败不会破坏 accepted base |
| 数据布局切换 | R6 packed `PmtCalib` AoS | 684.298 | SoA 多次尝试后，切换到顺序访问的 packed AoS 才稳定获益 |
| PDF 指针与 PMT 常量 | R7 time-PDF pointer → R8 PMTR fold | 682.305 → 641.439 | 清理每 PMT 重复解析，并把 job-invariant PMTR 合并进 cache record |
| 未胜方向回收 | R9 proTemp cache | 636.317 | R8 c0 虽未胜但已有 -5.4%，下一轮沿同 family 继续并最终入选 |
| Poisson 路线启动 | R10 fdmu-derived Poisson cache | 632.238 | 从插值/布局切到 time block 的指数与 Poisson 重算 |
| NPE 表进一步预算 | R11 eager theta table | 629.544 | 将 NPE-map Eval 扩大为 per-call theta table |
| Poisson 路线深化 | R12 sparse Poisson cache fill | 567.792 | 跳过 `RecNpe==0` 的无效构建，成为续跑最大单轮收益（-9.8%） |
| Time-PDF 死计算清理 | R13 IProb eval guard → R14 kk>=2 pointer hoist | 564.680 → 559.415 | 后期收益变小，转为针对具体分支和指针解析的细粒度优化 |

最终 accepted chain：

```text
944.096
  └─ R1  909.775  npe1 partition
      └─ R2  815.882  charge-PDF bin precompute
          └─ R3  760.054  NPE-map r-bin hoist
              └─ R4  718.173  time-PDF bin hoist
                  ├─ R5  718.173  no update
                  └─ R6  684.298  packed PmtCalib AoS
                      └─ R7  682.305  time-PDF pointer hoist
                          └─ R8  641.439  PMTR fold
                              └─ R9  636.317  proTemp cache
                                  └─ R10 632.238  Poisson precompute
                                      └─ R11 629.544  NPE eager table
                                          └─ R12 567.792  sparse Poisson fill
                                              └─ R13 564.680  dead IProb guard
                                                  └─ R14 559.415  kk>=2 pointer hoist
```

## 5. 多方向探索如何影响后续选择

### 5.1 `continue` 与 `switch` 保持了实际平衡

42 个候选中，`continue` 22 个、`switch` 20 个；13 个 winners 中分别为 7 和 6 个。两组比例都接近 1:1，说明 proposer 没有只沿单一路线递推，也没有为了“新颖”而持续切换。两类方向都能通过实测进入 accepted chain：

- `continue` 的典型成功：R3 NPE-map hoist、R4 time-PDF bin hoist、R8 PMTR fold、R12 sparse Poisson fill。
- `switch` 的典型成功：R1 npe1 partition、R2 charge-PDF bins、R6 packed AoS、R10 Poisson precompute、R13 dead IProb guard。

### 5.2 未胜方向不是立即遗忘，而是形成可复用证据

| 早期探索 | 当轮结果 | 后续演化 |
|---|---|---|
| R4 c0 calibration SoA | -4.1%，但不及 time-PDF c1 的 -5.5% | R5 直接 retry 回退；R6 改成 packed AoS 后 -4.7% 并入选 |
| R8 c0 proTemp cache | -5.4%，仅略慢于 PMTR c1 的 -6.0% | R9 继续该 family，再得 -0.8% 并入选 |
| R10 c1 Poisson precompute | -0.6% 入选 | R11 的 dmu/fdmu 扩展回退，但 R12 改为只 sparse-fill 有效 PMT，获得 -9.8% |
| R12 c1 defer time1d bins | -4.2%，但输给 sparse Poisson 的 -9.8% | R13 retry 反而 +7.1%，系统拒绝并切到 dead IProb guard |
| PMT position/cache-line 合并 | R12 c0 有 -2.0%，未胜 | R14 c0 扩到 pos_x/y/z 后结构跨 cache line，+4.6%，被拒绝 |

这里体现了并行探索的主要价值：单轮不仅产出一个 winner，还同时产生两个“反事实实验”。后续 proposer 可以区分：

1. **方向有效但当轮不够快**：值得换形态继续，如 proTemp、Poisson。
2. **方向在当前布局下不稳定**：需要改变数据组织，如 calibration SoA → packed AoS。
3. **局部收益不可叠加或已饱和**：重复后回退，如 time1d defer、PMT position fold。

### 5.3 Score/risk 是门禁与质量信号，性能 objective 才决定同轮 winner

本 run 的选择不是简单取最高 score。R1、R4、R8 都选择了 score 略低但 `SPEED_MS` 更好的候选。更准确的选择顺序是：

```text
实现成功
  → correctness / landing gate 可接受
  → risk 可接受且有可比较 objective
  → 必须优于当前 incumbent
  → 在 eligible candidates 中选择最低 SPEED_MS
```

因此 judge score 更像实现质量、可信度和风险的综合信号，而不是覆盖性能目标的最终排序值。

## 6. 稳定性结论

### 6.1 已验证稳定的部分

- **候选隔离稳定**：14 个完整 round 的 42 个候选均在独立 worktree 中探索，没有从日志观察到候选间代码串扰。
- **并行调度与选择解耦**：executor/judger 完成顺序高度交错，但 winner 可以是最早、中间或最晚完成的方向。
- **保守更新稳定**：只有 eligible 且更快的候选才能更新 accepted base；R5 证明整轮失败时能够原地保持。
- **断点续跑稳定**：续跑准确识别 10 个已完成 round 和 parent `357dee30f6`，R11 从正确代码状态继续。
- **方向选择有多样性**：`continue/switch` 的提案与胜出比例都接近 1:1，没有出现固定偏好或单 family 垄断全部轮次。
- **累计优化单调**：accepted objective 从 944.096 单调降到 559.415；候选自身虽频繁回退，但回退未进入主链。

### 6.2 暴露出的稳定性边界

- **Proposer 是单点失败源**：R15 在 5 次 structured-output retry 后仍无法给出合法 proposals，导致整轮在并行执行前终止。候选并行层稳定，但上游 proposal 生成还不具备同等容错。
- **长尾候选拉长 round**：R13 c2 executor 约 3027 秒，尽管最终高风险且未胜，selector 仍需等待它才能作完整比较。公平性得到保证，但 round latency 受最慢方向控制。
- **绘图依赖反复报错**：续跑中 `matplotlib` 与 NumPy 2.0 ABI 不兼容，`progress.png` 更新失败。该异常被降级为 warning，未破坏优化主循环，但观测面不稳定。
- **短基准存在噪声迹象**：若干低于 1% 的收益（R7、R9–R11、R13–R14）接近测量噪声区间。单次方向选择流程是稳定的，但小收益是否可复现仍需重复 benchmark 验证。
- **高风险候选缺少 objective 时诊断较粗**：若 gate 未通过，日志通常只留下 score/risk 和截断 feedback，没有统一的失败类别与完整性能栏，降低了后续 proposer 对失败原因的可利用性。

## 7. 总结

本次 run 对“单 round 并行多方向探索”的核心验证是正面的：

1. 每轮同时提出 3 个彼此隔离的优化方向，42 个候选中 `continue/switch` 为 **22/20**，探索构成接近均衡。
2. 实现和评判可以并行流水，最终选择不受完成顺序、candidate 编号或最高 judge score 绑架。
3. 选择器始终保护 incumbent：14 个完成轮次中 13 次选出更快方案，1 次无改善则保持，accepted 性能从 **944.096 降至 559.415 ms/evt（-40.7%）**。
4. 多方向探索提供了 winner 以外的实验证据，使 calibration cache、proTemp、Poisson 等路线能够在后续 round 改形态后再次进入主链。
5. 当前主要风险不在候选并行与方向选择，而在并行层之前的 proposer 结构化输出可靠性、最慢候选造成的尾延迟，以及小幅收益的重复测量可信度。

因此，可以认为本 run 已验证：**在候选能够正常生成的前提下，三方向并行实现、独立 gate/eval、按 objective 选择并更新 parent 的主流程是稳定的；但“完整 15 轮无人值守运行”的稳定性尚未通过，因为 R15 proposer 失败导致任务提前终止。**
