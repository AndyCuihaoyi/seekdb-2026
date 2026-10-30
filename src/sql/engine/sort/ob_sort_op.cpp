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

#define USING_LOG_PREFIX SQL_ENG

#include "sql/engine/sort/ob_sort_op.h"
#include "sql/engine/sort/ob_fts_sort_profile.h"
#include "sql/engine/px/ob_px_util.h"
#include "sql/engine/aggregate/ob_hash_groupby_op.h"
#include "sql/engine/expr/ob_expr_topn_filter.h"

namespace oceanbase
{
namespace sql
{

namespace
{
void ob_fts_prof_print_sort(ObFtsSortProfile &p)
{
  char buf[2048];
  const int64_t c0_n = p.add_rows_ + p.refill_rows_ - p.cell0_null_;
  const int64_t c1_n = p.add_rows_ + p.refill_rows_ - p.cell1_null_;
  ::snprintf(buf, sizeof(buf),
      "SORT tid=%ld rows=%ld cmp=%ld add_rows=%ld add_cyc=%ld refill_rows=%ld refill_cyc=%ld "
      "prescan_calls=%ld prescan_rows=%ld prescan_cyc=%ld prescan_refill_cyc=%ld "
      "prescan_dump=%ld prescan_file=%ld sort_cyc=%ld proc_cyc=%ld "
      "path=%ld enc=%ld ctype=%ld cformat=%ld chunks=%ld store_rows=%ld dumps=%ld "
      "row_bytes=%ld c0_min=%ld c0_avg=%ld c0_max=%ld c0_null=%ld c0_x16=%ld "
      "c1_min=%ld c1_avg=%ld c1_max=%ld c1_null=%ld c1_x16=%ld",
      (long)p.tid_, (long)p.sort_rows_, (long)p.cmp_count_,
      (long)p.add_rows_, (long)p.add_tsc_, (long)p.refill_rows_, (long)p.refill_tsc_,
      (long)p.prescan_calls_, (long)p.prescan_rows_, (long)p.prescan_tsc_,
      (long)p.prescan_refill_tsc_, (long)p.prescan_dumps_, (long)p.prescan_file_size_,
      (long)p.sort_tsc_, (long)p.proc_tsc_, (long)p.sort_path_, (long)p.encode_sortkey_,
      (long)p.compress_type_, (long)p.compact_format_, (long)p.sort_chunks_,
      (long)p.store_rows_, (long)p.dump_calls_, (long)p.row_bytes_,
      (long)((INT64_MAX == p.cell0_min_) ? -1 : p.cell0_min_),
      (long)((c0_n > 0) ? (p.cell0_sum_ / c0_n) : 0), (long)p.cell0_max_,
      (long)p.cell0_null_, (long)p.cell0_16_,
      (long)((INT64_MAX == p.cell1_min_) ? -1 : p.cell1_min_),
      (long)((c1_n > 0) ? (p.cell1_sum_ / c1_n) : 0), (long)p.cell1_max_,
      (long)p.cell1_null_, (long)p.cell1_16_);
  ob_fts_prof_append(buf);
}

void ob_fts_prof_print_out(ObFtsSortProfile &p)
{
  char buf[512];
  ::snprintf(buf, sizeof(buf),
      "OUT tid=%ld rows=%ld out_cyc=%ld op_cyc=%ld add_rows=%ld refill_rows=%ld "
      "sort_rows=%ld sort_cyc=%ld path=%ld c0_x16=%ld c1_x16=%ld",
      (long)p.tid_, (long)p.out_rows_, (long)p.out_tsc_, (long)p.op_tsc_,
      (long)p.add_rows_, (long)p.refill_rows_, (long)p.sort_rows_, (long)p.sort_tsc_,
      (long)p.sort_path_, (long)p.cell0_16_, (long)p.cell1_16_);
  ob_fts_prof_append(buf);
}

} // anonymous namespace

ObSortSpec::ObSortSpec(common::ObIAllocator &alloc, const ObPhyOperatorType type)
  : ObOpSpec(alloc, type),
  topn_expr_(nullptr),
  topk_limit_expr_(nullptr),
  topk_offset_expr_(nullptr),
  all_exprs_(alloc),
  sort_collations_(alloc),
  sort_cmp_funs_(alloc),
  minimum_row_count_(0),
  topk_precision_(0),
  prefix_pos_(0),
  is_local_merge_sort_(false),
  is_fetch_with_ties_(false),
  prescan_enabled_(false),
  enable_encode_sortkey_opt_(false),
  part_cnt_(0),
  pd_topn_filter_info_(alloc)
{}

OB_SERIALIZE_MEMBER((ObSortSpec, ObOpSpec),
                    topn_expr_,
                    topk_limit_expr_,
                    topk_offset_expr_,
                    all_exprs_,
                    sort_collations_,
                    sort_cmp_funs_,
                    minimum_row_count_,
                    topk_precision_,
                    prefix_pos_,
                    is_local_merge_sort_,
                    is_fetch_with_ties_,
                    prescan_enabled_,
                    enable_encode_sortkey_opt_,
                    part_cnt_,
                    compress_type_,
                    pd_topn_filter_info_);

ObSortOp::ObSortOp(ObExecContext &ctx_, const ObOpSpec &spec, ObOpInput *input)
  : ObOperator(ctx_, spec, input),
  sort_impl_(op_monitor_info_),
  prefix_sort_impl_(op_monitor_info_),
  read_func_(&ObSortOp::sort_impl_next),
  read_batch_func_(&ObSortOp::sort_impl_next_batch),
  sort_row_count_(0),
  is_first_(true),
  ret_row_count_(0),
  iter_end_(false)
{}

int ObSortOp::inner_open()
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(child_)) {
    ret = OB_ERR_UNEXPECTED;
  }
  return OB_SUCCESS;
}

int ObSortOp::inner_rescan()
{
  if (MY_SPEC.enable_pd_topn_filter() && !MY_SPEC.pd_topn_filter_info_.is_shuffle_) {
    // for local topn runtime filter, rescan topn filter expression context
    reset_pd_topn_filter_expr_ctx();
  }
  reset();
  iter_end_ = false;
  return ObOperator::inner_rescan();
}

void ObSortOp::reset_pd_topn_filter_expr_ctx()
{
  uint32_t expr_ctx_id = MY_SPEC.pd_topn_filter_info_.expr_ctx_id_;
  ObExprTopNFilterContext *topn_filter_ctx =
      static_cast<ObExprTopNFilterContext *>(ctx_.get_expr_op_ctx(expr_ctx_id));
  if (nullptr != topn_filter_ctx) {
    topn_filter_ctx->reset_for_rescan();
  }
}

void ObSortOp::reset()
{
  sort_impl_.reset();
  prefix_sort_impl_.reset();
  read_func_ = &ObSortOp::sort_impl_next;
  read_batch_func_ = &ObSortOp::sort_impl_next_batch;
  sort_row_count_ = 0;
  ret_row_count_ = 0;
  is_first_ = true;
}

void ObSortOp::destroy()
{
  sort_impl_.unregister_profile_if_necessary();
  sort_impl_.~ObSortOpImpl();
  prefix_sort_impl_.unregister_profile_if_necessary();
  prefix_sort_impl_.~ObPrefixSortImpl();
  read_func_ = nullptr;
  read_batch_func_ = nullptr;
  sort_row_count_ = 0;
  is_first_ = true;
  ret_row_count_ = 0;
  ObOperator::destroy();
}

int ObSortOp::inner_close()
{
  sort_impl_.collect_memory_dump_info(op_monitor_info_);
  sort_impl_.unregister_profile();
  prefix_sort_impl_.unregister_profile();
  return OB_SUCCESS;
}

int ObSortOp::get_int_value(const ObExpr *in_val, int64_t &out_val)
{
  int ret = OB_SUCCESS;
  ObDatum *datum = NULL;
  if (NULL != in_val) {
    if (OB_FAIL(in_val->eval(eval_ctx_, datum))) {
    } else if (OB_ISNULL(datum)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (datum->is_null()) {
      out_val = 0;
    } else {
      out_val = *datum->int_;
    }
  }
  return ret;
}

int ObSortOp::get_topn_count(int64_t &topn_cnt)
{
  int ret = OB_SUCCESS;
  topn_cnt = INT64_MAX;
  if ((OB_ISNULL(MY_SPEC.topn_expr_) && OB_ISNULL(MY_SPEC.topk_limit_expr_))) {
    // do nothing
  } else if (((NULL != MY_SPEC.topn_expr_) && (NULL != MY_SPEC.topk_limit_expr_))) {
    ret = OB_ERR_UNEXPECTED;
  } else if (NULL != MY_SPEC.topn_expr_) {
    if (OB_FAIL(get_int_value(MY_SPEC.topn_expr_, topn_cnt))) {
    } else {
      topn_cnt = std::max(MY_SPEC.minimum_row_count_, topn_cnt);
    }
  } else if (NULL != MY_SPEC.topk_limit_expr_) {
    int64_t limit = -1;
    int64_t offset = 0;
    if ((OB_FAIL(get_int_value(MY_SPEC.topk_limit_expr_, limit))
        || OB_FAIL(get_int_value(MY_SPEC.topk_offset_expr_, offset)))) {
    } else if (OB_UNLIKELY(limit < 0 || offset < 0)) {
      ret = OB_ERR_ILLEGAL_VALUE;
    } else {
      // TODO & FIXME by longzhong.wlz : Wait for groupby implementation to handle this logic
      topn_cnt = std::max(MY_SPEC.minimum_row_count_, limit + offset);
      int64_t row_count = 0;
      ObPhyOperatorType op_type = child_->get_spec().type_;
      if (PHY_HASH_GROUP_BY != op_type) {
        ret = OB_ERR_UNEXPECTED;
      } else {
        row_count = static_cast<ObHashGroupByOp *>(child_)->get_hash_groupby_row_count();
      }
      if (OB_SUCC(ret)) {
        topn_cnt = std::max(topn_cnt,
                            static_cast<int64_t>(row_count * MY_SPEC.topk_precision_ / 100));
        if (topn_cnt >= row_count) {
          ctx_.get_physical_plan_ctx()->set_is_result_accurate(true);
        } else {
          ctx_.get_physical_plan_ctx()->set_is_result_accurate(false);
        }
      }
    }
  }
  return ret;
}

int ObSortOp::process_sort()
{
  int ret = OB_SUCCESS;
  const bool fts_prof = ob_fts_sort_prof_on();
  if (fts_prof) {
    ObFtsSortProfile &p = ob_fts_sort_prof();
    p.reset();
    p.op_start_tsc_ = static_cast<int64_t>(ob_fts_sort_tsc());
  }
  if (read_func_ == &ObSortOp::prefix_sort_impl_next) {
    // prefix sort get child row in it's own wrap, do nothing here
  } else if (read_func_ == &ObSortOp::sort_impl_next) {
    bool need_dump = false;
    while (OB_SUCC(ret)) {
      clear_evaluated_flag();
      if (OB_FAIL(try_check_status())) {
      } else if (OB_FAIL(child_->get_next_row())) {
        if (OB_ITER_END != ret) {
        }
      } else {
        sort_row_count_++;
        OZ(sort_impl_.add_row(MY_SPEC.all_exprs_, need_dump));
        sort_impl_.collect_memory_dump_info(op_monitor_info_);
        if (need_dump && MY_SPEC.prescan_enabled_) {
          break;
        }
      }
    }
    if (OB_SUCC(ret) && need_dump && MY_SPEC.prescan_enabled_
        && OB_FAIL(scan_all_then_sort())) {
      if (OB_ITER_END != ret) {
      }
    }
    if (OB_ITER_END == ret) {
      ret = OB_SUCCESS;
    }
    OZ(sort_impl_.sort());
    sort_impl_.collect_memory_dump_info(op_monitor_info_);
  } else {
    ret = OB_ERR_UNEXPECTED;
  }
  if (fts_prof) {
    ObFtsSortProfile &p = ob_fts_sort_prof();
    p.proc_tsc_ = static_cast<int64_t>(ob_fts_sort_tsc()) - p.op_start_tsc_;
    p.sort_rows_ = sort_row_count_;
    ob_fts_prof_print_sort(p);
  }
  return ret;
}

int ObSortOp::process_sort_batch()
{
  int ret = OB_SUCCESS;
  const bool fts_prof = ob_fts_sort_prof_on();
  if (fts_prof) {
    ObFtsSortProfile &p = ob_fts_sort_prof();
    p.reset();
    p.op_start_tsc_ = static_cast<int64_t>(ob_fts_sort_tsc());
  }
  if (read_batch_func_ == &ObSortOp::prefix_sort_impl_next_batch) {
    // prefix sort get child row in it's own wrap, do nothing here
  } else if (read_batch_func_ == &ObSortOp::sort_impl_next_batch) {
    bool need_dump = false;
    while (OB_SUCC(ret)) {
      clear_evaluated_flag();
      const ObBatchRows *input_brs = NULL;
      if (OB_FAIL(try_check_status())) {
      } else if (OB_FAIL(child_->get_next_batch(MY_SPEC.max_batch_size_, input_brs))) {
      } else {
        if (input_brs->size_ > 0) {
          sort_row_count_ += input_brs->size_
              - input_brs->skip_->accumulate_bit_cnt(input_brs->size_);
          OZ(sort_impl_.add_batch(MY_SPEC.all_exprs_, *input_brs->skip_,
                                  input_brs->size_, need_dump));
          sort_impl_.collect_memory_dump_info(op_monitor_info_);
        }
        if (input_brs->end_ || (need_dump && MY_SPEC.prescan_enabled_)) {
          break;
        }
      }
    }
    if (OB_SUCC(ret) && need_dump && MY_SPEC.prescan_enabled_
        && OB_FAIL(scan_all_then_sort_batch())) {
      if (OB_ITER_END != ret) {
      }
    }
    op_monitor_info_.otherstat_7_id_ = ObSqlMonitorStatIds::ROW_COUNT;
    op_monitor_info_.otherstat_7_value_ = sort_row_count_; 
    OZ(sort_impl_.sort());
    sort_impl_.collect_memory_dump_info(op_monitor_info_);
  } else {
    ret = OB_ERR_UNEXPECTED;
  }
  if (fts_prof) {
    ObFtsSortProfile &p = ob_fts_sort_prof();
    p.proc_tsc_ = static_cast<int64_t>(ob_fts_sort_tsc()) - p.op_start_tsc_;
    p.sort_rows_ = sort_row_count_;
    ob_fts_prof_print_sort(p);
  }
  return ret;
}

// if we need to do std sort, the thread will be blocked and cannot
// drive the table scan below, which will block other sort ops.
// To relieve this, we scan all rows into a cache store first then
// continue the sort part.
int ObSortOp::scan_all_then_sort()
{
  int ret = OB_SUCCESS;
  const bool fts_prof = ob_fts_sort_prof_on();
  const uint64_t fts_fill_t0 = fts_prof ? ob_fts_sort_tsc() : 0;
  SMART_VAR(ObCompactStore, cache_store) {
    if (OB_FAIL(cache_store.init(2 * 1024 * 1024,
        ObCtxIds::DEFAULT_CTX_ID, "SORT_CACHE_CTX", true/*enable dump*/, 0, true,
        MY_SPEC.compress_type_, &MY_SPEC.all_exprs_))) {
    } else if (OB_FAIL(cache_store.alloc_dir_id())) {
    }
    while (OB_SUCC(ret)) {
      clear_evaluated_flag();
      if (OB_FAIL(try_check_status())) {
      } else if (OB_FAIL(child_->get_next_row())) {
        if (OB_ITER_END != ret) {
        }
      } else {
        sort_row_count_++;
        if (OB_FAIL(cache_store.add_row(MY_SPEC.all_exprs_, eval_ctx_))) {
        }
      }
    }

    if (OB_ITER_END == ret) {
      ret = OB_SUCCESS;
    }

    if (OB_SUCC(ret)) {
      if (OB_FAIL(cache_store.finish_add_row(false))) {
      } else if (fts_prof) {
        ObFtsSortProfile &p = ob_fts_sort_prof();
        ++p.prescan_calls_;
        p.prescan_rows_ += cache_store.get_row_cnt();
        p.prescan_tsc_ += static_cast<int64_t>(ob_fts_sort_tsc() - fts_fill_t0);
        p.prescan_dumps_ += cache_store.has_dumped() ? 1 : 0;
        p.prescan_file_size_ += cache_store.get_file_size();
      }
      if (OB_SUCC(ret)) {
        const uint64_t fts_refill_t0 = fts_prof ? ob_fts_sort_tsc() : 0;
        const ObChunkDatumStore::StoredRow *store_row = NULL;
        bool has_next = false;
        while (OB_SUCC(ret) && OB_SUCC(cache_store.has_next(has_next)) && has_next) {
          if (OB_FAIL(cache_store.get_next_row(store_row))) {
            if (OB_ITER_END != ret) {
            }
          } else if (OB_ISNULL(store_row)) {
            ret = OB_ERR_UNEXPECTED;
          } else {
            OZ(sort_impl_.add_stored_row(*store_row));
          }
        }
        if (fts_prof) {
          ob_fts_sort_prof().prescan_refill_tsc_ +=
              static_cast<int64_t>(ob_fts_sort_tsc() - fts_refill_t0);
        }
      }
    }
  }
  return ret;
}

int ObSortOp::scan_all_then_sort_batch()
{
  int ret = OB_SUCCESS;
  const bool fts_prof = ob_fts_sort_prof_on();
  const uint64_t fts_fill_t0 = fts_prof ? ob_fts_sort_tsc() : 0;
  SMART_VAR(ObCompactStore, cache_store) {
    if (OB_FAIL(cache_store.init(16 * 1024,
        ObCtxIds::DEFAULT_CTX_ID, "SORT_CACHE_CTX", true/*enable dump*/, 0, true,
        MY_SPEC.compress_type_, &MY_SPEC.all_exprs_))) {
    } else if (OB_FAIL(cache_store.alloc_dir_id())) {
    }
    while (OB_SUCC(ret)) {
      clear_evaluated_flag();
      const ObBatchRows *input_brs = NULL;
      if (OB_FAIL(try_check_status())) {
      } else if (OB_FAIL(child_->get_next_batch(MY_SPEC.max_batch_size_, input_brs))) {
      } else {
        if (input_brs->size_ > 0) {
          sort_row_count_ += input_brs->size_
              - input_brs->skip_->accumulate_bit_cnt(input_brs->size_);
          int64_t stored_row_count = -1;
          if (OB_FAIL(cache_store.add_batch(MY_SPEC.all_exprs_, eval_ctx_,
              *input_brs->skip_, input_brs->size_, stored_row_count))) {
          }
        }
        if (input_brs->end_) {
          break;
        }
      }
    }

    if (OB_ITER_END == ret) {
      ret = OB_SUCCESS;
    }
    op_monitor_info_.otherstat_7_id_ = ObSqlMonitorStatIds::ROW_COUNT;
    op_monitor_info_.otherstat_7_value_ = sort_row_count_; 
    if (OB_SUCC(ret)) {
      if (OB_FAIL(cache_store.finish_add_row(false))) {
      } else if (fts_prof) {
        ObFtsSortProfile &p = ob_fts_sort_prof();
        ++p.prescan_calls_;
        p.prescan_rows_ += cache_store.get_row_cnt();
        p.prescan_tsc_ += static_cast<int64_t>(ob_fts_sort_tsc() - fts_fill_t0);
        p.prescan_dumps_ += cache_store.has_dumped() ? 1 : 0;
        p.prescan_file_size_ += cache_store.get_file_size();
      }
      if (OB_SUCC(ret)) {
        const uint64_t fts_refill_t0 = fts_prof ? ob_fts_sort_tsc() : 0;
        const ObChunkDatumStore::StoredRow *store_row = NULL;
        bool has_next = false;
        while (OB_SUCC(ret) && OB_SUCC(cache_store.has_next(has_next)) && has_next) {
          if (OB_FAIL(cache_store.get_next_row(store_row))) {
            if (OB_ITER_END != ret) {
              LOG_WARN("failed to get next row");
            }
          } else if (OB_ISNULL(store_row)) {
            ret = OB_ERR_UNEXPECTED;
            LOG_WARN("failed to get next row");
          } else {
            OZ(sort_impl_.add_stored_row(*store_row));
          }
        }
        if (fts_prof) {
          ob_fts_sort_prof().prescan_refill_tsc_ +=
              static_cast<int64_t>(ob_fts_sort_tsc() - fts_refill_t0);
        }
      }
    }
  }
  return ret;
}

int ObSortOp::init_prefix_sort(int64_t row_count,
                               bool is_batch,
                               int64_t topn_cnt)
{
  int ret = OB_SUCCESS;
  OZ(prefix_sort_impl_.init(MY_SPEC.prefix_pos_, MY_SPEC.all_exprs_,
      &MY_SPEC.sort_collations_, &MY_SPEC.sort_cmp_funs_, &eval_ctx_, child_,
      this, ctx_, MY_SPEC.enable_encode_sortkey_opt_, sort_row_count_, topn_cnt,
      MY_SPEC.is_fetch_with_ties_));
  if (is_batch) {
    read_batch_func_ = &ObSortOp::prefix_sort_impl_next_batch;
  } else {
    read_func_ = &ObSortOp::prefix_sort_impl_next;
  }
  int aqs_head = MY_SPEC.enable_encode_sortkey_opt_ ? sizeof(oceanbase::sql::ObSortOpImpl::AQSItem) : 0;
  prefix_sort_impl_.set_input_rows(row_count);
  prefix_sort_impl_.set_input_width(MY_SPEC.width_ + aqs_head);
  prefix_sort_impl_.set_operator_type(MY_SPEC.type_);
  prefix_sort_impl_.set_operator_id(MY_SPEC.id_);
  prefix_sort_impl_.set_io_event_observer(&io_event_observer_);
  return ret;
}

int ObSortOp::init_sort(int64_t row_count,
                        bool is_batch,
                        int64_t topn_cnt)
{
  int ret = OB_SUCCESS;
  int64_t est_rows = MY_SPEC.rows_;
  if (OB_FAIL(ObPxEstimateSizeUtil::get_px_size(
      &ctx_, MY_SPEC.px_est_size_factor_, est_rows, est_rows))) {
  }
  OZ(sort_impl_.init(&MY_SPEC.sort_collations_, &MY_SPEC.sort_cmp_funs_, &eval_ctx_,
                     &ctx_, MY_SPEC.enable_encode_sortkey_opt_, MY_SPEC.is_local_merge_sort_,
                     false /* need_rewind */, MY_SPEC.part_cnt_, topn_cnt,
                     MY_SPEC.is_fetch_with_ties_, ObChunkDatumStore::BLOCK_SIZE,
                     MY_SPEC.compress_type_, &MY_SPEC.all_exprs_, est_rows,
                     MY_SPEC.prescan_enabled_, &MY_SPEC.pd_topn_filter_info_));
  if (is_batch) {
    read_batch_func_ = &ObSortOp::sort_impl_next_batch;
  } else {
    read_func_ = &ObSortOp::sort_impl_next;
  }
  int aqs_head = MY_SPEC.enable_encode_sortkey_opt_ ? sizeof(oceanbase::sql::ObSortOpImpl::AQSItem) : 0;
  sort_impl_.set_input_rows(row_count);
  sort_impl_.set_input_width(MY_SPEC.width_ + aqs_head);
  sort_impl_.set_operator_type(MY_SPEC.type_);
  sort_impl_.set_operator_id(MY_SPEC.id_);
  sort_impl_.set_io_event_observer(&io_event_observer_);
  return ret;
}

int ObSortOp::inner_get_next_row()
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(iter_end_)) {
    ret = OB_ITER_END;
  } else if (is_first_) {
    // Here what we want is to account
    // Charge memory to the runtime that owns the execution context.
    is_first_ = false;
    int64_t topn_cnt = INT64_MAX;
    int64_t row_count = MY_SPEC.rows_;
    
    if (OB_FAIL(ObPxEstimateSizeUtil::get_px_size(
        &ctx_, MY_SPEC.px_est_size_factor_, MY_SPEC.rows_, row_count))) {
    } else if (OB_FAIL(get_topn_count(topn_cnt))) {
    } else if (topn_cnt <= 0) { 
      iter_end_ = true; 
      ret = OB_ITER_END;
    } else if (MY_SPEC.prefix_pos_ > 0) {
      if (OB_FAIL(init_prefix_sort(row_count, false, topn_cnt))) {
      }
    } else {
      if (OB_FAIL(init_sort(row_count, false, topn_cnt))) {
      }
    }
    if (OB_SUCC(ret)) {
      if (OB_FAIL(process_sort())) { // process sort
        if (OB_ITER_END != ret) {
        }
      }
    }
  }

  if (OB_SUCC(ret)) {
    clear_evaluated_flag();
    const bool fts_prof = ob_fts_sort_prof_on();
    const uint64_t fts_t0 = fts_prof ? ob_fts_sort_tsc() : 0;
    if (OB_FAIL((this->*read_func_)())) {
      if (OB_ITER_END != ret) {
        LOG_WARN("get next row failed");
      } else {
        if (ctx_.get_my_session()->get_ddl_info().is_ddl() && ret_row_count_ != sort_row_count_) {
          ret = OB_CHECKSUM_ERROR;
        }
        iter_end_ = true;
        if (fts_prof) {
          ObFtsSortProfile &p = ob_fts_sort_prof();
          p.out_tsc_ += static_cast<int64_t>(ob_fts_sort_tsc() - fts_t0);
          p.op_tsc_ = static_cast<int64_t>(ob_fts_sort_tsc()) - p.op_start_tsc_;
          ob_fts_prof_print_out(p);
        }
        reset();
      }
    } else {
      ++ret_row_count_;
      if (fts_prof) {
        ObFtsSortProfile &p = ob_fts_sort_prof();
        p.out_tsc_ += static_cast<int64_t>(ob_fts_sort_tsc() - fts_t0);
        ++p.out_rows_;
      }
      LOG_DEBUG("finish ObSortOp::inner_get_next_row", K(ObToStringExprRow(eval_ctx_, MY_SPEC.output_)), K(ret_row_count_), K(MY_SPEC.output_));
    }
  }
  return ret;
}

int ObSortOp::inner_get_next_batch(const int64_t max_row_cnt)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(iter_end_)) {
    brs_.end_ = true;
    brs_.size_ = 0;
  } else if (is_first_) {
    is_first_ = false;
    int64_t topn_cnt = INT64_MAX;
    int64_t row_count = MY_SPEC.rows_;
    
    if (OB_FAIL(ObPxEstimateSizeUtil::get_px_size(
        &ctx_, MY_SPEC.px_est_size_factor_, MY_SPEC.rows_, row_count))) {
    } else if (OB_FAIL(get_topn_count(topn_cnt))) {
    } else if (topn_cnt <= 0) { 
      brs_.end_ = true;
      brs_.size_ = 0;
    } else if (MY_SPEC.prefix_pos_ > 0) {
      if (OB_FAIL(init_prefix_sort(row_count, true, topn_cnt))) {
      }
    } else {
      if (OB_FAIL(init_sort(row_count, true, topn_cnt))) {
      }
    }
    if (OB_SUCC(ret) && !brs_.end_) {
      if (OB_FAIL(process_sort_batch())) {
      }
    }
  }

  if (OB_SUCC(ret) && !brs_.end_) {
    clear_evaluated_flag();
    const bool fts_prof = ob_fts_sort_prof_on();
    const uint64_t fts_t0 = fts_prof ? ob_fts_sort_tsc() : 0;
    if (OB_FAIL((this->*read_batch_func_)(std::min(max_row_cnt, MY_SPEC.max_batch_size_)))) {
    } else {
      ret_row_count_ += brs_.size_;
      if (fts_prof) {
        ObFtsSortProfile &p = ob_fts_sort_prof();
        p.out_tsc_ += static_cast<int64_t>(ob_fts_sort_tsc() - fts_t0);
        p.out_rows_ += brs_.size_;
      }
      if (brs_.end_) {
        if (ctx_.get_my_session()->get_ddl_info().is_ddl() && ret_row_count_ != sort_row_count_) {
          ret = OB_CHECKSUM_ERROR;
        }
        if (fts_prof) {
          ObFtsSortProfile &p = ob_fts_sort_prof();
          p.op_tsc_ = static_cast<int64_t>(ob_fts_sort_tsc()) - p.op_start_tsc_;
          ob_fts_prof_print_out(p);
        }
      }
    }
  }
  return ret;
}

} // end namespace sql
} // end namespace oceanbase
