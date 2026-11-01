# hipVS bridge 参数基准（250K 规模）

本目录是 `lib=cuvs` bridge 的 CAGRA 构建参数基准，用于复现
`seekdb_cuvs_bridge.c` 中默认参数的取舍。

## 为什么改参数

V5 基线在 `seekdb_cuvs_bridge.c` 中写死：

```c
ip->build_algo             = NN_DESCENT;
ip->graph_degree           = 128;
ip->intermediate_graph_degree = 256;
ip->nn_descent_niter       = 100;   /* 库默认 20 */
```

`nn_descent_niter=100` 把大部分构建时间花在图质量已经收敛之后的迭代上。

## 基准方法

`v5_param_probe` 直接调用 hipVS C API，复刻 bridge 的构图参数与检索配置
（`itopk_size=max(4*topk,512)`、`search_width=4`），数据为 2500 个高斯质心的聚簇向量，
查询与底库共用质心。`bridge_bench` 则 `dlopen` 编译出的 bridge，按 seekdb 的真实调用
模式（build → serialize → deserialize → 4096 条单行查询）计时。

```bash
# 构图参数扫描
hipcc -O2 -std=c++17 -D__HIP_PLATFORM_AMD__ -I/opt/hipvs/include -I/opt/rocm/include \
      v5_param_probe.cpp -o v5_param_probe -L/opt/hipvs/lib -Wl,-rpath,/opt/hipvs/lib:/opt/rocm/lib -lcuvs_c
./v5_param_probe --n 250000 --nq 100 --dim 128 --gd 128 --igd 128 --niter 30

# bridge 端到端（含快照与单行查询）
g++ -O2 -std=c++17 bridge_bench.cpp -o bridge_bench -ldl
./bridge_bench /path/to/libseekdb_cuvs_bridge.so --n 250000 --dim 128 --queries 200
```

## 实测（Radeon Cloud，gfx1100，ROCm 7.2.4，128 核）

构图参数扫描（250K，聚簇数据）：

| gd / igd / niter | dim128 构建 | recall@10 | dim256 构建 | recall@10 |
|---|---|---|---|---|
| 128 / 256 / 100（V5 原值） | 41.8 s | 1.0000 | 44.1 s | 1.0000 |
| 128 / 256 / 40 | 22.6 s | 1.0000 | 23.0 s | 1.0000 |
| 128 / 128 / 20 | 9.6 s | 1.0000 | 9.6 s | 1.0000 |
| 64 / 128 / 20 | 8.5 s | 1.0000 | 9.1 s | 1.0000 |
| 32 / 64 / 20 | 7.5 s | 0.5280 | — | — |

bridge 端到端（构建设置 + 4096 条单行查询）：

| 配置 | dim128 构建 | QPS | dim256 构建 | QPS | recall@10 |
|---|---|---|---|---|---|
| 128/256/100（V5） | 44.7 s | 350.1 | 42.1 s | 291.9 | 1.0000 |
| 64/128/20 | 16.4 s | 156.7（45%） | 9.0 s | 210.2（72%） | 1.0000 |
| 128/256/20 | 14.4 s | 308.9（88%） | 14.8 s | 254.0（87%） | 1.0000 |
| **128/128/30（默认）** | **12.6–13.5 s** | **351.1 / 348.2** | **13.2 s** | 286.6 | 1.0000 |

## 取舍

- `graph_degree` 不能降：降到 64 会让查询吞吐掉到基线的 45%（dim128）/72%（dim256），
  触发查询回归门槛。
- `nn_descent_niter=20` 虽然构建更快，但查询吞吐只剩 96%，相对门槛（90%）余量过小；
  `niter=30` 把吞吐拉回约 100%，构建仍有 3.2×。
- `intermediate_graph_degree` 从 256 降到 128 在构建和查询上都更优。

> 以上为 bridge 层口径。评测计的是端到端（含快照持久化、DDL 链路与首查询），
> 最终加速比需以端到端测量为准。
