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
    const int64_t min_len = MIN(lhs.len_, rhs.len_);
    const int memcmp_ret = min_len > 0 ? memcmp(lhs.ptr_, rhs.ptr_, min_len) : 0;
    cmp_ret = memcmp_ret < 0 ? -1
            : (memcmp_ret > 0 ? 1
            : (lhs.len_ < rhs.len_ ? -1 : (lhs.len_ > rhs.len_ ? 1 : 0)));
  } else {
    ret = cmp_func_(lhs, rhs, cmp_ret, nullptr);
  }
  return ret;
}

} // namespace storage
} // namespace oceanbase

#endif
