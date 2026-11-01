// Bridge-level benchmark for the seekdb <-> hipVS shim.
//
// Loads libseekdb_cuvs_bridge.so through the same C ABI seekdb uses
// (seekdb_cuvs_build / search / serialize / deserialize / free), so the numbers
// cover the real call pattern: one build, one snapshot write, one snapshot reload,
// then many single-row queries (the SQL path issues nq=1 per statement).
#include <dlfcn.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <string>
#include <vector>

namespace {
using Clock = std::chrono::steady_clock;
double ms(const Clock::time_point &t0) {
  return std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
}

struct Api {
  void *(*build)(const float *, long, long);
  int (*search)(void *, const float *, long, long, unsigned *, float *);
  int (*serialize)(void *, const char *);
  void *(*deserialize)(const char *, long, long);
  void (*free_)(void *);
};

template <typename T>
T sym(void *h, const char *n) {
  auto p = reinterpret_cast<T>(dlsym(h, n));
  if (!p) { fprintf(stderr, "missing symbol %s\n", n); exit(2); }
  return p;
}

void gen(int64_t rows, int64_t qrows, int64_t dim, int clusters, uint32_t seed,
         std::vector<float> *base, std::vector<float> *q) {
  std::mt19937 rng(seed);
  std::normal_distribution<float> cen(-4.0f, 4.0f), jit(0.0f, 0.05f);
  std::uniform_int_distribution<int> pick(0, clusters - 1);
  std::vector<std::vector<float>> c(clusters, std::vector<float>(dim));
  for (auto &v : c)
    for (auto &x : v) x = cen(rng);
  base->resize(rows * dim);
  for (int64_t r = 0; r < rows; ++r) {
    const auto &mu = c[pick(rng)];
    for (int64_t d = 0; d < dim; ++d) (*base)[r * dim + d] = mu[d] + jit(rng);
  }
  q->resize(qrows * dim);
  for (int64_t r = 0; r < qrows; ++r) {
    const auto &mu = c[pick(rng)];
    for (int64_t d = 0; d < dim; ++d) (*q)[r * dim + d] = mu[d] + jit(rng);
  }
}
}  // namespace

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr,
            "usage: %s BRIDGE.so [--n N] [--dim D] [--queries Q] [--k K] [--single NQ1]\n",
            argv[0]);
    return 1;
  }
  int64_t n = 250000, qn = 200, single = 4096;
  int dim = 128, k = 10, clusters = 0;
  std::string snap;
  for (int i = 2; i < argc; ++i) {
    std::string a = argv[i];
    auto nx = [&]() -> const char * { return (i + 1 < argc) ? argv[++i] : "0"; };
    if (a == "--n") n = atoll(nx());
    else if (a == "--dim") dim = atoi(nx());
    else if (a == "--queries") qn = atoll(nx());
    else if (a == "--k") k = atoi(nx());
    else if (a == "--single") single = atoll(nx());
    else if (a == "--clusters") clusters = atoi(nx());
    else if (a == "--snap") snap = nx();
  }
  if (clusters <= 0) clusters = (int)std::max<int64_t>(1, n / 100);

  void *dl = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
  if (!dl) { fprintf(stderr, "dlopen: %s\n", dlerror()); return 2; }
  Api api{sym<decltype(api.build)>(dl, "seekdb_cuvs_build"),
          sym<decltype(api.search)>(dl, "seekdb_cuvs_search"),
          sym<decltype(api.serialize)>(dl, "seekdb_cuvs_serialize"),
          sym<decltype(api.deserialize)>(dl, "seekdb_cuvs_deserialize"),
          sym<decltype(api.free_)>(dl, "seekdb_cuvs_free")};

  std::vector<float> base, q;
  gen(n, qn, dim, clusters, 20261101, &base, &q);

  // exact ground truth for the recall sample
  std::vector<std::vector<int64_t>> gt(qn);
  {
    std::vector<std::pair<float, int64_t>> tmp(n);
    for (int64_t i = 0; i < qn; ++i) {
      const float *qv = q.data() + i * dim;
      for (int64_t r = 0; r < n; ++r) {
        const float *bv = base.data() + r * dim;
        float acc = 0.f;
        for (int d = 0; d < dim; ++d) { float t = qv[d] - bv[d]; acc += t * t; }
        tmp[r] = {acc, r};
      }
      std::partial_sort(tmp.begin(), tmp.begin() + k, tmp.end());
      gt[i].resize(k);
      for (int j = 0; j < k; ++j) gt[i][j] = tmp[j].second;
    }
  }

  auto t_build = Clock::now();
  void *h = api.build(base.data(), n, dim);
  double build_ms = ms(t_build);
  if (!h) { fprintf(stderr, "build failed\n"); return 3; }

  std::string path = snap.empty() ? "/workspace/bridge_snapshot.bin" : snap;
  auto t_ser = Clock::now();
  int src = api.serialize(h, path.c_str());
  double ser_ms = ms(t_ser);
  api.free_(h);

  auto t_de = Clock::now();
  void *h2 = api.deserialize(path.c_str(), n, dim);
  double de_ms = ms(t_de);
  if (!h2) { fprintf(stderr, "deserialize failed\n"); return 3; }

  // recall on the sample, batched
  std::vector<unsigned> ids(qn * k);
  std::vector<float> dist(qn * k);
  api.search(h2, q.data(), qn, k, ids.data(), dist.data());
  int64_t hit = 0;
  for (int64_t i = 0; i < qn; ++i)
    for (int j = 0; j < k; ++j)
      if (std::find(gt[i].begin(), gt[i].end(), (int64_t)ids[i * k + j]) != gt[i].end()) ++hit;
  double recall = double(hit) / double(qn * k);

  // single-row queries, the way the SQL layer calls it
  auto t_single = Clock::now();
  int bad = 0;
  for (int64_t i = 0; i < single; ++i) {
    unsigned one[64];
    float od[64];
    if (api.search(h2, q.data() + (i % qn) * dim, 1, k, one, od) != 0) ++bad;
    for (int j = 0; j < k; ++j)
      if (one[j] >= (unsigned)n) ++bad;
  }
  double single_ms = ms(t_single);

  api.free_(h2);

  const char *gd = getenv("SEEKDB_CUVS_GRAPH_DEGREE");
  printf("[BRIDGE] n=%lld dim=%d gd=%s igd=%s niter=%s | build_ms=%.0f serialize_ms=%.0f "
         "deserialize_ms=%.0f | recall@%d=%.4f | nq1_queries=%lld total_ms=%.0f qps=%.1f bad=%d\n",
         (long long)n, dim, gd ? gd : "64(default)",
         getenv("SEEKDB_CUVS_INTERMEDIATE_GRAPH_DEGREE") ? getenv("SEEKDB_CUVS_INTERMEDIATE_GRAPH_DEGREE") : "128(default)",
         getenv("SEEKDB_CUVS_NN_DESCENT_NITER") ? getenv("SEEKDB_CUVS_NN_DESCENT_NITER") : "20(default)",
         build_ms, ser_ms, de_ms, k, recall, (long long)single, single_ms,
         single_ms > 0 ? single * 1000.0 / single_ms : 0.0, bad);
  (void)src;
  return 0;
}
