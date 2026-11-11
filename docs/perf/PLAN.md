# MiniVectorDB 加赛性能优化执行方案（实测重排版）

> 本文件记录 P1–P6 的执行顺序、每项的假设、实测前后对比、以及放弃项及理由。
> 数字全部来自本机实测，测量脚本在 `tools/` 下，可复跑核对。
> 基准 commit：`b88e020944f78f1800b8b900300de6b4629d36a0`

---

## 0. 口径确认

### 0.1 性能指标公式（赛题 §3.3.3）

```
R_T = (T_base + ε_T) / (T + ε_T)        ε_T = 1 ms
R_B = (B_base + ε_B) / (B + ε_B)        ε_B = 1 MiB
R_M = (M_base + ε_M) / (M + ε_M)        ε_M = 1 MiB
f(R) = R / (1 + R)

E_写入 = 0.6·f(R_T) + 0.2·f(R_B) + 0.2·f(R_M)
E_查询 = 0.6·f(R_T) + 0.2·f(R_B) + 0.2·f(R_M)
E       = (E_写入 + 2·E_查询) / 3
```

### 0.2 三轴权重

| 项目 | 耗时 | I/O | 内存 | 项目总权重 |
|---|---:|---:|---:|---:|
| 批量写入 | 20% | 6⅔% | 6⅔% | 33⅓% |
| 向量 TopK 查询 | 40% | 13⅓% | 13⅓% | 66⅔% |
| 合计 | 60% | 20% | 20% | 100% |

**查询占 2/3 权重 ⇒ 查询时间是第一杠杆。** 但注意 `f(R) = R/(1+R)` 是**饱和函数**：
`R_T` 提高 2 倍只让 `f(R_T)` 从 0.667 升到 0.75（+12.5%）；`R_T` 提高 10 倍才到 0.909。
所以**单项优化到接近基线后收益急剧递减，必须三轴同向改善**。

### 0.3 平台 E 的口径冲突（待实测校准）

平台返回 E = 1829.577226，而文档定义的 E ∈ [0,1]。平台显然用自己的换算，
**不能假设 E 与某一轴线性**。台账 `ledger.md` 记录每次提测的平台 E，
用两点以上拟合后再决定精力分配；在拿到第二个数据点之前，按"三轴同向改善、
时间优先"的保守假设执行。

### 0.4 提交模式：exact

`Dockerfile` 中 `ENV MINIVECDB_MODE=exact`，平台上一轮正式提测跑的是 **exact 模式**。
实测两个模式：

| 模式 | 查询 T | 写入 T | 查询 I/O | 查询峰值 RSS | Recall |
|---|---:|---:|---:|---:|---:|
| **exact（提交模式）** | **1360 ms** | 1943 ms | 7.38 MB | 14.7 MB | 1.0 / 1.0 |
| ann | 3170 ms | 2445 ms | 7.38 MB | 14.7 MB | 1.0 / 1.0 |

**ann 比 exact 慢 2.3 倍**，因为 ann 路径逐行通过闭包取距离（每次
`read_row_scalars` + `read_vector_cached` 的 `.to_vec()` 拷贝 + 一次
`l2_dispatch`），而 exact 路径按块扫描。提交模式保持 exact（已冻结在 Dockerfile）。

---

## 1. 基线（三轴实测）

测量命令：

```
python3 tools/bench.py --mode exact --rows 10000 --dim 128 --queries 1000 \
        --dump-baseline docs/perf/answers-baseline-exact.txt
```

| 轴 | exact | ann |
|---|---:|---:|
| 查询 T | 1360.1 ms | 3170.5 ms |
| 查询 I/O | 7 378 838 B | 7 378 838 B |
| 查询峰值 RSS | 14 732 KB | 14 732 KB |
| 写入 T | 1942.6 ms | 2445.0 ms |
| 写入 I/O | 23 104 906 B | 23 104 304 B |
| 写入峰值 RSS | 3 756 KB | 3 764 KB |
| 索引准备 T | — | 120.2 ms |
| Recall@10 过滤/不过滤 | 1.0 / 1.0 | 1.0 / 1.0 |

answers 基线文件（只含主键，不含距离，故距离的求和顺序允许变化，只要排序不变）：
`docs/perf/answers-baseline-exact.txt` 与 `docs/perf/answers-baseline-ann.txt`
**逐字节相同**。

---

## 2. 归因：1090 ms 到底在哪

### 2.1 任务书 P1 的前提与实测不符

任务书 P1 判断现状是"每行每 8 个元素做一次横向归约，1000 万次归约开销"，
并建议改为 8 行并行 + 内存转置副本。微基准（`/tmp/bench_kernel.rs`，
1000 查询 × 10000 行 × 128 维）结果：

| 内核变体 | 耗时 | 相对现状 |
|---|---:|---:|
| **A 现状：逐行、每 8 元素一次归约** | **268.8 ms** | 1.00× |
| B 8 行并行（行主序，每行 1 个累加器） | 508.4 ms | 0.53×（慢 1.9×） |
| C 8 行并行（内存转置 [dim][row]） | 369.8 ms | 0.73×（慢 1.4×） |

**结论：任务书 P1 方案本身会让查询变慢。** 原因：现状内核在 128 维上做
16 次迭代，每次 1 次归约，归约的 5 条 shuffle/add 指令被同批的 8 次 FMA 摊薄，
远未成为瓶颈；而"8 行并行 + 每行独立累加器"需要把标量广播成向量
（`_mm256_set1_ps`），把原本 8 条连续 SIMD 指令变成 8 次广播 + 8 次 FMA，
指令数反而翻倍；转置版额外破坏了访存连续性。

**因此 P1 的原方案被放弃**，理由记录在 §4。

### 2.2 真正的构成（perf 采样 + 成本模型）

**(a) perf 采样**（`tools/perf_query_only.py`，带符号的 release 构建，
`-F 999`，1000 次 TopK 查询的热缓存段）：

| 符号 | 自身占比 |
|---|---:|
| `vector::dist::l2_sq_avx2` | 28.05% |
| `exec::query::eval_expr` | 17.98% |
| `engine::Engine::exec_distance_topk` | 7.58% |
| `__memcmp_evex_movbe` | 4.01% |
| `malloc` | 3.79% |
| `_int_free` | 3.68% |
| `<f64 as FromStr>::from_str` | 3.44% |
| `cfree` | 2.49% |
| `value::vector::Vector::parse_literal` | 2.26% |
| `exec::query::RowValues::at` | 2.13% |
| `value::vector::parse_f32` | 2.02% |
| `str::pattern::is_contained_in` | 2.02% |
| `exec::query::compare_values` | 1.90% |

**(b) 成本模型**（`tools/decompose_query.py`：同一 TopK 语句在不同行数下拟合
`per_query_us = fixed_us + per_row_ns × rows / 1000`）：

| | 固定每查询开销 | 每行成本 |
|---|---:|---:|
| 不过滤 | 341.3 µs | 44.55 ns/行 |
| 过滤 | 323.6 µs | **118.01 ns/行** |

**(c) 核算**（`tools/account_query_time.py`）：

| 构成 | 1000 查询合计 | 占比 |
|---|---:|---:|
| 固定每查询开销 | 333.0 ms | 29.1% |
| 每行成本（不过滤 500 次） | 222.8 ms | 19.4% |
| 每行成本（过滤 500 次） | 590.0 ms | 51.5% |
| └ 其中**谓词求值**多出的部分 | **367.3 ms** | **32.1%** |
| 模型合计 | 1145.8 ms | — |
| 实测 | 1360.1 ms | — |

（差 214 ms 是冷启动首遍与缓存增长，模型拟合的是热缓存段。）

### 2.3 三个结论

1. **纯距离计算只占 ~270 ms / 1360 ms ≈ 20%**，不是主要矛盾。
2. **固定每查询开销 333 ms（29%）**：每条 SQL 都要解析一个 128 个浮点数的
   向量字面量（`Vector::parse_literal` + `parse_f32` + `f64::from_str`，
   perf 中合计约 8.4%），再渲染 JSON 响应。
3. **过滤路径是最大单项**（590 ms，51.5%）：每行每谓词要克隆一次 `String`
   （`ColValue::Text(s.clone())`），`eval_expr` 为每行构造 `EvalValue::Text`，
   再用 `compare_values` 做 `String` 比较。过滤组每行成本是不过滤组的 **2.65 倍**。

---

## 3. 重排后的执行顺序

任务书给的顺序是 P1(时间) → P2(时间) → P3(I/O) → P4(内存) → P5(提交)。
**P1 的方案被实测证伪后，顺序按实测的杠杆重排**：

| 序号 | 项目 | 影响的轴 | 预估收益 | 依据 |
|---|---|---|---|---|
| **P-A** | 固定每查询开销：向量字面量零拷贝解析、响应渲染 | T | −200~250 ms | §2.3-2，333 ms 固定开销 |
| **P-B** | 过滤路径：谓词零分配 + 低基数列倒排 | T | −250~400 ms | §2.3-3，367 ms 谓词 + 减少扫描行 |
| **P-C** | 距离核：平方距离排序（免每行 sqrt）、AVX-512 评估 | T | −50~120 ms | §2.1，270 ms 距离计算 |
| **P-D** | 距离核：AVX2 8 累加器寄存器批处理（无转置） | T | 待测 | §2.1 变体 B 的教训 |
| **P-E** | 非 TopK SELECT 路径：主键查找走索引 | T（准入安全） | 34 770 ms → <1 ms | §3.1 |
| **P3** | 写放大与索引构建 I/O | B, M | B −40~60% | 任务书 |
| **P4** | 峰值内存 | M | 随 P3 下降 | 任务书 |
| **P5** | 台账与提交纪律 | — | — | 任务书 |
| **P6** | Agent prompt 材料 | — | +40 分 | 任务书 |

### 3.1 附带发现：非 TopK 的 SELECT 完全没有索引

`tools/probe_query_only.py` 实测（10000 行表）：

| 语句 | 1000 次耗时 |
|---|---:|
| `SELECT id FROM t WHERE id = 1;`（主键等值） | **34 770 ms** |
| `SELECT id FROM t WHERE cat = 'c1';`（普通列 + 全扫描） | 36 022 ms |
| TopK 查询（不过滤） | 610 ms |

`WHERE id = 1` 竟然比 TopK 慢 57 倍。原因在 `exec/query.rs`：
非 TopK 路径走 `collect_rows`，对**每一行**调用 `read_row`（解码全部列，
包括 128 维向量 = 每行 512 B 堆分配），然后 `eval_expr` 对 `L2Distance`
**每行重新解析一次向量字面量**（`Vector::parse_literal` 在循环体内）。
10 000 行 × 1000 查询 = 1000 万次字面量解析。

这不影响性能 benchmark（它只跑 TopK），但它是**准入用例的定时炸弹**：
MySQL case 一票否决，而公开用例里就有 `WHERE id = ?` 形状的查询。
**P-E 因此被提升到高优先级**，属于"不改变语义、只消除冗余"的加固。

---

## 4. 放弃项与理由

### 4.1 放弃"P1 的 8 行并行 + 内存转置副本"

- **理由**：微基准显示它比现状**慢 1.4~1.9 倍**（§2.1）。转置副本还要额外
  占 5 MB 内存（10 000 × 128 × 4 B），直接推高 M 轴，与 P4 的目标相反。
- **证据**：`/tmp/bench_kernel.rs`（变体 A/B/C 的耗时）。
- **替代**：改为 P-C（平方距离免 sqrt）+ P-D（保持行主序的多累加器变体，
  先测后定）。**磁盘布局不变**，符合 `docs/design.md` 的既有结论。

### 4.2 暂不改造 ANN 路径

- **理由**：提交模式是 exact（Dockerfile 已冻结），ANN 路径不在提测路径上，
  改动它不产生 E 收益，却占用提交额度与准入风险。
- **保留**：ANN 路径的正确性（与 exact 逐字节相同的答案）必须继续通过回归，
  防止 `MINIVECDB_MODE` 被误改。

### 4.3 P3-1（WAL 收紧为只保留未落盘事务）暂缓

- **理由**：改动 WAL 生命周期属于崩溃恢复语义的边界，一票否决项风险最高。
  先做 P3-2/P3-3（不改变恢复语义的部分），用 killtest 验证；只有当 P3-2/P3-3
  不足以把 B 降到目标区间，才回头评估 P3-1。

---

## 5. 每项的验收闸门（全部改动后按顺序跑）

1. `cargo build --release --offline --locked` + `cargo test --release --offline --locked`
2. `python3 tools/run_public_cases.py` 以及 `--mode ann`
3. 官方 `protocol_check/run.py --image <image>`（目标 20/20 PASS）
4. `python3 tools/selftest.py`
5. `python3 tools/check_formal_requirements.py`（27 项）+ `python3 tools/check_resource_limits.py`
6. `bash tools/smoke.sh`
7. `bash tools/killtest.sh` 与 `bash tools/killtest_container.sh`
8. **语义闸门**：`--dump-baseline` 前后 answers **逐字节 diff 为空**；
   exact 与 ann 的 answers 也逐字节相同
9. `python3 tools/perf_compare.py --before <before.json> --after <after.json>`

---

## 6. 进度

| 项 | 状态 | 改前 → 改后 |
|---|---|---|
| 基线复现 | ✅ | 见 §1 |
| 归因 | ✅ | 见 §2 |
| P-A 固定开销 | ⬜ | |
| P-B 过滤路径 | ⬜ | |
| P-C/P-D 距离核 | ⬜ | |
| P-E 非 TopK 索引 | ⬜ | |
| P3 I/O | ⬜ | |
| P4 内存 | ⬜ | |
| P5 台账 | ⬜ | |
| P6 Agent 材料 | ⬜ | |
