# OMILREC v1.0.0 SimpleLoop 自主优化 test-7 —— 20 轮汇总

- **Run**: `omilrec-v100-selfloop-test-7`
- **Baseline**: `8218ba3`，**924.25 ms/evt**
- **目标**: 在 bit-identical correctness gate 不变的前提下，降低 `OMILRECV2` 重建耗时
- **测试口径**: `scripts/sl_eval.sh --evtmax 10`，10 events，`reference/ref_10evt.root`，tolerance 0
- **核心机制变化**: 本次测试引入 proposer 反思链，用于减少 proposer 沿同一方向死磕或重复无效尝试的问题

## 汇报版概览

| 指标 | 结果 |
|---|---|
| Baseline | **924.25 ms/evt** |
| 最佳结果 | **543.75 ms/evt**，R17，commit `9d40476` |
| 累计提速 | **-41.2%** vs baseline，约 **1.70x** |
| 正确性 | 17 轮 PASS，2 轮 FAIL，1 轮 no-op/no eval |
| 反思链效果 | proposer 多次在低收益/回退后切换方向，避免长期死磕同一 hoist 路线；但仍出现一次重复已实现方向、以及后期 control-flow specialization 连续失败 |
| 主要有效方向 | PMT 常量缓存、QPDF/NPE/time-PDF bin-search hoist、PMT 常量 AoS 数据布局 |
| 当前结论 | 反思链有效提高了方向切换能力，但还需要更强的“已实现检测”和“失败后恢复到 best”的机制 |

## 20 轮明细

| 轮 | proposal 方向 | proposer 反思/决策 | executor 结果 | judger 结果 | speed |
|---|---|---|---|---|---|
| R1 | PMT 几何/校准常量 SoA cache | 首轮从 FCN 热循环中找安全 hoist | commit `c7f338e`，2 files | score 0.80，low，PASS | **820.77**，-11.2% |
| R2 | NPE/QPDF `GetArray()` 指针 cache + inline kernel | 沿 R1 的缓存方向继续 | commit `b3e95ce`，4 files | score 0.45，low，PASS；但回退 | **841.73**，+2.6% vs prior |
| R3 | QPDF k-loop bin-search hoist | 反思 R2 无效，转向 k-loop 内不变量 | commit `ec27070`，1 file | score 0.88，low，PASS | **706.69**，-16.0% |
| R4 | CalLTOF 中 EvtR/折射率 hoist | 切到 time geometry 不变量 | commit `e296f57`，2 files | score 0.55，low，PASS；基本噪声 | **706.04**，-0.1% |
| R5 | NPE-map R/theta bin-search hoist | 回到已验证有效的 bin-search hoist 家族 | commit `6e41cc0`，1 file | score 0.80，low，PASS | **677.73**，-4.0% |
| R6 | time-PDF bin-search hoist + inline | 攻剩余 heavy time-PDF block | commit `365feb6`，3 files | score 0.83，low，PASS | **617.59**，-8.9% |
| R7 | time-PDF hist pointer per-FCN cache | 延续 time-PDF cache | commit `6824e80`，1 file | score 0.40，medium，PASS；小回退 | **622.63**，+0.8% |
| R8 | dstn sqrt 延迟到 time gate 内 | 反思 cache 方向收益不足，切到 defer dead work | commit `4e3ba99`，1 file | score 0.80，low，PASS | **600.09**，-3.6% |
| R9 | LHIT charge/time/nPE eval-top pointer cache | 切到 readout cache，但收益假设未验证 | commit `16baff5`，4 files | score 0.45，low，PASS；回退 | **618.84**，+3.1% |
| R10 | `abs(m_R)` FCN-level hoist | 回到低风险标量 hoist | commit `a40dadb`，1 file | score 0.80，low，PASS | **591.92**，-4.4% |
| R11 | 重复 PMT SoA hoist | 反思链误判当前代码状态，提出已实现方向 | 无 diff / no commit | score 0.05，low；no eval | **NA** |
| R12 | NPE/time-PDF idx2D row-base hoist | 纠偏后切到未做过的 index 计算 | commit `9d36fbf`，1 file | score 0.83，low，PASS | **567.58**，-4.1% |
| R13 | live-PMT index list 压缩 | 反思 per-PMT body 已接近耗尽，切到 loop domain | commit `1df77d5`，2 files | score 0.35，low，PASS；回退 | **574.38**，+1.2% |
| R14 | QPDF k-loop `isDyn` base hoist | 小粒度补漏 | commit `b8a5288`，1 file | score 0.55，low，PASS；近似噪声 | **573.65**，-0.1% |
| R15 | TpdfR3 `std::map` 换 vector/lower_bound | 明确切到 data-structure swap | commit `bdbe1b3`，2 files | score 0.30，high，PASS；默认路径基本未触发 | **574.34**，+0.1% |
| R16 | PMT 常量 SoA -> AoS packed struct | 反思 hoist 路线停滞，切到数据布局 | commit `6ada8ff`，2 files | score 0.80，low，PASS | **543.95**，-5.3% |
| R17 | LHIT 三数组 -> AoS readout record | 延续 R16 数据布局假设 | commit `9d40476`，4 files | score 0.45，medium，PASS；几乎持平 | **543.75**，-0.0%，当前 best |
| R18 | `enableTimeInfo` per-PMT specialization | 切到 control-flow specialization | commit `55fa90c`，1 file | score 0.35，low，PASS；大幅回退 | **600.86**，+10.5% |
| R19 | 将 R18 的 per-PMT 分支外提成双 loop | 试图修正 R18 的分支开销 | commit `c936fe1`，1 file | score 0.18，high，**FAIL** | **NA** |
| R20 | revert R18/R19 specialization，回到 R17/R16 形态 | 反思 specialization 路线失败，切换为恢复 | commit `4992e9a`，1 file | score 0.25，medium，**FAIL**；未恢复成功 | **NA** |

## 结论

| 观察 | 说明 |
|---|---|
| 反思链的正向作用 | R2/R4/R7/R9/R13/R15 等低收益或回退后，proposer 能逐步从单纯 hoist/cache 切到 defer dead work、loop domain、data structure、data layout 等新机制。 |
| 最明显的成功切换 | R15 flat 后，R16 切到 PMT 常量 AoS 数据布局，取得 **-5.3%** 的真实收益，并把全局 best 推到约 **543.75 ms/evt**。 |
| 仍存在的问题 | R11 提出已实现方向导致 no-op；R18-R20 在 control-flow specialization/revert 上连续消耗 3 轮，说明反思链还需要更强的失败恢复和源码状态核查。 |
| 建议改进 SimpleLoop | 支持失败或明显回退后从 best commit 继续，而不是严格从 serial parent 继续；同时让 proposer 在出 proposal 前显式检查“当前链上是否已实现/是否属于近期失败机制”。 |
