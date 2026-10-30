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

#include "sql/engine/sort/ob_fts_aux_sort_impl.h"
#include "data_plane/ob_order_perserving_encoder.h"
#include "lib/utility/ob_sort.h"
#include "lib/time/ob_time_utility.h"
#include "sql/engine/sort/ob_fts_sort_profile.h"

namespace oceanbase
{
using namespace common;
namespace sql
{

namespace
{

// Bounds for the FTS aux radix route. A row count outside [MIN, MAX] or a key
// arena over the cap falls back to the legacy comparator, exactly like the C2
// encoded-key route it replaces.
const int64_t FTS_RADIX_MIN_ROWS = 1024;
const int64_t FTS_RADIX_MAX_ROWS = 4LL << 20;
const int64_t FTS_RADIX_LEAF_ROWS = 24;
const int64_t FTS_RADIX_MAX_COLUMNS = 8;
const int64_t FTS_RADIX_KEY_MEM_CAP = 256LL << 20;
const int64_t FTS_RADIX_KEY_CHUNK_SIZE = 1LL << 20;
const int64_t FTS_RADIX_BUCKET_COUNT = 256;
// Recursion only happens at bytes that split a range, so the stack is bounded by
// this depth; deeper ranges are ordered by the exact comparator instead.
const int64_t FTS_RADIX_MAX_DEPTH = 32;
const int64_t FTS_RADIX_STATUS_CHECK_MASK = 8191;

struct FtsAuxKeyEntry
{
  const char *key_;
  uint32_t len_;
  uint32_t pad_;
  ObChunkDatumStore::StoredRow *row_;
};

struct FtsAuxKeyChunk
{
  FtsAuxKeyChunk *prev_;
  char data_[0];
};

// can_encode_sortkey() omits ObVarcharType although the encoder handles it, and the
// FTS aux sort keys are varchar; the same validated collation set as the C2 route.
inline bool is_fts_aux_key_encodable(const ObObjType type, const ObCollationType cs)
{
  return share::ObOrderPerservingEncoder::can_encode_sortkey(type, cs)
         || (ObVarcharType == type
             && (CS_TYPE_COLLATION_FREE == cs || CS_TYPE_BINARY == cs
                 || CS_TYPE_UTF8MB4_BIN == cs || CS_TYPE_UTF8MB4_GENERAL_CI == cs));
}

// Bytes past the end of a key read as 0, which is what the byte order of the
// padded keys has to agree with; see compare_fts_aux_keys() for the exact order.
inline uint8_t get_fts_aux_key_byte(const FtsAuxKeyEntry &entry, const int64_t depth)
{
  return (depth < static_cast<int64_t>(entry.len_))
      ? static_cast<uint8_t>(entry.key_[depth]) : static_cast<uint8_t>(0);
}

// Exact order of two keys known to share [0, depth): memcmp over the shorter
// remaining length, then the shorter key first. Equal only for equal keys.
inline int compare_fts_aux_keys(const FtsAuxKeyEntry &l, const FtsAuxKeyEntry &r,
                                const int64_t depth)
{
  const int64_t left_len = (l.len_ > depth) ? (l.len_ - depth) : 0;
  const int64_t right_len = (r.len_ > depth) ? (r.len_ - depth) : 0;
  const int64_t cmp_len = left_len < right_len ? left_len : right_len;
  int cmp = (cmp_len > 0) ? MEMCMP(l.key_ + depth, r.key_ + depth, cmp_len) : 0;
  if (0 == cmp && left_len != right_len) {
    cmp = (left_len < right_len) ? -1 : 1;
  }
  return cmp;
}

struct FtsAuxKeyEntryCmp
{
  int *ret_;
  int64_t *cmp_count_;
  ObExecContext *exec_ctx_;
  int64_t depth_;
  bool operator()(const FtsAuxKeyEntry &l, const FtsAuxKeyEntry &r) const
  {
    bool less = false;
    int &ret = *ret_;
    if (OB_UNLIKELY(OB_SUCCESS != ret)) {
      // keep strict weak ordering after an error, as the legacy comparator does
      less = l.row_ < r.row_;
    } else if (OB_UNLIKELY(0 == ((*cmp_count_)++ & FTS_RADIX_STATUS_CHECK_MASK))
               && OB_FAIL(exec_ctx_->check_status())) {
      less = l.row_ < r.row_;
    } else {
      less = compare_fts_aux_keys(l, r, depth_) < 0;
    }
    return less;
  }
};

void fts_aux_insertion_sort(FtsAuxKeyEntry *entries, const int64_t n, const int64_t depth)
{
  for (int64_t i = 1; i < n; ++i) {
    const FtsAuxKeyEntry cur = entries[i];
    int64_t j = i - 1;
    while (j >= 0 && compare_fts_aux_keys(entries[j], cur, depth) > 0) {
      entries[j + 1] = entries[j];
      --j;
    }
    entries[j + 1] = cur;
  }
}

int fts_aux_exact_sort(FtsAuxKeyEntry *entries, const int64_t n, const int64_t depth,
                       ObExecContext *exec_ctx, int64_t &cmp_count)
{
  int ret = OB_SUCCESS;
  int cmp_ret = OB_SUCCESS;
  FtsAuxKeyEntryCmp cmp;
  cmp.ret_ = &cmp_ret;
  cmp.cmp_count_ = &cmp_count;
  cmp.exec_ctx_ = exec_ctx;
  cmp.depth_ = depth;
  lib::ob_sort(entries, entries + n, cmp);
  if (OB_SUCCESS != cmp_ret) {
    ret = cmp_ret;
  }
  return ret;
}

// In-place MSD (american flag) byte radix sort of entries[0, n) by the encoded key,
// starting at byte `depth`. Terminates a range with the exact comparator whenever the
// keys in it are exhausted, when it is small, or when it is too deep, so the result is
// exactly the order compare_fts_aux_keys() defines.
int fts_aux_msd_sort(FtsAuxKeyEntry *entries, const int64_t n, int64_t depth,
                     ObExecContext *exec_ctx, int64_t &cmp_count)
{
  int ret = OB_SUCCESS;
  // starts[b] / starts[b + 1] bound bucket b, flags[b] is bucket b's write cursor
  int32_t starts[FTS_RADIX_BUCKET_COUNT + 1];
  int32_t flags[FTS_RADIX_BUCKET_COUNT];
  FtsAuxKeyEntry *cur = entries;
  int64_t cur_cnt = n;
  while (OB_SUCC(ret) && cur_cnt > FTS_RADIX_LEAF_ROWS && depth < FTS_RADIX_MAX_DEPTH) {
    MEMSET(starts, 0, sizeof(int32_t) * FTS_RADIX_BUCKET_COUNT);
    int64_t max_len = 0;
    for (int64_t i = 0; OB_SUCC(ret) && i < cur_cnt; ++i) {
      const FtsAuxKeyEntry &entry = cur[i];
      if (entry.len_ > max_len) {
        max_len = entry.len_;
      }
      starts[get_fts_aux_key_byte(entry, depth)] += 1;
      if (0 == (++cmp_count & FTS_RADIX_STATUS_CHECK_MASK)
          && OB_FAIL(exec_ctx->check_status())) {
        LOG_WARN("fts aux radix sort interrupted", K(ret));
      }
    }
    if (OB_FAIL(ret)) {
    } else if (max_len <= depth) {
      // Every key in the range ends at or before `depth`, so the bytes left are all
      // padding and only the exact comparator can order them.
      ret = fts_aux_exact_sort(cur, cur_cnt, depth, exec_ctx, cmp_count);
      break;
    } else {
      int64_t bucket_used = 0;
      int32_t run = 0;
      for (int64_t b = 0; b < FTS_RADIX_BUCKET_COUNT; ++b) {
        const int32_t cnt = starts[b];
        starts[b] = run;
        flags[b] = run;
        run += cnt;
        if (cnt > 0) {
          ++bucket_used;
        }
      }
      starts[FTS_RADIX_BUCKET_COUNT] = run;
      if (bucket_used <= 1) {
        // One common byte at `depth`: no movement needed, go one byte deeper.
        ++depth;
      } else {
        // American flag placement. Each swap puts one element at the front of its own
        // bucket; buckets before the current one are already complete, which is what
        // makes it impossible to meet an element with a smaller byte here.
        for (int64_t b = 0; OB_SUCC(ret) && b < FTS_RADIX_BUCKET_COUNT; ++b) {
          int64_t pos = flags[b];
          const int64_t end = starts[b + 1];
          while (pos < end && OB_SUCC(ret)) {
            const int64_t got = get_fts_aux_key_byte(cur[pos], depth);
            if (OB_UNLIKELY(got < b)) {
              ret = OB_ERR_UNEXPECTED;
              LOG_WARN("unexpected fts aux radix bucket", K(ret), K(b), K(got), K(pos));
            } else if (got == b) {
              ++pos;
            } else {
              const FtsAuxKeyEntry tmp = cur[pos];
              cur[pos] = cur[flags[got]];
              cur[flags[got]] = tmp;
              flags[got] += 1;
            }
          }
          flags[b] = pos;
          if (0 == (++cmp_count & FTS_RADIX_STATUS_CHECK_MASK)
              && OB_FAIL(exec_ctx->check_status())) {
            LOG_WARN("fts aux radix sort interrupted", K(ret));
          }
        }
        for (int64_t b = 0; OB_SUCC(ret) && b < FTS_RADIX_BUCKET_COUNT; ++b) {
          const int64_t cnt = starts[b + 1] - starts[b];
          if (cnt > 1 && OB_FAIL(fts_aux_msd_sort(cur + starts[b], cnt, depth + 1,
                                                  exec_ctx, cmp_count))) {
            LOG_WARN("failed to radix sort fts aux key bucket", K(ret), K(b), K(cnt));
          }
        }
        break;
      }
    }
  }
  if (OB_SUCC(ret) && cur_cnt > 1) {
    if (cur_cnt <= FTS_RADIX_LEAF_ROWS) {
      fts_aux_insertion_sort(cur, cur_cnt, depth);
    } else if (depth >= FTS_RADIX_MAX_DEPTH) {
      ret = fts_aux_exact_sort(cur, cur_cnt, depth, exec_ctx, cmp_count);
    }
  }
  return ret;
}

// Linear pass verifying the bucketed result really is compare_fts_aux_keys() order.
// Zero padding equals the memcmp+shorter-first order unless a key is a proper prefix of
// the next one through zero bytes; this catches that without trusting the argument, and
// a failure falls back to the comparator sort instead of writing a wrong row order.
int check_fts_aux_key_order(FtsAuxKeyEntry *entries, const int64_t n,
                            ObExecContext *exec_ctx, int64_t &cmp_count, bool &ordered)
{
  int ret = OB_SUCCESS;
  ordered = true;
  for (int64_t i = 1; OB_SUCC(ret) && ordered && i < n; ++i) {
    if (compare_fts_aux_keys(entries[i - 1], entries[i], 0) > 0) {
      ordered = false;
    } else if (0 == (++cmp_count & FTS_RADIX_STATUS_CHECK_MASK)
               && OB_FAIL(exec_ctx->check_status())) {
      LOG_WARN("fts aux radix sort interrupted", K(ret));
    }
  }
  return ret;
}

// Order-preserving encoding of the sort columns of one stored row, memcmp mode;
// the same conditioning the C2 encoded-key route uses. OB_BUF_NOT_ENOUGH means the
// caller should retry with a larger buffer.
int encode_fts_aux_key(ObChunkDatumStore::StoredRow &sr,
                       const ObIArray<ObSortFieldCollation> &collations,
                       share::ObEncParam *params,
                       const int64_t col_cnt,
                       unsigned char *buf,
                       const int64_t buf_len,
                       int64_t &key_len)
{
  int ret = OB_SUCCESS;
  ObDatum *cells = sr.cells();
  key_len = 0;
  for (int64_t i = 0; OB_SUCC(ret) && i < col_cnt; ++i) {
    int64_t part_len = 0;
    if (OB_UNLIKELY(buf_len - key_len < 1)) {
      ret = OB_BUF_NOT_ENOUGH;
    } else if (OB_FAIL(share::ObSortkeyConditioner::process_key_conditioning(
                   cells[collations.at(i).field_idx_], buf + key_len,
                   buf_len - key_len, part_len, params[i]))) {
    } else if (OB_UNLIKELY(!params[i].is_valid_uni_)) {
      ret = OB_NOT_SUPPORTED;
    } else {
      key_len += part_len;
    }
  }
  return ret;
}

} // namespace

int FtsAuxRadixSortImpl::sort()
{
  int ret = OB_SUCCESS;
  const bool fts_prof = ob_fts_sort_prof_on();
  const int64_t sort_start_time = ObTimeUtility::fast_current_time();
  const uint64_t fts_t0 = fts_prof ? ob_fts_sort_tsc() : 0;
  bool sorted = false;
  bool radix_candidate = false;
  radix_keyed_rows_ = 0;
  radix_keyed_bytes_ = 0;
  radix_fallbacks_ = 0;
  if (!is_inited()) {
    ret = OB_NOT_INIT;
  } else if (!rows_->empty()) {
    radix_candidate = sort_chunks_.is_empty() && !need_imms() && !use_heap_sort_
        && !use_partition_topn_sort_ && 0 == part_cnt_ && INT64_MAX == topn_cnt_
        && !is_fetch_with_ties_ && !local_merge_sort_;
  }
  if (OB_FAIL(ret)) {
  } else if (!radix_candidate) {
    // spilled input, topn, ties, partition sort or already-ordered rows:
    // the untouched legacy route
    if (OB_FAIL(ObSortOpImpl::sort())) {
      if (OB_ITER_END != ret) {
        LOG_WARN("failed to sort by legacy route", K(ret));
      }
    }
  } else {
    iter_.reset();
    if (OB_FAIL(radix_sort_rows(sorted))) {
      LOG_WARN("failed to radix sort fts aux rows", K(ret));
    } else if (!sorted) {
      ++radix_fallbacks_;
      lib::ob_sort(&rows_->at(0), &rows_->at(0) + rows_->count(), CopyableComparer(comp_));
    }
    if (OB_SUCCESS != comp_.ret_) {
      ret = comp_.ret_;
    }
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(iter_.init(&datum_store_))) {
      LOG_WARN("failed to init iterator", K(ret));
    } else {
      row_idx_ = 0;
      next_stored_row_func_ = &FtsAuxRadixSortImpl::array_next_stored_row;
      op_monitor_info_->otherstat_1_id_ = ObSqlMonitorStatIds::SORT_SORTED_ROW_COUNT;
      op_monitor_info_->otherstat_1_value_ += rows_->count();
      op_monitor_info_->otherstat_3_id_ = ObSqlMonitorStatIds::SORT_INMEM_SORT_TIME;
      op_monitor_info_->otherstat_3_value_
          += ObTimeUtility::fast_current_time() - sort_start_time;
    }
    if (fts_prof) {
      ObFtsSortProfile &p = ob_fts_sort_prof();
      p.sort_tsc_ += static_cast<int64_t>(ob_fts_sort_tsc() - fts_t0);
      p.sort_rows_ = rows_->count();
      p.cmp_count_ = comp_.cmp_count_;
      p.compress_type_ = static_cast<int64_t>(compress_type_);
      p.compact_format_ = use_compact_format_ ? 1 : 0;
      p.encode_sortkey_ = enable_encode_sortkey_ ? 1 : 0;
      p.store_rows_ = datum_store_.get_row_cnt();
      p.sort_chunks_ = sort_chunks_.get_size();
      p.fts_key_rows_ = radix_keyed_rows_;
      p.fts_key_bytes_ = radix_keyed_bytes_;
      if (radix_fallbacks_ > 0) {
        ++p.fts_key_fallback_;
      }
      // 6 = fts aux radix, 5 = C2 encoded-key introsort, 2 = legacy comparator
      p.sort_path_ = sorted ? 6 : (radix_fallbacks_ > 0 ? 2 : p.sort_path_);
    }
  }
  return ret;
}

int FtsAuxRadixSortImpl::radix_sort_rows(bool &sorted)
{
  int ret = OB_SUCCESS;
  sorted = false;
  const int64_t row_cnt = rows_->count();
  const int64_t col_cnt = (NULL != sort_collations_) ? sort_collations_->count() : 0;
  bool usable = (row_cnt >= FTS_RADIX_MIN_ROWS && row_cnt <= FTS_RADIX_MAX_ROWS
                 && col_cnt > 0 && col_cnt <= FTS_RADIX_MAX_COLUMNS && NULL != sort_exprs_);
  if (usable) {
    ObIAllocator &alloc = mem_context_->get_malloc_allocator();
    share::ObEncParam params[FTS_RADIX_MAX_COLUMNS];
    FtsAuxKeyEntry *entries = NULL;
    FtsAuxKeyChunk *chunks = NULL;
    char *chunk_cur = NULL;
    int64_t chunk_left = 0;
    unsigned char *big_buf = NULL;
    int64_t big_cap = 0;
    unsigned char stack_buf[512];
    int64_t key_bytes = 0;
    int64_t keyed_rows = 0;
    for (int64_t i = 0; usable && OB_SUCC(ret) && i < col_cnt; ++i) {
      const ObSortFieldCollation &sc = sort_collations_->at(i);
      const ObExpr *expr = NULL;
      if (OB_UNLIKELY(sc.field_idx_ >= static_cast<uint32_t>(sort_exprs_->count()))) {
        usable = false;
      } else if (OB_ISNULL(expr = sort_exprs_->at(sc.field_idx_))) {
        ret = OB_ERR_UNEXPECTED;
      } else if (!is_fts_aux_key_encodable(expr->datum_meta_.type_, sc.cs_type_)) {
        usable = false;
      } else {
        share::ObEncParam &param = params[i];
        param.type_ = expr->datum_meta_.type_;
        param.cs_type_ = sc.cs_type_;
        param.is_var_len_ = false;
        param.is_memcmp_ = true;
        param.is_nullable_ = true;
        param.is_null_first_ = (NULL_FIRST == sc.null_pos_);
        param.is_asc_ = sc.is_ascending_;
        param.is_valid_uni_ = true;
        param.is_simdopt_ = true;
        param.enc_mod_ = share::OB_NONE;
      }
    }
    if (OB_SUCC(ret) && usable) {
      if (OB_ISNULL(entries = static_cast<FtsAuxKeyEntry *>(
                        alloc.alloc(row_cnt * static_cast<int64_t>(sizeof(FtsAuxKeyEntry)))))) {
        usable = false;
      }
    }
    for (int64_t i = 0; usable && OB_SUCC(ret) && i < row_cnt; ++i) {
      ObChunkDatumStore::StoredRow *sr = rows_->at(i);
      const unsigned char *key_ptr = stack_buf;
      int64_t key_len = 0;
      int rc = OB_SUCCESS;
      if (OB_ISNULL(sr)) {
        usable = false;
      } else {
        rc = encode_fts_aux_key(*sr, *sort_collations_, params, col_cnt, stack_buf,
                                static_cast<int64_t>(sizeof(stack_buf)), key_len);
        if (OB_BUF_NOT_ENOUGH == rc) {
          // the key does not fit the inline buffer: encode it once more into a
          // scratch buffer sized from the stored cell lengths
          int64_t need = 64;
          const ObDatum *cells = sr->cells();
          for (int64_t c = 0; c < col_cnt; ++c) {
            need += 7 * cells[sort_collations_->at(c).field_idx_].len_ + 32;
          }
          if (need > big_cap) {
            if (NULL != big_buf) {
              alloc.free(big_buf);
              big_buf = NULL;
              big_cap = 0;
            }
            if (OB_NOT_NULL(big_buf = static_cast<unsigned char *>(alloc.alloc(need)))) {
              big_cap = need;
            }
          }
          if (NULL != big_buf) {
            rc = encode_fts_aux_key(*sr, *sort_collations_, params, col_cnt,
                                    big_buf, big_cap, key_len);
            key_ptr = big_buf;
          }
        }
      }
      if (!usable) {
      } else if (OB_SUCCESS != rc) {
        if (OB_NOT_SUPPORTED != rc && OB_BUF_NOT_ENOUGH != rc) {
          LOG_WARN("failed to encode fts aux sort key, fall back to legacy sort", K(rc), K(i));
        }
        usable = false;
      } else if (OB_UNLIKELY(key_len <= 0 || key_len > static_cast<int64_t>(UINT32_MAX)
                             || key_bytes + key_len > FTS_RADIX_KEY_MEM_CAP)) {
        usable = false;
      } else {
        if (chunk_left < key_len) {
          const int64_t chunk_size = key_len > FTS_RADIX_KEY_CHUNK_SIZE
              ? key_len : FTS_RADIX_KEY_CHUNK_SIZE;
          FtsAuxKeyChunk *chunk = static_cast<FtsAuxKeyChunk *>(
              alloc.alloc(sizeof(FtsAuxKeyChunk) + chunk_size));
          if (OB_ISNULL(chunk)) {
            usable = false;
          } else {
            chunk->prev_ = chunks;
            chunks = chunk;
            chunk_cur = chunk->data_;
            chunk_left = chunk_size;
          }
        }
        if (usable) {
          MEMCPY(chunk_cur, key_ptr, key_len);
          entries[i].key_ = chunk_cur;
          entries[i].len_ = static_cast<uint32_t>(key_len);
          entries[i].pad_ = 0;
          entries[i].row_ = sr;
          chunk_cur += key_len;
          chunk_left -= key_len;
          key_bytes += key_len;
          ++keyed_rows;
        }
      }
    }
    if (OB_SUCC(ret) && usable) {
      int64_t cmp_count = 0;
      bool ordered = false;
      if (OB_FAIL(fts_aux_msd_sort(entries, row_cnt, 0, exec_ctx_, cmp_count))) {
        if (OB_ITER_END != ret) {
          LOG_WARN("failed to radix sort fts aux keys", K(ret), K(row_cnt));
        }
      } else if (OB_FAIL(check_fts_aux_key_order(entries, row_cnt, exec_ctx_,
                                                 cmp_count, ordered))) {
        if (OB_ITER_END != ret) {
          LOG_WARN("failed to check fts aux key order", K(ret), K(row_cnt));
        }
      } else if (!ordered) {
        LOG_WARN("fts aux radix order does not match the key comparator, "
                 "fall back to legacy sort", K(row_cnt));
        usable = false;
      } else {
        for (int64_t i = 0; i < row_cnt; ++i) {
          rows_->at(i) = entries[i].row_;
        }
        radix_keyed_rows_ = keyed_rows;
        radix_keyed_bytes_ = key_bytes;
        sorted = true;
      }
    }
    if (NULL != entries) {
      alloc.free(entries);
    }
    if (NULL != big_buf) {
      alloc.free(big_buf);
    }
    while (NULL != chunks) {
      FtsAuxKeyChunk *prev = chunks->prev_;
      alloc.free(chunks);
      chunks = prev;
    }
  }
  return ret;
}

} // end namespace sql
} // end namespace oceanbase
