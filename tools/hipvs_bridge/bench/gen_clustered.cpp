// Mixture-of-Gaussians vector set generator.
//
// Plain isotropic Gaussian data in 128-d is nearly useless for ANN quality checks: every
// point is at a similar distance, so Recall@10 stays low no matter how good the graph is.
// Real embeddings live near a few centroids, which is what this produces.
//
// Output: raw little-endian float32, row-major, shape (rows, dim), no header.
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <random>
#include <string>
#include <vector>

int main(int argc, char **argv) {
  int64_t rows = 1000000;
  int dim = 128;
  int clusters = 1000;
  float spread = 0.05f;   // within-cluster sigma
  float extent = 4.0f;    // centroid spread
  uint32_t seed = 20261101;
  std::string out;
  int64_t query_rows = 0;
  std::string query_out;

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&]() {
      if (i + 1 >= argc) { fprintf(stderr, "missing value after %s\n", a.c_str()); exit(1); }
      return argv[++i];
    };
    if (a == "--rows") rows = atoll(next());
    else if (a == "--dim") dim = atoi(next());
    else if (a == "--clusters") clusters = atoi(next());
    else if (a == "--spread") spread = atof(next());
    else if (a == "--extent") extent = atof(next());
    else if (a == "--seed") seed = (uint32_t)atoi(next());
    else if (a == "--out") out = next();
    else if (a == "--query-rows") query_rows = atoll(next());
    else if (a == "--query-out") query_out = next();
    else {
      fprintf(stderr,
              "usage: %s --out FILE [--rows N] [--dim D] [--clusters K] [--spread S]"
              " [--extent E] [--seed S]\n",
              argv[0]);
      return 1;
    }
  }
  if (out.empty()) { fprintf(stderr, "--out required\n"); return 1; }

  std::mt19937 rng(seed);
  std::normal_distribution<float> centroid(-extent, extent);
  std::normal_distribution<float> jitter(0.0f, spread);
  std::uniform_int_distribution<int> pick(0, clusters - 1);

  std::vector<std::vector<float>> cents(clusters, std::vector<float>(dim));
  for (int c = 0; c < clusters; ++c)
    for (int d = 0; d < dim; ++d) cents[c][d] = centroid(rng);

  std::vector<float> v(rows * dim);
  for (int64_t r = 0; r < rows; ++r) {
    const std::vector<float> &mu = cents[pick(rng)];
    for (int d = 0; d < dim; ++d) v[r * dim + d] = mu[d] + jitter(rng);
  }

  std::ofstream os(out, std::ios::binary);
  if (!os) { fprintf(stderr, "cannot write %s\n", out.c_str()); return 1; }
  os.write(reinterpret_cast<const char *>(v.data()), (std::streamsize)v.size() * sizeof(float));
  os.close();
  fprintf(stderr, "[GEN] %s rows=%lld dim=%d clusters=%d spread=%.3f bytes=%lld\n", out.c_str(),
          (long long)rows, dim, clusters, spread, (long long)v.size() * 4);

  // Queries must be drawn around the SAME centroids as the base set; a second run with a
  // different seed produces a different cluster structure and the recall figures become
  // meaningless.
  if (query_rows > 0) {
    std::normal_distribution<float> jq(0.0f, spread);
    std::vector<float> qv(query_rows * dim);
    for (int64_t r = 0; r < query_rows; ++r) {
      const std::vector<float> &mu = cents[pick(rng)];
      for (int d = 0; d < dim; ++d) qv[r * dim + d] = mu[d] + jq(rng);
    }
    std::ofstream qos(query_out, std::ios::binary);
    if (!qos) { fprintf(stderr, "cannot write %s\n", query_out.c_str()); return 1; }
    qos.write(reinterpret_cast<const char *>(qv.data()), (std::streamsize)qv.size() * sizeof(float));
    qos.close();
    fprintf(stderr, "[GEN] %s rows=%lld dim=%d (same centroids)\n", query_out.c_str(),
            (long long)query_rows, dim);
  }
  return 0;
}
