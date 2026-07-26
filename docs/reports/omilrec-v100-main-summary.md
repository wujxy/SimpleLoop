# OMILRECV2 v1.0.0 优化 · SimpleLoop 12-proposal 完整 run 结果

**代码库**：https://github.com/wujxy/SimpleLoop · **任务**：omilrec-v100（v1.0.0 未优化基线，~874 ms/evt，Intel Xeon 8358P）
**模式**：static-proposal（12 个外部 proposal 顺序驱动，跳过 claude proposer，judger 仍打分 + 给 feedback）
**总时长**：4h10min（2026-07-17 00:35 → 04:45），12 轮全部产 commit（无 gate 拒、无超时）
**正确性门**：e2e bit-identical（10 events，tolerance 0，15 个 RecVertex 字段全匹配）

## 总览

- **最终成绩**：基线 874.50 → 链末端 333.58 ms/evt，**~2.6x 加速**
- **best commit（按 judger 分数）**：round 5（proposal #6），`7bec7116`，score 0.88，571.79 ms/evt（-34.6% vs 基线）
- **最快 commit（按实测速度）**：round 10（proposal #11），`c4a3ff2d`，321.81 ms/evt（-63.2% vs 基线，2.72x）
- **12 轮中**：10 轮正确性 PASS，1 轮 FAIL（round 3），1 轮回退（round 11）

## 各轮明细

| r | proposal 内容（简） | executor 执行情况 | judger 判断 | vs prior | vs 基线 | ms/evt |
|---|---|---|---|---|---|---|
| 基线 | — | — | — | — | — | 874.50 |
| 0 | #1 SoA flatten：per-LPMT 常量（x/y/z/R/isDyn/LPDE/LGain/LQ1/LQRes/LDNR/timeOffset/LDarkMu）缓存成连续数组，热循环改直读，替换 `ALL_LPMT_pos` accessor + virtual getter | 完整：cache 填在 `LoadPMTPara()` 后（位置对），`Calculate_EVLikelihood`/`GetChargeCenter`/`CalLTOF`/`RemoveDN` 全改读数组，算术顺序保留 | PASS 10/10；方向对、实现干净；收益 modest（去 virtual 开销非主成本） | -3.53% | -3.53% | 843.67 |
| 1 | #2 histogram 数组缓存：在 `Load_ExpectedPEQTime` 缓存 ROOT 直方图 `GetArray()` 指针，加 `_arr` interpolator 变体，FCN call site 改用 | 完整：nPE/QPDF/timePDF 三种 cache 全建且 call site 全接通（nullptr 处理忠实，hybrid 分支守护对）；**但 latent 风险**：`m_z<0`+hybrid 关闭时读 cached nullptr（10-event 没爆） | PASS 10/10；实现干净；**但收益低于噪声底，+0.46% 回退** → 压低分；flag 点出 latent nullptr 风险 | +0.46% | -3.1% | 847.55 |
| 2 | #3 bulk geometry split：geometry（cos_theta/acos/dstn）拆成 3 个 homogeneous bulk pass，累积循环改读 scratch | 完整：3 个 pass 独立、scratch 在 `initialize()` 一次分配（非每 FCN call）、算子/求值序/括号/类型全保留 | PASS 10/10；clean bit-identical refactor（通路改非算式）；storage 通路改动正确 | -10.8% | -13.6% | 755.80 |
| 3 | #4 hoist per-event 量：PMT_Hit/hitTime/RecNpe/dmu/fdmu 等 Minuit-invariant 量 hoist 出 FCN，每 event 算一次 | 实现量大且小心，但 **cache refresh 漏点**——某处 `Calculate_EVLikelihood` 读到 stale/wrong-state cache → 正确性崩 | **FAIL**：CORRECTNESS=FAIL，SPEED_MS=NA；方向对但实现非 bit-identical，cap 到 0.20；链没动（不 commit 推进） | NA | NA | FAIL |
| 4 | #5 share Rsp：几何 pass 已算的 PMT-vertex 距离复用给 LTOF，避免第二个 sqrt；hoist vertex-only 量（EvtR/RfrIndxLS/WR） | 完整：复用 `m_scratch_Rsp` 省第二个 sqrt，vertex-only 量 hoist 到 FCN 入口；**从 r2 fork（r3 FAIL 链没动）**，恢复正确性并落地有效速度 | PASS 10/10；正确恢复 + 改进；正确的 bit-identical refactor（省 sqrt + hoist vertex 量） | -5.9%* | -5.9%* | 822.43 |
| 5 | #6 1440-bin nPE-map 预算：FCN 入口一次算全 1440 角 bin 的 1D/2D map 插值，PMT 只做 theta-bin blend；cache 仅 vertex 坐标变时重算 | 完整：1440-bin 预算正确（arr[k],RVar,m_Theta 的纯函数，blend 保留）；**但 latent 风险**：cache validity 只 keyed (RVar,m_Theta) 没 keyed MODE，跨 mode 复发读 stale | PASS 10/10；**系列最大单轮赢**（1440 vs ~34k 插值调用，robust）；best commit；flag 出 MODE-keyed validity 风险 | **-30.48%** | -34.61% | 571.79 |
| 6 | #7 hoist charge-PDF bin：fired-PMT 的 Poisson k 循环里，QPDF bin index+lerp weight（只依赖 PMT_Hit）hoist 出循环，k 循环改直读 raw array | 完整：`FindQPDFBinD` hoist 出 k 循环，per-k `InterpolateQPDFArrayD` 换成 raw-array 直读 + inline lerp；Poisson 递推/break/floor/TMath::Gaus fallback 保留 | PASS 10/10；legitimate bit-identical refactor；收益 modest（k 循环非主成本） | -4.14% | -37.31% | 548.11 |
| 7 | #8 hoist time-PDF bin：time likelihood 里 VVar0/tres grid 的 bin index/weight 每次算，hoist 出 per-PMT 块，1D TH1 积分数组复用同 x-grid | 完整：2D time-PDF bin search hoist 出 per-PMT 块，`interp_bilinear_uniform_center_TH2_arrayF` 拆成 `FindTimePDFBins2D` + `EvalTimePDF2DFromBins`/`EvalTimePDF1D`，算术逐项保留 | PASS 10/10；clean bit-identical refactor（fetch+blend 不变，out-of-range→0.0 语义保留）；low risk | -15.0% | -46.7% | 465.77 |
| 8 | #9 phase split：按 `enableQInfo`/`enableTimeInfo` 把 PMT 循环拆 Q-only/T-only/combined/neither，去掉内层 per-PMT phase branch；`!enableTimeInfo` 时跳过 Rsp/dstn/LTOF pass | 完整：phase split 干净，`if(QInfoIsValid)` guard hoist 出内循环，跳过 Pass 3/4 合理（只 time path 用），`m_NPE`/`m_UfrmScale` 累加器 verbatim 保留 | PASS 10/10；clean bit-identical refactor；low risk | -3.54% | -48.61% | 449.29 |
| 9 | #10 per-event index list：fired/unfired/time-eligible LPMT 索引表在 event 数据加载后建一次；time-eligibility（RecNpe 范围/TimeLHMode/Z_threshold）+ IsLost hoist 出 Minuit 内循环 | 完整：time-eligibility gate + IsLost membership hoist 出 Minuit 内循环到 once-per-event `PrepareEventCache`；NaN 语义 `!(PMT_Hit<PedThres)` 匹配原代码；`enableTimeInfo` 正确保留为 phase flag 不 baked 进 cache | PASS 10/10；legitimate sound win；trajectory 累计 2.15x | -9.51% | -53.5% | 406.56 |
| 10 | #11 TMLE-indexed geometry：TMLE-only 调用时，几何/nPE 插值/expected PE/LTOF 只对 `time_eligible_idx` 算（非全 LPMT）；跳过 `m_UfrmScale` 累加（QTMLE 重算前不读） | 完整：正确 scoped 到 TMLE-only phase（`enableTimeInfo && !enableQInfo`），consumed values 可证限制在 time-eligible PMT；4 个几何 bulk pass 限制到 `m_time_eligible_idx` | PASS 10/10；方向对（paper 命名优化）；scoped 正确；**链末端最快 commit** | -20.9% | -63.2% | 321.81 |
| 11 | #12 RecNpe==1 fast path：RecNpe==1 且 `!enableQTimePdf` 时只算两个 Ge time-PDF 插值（TIdA/TIdB）+ 终概率，跳过积分 time-PDF 和 RecNpe==2 通用路径 | 实现了但方向错：per-hit branch + 两个 time-likelihood 块各插 30 行重复代码，**branch overhead/icache 压力 > 跳过的积分工作** | PASS 10/10（数值安全）；但 **+3.66% 回退** → 压到 0.25；judger 推测 RecNpe==1 可能不命中/编译器已 elide 死工作 | +3.66% | -61.8% | 333.58 |

\* round 4 从 round 2 的 commit fork（round 3 FAIL 不推进链），vs prior 基准是 round 2（755.80）；judger 给的 -5.9% 是 vs baseline 口径（874.50→822.43），相对 round 2 实为 +8.8% 回退（但恢复了 r3 破的正确性）。

## 速度轨迹图（ms/evt）

```
874.5 ┤■ 基线
843.7 ┤  ■ r0  SoA flatten
847.6 ┤    ■ r1  hist cache（噪声内回退）
755.8 ┤      ■ r2  bulk geometry split
 FAIL ┤        ■ r3  hoist（崩，链不动）
822.4 ┤          ■ r4  share Rsp（从 r2 fork，恢复正确性）
571.8 ┤            ■ r5  1440-bin nPE 预算 ← best（按 score）
548.1 ┤              ■ r6  hoist charge-PDF bin
465.8 ┤                ■ r7  hoist time-PDF bin
449.3 ┤                  ■ r8  phase split
406.6 ┤                    ■ r9  per-event index list
321.8 ┤                      ■ r10 TMLE-indexed geometry ← 最快
333.6 ┤                        ■ r11 RecNpe==1 fast path（回退）
      └──────────────────────────────────────────────────────
       r0   r1   r2   r3   r4   r5   r6   r7   r8   r9  r10  r11
```

## 关键观察

### 1. 系统机制全程工作良好
- **judger 两轴比较生效**：每轮都给 vs prior + vs baseline 两个 delta；回退轮（r1/r4/r11）被 prior 轴压分，正确性崩（r3）被 cap 到 0.20，trajectory flag 持续在场
- **executor 去偏置生效**：histogram cache（r1）三种 cache 全接通（无死代码），TMLE-indexed（r10）大改写完整落地；1h timeout 全程无超时
- **gate 还原 frozen 副作用生效**：12 轮无 gate 拒（bench 写脏 speed.csv 的情况没再现）
- **链式串接正确**：r3 FAIL 后链没动，r4 从 r2 fork 正确恢复

### 2. judger 抓到 3 个 10-event 测不出的 latent 风险
- **r1**：`m_z<0` + `hybridTimePdfEnabled=false` 读 cached nullptr（样本无 `m_z<0` event）
- **r5**：nPE-map cache validity 只 keyed (RVar,m_Theta) 没 keyed MODE，跨 mode 复发读 stale
- **r3**：cache refresh 漏点（直接导致 FAIL，非 latent）

这些在 10-event bit-identical 门下要么没爆（r1/r5）要么爆了（r3），说明 **v1.0.0 的 10-event 门太弱**——要可信地 ship 这些 commit 需更大样本 + `m_z<0` 事件覆盖。

### 3. 12 个 proposal 的实际收益分布
- **大赢（>15% vs prior）**：r5 1440-bin 预算（-30%）、r7 time-PDF bin hoist（-15%）、r10 TMLE-indexed（-21%）——都是"hoist 跨 Minuit 迭代重复的工作"
- **小赢（<10%）**：r0/r2/r6/r8/r9——去 virtual、bulk split、bin hoist、phase split、index list，收益 modest（多在噪声边缘）
- **无效/回退**：r1（收益低于噪声底）、r3（崩）、r11（branch overhead 反噬）
- **最大赢点**：预计算/hoist 跨 Minuit 迭代的重复计算（r5/r7/r10），而非去 virtual dispatch（r0/r1）——OMILRECV2 的主成本在 FCN 内层 per-PMT per-iteration 的算术，不在数据取用

### 4. best 选择的设计张力
- best 按 **judger 分数**选 = r5（0.88，571ms），但**不是最快的正确 commit**
- 最快正确 commit = r10（321ms，0.85）——score 略低但速度远优
- 若 goal 是"最快且正确"，best 按 score 选会漏掉 r10。当前 store 逻辑 `score > best_score`（先到者赢，r5 0.88 先于 r7 0.88）

## SimpleLoop 机制改进点（本轮暴露）

| 问题 | 状态 |
|---|---|
| judger 缺 prior 轴，只比 baseline | ✅ 已修（系统记录每轮 eval，喂 prior+baseline 两轴） |
| eval 截断（4000 cap 切掉 SPEED_MS 行） | ✅ 已修（16000） |
| executor prompt "smallest change" 压住大改写 | ✅ 已修（IN FULL + bit-faithful 正交） |
| proposer prompt "keep it short/small" 配对偏置 | ✅ 已修（size 非美德非罪） |
| executor 跑 bench 写脏 frozen 文件被 gate 拒 | ✅ 已修（prompt 要求还原 bench 副作用） |
| executor timeout 1800s 不够大改写 | ✅ 已修（3600s + 可配置） |
| best 按 score 不按 speed（漏掉最快正确 commit） | ⏳ 待讨论 |

## 结论

SimpleLoop 在 OMILRECV2 v1.0.0 上，用 12 个 paper-proposal 串行驱动，**端到端跑通，874 → 333 ms/evt（~2.6x）**。系统机制（proposer-skip 静态模式 + executor 去偏置 + judger 两轴 + harness 独占 commit/eval + 链式串接）全程稳定，12 轮无机制性失败；2 次执行问题（r3 崩、r11 回退）均被 judger 准确识别并压分。best commit（r5，571ms，-34.6%）与最快 commit（r10，321ms，-63.2%）的差距，指向 best 选择策略的下一步改进方向。
