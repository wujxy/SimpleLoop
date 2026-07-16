# OMILREC 论文优化复现 Proposals 汇总

目标：基于最旧 OMILREC 代码形态复现论文中的优化路径。起点假设为 `v1.0.0` 一类单体实现，热点主要在 `OMILRECV2::Calculate_EVLikelihood` 和 `RecHelper` 插值函数中。总集合为 `omilrec-paper-proposals-total.yaml`，主测试集合为 `omilrec-paper-proposals-main.yaml`，短 smoke 集合为 `omilrec-paper-proposals-test.yaml`。

| 序号 | 论文优化阶段 | SimpleLoop proposal 摘要 | 主要作用点 | 所属集合 | 预期收益 | 风险与验证重点 |
|---:|---|---|---|---|---|---|
| 1 | 数据布局扁平化 | 缓存每个 LPMT 的位置、半径、isDyn、LPDE、gain、Q1、QRes、timeOffset、暗噪声输入等常量到连续数组 | `OMILRECV2.{cc,h}` 初始化与 `Calculate_EVLikelihood` | test / main / total | 去掉大量 `TVector3` 访问和 tool 虚函数调用，改善 cache locality | 应保持 bit-identical；注意不要改变 expected PE 乘法顺序 |
| 2 | ROOT histogram 访问扁平化 | 在 `Load_ExpectedPEQTime` 中缓存 nPE map、charge PDF、time PDF 的 raw array 指针 | `Load_ExpectedPEQTime`、`RecHelper`、FCN 插值调用 | test / main / total | 去掉 ROOT histogram accessor / pointer chasing，为后续 inline 插值铺路 | 插值公式和 binning 规则必须完全一致 |
| 3 | Bulk vectorizable geometry | 将 cos theta、acos、PMT-vertex distance 拆成独立 bulk loops，写入 scratch arrays | `Calculate_EVLikelihood` 几何部分 | test / main / total | 让编译器更容易自动向量化，减少混合分支带来的流水线停顿 | 几何数组值必须与原 per-PMT 计算一致 |
| 4 | Hoist Minuit-invariant work | 每事件预计算 PMT_Hit、hitTime、hitNPE、dmu、fdmu、LPDE 相关常量和 QPDF 参数 | 事件加载后、`RemoveDN` 后、FCN 前 | test / main / total | 把每次 Minuit FCN 都重复做的 per-event 工作降为一次 | `RemoveDN` 会改变 hit charge，缓存时机必须在其之后 |
| 5 | 复用距离数组做 LTOF | 用 geometry pass 已经算出的 `Rsp` 直接计算 LTOF，替换 FCN 内 `CalLTOF` 重复 sqrt | FCN time path、`CalLTOF` 调用点 | main / total | 避免每次时间似然里重复计算 PMT-vertex distance | 保留 `CalLTOF` 给非 FCN 路径；LTOF 公式不能变 |
| 6 | nPE angular-bin 预计算 | 对当前 trial vertex 预先计算 1440 个 theta bin 的 expected nPE，PMT 内只做线性 blend | nPE map interpolation | main / total | 将昂贵的 1D/2D map 插值从每 PMT 降到每 bin | `Use3DMap`、`isMixingPhase`、R/theta 变化时缓存失效要正确 |
| 7 | QPDF bin-finding hoist | fired PMT 的 `PMT_Hit` QPDF bin 和插值权重只算一次，Poisson k-loop 内直接 raw-array lerp | charge likelihood fired path | main / total | 去掉 k-loop 内重复 floor/clamp/bin search | Poisson recurrence、break 条件、`TMath::Gaus` fallback 保持原样 |
| 8 | Time PDF bin-finding hoist | time path 中对 `VVar0`、`tres` 的 x/y bin 和权重只算一次，所有 time PDF 插值复用 | time likelihood path | main / total | 减少多次 `InterpolateTimePDFTH1F/TH2F` 的重复 bin search | uniform-grid 公式必须匹配 `RecHelper` |
| 9 | 按 fit phase 拆 PMT loop | QMLE 只跑 charge，TMLE 只跑 time，QTMLE 跑 combined，移除内层 phase 分支 | `Calculate_EVLikelihood` 主 PMT loop | main / total | 降低分支和无用工作，为 indexed loops 做准备 | `Qndf`、`Tndf`、`m_NPE`、`m_UfrmScale` 语义不能丢 |
| 10 | 每事件 index lists | 预先构建 `unfired_idx`、`fired_idx`、`time_eligible_idx` | event preparation、charge/time likelihood loops | main / total | 避免每次 FCN 对所有 LPMT 做阈值和 eligibility 判断 | time-eligible 条件必须等价：RecNpe、TimeLHMode、Z_threshold、IsLost |
| 11 | TMLE-indexed geometry | TMLE-only 时只对 `time_eligible_idx` 计算 geometry、nPE、expected PE、LTOF，并跳过未使用的 `m_UfrmScale` | TMLE-only FCN path | main / total | 将 TMLE 从 17k PMTs 缩到约 1.7k time-eligible PMTs | 只作用于 TMLE-only；不要影响 QMLE/QTMLE |
| 12 | RecNpe=1 time fast path | 对 `RecNpe==1 && !enableQTimePdf` 只算必要的两个 Ge time PDF 插值和最终概率 | time likelihood `RecNpe==1` 分支 | main / total | 跳过 integral PDFs 和 RecNpe=2/general-path 工作 | 先用 double 中间量；float fast path 需 FCN drift gate 证明 |
| 13 | Unfired charge algebra | 将 unfired charge likelihood 从 `exp(-pe)*(1+ProbQ*pe)` 改成 `2*pe - 2*log1p(ProbQ*pe)` 的直接贡献 | charge likelihood unfired path | total | 消除 unfired PMT 上的 `exp()` 和函数/分支开销 | 第一项显式 arithmetic-changing 优化；必须满足 FCN drift <= `1e-13` |
| 14 | Reciprocal / scalar constants precompute | 预计算 `1/(LPMTCalibEnergy*nHESF)`、grid inverse width、PMT_R/LS_R powers、`1e6/c` 等 | FCN 和 helper 常量区 | total | 减少重复除法和标量计算 | reciprocal 可能改变 FP 舍入；敏感处逐项隔离验证 |
| 15 | QMLE skip distance sqrt | charge-only QMLE 中不计算 `dstn/Rsp/LTOF/time-PDF` 状态 | QMLE charge-only path | total | 去掉 QMLE 中不被 charge likelihood 消费的 sqrt 和 time 状态 | theta/nPE/expected PE 仍需保留，因为 charge path 需要 |
| 16 | Bulk input load | 将 charge、first-hit time、hit NPE 等输入批量装入 per-event arrays | input tool 读取与 event preparation | total | 减少每 PMT 输入虚函数读取，类似论文的 bulk memcpy load | 若旧 API 无安全 bulk 接口，只加本地窄 helper，不扩大工具契约 |
| 17 | 可选：改进初始种子 | 用 online/OEC 能量-半径估计作为 QMLE 初始值，旧固定 seed 作为 fallback | Minuit setup / QMLE seed | total | 减少 Minuit 迭代次数，论文中约 20-30% 调用数收益 | 改变 minimizer path；需端到端 4 mm / 7 keV gate，近边界事件可能到另一局部极小 |
| 18 | 可选：抽 free functions | 将稳定后的 FCN 子计算抽到 `omilrec_*` free-function 文件，镜像现代结构 | `OMILRECV2/src/omilrec_*` | total | 降低后续优化风险，便于独立测试和复用 | 不追求大重构；行为必须不变 |

## 文件划分

| 文件 | proposal 范围 | 用途 |
|---|---|---|
| `omilrec-paper-proposals-test.yaml` | 1-4 | 短 smoke run，先验证 SimpleLoop、构建和早期保守优化能跑通 |
| `omilrec-paper-proposals-main.yaml` | 1-12 | 主要测试版本，覆盖论文中大部分保守优化和 dominant-case fast path |
| `omilrec-paper-proposals-total.yaml` | 1-18 | 总集合，包含算术变化、高风险 seed 和后续工程化整理 |

## 推荐执行顺序

| 阶段 | Proposal 范围 | 目标 | 建议说明 |
|---|---|---|---|
| Smoke | 1-4 | 先跑通数据布局、raw arrays、bulk geometry、per-event hoist | 用 `test` 文件，失败时定位成本最低 |
| 主测试 | 1-12 | 优先复现论文中 bit-identical 或接近 bit-identical 的主路径 | 用 `main` 文件，重点看 FCN gate 和 speed delta |
| 总集合 | 1-18 | 包含 arithmetic-changing 和高风险/后续工程化项 | 用 `total` 文件；13 以后建议逐条审 history |

## 对应论文主线

| 论文 pattern | 对应 proposals |
|---|---|
| Data-layout flattening | 1、2、16 |
| Bulk vectorizable geometry | 3、5、11、15 |
| Hoisting Minuit-invariant work | 4、5、14 |
| Per-event / per-vertex precomputation | 6、7、8、10、14 |
| Fit-phase loop splitting and indexing | 9、10、11、15 |
| Reduced-precision / dominant-case fast path | 12 |
| Algebraic likelihood simplification | 13 |
| Better minimizer seed | 17 |
| Testable extracted implementation | 18 |
