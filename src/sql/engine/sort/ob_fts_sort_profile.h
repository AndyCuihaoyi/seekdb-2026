/**
 * C0 profiling only: segment counters for the FTS build sort path.
 * Gated by env OB_FTS_SORT_PROFILE; no behavior change when unset.
 */
#ifndef OCEANBASE_SQL_ENGINE_SORT_OB_FTS_SORT_PROFILE_H_
#define OCEANBASE_SQL_ENGINE_SORT_OB_FTS_SORT_PROFILE_H_

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <climits>

namespace oceanbase
{
namespace sql
{

struct ObFtsSortProfile
{
  int64_t tid_;
  int64_t op_start_tsc_;
  int64_t proc_tsc_;
  int64_t add_rows_;
  int64_t add_tsc_;
  int64_t child_tsc_;
  int64_t refill_rows_;
  int64_t refill_tsc_;
  int64_t prescan_calls_;
  int64_t prescan_rows_;
  int64_t prescan_tsc_;
  int64_t prescan_child_tsc_;
  int64_t prescan_add_tsc_;
  int64_t prescan_finish_tsc_;
  int64_t prescan_read_tsc_;
  int64_t prescan_refill_tsc_;
  int64_t prescan_dumps_;
  int64_t prescan_file_size_;
  int64_t sort_tsc_;
  int64_t sort_rows_;
  int64_t cmp_count_;
  int64_t out_rows_;
  int64_t out_tsc_;
  int64_t op_tsc_;
  int64_t dump_calls_;
  int64_t store_rows_;
  int64_t sort_chunks_;
  int64_t row_bytes_;
  int64_t cell0_min_;
  int64_t cell0_max_;
  int64_t cell0_sum_;
  int64_t cell0_null_;
  int64_t cell0_16_;
  int64_t cell1_min_;
  int64_t cell1_max_;
  int64_t cell1_sum_;
  int64_t cell1_null_;
  int64_t cell1_16_;
  int64_t compress_type_;
  int64_t compact_format_;
  int64_t encode_sortkey_;
  int64_t sort_path_;
  bool sort_printed_;
  bool out_printed_;

  void reset()
  {
    memset(this, 0, sizeof(*this));
    cell0_min_ = INT64_MAX;
    cell1_min_ = INT64_MAX;
    compress_type_ = -1;
    sort_path_ = -1;
    tid_ = static_cast<int64_t>(::syscall(SYS_gettid));
  }
};

inline bool ob_fts_sort_dump_on()
{
  static const bool on = (nullptr != ::getenv("OB_FTS_SORT_DUMP"));
  return on;
}

inline void ob_fts_prof_append(const char *line)
{
  FILE *fp = ::fopen("/tmp/fts_sort_prof.txt", "a");
  if (NULL != fp) {
    ::fprintf(fp, "%s\n", line);
    ::fclose(fp);
  }
}

inline bool ob_fts_sort_prof_on()
{
  static const bool on = (nullptr != ::getenv("OB_FTS_SORT_PROFILE"));
  return on;
}

inline uint64_t ob_fts_sort_tsc()
{
#if defined(__x86_64__) || defined(__i386__)
  return __builtin_ia32_rdtsc();
#else
  return static_cast<uint64_t>(::clock());
#endif
}

inline ObFtsSortProfile &ob_fts_sort_prof()
{
  static thread_local ObFtsSortProfile prof;
  return prof;
}

} // namespace sql
} // namespace oceanbase
#endif // OCEANBASE_SQL_ENGINE_SORT_OB_FTS_SORT_PROFILE_H_
