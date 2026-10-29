/*
 * Copyright (c) 2025 OceanBase.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#ifndef OB_SPARSE_DAAT_ITER_H_
#define OB_SPARSE_DAAT_ITER_H_

#include "ob_i_sparse_retrieval_iter.h"
#include "ob_sparse_utils.h"
#include "share/vector/ob_bit_vector.h"
#include "storage/access/ob_scan_merge_loser_tree.h"
#include "storage/access/ob_simple_rows_merger.h"

namespace oceanbase
{
namespace storage
{

struct ObSRMergeItem
{
  ObSRMergeItem() : relevance_(0.0), iter_idx_(-1), equal_with_next_(false) {}
  ~ObSRMergeItem() = default;
  TO_STRING_KV(K_(iter_idx), K_(relevance), K_(equal_with_next));

  double relevance_;
  int64_t iter_idx_;
  bool equal_with_next_;
};

struct ObSRMergeCmp
{
  ObSRMergeCmp();
  virtual ~ObSRMergeCmp() {}

  int init(sql::ObDatumMeta id_meta, const ObFixedArray<const ObDatum *, ObIAllocator> *iter_ids);
  int cmp(const ObSRMergeItem &l, const ObSRMergeItem &r, int64_t &cmp_ret);
  int cmp_datum(const ObDatum &l, const ObDatum &r, int64_t &cmp_ret);
private:
  common::ObDatumCmpFuncType cmp_func_;
  // raw backing storage of the caller-provided fixed array, valid for the iter's lifetime (count fixed at init)
  const ObDatum * const *id_datums_;
  // binary collation docid fast path, same result as strcmpsp(CS_TYPE_BINARY, ..., true)
  bool is_binary_id_;
  bool is_inited_;
};

typedef ObSimpleRowsMerger<ObSRMergeItem, ObSRMergeCmp> ObSRSimpleMerger;
typedef ObMergeLoserTree<ObSRMergeItem, ObSRMergeCmp> ObSRLoserTree;
typedef common::ObRowsMerger<ObSRMergeItem, ObSRMergeCmp> ObSRMergeHeap;

// per-dim current row of the register-based DAAT merge (replaces the merge heap in the
// base-class round path; the heap path stays for iters that drive the heap directly)
struct ObSRDimState
{
  ObSRDimState() : id_(nullptr), rel_(0.0), active_(false) {}
  const ObDatum *id_;
  double rel_;
  bool active_;
};

// implementation of basic DaaT query processing algorithm primitives
class ObSRDaaTIterImpl : public ObISparseRetrievalMergeIter
{
public:
  ObSRDaaTIterImpl();
  virtual ~ObSRDaaTIterImpl() {}
  virtual int get_next_row() override;
  virtual int get_next_rows(const int64_t capacity, int64_t &count) override;
  int init(
      ObSparseRetrievalMergeParam &iter_param,
      ObIArray<ObISRDaaTDimIter *> &dim_iters,
      ObIAllocator &iter_allocator,
      ObSRDaaTRelevanceCollector &relevance_collector);
  virtual void reuse(const bool switch_tablet = false) override;
  virtual void reset() override;
  virtual int get_query_max_score(double &score) override;
  
  INHERIT_TO_STRING_KV("ObISparseRetrievalMergeIter", ObISparseRetrievalMergeIter,
      K_(next_round_iter_idxes), K_(next_round_cnt));
protected:
  virtual int pre_process();
  virtual int do_one_merge_round(int64_t &count);
  virtual int fill_merge_heap();
  virtual int collect_dims_by_id(const ObDatum *&id_datum, double &relevance, bool &got_valid_id);
  virtual int process_collected_row(const ObDatum &id_datum, const double relevance);
  virtual int filter_on_demand(const int64_t count, const double relevance, bool &need_project);
  virtual int cache_result(int64_t &count, const ObDatum &id_datum, const double relevance);
  virtual int project_results(const int64_t count);
  int init_merge_heap(const int64_t count);
  // Batched filter path: merge rows without evaluating the filter per row, then evaluate
  // the filter expression once for the whole batch and compact the surviving rows.
  int do_one_merge_round_raw(int64_t &count);
  int buffer_raw_result(int64_t &count, const ObDatum &id_datum, const double relevance);
  int apply_batch_filter(const int64_t raw_count, int64_t &count);
  // register-based DAAT merge: advance dims consumed last round, pick the min docid,
  // collect all dims holding it (ascending iter_idx, same float-sum order as the heap path)
  int fast_fill_dims();
  int fast_collect_dims(const ObDatum *&id_datum, double &relevance, bool &got_valid_id);
  int fast_pick_min(const ObDatum *&id_datum, bool &got_valid_id);
  // direct-to-output merge: writes the projected datums straight from the merge loop,
  // no intermediate buffer, no filter (keep-all) and no collector when merge_emit_all_
  int do_one_merge_round_emit(int64_t &count, ObDatum *id_datums, ObDatum *rel_datums);
  int emit_row(int64_t &count, const ObDatum &id_datum, ObDatum *id_datums, ObDatum *rel_datums);
protected:
  ObIAllocator *iter_allocator_;
  ObSparseRetrievalMergeParam *iter_param_;
  ObIArray<ObISRDaaTDimIter *> *dim_iters_;
  ObSRMergeCmp merge_cmp_;
  ObSRMergeHeap *merge_heap_;
  ObSRDaaTRelevanceCollector *relevance_collector_;
  ObFixedArray<const ObDatum *, ObIAllocator> iter_domain_ids_; // record every dim iter's output domain id, one (ObDatum *) for one dim iter
  // raw backing storage of iter_domain_ids_, count fixed at init so the pointer is stable
  const ObDatum **domain_id_ptrs_;
  // dim iter pointers copied at init, avoids bounds-checked virtual at() in the merge loop
  ObISRDaaTDimIter **dim_iter_ptrs_;
  int64_t dim_cnt_;
  // filter is BOOL(relevance_proj_expr): row passes iff relevance != 0.0
  bool relevance_bool_filter_;
  ObFixedArray<ObDocIdExt, ObIAllocator> buffered_domain_ids_; // cache for output
  ObFixedArray<double, ObIAllocator> buffered_relevances_;
  ObFixedArray<int64_t, ObIAllocator> next_round_iter_idxes_;
  // raw backing storage of the three arrays above, fixed count at init; avoids the
  // bounds-checked virtual element access in the per-row merge loops
  ObDocIdExt *buffered_ids_;
  double *buffered_rels_;
  int64_t *next_round_idx_;
  // scratch output datums for the direct-emit merge; the projection frame they are
  // published to is shared with the underlying scan and rewritten on every batch load
  ObDatum *emit_datums_;
  // per-dim weights copied at init; nullptr when the caller supplies none
  double *dim_weight_vals_;
  int64_t next_round_cnt_;
  sql::ObBitVector *filter_skip_;
  void (*set_datum_func_)(ObDatum &, const ObDocIdExt &);
  // register-based merge state; used when use_fast_merge_ is set, otherwise merge_heap_
  ObSRDimState *dim_states_;
  bool use_fast_merge_;
  // BOOL(relevance) filter with relevance skipped by optimizer: always true
  bool relevance_keep_all_;
  // keep-all and the collector accepts every merged doc: skip the collector per doc
  bool merge_emit_all_;
  // relevance is not computed at all: never fetch the per-dim posting score
  bool skip_dim_score_;
private:
  DISALLOW_COPY_AND_ASSIGN(ObSRDaaTIterImpl);
};

} // namespace storage
} // namespace oceanbase

#endif
