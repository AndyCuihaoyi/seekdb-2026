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

#ifndef _OCEANBASE_STORAGE_FTS_DICT_OB_FT_DICT_HUB_H_
#define _OCEANBASE_STORAGE_FTS_DICT_OB_FT_DICT_HUB_H_

#include <atomic>

#include "lib/charset/ob_charset.h"
#include "lib/lock/ob_bucket_lock.h"
#include "lib/lock/ob_tc_rwlock.h"
#include "lib/allocator/page_arena.h"
#include "storage/fts/dict/ob_ft_cache_container.h"
#include "storage/fts/dict/ob_ft_dict_def.h"

namespace oceanbase
{
namespace storage
{
// typedef uint64_t ObFTTableID;
// typedef ObFTTableID ObFTDictTableID;

class ObFTDictInfo
{
public:
  ObFTDictInfo()
      : name_(""),
        type_(ObFTDictType::DICT_TYPE_INVALID),
        charset_(CHARSET_INVALID),
        version_(0),
        range_count_(0)
  {
  }

public:
  char name_[2048]; // for now
  ObFTDictType type_;
  ObCharsetType charset_;
  int64_t version_; // in memory
  int32_t range_count_;
};

struct ObFTDictInfoKey
{
public:
  ObFTDictInfoKey()
      : type_(static_cast<uint64_t>(ObFTDictType::DICT_TYPE_INVALID))
  {
  } // default constructor
  ObFTDictInfoKey(const uint64_t type)
      : type_(type)
  {
  }
  int hash(uint64_t &hash_value) const
  {
    int ret = OB_SUCCESS;
    hash_value = hash();
    return ret;
  }

  uint64_t hash() const
  {
    uint64_t hash = 0;
    hash = common::murmurhash(&type_, sizeof(int64_t), hash);
    return hash;
  }

  bool operator==(const ObFTDictInfoKey &other) const
  {
    return type_ == other.type_;
  }

  int compare(const ObFTDictInfoKey &other) const
  {
    int ret = 0;
    if (0 == ret) {
      ret = type_ - other.type_;
    }
    return ret;
  }

private:
  uint64_t type_;
  // name
};

class ObFTCacheRangeContainer;
class ObIFTDict;

// A range dict built once per built-in dict type and shared by all parsers. The dict
// content is already pinned by the ObDictCache once built, so sharing the wrapper adds
// no additional staleness: the same KV cache entries back every parser today.
class ObFTCachedRangeDict final
{
public:
  ObFTCachedRangeDict() : alloc_(lib::ObMemAttr("FTCachedDict")), container_(alloc_), dict_(nullptr) {}
  ~ObFTCachedRangeDict() { reset(); }

  // Also reached from ObFTDictHub::destroy(), i.e. from deinit_fts(), which
  // ObServer::destroy() runs after ObDictCache::destroy() and ObKVGlobalCache::destroy()
  // have already completed. That stays safe only because container_.reset() merely drops
  // ObKVCacheHandle protection: ~ObKVCacheHandle clears the hazard pointer and returns it
  // to the thread-local free list, without calling any ObDictCache/ObKVGlobalCache API.
  // Do not add cache-facing calls (erase/revert/destroy_cache) to this path.
  void reset()
  {
    if (OB_NOT_NULL(dict_)) {
      dict_->~ObIFTDict();
      alloc_.free(dict_);
      dict_ = nullptr;
    }
    container_.reset();
    alloc_.reset();
  }

public:
  common::ObArenaAllocator alloc_;
  ObFTCacheRangeContainer container_;
  ObIFTDict *dict_;
  DISALLOW_COPY_AND_ASSIGN(ObFTCachedRangeDict);
};

class ObFTDictHub
{
public:
  static constexpr int64_t FT_DICT_TYPE_CNT = static_cast<int64_t>(ObFTDictType::DICT_IK_STOP) + 1;

  ObFTDictHub() : is_inited_(false), dict_map_(), rw_dict_lock_(), dict_cache_lock_()
  {
    MEMSET(cached_dicts_, 0, sizeof(cached_dicts_));
    for (int64_t i = 0; i < FT_DICT_TYPE_CNT; ++i) {
      ready_[i].store(false, std::memory_order_relaxed);
    }
  }
  ~ObFTDictHub() {}

  int init();

  // Requires a quiescent server: no thread may be inside get_cached_dict(). ObServer::destroy()
  // satisfies this by stopping all tenant/SQL workers before deinit_fts() reaches here.
  int destroy();

  int build_cache(const ObFTDictDesc &desc, ObFTCacheRangeContainer &container);

  int load_cache(const ObFTDictDesc &desc, ObFTCacheRangeContainer &container);

  // Returns the process-lifetime cached dict for desc.type_, loading/building it on first
  // use. The returned dict is immutable after init and safe to share across threads.
  int get_cached_dict(const ObFTDictDesc &desc, ObIFTDict *&dict);

private:
  int get_dict_info(const ObFTDictInfoKey &key, ObFTDictInfo &info);

  int put_dict_info(const ObFTDictInfoKey &key, const ObFTDictInfo &info);

  int build_cached_dict(const ObFTDictDesc &desc, ObFTCachedRangeDict &entry);

private:
  bool is_inited_;
  // holds info of dict
  hash::ObHashMap<ObFTDictInfoKey, ObFTDictInfo> dict_map_;
  ObBucketLock rw_dict_lock_;
  common::TCRWLock dict_cache_lock_;
  ObFTCachedRangeDict *cached_dicts_[FT_DICT_TYPE_CNT];
  // Publication protocol: build_cached_dict() fills entry->dict_ under dict_cache_lock_,
  // then get_cached_dict() publishes ready_[i] with release. Fast-path readers acquire-load
  // ready_ and dereference entry->dict_ without the lock, so the dict (and the KV cache
  // entries pinned by its container) must stay valid until destroy(), and destroy() must
  // not run concurrently with any reader (see destroy()).
  std::atomic<bool> ready_[FT_DICT_TYPE_CNT];
};

} //  namespace storage
} //  namespace oceanbase

#endif // _OCEANBASE_STORAGE_FTS_DICT_OB_FT_DICT_HUB_H_
