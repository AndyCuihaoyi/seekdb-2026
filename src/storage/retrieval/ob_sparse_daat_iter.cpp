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

#define USING_LOG_PREFIX STORAGE

#include "ob_sparse_daat_iter.h"

namespace oceanbase
{
namespace storage
{

ObSRMergeCmp::ObSRMergeCmp()
  : cmp_func_(nullptr),
    id_datums_(nullptr),
    is_binary_id_(false),
    is_inited_(false)
{
}

int ObSRMergeCmp::init(
    sql::ObDatumMeta id_meta,
    const ObFixedArray<const ObDatum *, ObIAllocator> *iter_ids)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(iter_ids)) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    id_datums_ = iter_ids->get_data();
    is_binary_id_ = (id_meta.cs_type_ == common::CS_TYPE_BINARY);
    common::ObDatumBasicFuncs *basic_funcs = ObDatumFuncs::get_basic_func(id_meta.type_, id_meta.cs_type_);
    cmp_func_ = basic_funcs->null_first_cmp_;
    if (OB_ISNULL(cmp_func_)) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      is_inited_ = true;
    }
  }
  return ret;
}

int ObSRMergeCmp::cmp(
    const ObSRMergeItem &l,
    const ObSRMergeItem &r,
    int64_t &cmp_ret)
{
  int ret = OB_SUCCESS;
  if (IS_NOT_INIT) {
    ret = OB_NOT_INIT;
  } else {
    ret = cmp_datum(*id_datums_[l.iter_idx_], *id_datums_[r.iter_idx_], cmp_ret);
  }
  return ret;
}

int ObSRMergeCmp::cmp_datum(const ObDatum &l_id, const ObDatum &r_id, int64_t &cmp_ret)
{
  int ret = OB_SUCCESS;
  if (IS_NOT_INIT) {
    ret = OB_NOT_INIT;
  } else if (OB_LIKELY(is_binary_id_) && OB_LIKELY(!l_id.is_null()) && OB_LIKELY(!r_id.is_null())) {
    // bit-identical to strcmpsp(CS_TYPE_BINARY, ..., cmp_endspace), which for binary
    // collation is memcmp over the common prefix with length-difference tiebreak
    const int64_t min_len = MIN(l_id.len_, r_id.len_);
    const int memcmp_ret = min_len > 0 ? memcmp(l_id.ptr_, r_id.ptr_, min_len) : 0;
    cmp_ret = memcmp_ret < 0 ? -1
            : (memcmp_ret > 0 ? 1
            : (l_id.len_ < r_id.len_ ? -1 : (l_id.len_ > r_id.len_ ? 1 : 0)));
  } else {
    int tmp_ret = 0;
    if (OB_FAIL(cmp_func_(l_id, r_id, tmp_ret, nullptr))) {
    } else {
      cmp_ret = tmp_ret;
    }
  }
  return ret;
}

ObSRDaaTIterImpl::ObSRDaaTIterImpl()
  : ObISparseRetrievalMergeIter(),
    iter_allocator_(nullptr),
    iter_param_(nullptr),
    dim_iters_(nullptr),
    merge_cmp_(),
    merge_heap_(nullptr),
    relevance_collector_(nullptr),
    iter_domain_ids_(),
    domain_id_ptrs_(nullptr),
    dim_iter_ptrs_(nullptr),
    dim_cnt_(0),
    relevance_bool_filter_(false),
    buffered_domain_ids_(),
    buffered_relevances_(),
    next_round_iter_idxes_(),
    next_round_cnt_(0),
    filter_skip_(nullptr),
    set_datum_func_(nullptr),
    dim_states_(nullptr),
    use_fast_merge_(false),
    relevance_keep_all_(false)
{
}

int ObSRDaaTIterImpl::init(
    ObSparseRetrievalMergeParam &iter_param,
    ObIArray<ObISRDaaTDimIter *> &dim_iters,
    ObIAllocator &iter_allocator,
    ObSRDaaTRelevanceCollector &relevance_collector)
{
  int ret = OB_SUCCESS;
  if (IS_INIT) {
    ret = OB_INIT_TWICE;
  } else if (OB_UNLIKELY(iter_param.max_batch_size_ <= 0)) {
      ret = OB_ERR_UNEXPECTED;
  } else {
    iter_allocator_ = &iter_allocator;
    iter_param_ = &iter_param;
    dim_iters_ = &dim_iters;
    relevance_collector_ = &relevance_collector;
    const int64_t max_batch_size = OB_MAX(iter_param_->eval_ctx_->max_batch_size_, iter_param_->max_batch_size_);
    if (iter_param_->id_proj_expr_->datum_meta_.type_ == common::ObUInt64Type) {
      set_datum_func_ = ObISparseRetrievalMergeIter::set_datum_int;
    } else {
      set_datum_func_ = ObISparseRetrievalMergeIter::set_datum_shallow;
    }
    if (OB_UNLIKELY(dim_iters.count() == 0)) {
    } else if (OB_NOT_NULL(iter_param_->dim_weights_) && dim_iters.count() != iter_param_->dim_weights_->count()) {
      ret = OB_ERR_UNEXPECTED;
    } else if (FALSE_IT(iter_domain_ids_.set_allocator(iter_allocator_))) {
    } else if (OB_FAIL(iter_domain_ids_.init(dim_iters.count()))) {
    } else if (OB_FAIL(iter_domain_ids_.prepare_allocate(dim_iters.count()))) {
    } else if (FALSE_IT(next_round_iter_idxes_.set_allocator(iter_allocator_))) {
    } else if (OB_FAIL(next_round_iter_idxes_.init(dim_iters.count()))) {
    } else if (OB_FAIL(next_round_iter_idxes_.prepare_allocate(dim_iters.count()))) {
    } else if (FALSE_IT(buffered_domain_ids_.set_allocator(iter_allocator_))) {
    } else if (OB_FAIL(buffered_domain_ids_.init(max_batch_size))) {
    } else if (OB_FAIL(buffered_domain_ids_.prepare_allocate(max_batch_size))) {
    } else if (FALSE_IT(buffered_relevances_.set_allocator(iter_allocator_))) {
    } else if (OB_FAIL(buffered_relevances_.init(max_batch_size))) {
    } else if (OB_FAIL(buffered_relevances_.prepare_allocate(max_batch_size))) {
    } else if (OB_ISNULL(filter_skip_ = sql::to_bit_vector(
                            iter_allocator_->alloc(sql::ObBitVector::memory_size(max_batch_size))))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else if (OB_FAIL(merge_cmp_.init(iter_param_->id_proj_expr_->datum_meta_, &iter_domain_ids_))) {
    } else if (OB_FAIL(init_merge_heap(dim_iters_->count()))) {
    } else {
      for (int64_t i = 0; i < dim_iters.count(); ++i) {
        next_round_iter_idxes_[i] = i;
      }
    }

    if (OB_SUCC(ret)) {
      dim_cnt_ = dim_iters.count();
      domain_id_ptrs_ = iter_domain_ids_.get_data();
      if (dim_cnt_ > 0
          && OB_ISNULL(dim_iter_ptrs_ = static_cast<ObISRDaaTDimIter **>(
              iter_allocator_->alloc(dim_cnt_ * sizeof(ObISRDaaTDimIter *))))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else if (dim_cnt_ > 0
          && OB_ISNULL(dim_states_ = static_cast<ObSRDimState *>(
              iter_allocator_->alloc(dim_cnt_ * sizeof(ObSRDimState))))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else {
        for (int64_t i = 0; i < dim_cnt_; ++i) {
          dim_iter_ptrs_[i] = dim_iters.at(i);
          dim_states_[i] = ObSRDimState();
        }
        // fast-path filter: BOOL(relevance) is equivalent to keeping rows with relevance != 0
        ObExpr *filter_expr = iter_param_->filter_expr_;
        ObExpr *relevance_proj_expr = iter_param_->relevance_proj_expr_;
        relevance_bool_filter_ = OB_NOT_NULL(filter_expr) && OB_NOT_NULL(relevance_proj_expr)
                                 && T_OP_BOOL == filter_expr->type_
                                 && 1 == filter_expr->arg_cnt_
                                 && filter_expr->args_[0] == relevance_proj_expr;
        // relevance skipped by optimizer (score provably unobserved): BM25 score is
        // strictly positive with positive field boost / dim weights (validated by the
        // caller), so the BOOL(relevance) filter is always true. For a bare
        // WHERE MATCH(...) predicate the filter shares the same rt expr as the
        // projection column (cg caches rt exprs by raw expr), so it is also
        // always-true and evaluated as keep-all.
        relevance_keep_all_ = !iter_param_->need_calc_relevance()
                              && OB_NOT_NULL(filter_expr) && OB_NOT_NULL(relevance_proj_expr)
                              && (filter_expr == relevance_proj_expr
                                  || (T_OP_BOOL == filter_expr->type_
                                      && 1 == filter_expr->arg_cnt_
                                      && filter_expr->args_[0] == relevance_proj_expr));
      }
    }

    if (OB_SUCC(ret)) {
      use_fast_merge_ = dim_cnt_ > 0;
      next_round_cnt_ = dim_iters.count();
      input_row_cnt_ = 0;
      output_row_cnt_ = 0;
      is_inited_ = true;
    }
  }
  return ret;
}

void ObSRDaaTIterImpl::reset()
{
  if (OB_NOT_NULL(merge_heap_)) {
    merge_heap_->~ObSRMergeHeap();
    merge_heap_ = nullptr;
  }
  if (OB_NOT_NULL(relevance_collector_)) {
    relevance_collector_->reset();
    relevance_collector_ = nullptr;
  }
  if (OB_NOT_NULL(iter_allocator_) && OB_NOT_NULL(dim_iter_ptrs_)) {
    iter_allocator_->free(dim_iter_ptrs_);
  }
  dim_iter_ptrs_ = nullptr;
  if (OB_NOT_NULL(iter_allocator_) && OB_NOT_NULL(dim_states_)) {
    iter_allocator_->free(dim_states_);
  }
  dim_states_ = nullptr;
  use_fast_merge_ = false;
  relevance_keep_all_ = false;
  domain_id_ptrs_ = nullptr;
  dim_cnt_ = 0;
  relevance_bool_filter_ = false;
  iter_domain_ids_.reset();
  buffered_domain_ids_.reset();
  buffered_relevances_.reset();
  next_round_iter_idxes_.reset();
  filter_skip_ = nullptr;
  next_round_cnt_ = 0;
  input_row_cnt_ = 0;
  output_row_cnt_ = 0;
  is_inited_ = false;
}

void ObSRDaaTIterImpl::reuse(const bool switch_tablet)
{
  if (OB_NOT_NULL(dim_iters_)) {
    if (OB_NOT_NULL(merge_heap_)) {
      merge_heap_->reuse();
      merge_heap_->open(dim_iters_->count());
    }
    next_round_cnt_ = dim_iters_->count();
    for (int64_t i = 0; i < next_round_cnt_; ++i) {
      next_round_iter_idxes_[i] = i;
    }
    if (OB_NOT_NULL(dim_states_)) {
      for (int64_t i = 0; i < dim_cnt_; ++i) {
        dim_states_[i] = ObSRDimState();
      }
    }
  }
  if (OB_NOT_NULL(relevance_collector_)) {
    relevance_collector_->reuse();
  }
  input_row_cnt_ = 0;
  output_row_cnt_ = 0;
}

  int ObSRDaaTIterImpl::get_query_max_score(double &score)
  {
    int ret = OB_SUCCESS;
    score = 0.0;
    for (int64_t i = 0; OB_SUCC(ret) && i < dim_iters_->count(); ++i) {
      ObISRDaaTDimIter *dim_iter = dim_iters_->at(i);
      double dim_score = 0.0;
      if (OB_ISNULL(dim_iter) || OB_ISNULL(iter_param_->dim_weights_)) {
        ret = OB_ERR_UNEXPECTED;
      } else if (OB_FAIL(dim_iter->get_dim_max_score(dim_score))) {
      } else {
        score += dim_score * iter_param_->field_boost_ * iter_param_->dim_weights_->at(i);
      }
    }

    return ret;
  }

int ObSRDaaTIterImpl::get_next_row()
{
  int ret = OB_SUCCESS;
  int64_t count = 0;
  if (OB_FAIL(get_next_rows(1, count))) {
    if (OB_UNLIKELY(OB_ITER_END != ret)) {
    } else if (OB_UNLIKELY(count != 0)) {
      ret = OB_ERR_UNEXPECTED;
    }
  } else if (OB_UNLIKELY(count != 1)) {
    ret = OB_ERR_UNEXPECTED;
  }
  return ret;
}

int ObSRDaaTIterImpl::get_next_rows(const int64_t capacity, int64_t &count)
{
  int ret = OB_SUCCESS;
  if (IS_NOT_INIT) {
    ret = OB_NOT_INIT;
  } else if (OB_UNLIKELY(0 == capacity)) {
    count = 0;
  } else if (0 == dim_iters_->count()) {
    ret = OB_ITER_END;
  } else if (iter_param_->limit_param_->is_valid() && output_row_cnt_ >= iter_param_->limit_param_->limit_) {
    ret = OB_ITER_END;
  } else if (OB_FAIL(pre_process())) {
    if (OB_UNLIKELY(OB_ITER_END != ret)) {
    } else {
      count = 0;
    }
  } else {
    count = 0;
    int64_t real_capacity = MIN(capacity, iter_param_->max_batch_size_);
    // A filter can only be evaluated in batch under vectorized execution, and only when
    // the filter itself produces per-row results; otherwise keep the original row-wise
    // behavior.
    const bool use_batch_filter = iter_param_->need_filter()
                                  && iter_param_->eval_ctx_->is_vectorized()
                                  && OB_NOT_NULL(iter_param_->filter_expr_)
                                  && iter_param_->filter_expr_->is_batch_result();
    if (use_batch_filter) {
      // batch evaluation indexes the eval ctx datum vectors, which are sized by the ctx
      real_capacity = MIN(real_capacity, iter_param_->eval_ctx_->max_batch_size_);
    }
    if (!use_batch_filter) {
      while (OB_SUCC(ret) && count < real_capacity) {
        if (OB_FAIL(do_one_merge_round(count))) {
          if (OB_UNLIKELY(OB_ITER_END != ret)) {
          }
        }
      }

      if (OB_SUCC(ret) || OB_ITER_END == ret) {
        if (count > 0 && OB_FAIL(project_results(count))) {
        }
      }
    } else {
      bool iter_end = false;
      // A raw batch can be fully filtered out, in which case no row is produced and the
      // next raw batch must be merged before returning.
      while (OB_SUCC(ret) && 0 == count && !iter_end) {
        int64_t raw_count = 0;
        while (OB_SUCC(ret) && !iter_end && raw_count < real_capacity) {
          if (OB_FAIL(do_one_merge_round_raw(raw_count))) {
            if (OB_UNLIKELY(OB_ITER_END != ret)) {
            } else {
              iter_end = true;
              ret = OB_SUCCESS;
            }
          }
        }

        if (OB_SUCC(ret) && raw_count > 0) {
          if (OB_FAIL(apply_batch_filter(raw_count, count))) {
            if (OB_UNLIKELY(OB_ITER_END != ret)) {
            } else {
              iter_end = true;
              ret = OB_SUCCESS;
            }
          }
        } else if (OB_SUCC(ret)) {
          iter_end = true;
        }
      }

      if (OB_SUCC(ret) && count > 0 && OB_FAIL(project_results(count))) {
      }
      if (OB_SUCC(ret) && iter_end) {
        ret = OB_ITER_END;
      }
    }
  }
  return ret;
}

int ObSRDaaTIterImpl::pre_process()
{
  return OB_NOT_IMPLEMENT;
}

int ObSRDaaTIterImpl::do_one_merge_round(int64_t &count)
{
  int ret = OB_SUCCESS;
  bool need_project = true;
  double relevance = 0.0;
  const ObDatum *id_datum = nullptr;
  if (use_fast_merge_) {
    if (OB_FAIL(fast_fill_dims())) {
      if (OB_UNLIKELY(OB_ITER_END != ret)) {
      }
    } else if (OB_FAIL(fast_collect_dims(id_datum, relevance, need_project))) {
    } else if (need_project && OB_FAIL(filter_on_demand(count, relevance, need_project))) {
    } else if (need_project && OB_FAIL(cache_result(count, *id_datum, relevance))) {
      if (OB_ITER_END != ret) {
      }
    }
    return ret;
  }
  if (OB_FAIL(fill_merge_heap())) {
    if (OB_UNLIKELY(OB_ITER_END != ret)) {
    }
  } else if (OB_FAIL(collect_dims_by_id(id_datum, relevance, need_project))) {
  } else if (need_project && OB_FAIL(filter_on_demand(count, relevance, need_project))) {
  } else if (need_project && OB_FAIL(cache_result(count, *id_datum, relevance))) {
    if (OB_ITER_END != ret) {
    }
  }
  return ret;
}

int ObSRDaaTIterImpl::do_one_merge_round_raw(int64_t &count)
{
  int ret = OB_SUCCESS;
  bool got_valid_id = false;
  double relevance = 0.0;
  const ObDatum *id_datum = nullptr;
  if (use_fast_merge_) {
    if (OB_FAIL(fast_fill_dims())) {
      if (OB_UNLIKELY(OB_ITER_END != ret)) {
      }
    } else if (OB_FAIL(fast_collect_dims(id_datum, relevance, got_valid_id))) {
    } else if (got_valid_id && OB_FAIL(buffer_raw_result(count, *id_datum, relevance))) {
    }
    return ret;
  }
  if (OB_FAIL(fill_merge_heap())) {
    if (OB_UNLIKELY(OB_ITER_END != ret)) {
    }
  } else if (OB_FAIL(collect_dims_by_id(id_datum, relevance, got_valid_id))) {
  } else if (got_valid_id && OB_FAIL(buffer_raw_result(count, *id_datum, relevance))) {
  }
  return ret;
}

int ObSRDaaTIterImpl::fast_fill_dims()
{
  int ret = OB_SUCCESS;
  for (int64_t i = 0; OB_SUCC(ret) && i < next_round_cnt_; ++i) {
    const int64_t d = next_round_iter_idxes_[i];
    ObSRDimState &st = dim_states_[d];
    ObISRDaaTDimIter *dim_iter = dim_iter_ptrs_[d];
    if (OB_ISNULL(dim_iter)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(dim_iter->get_next_row())) {
      if (OB_UNLIKELY(OB_ITER_END != ret)) {
        LOG_WARN("fail to try load next batch dimension data", K(ret));
      } else {
        st.active_ = false;
        ret = OB_SUCCESS;
      }
    } else if (OB_FAIL(dim_iter->get_curr_score(st.rel_))) {
    } else if (OB_NOT_NULL(iter_param_->dim_weights_) && FALSE_IT(st.rel_ = st.rel_ * iter_param_->field_boost_ * iter_param_->dim_weights_->at(d))) {
    } else if (OB_FAIL(dim_iter->get_curr_id(st.id_))) {
    } else {
      st.active_ = true;
    }
  }
  return ret;
}

int ObSRDaaTIterImpl::fast_collect_dims(const ObDatum *&id_datum, double &relevance, bool &got_valid_id)
{
  int ret = OB_SUCCESS;
  relevance = 0.0;
  got_valid_id = false;
  int64_t min_d = -1;
  for (int64_t d = 0; OB_SUCC(ret) && d < dim_cnt_; ++d) {
    if (!dim_states_[d].active_) {
      continue;
    } else if (min_d < 0) {
      min_d = d;
    } else {
      int64_t cmp_ret = 0;
      if (OB_FAIL(merge_cmp_.cmp_datum(*dim_states_[d].id_, *dim_states_[min_d].id_, cmp_ret))) {
      } else if (cmp_ret < 0) {
        min_d = d;
      }
    }
  }
  if (OB_FAIL(ret)) {
  } else if (min_d < 0) {
    ret = OB_ITER_END;
  } else {
    const ObDatum *min_id = dim_states_[min_d].id_;
    next_round_cnt_ = 0;
    for (int64_t d = 0; OB_SUCC(ret) && d < dim_cnt_; ++d) {
      if (!dim_states_[d].active_) {
        continue;
      }
      int64_t cmp_ret = 0;
      if (OB_FAIL(merge_cmp_.cmp_datum(*dim_states_[d].id_, *min_id, cmp_ret))) {
      } else if (0 != cmp_ret) {
        // stays in the merge with its current row, not re-fetched next round
      } else if (OB_FAIL(relevance_collector_->collect_one_dim(d, dim_states_[d].rel_))) {
      } else {
        next_round_iter_idxes_[next_round_cnt_++] = d;
      }
    }
    if (OB_SUCC(ret)) {
      id_datum = min_id;
      if (OB_ISNULL(id_datum)) {
        ret = OB_ERR_UNEXPECTED;
      } else if (OB_FAIL(relevance_collector_->get_result(relevance, got_valid_id))) {
      } else if (got_valid_id && OB_FAIL(process_collected_row(*id_datum, relevance))) {
      }
    }
  }
  return ret;
}

int ObSRDaaTIterImpl::buffer_raw_result(int64_t &count,
                                        const ObDatum &id_datum,
                                        const double relevance)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(count < 0 || count >= buffered_domain_ids_.count()
                  || count >= buffered_relevances_.count())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(buffered_domain_ids_[count].from_datum(id_datum))) {
  } else {
    buffered_relevances_[count] = relevance;
    ++count;
  }
  return ret;
}

int ObSRDaaTIterImpl::apply_batch_filter(const int64_t raw_count, int64_t &count)
{
  int ret = OB_SUCCESS;
  ObExpr *filter_expr = iter_param_->filter_expr_;
  ObExpr *relevance_proj_expr = iter_param_->relevance_proj_expr_;
  ObEvalCtx *eval_ctx = iter_param_->eval_ctx_;
  const common::ObLimitParam *limit_param = iter_param_->limit_param_;
  const int64_t limit = limit_param->limit_;
  const int64_t offset = limit_param->offset_;
  count = 0;
  if (relevance_keep_all_) {
    // BOOL(relevance) filter is provably always-true (relevance skipped by optimizer);
    // rows are kept without evaluating the filter, zero scores are written to keep
    // the projection machinery consistent
    for (int64_t i = 0; OB_SUCC(ret) && i < raw_count; ++i) {
      ++input_row_cnt_;
      if (limit_param->is_valid() && input_row_cnt_ <= offset) {
        continue;
      }
      if (count != i) {
        buffered_domain_ids_[count] = buffered_domain_ids_[i];
        buffered_relevances_[count] = buffered_relevances_[i];
      }
      ++count;
      ++output_row_cnt_;
      if (limit_param->is_valid() && output_row_cnt_ >= limit) {
        ret = OB_ITER_END;
      }
    }
    ObEvalCtx::BatchInfoScopeGuard guard(*eval_ctx);
    relevance_proj_expr->set_evaluated_projected(*eval_ctx);
    for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
      guard.set_batch_idx(i);
      ObDatum &relevance_proj_datum = relevance_proj_expr->locate_datum_for_write(*eval_ctx);
      relevance_proj_datum.set_double(buffered_relevances_[i]);
      relevance_proj_expr->get_evaluated_flags(*eval_ctx).set(i);
    }
    return ret;
  }
  if (relevance_bool_filter_) {
    // filter is BOOL(relevance): row survives iff relevance != 0.0, the expression
    // machinery (per-row datum writes, flag bookkeeping, vectorized BOOL eval) is skipped
    for (int64_t i = 0; OB_SUCC(ret) && i < raw_count; ++i) {
      if (0.0 == buffered_relevances_[i]) {
        continue;
      }
      ++input_row_cnt_;
      if (limit_param->is_valid() && input_row_cnt_ <= offset) {
        continue;
      }
      if (count != i) {
        buffered_domain_ids_[count] = buffered_domain_ids_[i];
        buffered_relevances_[count] = buffered_relevances_[i];
      }
      ++count;
      ++output_row_cnt_;
      if (limit_param->is_valid() && output_row_cnt_ >= limit) {
        ret = OB_ITER_END;
      }
    }
    return ret;
  }
  if (OB_NOT_NULL(filter_skip_)) {
    filter_skip_->init(raw_count);
  }
  {
    ObEvalCtx::BatchInfoScopeGuard guard(*eval_ctx);
    relevance_proj_expr->set_evaluated_projected(*eval_ctx);
    for (int64_t i = 0; i < raw_count; ++i) {
      guard.set_batch_idx(i);
      ObDatum &relevance_proj_datum = relevance_proj_expr->locate_datum_for_write(*eval_ctx);
      relevance_proj_datum.set_double(buffered_relevances_[i]);
      relevance_proj_expr->get_evaluated_flags(*eval_ctx).set(i);
    }
    guard.set_batch_idx(0);
    filter_expr->get_evaluated_flags(*eval_ctx).reset(raw_count);
    if (OB_FAIL(filter_expr->eval_batch(*eval_ctx, *filter_skip_, raw_count))) {
    }
  }

  if (OB_SUCC(ret)) {
    for (int64_t i = 0; OB_SUCC(ret) && i < raw_count; ++i) {
      const ObDatum &filter_datum = filter_expr->locate_expr_datum(*eval_ctx, i);
      if (filter_datum.is_null() || 0 == filter_datum.get_int()) {
        continue;
      }
      ++input_row_cnt_;
      if (limit_param->is_valid() && input_row_cnt_ <= offset) {
        continue;
      }
      if (count != i) {
        buffered_domain_ids_[count] = buffered_domain_ids_[i];
        buffered_relevances_[count] = buffered_relevances_[i];
      }
      ++count;
      ++output_row_cnt_;
      if (limit_param->is_valid() && output_row_cnt_ >= limit) {
        ret = OB_ITER_END;
      }
    }
  }
  return ret;
}

int ObSRDaaTIterImpl::fill_merge_heap()
{
  int ret = OB_SUCCESS;
  ObSRMergeItem item;
  for (int64_t i = 0; OB_SUCC(ret) && i < next_round_cnt_; ++i) {
    const int64_t iter_idx = next_round_iter_idxes_[i];
    ObISRDaaTDimIter *dim_iter = dim_iter_ptrs_[iter_idx];
    if (OB_ISNULL(dim_iter)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(dim_iter->get_next_row())) {
      if (OB_UNLIKELY(OB_ITER_END != ret)) {
        LOG_WARN("fail to try load next batch dimension data", K(ret));\
      } else {
        ret = OB_SUCCESS;
      }
    } else if (OB_FAIL(dim_iter->get_curr_score(item.relevance_))) {
    } else if (OB_NOT_NULL(iter_param_->dim_weights_) && FALSE_IT(item.relevance_ = item.relevance_ * iter_param_->field_boost_ * iter_param_->dim_weights_->at(iter_idx))) {
    } else if (OB_FAIL(dim_iter->get_curr_id(domain_id_ptrs_[iter_idx]))) {
    } else if (FALSE_IT(item.iter_idx_ = iter_idx)) {
    } else if (OB_FAIL(merge_heap_->push(item))) {
    } else {
      LOG_DEBUG("push item to merge heap", K(ret), K(i), K(iter_idx), K(next_round_cnt_), K(item), K(domain_id_ptrs_[iter_idx]));
    }
  }

  if (OB_FAIL(ret)) {
  } else if (merge_heap_->empty()) {
    ret = OB_ITER_END;
  } else if (0 != next_round_cnt_ && OB_FAIL(merge_heap_->rebuild())) {
  } else {
    next_round_cnt_ = 0;
  }
  return ret;
}

int ObSRDaaTIterImpl::collect_dims_by_id(const ObDatum *&id_datum, double &relevance, bool &got_valid_id)
{
  int ret = OB_SUCCESS;

  const ObSRMergeItem *top_item = nullptr;
  bool curr_doc_end = false;
  int64_t iter_idx = 0;
  relevance = 0.0;
  got_valid_id = false;

  while (OB_SUCC(ret) && !merge_heap_->empty() && !curr_doc_end) {
    if (merge_heap_->is_unique_champion()) {
      curr_doc_end = true;
    }
    if (OB_FAIL(merge_heap_->top(top_item))) {
    } else if (OB_FAIL(relevance_collector_->collect_one_dim(top_item->iter_idx_, top_item->relevance_))) {
    } else if (FALSE_IT(iter_idx = top_item->iter_idx_)) {
    } else if (OB_FAIL(merge_heap_->pop())) {
    } else {
      next_round_iter_idxes_[next_round_cnt_++] = iter_idx;
    }
  }

  if (OB_SUCC(ret)) {
    id_datum = domain_id_ptrs_[iter_idx];
    if (OB_ISNULL(id_datum)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(relevance_collector_->get_result(relevance, got_valid_id))) {
    } else if (got_valid_id && OB_FAIL(process_collected_row(*id_datum, relevance))) {
    }
  }

  return ret;
}

int ObSRDaaTIterImpl::process_collected_row(const ObDatum &id_datum, const double relevance)
{
  return OB_SUCCESS;
}

int ObSRDaaTIterImpl::filter_on_demand(const int64_t count, const double relevance, bool &need_project)
{
  int ret = OB_SUCCESS;
  ObExpr *filter_expr = iter_param_->filter_expr_;
  ObExpr *relevance_proj_expr = iter_param_->relevance_proj_expr_;
  ObEvalCtx *eval_ctx = iter_param_->eval_ctx_;
  ObEvalCtx::BatchInfoScopeGuard guard(*eval_ctx);

  if (!iter_param_->need_filter()) {
    need_project = true;
  } else if (eval_ctx->is_vectorized()) {
    if (OB_UNLIKELY(count >= eval_ctx->max_batch_size_)) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      guard.set_batch_idx(count);
      ObDatum &relevance_proj_datum = relevance_proj_expr->locate_datum_for_write(*eval_ctx);
      relevance_proj_datum.set_double(relevance);
      relevance_proj_expr->get_evaluated_flags(*eval_ctx).set(count);
      relevance_proj_expr->set_evaluated_projected(*eval_ctx);
      if (OB_UNLIKELY(!iter_param_->need_calc_relevance())) {
        // relevance skipped: the BOOL(score) filter is provably always-true
        need_project = true;
      } else {
        ObDatum *filter_res = nullptr;
        filter_expr->clear_evaluated_flag(*eval_ctx);
        if (OB_FAIL(filter_expr->eval(*eval_ctx, filter_res))) {
        } else {
          need_project = !(filter_res->is_null() || 0 == filter_res->get_int());
        }
      }
    }
  } else if (OB_UNLIKELY(0 != count)) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    guard.set_batch_idx(0);
    ObDatum &relevance_proj_datum = relevance_proj_expr->locate_datum_for_write(*eval_ctx);
    relevance_proj_datum.set_double(relevance);
    relevance_proj_expr->set_evaluated_projected(*eval_ctx);
  }
  return ret;
}

int ObSRDaaTIterImpl::cache_result(int64_t &count, const ObDatum &id_datum, const double relevance)
{
  int ret = OB_SUCCESS;
  const common::ObLimitParam *limit_param= iter_param_->limit_param_;
  const int64_t limit = limit_param->limit_;
  const int64_t offset = limit_param->offset_;
  ++input_row_cnt_;
  if (limit_param->is_valid() && input_row_cnt_ <= offset) {
    // TODO: Maybe we should not process offset logic here
    // don't need to project
  } else if (OB_UNLIKELY(count >= buffered_domain_ids_.count() || count >= buffered_relevances_.count())) {
    ret = OB_INVALID_ARGUMENT;
  } else {
    buffered_domain_ids_[count].from_datum(id_datum);
    buffered_relevances_[count] = relevance;
    ++count;
    ++output_row_cnt_;
    if (limit_param->is_valid() && output_row_cnt_ >= limit) {
      ret = OB_ITER_END;
    }
  }
  return ret;
}

int ObSRDaaTIterImpl::project_results(const int64_t count)
{
  int ret = OB_SUCCESS;
  ObExpr *relevance_proj_expr = iter_param_->relevance_proj_expr_;
  ObExpr *id_proj_expr = iter_param_->id_proj_expr_;
  ObEvalCtx *eval_ctx = iter_param_->eval_ctx_;
  ObEvalCtx::BatchInfoScopeGuard guard(*eval_ctx);

  if (eval_ctx->is_vectorized()) {
    sql::ObBitVector &id_evaluated_flags = id_proj_expr->get_evaluated_flags(*eval_ctx);
    id_proj_expr->set_evaluated_projected(*eval_ctx);
    for (int64_t i = 0; i < count; ++i) {
      guard.set_batch_idx(i);
      ObDatum &id_proj_datum = id_proj_expr->locate_datum_for_write(*eval_ctx);
      set_datum_func_(id_proj_datum, buffered_domain_ids_[i]);
      id_evaluated_flags.set(i);
    }
    if (iter_param_->need_project_relevance()) {
      sql::ObBitVector &relevance_evaluated_flags = relevance_proj_expr->get_evaluated_flags(*eval_ctx);
      relevance_proj_expr->set_evaluated_projected(*eval_ctx);
      for (int64_t i = 0; i < count; ++i) {
        guard.set_batch_idx(i);
        ObDatum &relevance_proj_datum = relevance_proj_expr->locate_datum_for_write(*eval_ctx);
        relevance_proj_datum.set_double(buffered_relevances_[i]);
        relevance_evaluated_flags.set(i);
      }
    }
  } else if (OB_UNLIKELY(1 != count)) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    guard.set_batch_idx(0);
    ObDatum &id_proj_datum = id_proj_expr->locate_datum_for_write(*eval_ctx);
    set_datum_func_(id_proj_datum, buffered_domain_ids_[0]);
    id_proj_expr->set_evaluated_projected(*eval_ctx);
    ObDatum &relevance_proj_datum = relevance_proj_expr->locate_datum_for_write(*eval_ctx);
    relevance_proj_datum.set_double(buffered_relevances_[0]);
    relevance_proj_expr->set_evaluated_projected(*eval_ctx);
  }
  return ret;
}

int ObSRDaaTIterImpl::init_merge_heap(const int64_t count)
{
  int ret = OB_SUCCESS;
  ObSRSimpleMerger *simple_merge = nullptr;
  ObSRMergeHeap *loser_tree = nullptr;
  if (count <= ObSRSimpleMerger::USE_SIMPLE_MERGER_MAX_TABLE_CNT) {
    if (OB_ISNULL(simple_merge = OB_NEWx(ObSRSimpleMerger, iter_allocator_, merge_cmp_))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else {
      merge_heap_ = simple_merge;
    }
  } else if (OB_ISNULL(loser_tree = OB_NEWx(ObSRLoserTree, iter_allocator_, merge_cmp_))) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else {
    merge_heap_ = loser_tree;
  }
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(merge_heap_->init(count, count, *iter_allocator_))) {
  } else if (OB_FAIL(merge_heap_->open(count))) {
  }
  return ret;
}

} // namespace storage
} // namespace oceanbase
