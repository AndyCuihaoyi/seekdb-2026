/**
 * FTS aux DDL sort impl: replaces the comparison sort of the order-preserving
 * encoded sort key with an MSD byte-radix pass, so the row order is produced by
 * bucketing the key bytes instead of O(n log n) memcmps.
 *
 * Selected only by ObSortOp, and only for the FTS aux DDL sort shape
 * (fts_aux_ddl_, 2-column aux rowkey, no topn/ties/partition/local-merge).
 * Everything else -- spilled input, external merge sort, tiny batches, unencodable
 * key columns, over-cap key memory -- delegates to ObSortOpImpl::sort() verbatim.
 */
#pragma once

#include "sql/engine/sort/ob_sort_op_impl.h"

namespace oceanbase
{
namespace sql
{

class FtsAuxRadixSortImpl : public ObSortOpImpl
{
public:
  explicit FtsAuxRadixSortImpl(ObMonitorNode &op_monitor_info)
    : ObSortOpImpl(op_monitor_info),
      radix_keyed_rows_(0),
      radix_keyed_bytes_(0),
      radix_fallbacks_(0)
  {}
  virtual ~FtsAuxRadixSortImpl() {}

  virtual int sort();

  // Rows whose key was built and bucketed by the radix pass in the last sort().
  int64_t get_radix_keyed_rows() const { return radix_keyed_rows_; }
  int64_t get_radix_keyed_bytes() const { return radix_keyed_bytes_; }

private:
  int radix_sort_rows(bool &sorted);

  int64_t radix_keyed_rows_;
  int64_t radix_keyed_bytes_;
  int64_t radix_fallbacks_;
};

} // end namespace sql
} // end namespace oceanbase
