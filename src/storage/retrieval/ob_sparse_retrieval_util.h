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

#ifndef OB_SPARSE_RETRIEVAL_UTIL_H_
#define OB_SPARSE_RETRIEVAL_UTIL_H_

#include "common/datum/ob_datum.h"
#include "share/datum/ob_datum_funcs.h"
#include <cstring>

namespace oceanbase
{
namespace storage
{

// Same ordering as memcmp over the common prefix with a length-difference tiebreak, but
// word-wise inline: domain ids are short fixed-width keys compared several times per
// emitted row, so the libc call overhead costs more than the byte work itself.
// Big-endian-normalized words keep u64 ordering identical to byte ordering.
inline int ob_binary_key_cmp(const char *lhs_ptr, const int64_t lhs_len,
                             const char *rhs_ptr, const int64_t rhs_len)
{
  const int64_t min_len = lhs_len < rhs_len ? lhs_len : rhs_len;
  int64_t off = 0;
  for (; off + 8 <= min_len; off += 8) {
    uint64_t l = 0;
    uint64_t r = 0;
    __builtin_memcpy(&l, lhs_ptr + off, sizeof(l));
    __builtin_memcpy(&r, rhs_ptr + off, sizeof(r));
    l = __builtin_bswap64(l);
    r = __builtin_bswap64(r);
    if (l != r) {
      return l < r ? -1 : 1;
    }
  }
  for (; off < min_len; ++off) {
    const unsigned char l = static_cast<const unsigned char>(lhs_ptr[off]);
    const unsigned char r = static_cast<const unsigned char>(rhs_ptr[off]);
    if (l != r) {
      return l < r ? -1 : 1;
    }
  }
  return lhs_len < rhs_len ? -1 : (lhs_len > rhs_len ? 1 : 0);
}

class ObDomainIdCmp
{
public:
  ObDomainIdCmp() : cmp_func_(nullptr), is_binary_id_(false) {}
  ~ObDomainIdCmp() {}
  inline int init(const ObObjMeta &obj_meta);
  inline int compare(const ObDatum &lhs, const ObDatum &rhs, int &cmp_ret) const;
  inline void reset() { cmp_func_ = nullptr; is_binary_id_ = false; }
private:
  common::ObDatumCmpFuncType cmp_func_;
  // binary collation domain ids can be compared with memcmp, same result as the
  // charset-aware compare but without the charset lookup and argument validation
  bool is_binary_id_;
  DISALLOW_COPY_AND_ASSIGN(ObDomainIdCmp);
};

int ObDomainIdCmp::init(const ObObjMeta &obj_meta)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(!obj_meta.is_valid())) {
    ret = OB_INVALID_ARGUMENT;
    STORAGE_LOG(WARN,"invalid argument", K(ret), K(obj_meta));
  } else {
    common::ObDatumBasicFuncs *id_basic_funcs = ObDatumFuncs::get_basic_func(
      obj_meta.get_type(), obj_meta.get_collation_type());
    if (OB_ISNULL(id_basic_funcs)) {
      ret = OB_ERR_UNEXPECTED;
      STORAGE_LOG(WARN,"failed to get basic functions", K(ret), K(obj_meta));
    } else {
      cmp_func_ = id_basic_funcs->null_first_cmp_;
      // numeric domain ids keep their value in the union bytes that ptr_ aliases, so a byte
      // compare would both mis-order them and dereference the value as an address
      is_binary_id_ = (common::CS_TYPE_BINARY == obj_meta.get_collation_type())
          && common::ObDatumFuncs::is_string_type(obj_meta.get_type());
    }
  }
  return ret;
}

int ObDomainIdCmp::compare(const ObDatum &lhs, const ObDatum &rhs, int &cmp_ret) const
{
  int ret = OB_SUCCESS;
  const bool lmax = lhs.is_max();
  const bool lmin = lhs.is_min();
  const bool rmax = rhs.is_max();
  const bool rmin = rhs.is_min();
  if (OB_UNLIKELY(nullptr == cmp_func_)) {
    ret = OB_NOT_INIT;
    STORAGE_LOG(WARN,"not init", K(ret));
  } else if (lhs.is_ext() || rhs.is_ext()) {
    if (OB_UNLIKELY(!(lmax || lmin || rmax || rmin))) {
      ret = OB_ERR_UNEXPECTED;
      STORAGE_LOG(WARN,"unexpected ext datum", K(ret), K(lhs), K(rhs));
    } else if (lmax || rmax) {
      cmp_ret = lmax - rmax;
    } else if (lmin || rmin) {
      cmp_ret = -(lmin - rmin);
    } else {
      ret = OB_ERR_UNEXPECTED;
    }
  } else if (OB_LIKELY(is_binary_id_) && !lhs.is_null() && !rhs.is_null()) {
    // bit-identical to strcmpsp(CS_TYPE_BINARY, ...): memcmp over the common prefix with a
    // length-difference tiebreak, without the charset lookup and argument validation
    cmp_ret = ob_binary_key_cmp(lhs.ptr_, lhs.len_, rhs.ptr_, rhs.len_);
  } else {
    ret = cmp_func_(lhs, rhs, cmp_ret, nullptr);
  }
  return ret;
}

} // namespace storage
} // namespace oceanbase

#endif
