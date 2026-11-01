// V5-baseline parameter probe.
//
// Reproduces the exact CAGRA build/search configuration used by
// tools/hipvs_bridge/seekdb_cuvs_bridge.c in the frozen V5 baseline
// (build_algo=NN_DESCENT, graph_degree/intermediate/niter configurable here;
// search itopk_size=max(4*topk,512), search_width=4) so that the build-time vs
// recall trade-off of those parameters can be measured directly.
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <random>
#include <string>
#include <vector>

#include <dlpack/dlpack.h>
#include <cuvs/core/c_api.h>
#include <cuvs/distance/distance.h>
#include <cuvs/neighbors/cagra.h>
#ifdef __HIP_PLATFORM_AMD__
#include <hip/hip_runtime.h>
#endif

namespace {
using Clock = std::chrono::steady_clock;
double ms(const Clock::time_point &t0) {
  return std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
}

void nop_deleter(DLManagedTensor *) {}
struct Tensor {
  DLManagedTensor mgr{};
  std::vector<int64_t> shape;
};
Tensor wrap(void *p, int64_t r, int64_t c, int32_t dev) {
  Tensor t;
  t.shape = {r, c};
  DLTensor &d = t.mgr.dl_tensor;
  d.data = p;
  d.device.device_type = static_cast<DLDeviceType>(dev);
  d.device.device_id = 0;
  d.ndim = 2;
  d.dtype.code = kDLFloat;
  d.dtype.bits = 32;
  d.dtype.lanes = 1;
  d.shape = t.shape.data();
  d.strides = nullptr;
  d.byte_offset = 0;
  t.mgr.deleter = nop_deleter;
  return t;
}

// Mixture-of-Gaussians, queries drawn around the same centroids as base.
void gen(int64_t rows, int64_t qrows, int dim, int clusters, uint32_t seed,
         std::vector<float> *base, std::vector<float> *q, std::vector<int64_t> *cent_of) {
  std::mt19937 rng(seed);
  std::normal_distribution<float> cen(-4.0f, 4.0f), jit(0.0f, 0.05f);
  std::uniform_int_distribution<int> pick(0, clusters - 1);
  std::vector<std::vector<float>> cents(clusters, std::vector<float>(dim));
  for (auto &c : cents)
    for (auto &x : c) x = cen(rng);
  base->resize(rows * dim);
  cent_of->resize(rows);
  for (int64_t r = 0; r < rows; ++r) {
    int c = pick(rng);
    (*cent_of)[r] = c;
    for (int d = 0; d < dim; ++d) (*base)[r * dim + d] = cents[c][d] + jit(rng);
  }
  q->resize(qrows * dim);
  for (int64_t r = 0; r < qrows; ++r) {
    int c = pick(rng);
    for (int d = 0; d < dim; ++d) (*q)[r * dim + d] = cents[c][d] + jit(rng);
  }
}

std::vector<std::vector<int64_t>> exact_gt(const std::vector<float> &base, int64_t n, int64_t dim,
                                           const std::vector<float> &q, int64_t nq, int k) {
  std::vector<std::vector<int64_t>> gt(nq);
  std::vector<std::pair<float, int64_t>> tmp(n);
  for (int64_t i = 0; i < nq; ++i) {
    const float *qv = q.data() + i * dim;
    for (int64_t r = 0; r < n; ++r) {
      const float *bv = base.data() + r * dim;
      float acc = 0.f;
      for (int64_t c = 0; c < dim; ++c) {
        float t = qv[c] - bv[c];
        acc += t * t;
      }
      tmp[r] = {acc, r};
    }
    std::partial_sort(tmp.begin(), tmp.begin() + k, tmp.end());
    gt[i].resize(k);
    for (int j = 0; j < k; ++j) gt[i][j] = tmp[j].second;
  }
  return gt;
}
}  // namespace

int main(int argc, char **argv) {
  int64_t n = 250000, nq = 200;
  int dim = 128, k = 10;
  size_t gd = 128, igd = 256, niter = 100;
  int clusters = 2500;
  uint32_t seed = 20261101;

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto nx = [&]() -> const char * {
      if (i + 1 >= argc) { fprintf(stderr, "missing value\n"); exit(1); }
      return argv[++i];
    };
    if (a == "--n") n = atoll(nx());
    else if (a == "--nq") nq = atoll(nx());
    else if (a == "--dim") dim = atoi(nx());
    else if (a == "--k") k = atoi(nx());
    else if (a == "--gd") gd = strtoull(nx(), nullptr, 10);
    else if (a == "--igd") igd = strtoull(nx(), nullptr, 10);
    else if (a == "--niter") niter = strtoull(nx(), nullptr, 10);
    else if (a == "--clusters") clusters = atoi(nx());
    else { fprintf(stderr, "unknown arg %s\n", a.c_str()); return 1; }
  }

  std::vector<float> base, q;
  std::vector<int64_t> cent;
  auto t_gen = Clock::now();
  gen(n, nq, dim, clusters, seed, &base, &q, &cent);
  double gen_ms = ms(t_gen);

  auto t_gt = Clock::now();
  auto gt = exact_gt(base, n, dim, q, nq, k);
  double gt_ms = ms(t_gt);

  cuvsResources_t res{};
  if (cuvsResourcesCreate(&res) != CUVS_SUCCESS) { fprintf(stderr, "res create failed\n"); return 2; }

  // ---- build exactly like V5's bridge ----
  auto t_build = Clock::now();
  Tensor dset = wrap(base.data(), n, dim, kDLCPU);
  cuvsCagraIndexParams_t ip;
  cuvsCagraIndexParamsCreate(&ip);
  ip->build_algo = NN_DESCENT;
  ip->graph_degree = gd;
  ip->intermediate_graph_degree = igd;
  ip->nn_descent_niter = niter;
  cuvsCagraIndex_t idx;
  cuvsCagraIndexCreate(&idx);
  if (cuvsCagraBuild(res, ip, &dset.mgr, idx) != CUVS_SUCCESS) {
    fprintf(stderr, "build failed: %s\n", cuvsGetLastErrorText());
    return 2;
  }
  cuvsStreamSync(res);
  double build_ms = ms(t_build);
  printf("[V5_PROBE] n=%lld dim=%d clusters=%d gd=%zu igd=%zu niter=%zu | gen_ms=%.0f gt_ms=%.0f "
         "build_ms=%.0f",
         (long long)n, dim, clusters, gd, igd, niter, gen_ms, gt_ms, build_ms);
  fflush(stdout);

  // ---- search exactly like V5's bridge: device tensors + RMM buffers ----
  std::vector<unsigned> ids(nq * k);
  std::vector<float> dist(nq * k);
  cuvsCagraSearchParams_t sp;
  cuvsCagraSearchParamsCreate(&sp);
  size_t itopk = std::max<size_t>(size_t(k) * 4, 512);
  if (sp->itopk_size < itopk) sp->itopk_size = itopk;
  sp->search_width = 4;

  float *qd = nullptr;
  unsigned *nd = nullptr;
  float *dd = nullptr;
  if (cuvsRMMAlloc(res, (void **)&qd, sizeof(float) * nq * dim) != CUVS_SUCCESS ||
      cuvsRMMAlloc(res, (void **)&nd, sizeof(unsigned) * nq * k) != CUVS_SUCCESS ||
      cuvsRMMAlloc(res, (void **)&dd, sizeof(float) * nq * k) != CUVS_SUCCESS) {
    fprintf(stderr, "rmm alloc failed\n");
    return 2;
  }
  cudaMemcpy(qd, q.data(), sizeof(float) * nq * dim, cudaMemcpyDefault);

  Tensor qt = wrap(qd, nq, dim, kDLCUDA), nt = wrap(nd, nq, k, kDLCUDA), dt = wrap(dd, nq, k, kDLCUDA);
  nt.mgr.dl_tensor.dtype.code = kDLUInt;
  nt.mgr.dl_tensor.dtype.bits = 32;
  cuvsFilter f;
  f.type = NO_FILTER;
  f.addr = 0;
  auto t_search = Clock::now();
  cuvsError_t se = cuvsCagraSearch(res, sp, idx, &qt.mgr, &nt.mgr, &dt.mgr, f);
  if (se != CUVS_SUCCESS) {
    printf(" search=failed(%s)\n", cuvsGetLastErrorText());
    return 2;
  }
  cudaMemcpy(ids.data(), nd, sizeof(unsigned) * nq * k, cudaMemcpyDefault);
  cudaMemcpy(dist.data(), dd, sizeof(float) * nq * k, cudaMemcpyDefault);
  double search_ms = ms(t_search);

  int64_t hit = 0;
  for (int64_t i = 0; i < nq; ++i)
    for (int j = 0; j < k; ++j)
      if (std::find(gt[i].begin(), gt[i].end(), (int64_t)ids[i * k + j]) != gt[i].end()) ++hit;
  double recall = double(hit) / double(nq * k);

  printf(" search_ms=%.1f recall@%d=%.4f\n", search_ms, k, recall);

  cuvsCagraSearchParamsDestroy(sp);
  cuvsCagraIndexDestroy(idx);
  cuvsCagraIndexParamsDestroy(ip);
  cuvsResourcesDestroy(res);
  return 0;
}
