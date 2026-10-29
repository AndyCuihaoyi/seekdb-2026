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

#include "object/ob_object.h"
#define USING_LOG_PREFIX STORAGE_FTS

#include "share/rc/ob_server_runtime.h"
#include "storage/fts/ob_fts_stop_word.h"
#include "storage/fts/ob_fts_parser_helper.h"
#include "storage/fts/ob_fts_parser_property.h"

namespace oceanbase
{
namespace storage
{

////////////////////////////////////////////////////////////////////////////////
// class ObStopWordChecker
ObStopWordChecker::~ObStopWordChecker()
{
  destroy();
}

namespace
{
// The length in the top bits followed by the raw bytes, so two different
// (length, bytes) pairs of at most ASCII_INDEX_MAX_WORD_LEN bytes never share a key.
OB_INLINE uint64_t pack_ascii_word(const char *str, const int64_t len)
{
  uint64_t key = static_cast<uint64_t>(len);
  for (int64_t i = 0; i < len; ++i) {
    key = (key << 8) | static_cast<unsigned char>(str[i]);
  }
  return key;
}

OB_INLINE int64_t ascii_index_slot(const uint64_t key)
{
  return static_cast<int64_t>((key * 0x9E3779B97F4A7C15ULL) >> 57);
}
} // namespace

bool ObStopWordChecker::build_ascii_index()
{
  bool valid = true;
  const int64_t stopword_count = sizeof(ob_stop_word_list) / sizeof(ob_stop_word_list[0]);
  MEMSET(ascii_index_, 0, sizeof(ascii_index_));
  for (int64_t i = 0; valid && i < stopword_count; ++i) {
    const char *word = ob_stop_word_list[i];
    const int64_t len = STRLEN(word);
    if (OB_UNLIKELY(0 == len || len > ASCII_INDEX_MAX_WORD_LEN)) {
      valid = false;
    }
    for (int64_t j = 0; valid && j < len; ++j) {
      if (OB_UNLIKELY(word[j] < 'a' || word[j] > 'z')) {
        valid = false;
      }
    }
    if (valid) {
      const uint64_t key = pack_ascii_word(word, len);
      int64_t slot = ascii_index_slot(key);
      while (OB_UNLIKELY(0 != ascii_index_[slot] && key != ascii_index_[slot])) {
        slot = (slot + 1) & (ASCII_INDEX_SLOT_CNT - 1);
      }
      ascii_index_[slot] = key;
    }
  }
  return valid;
}

bool ObStopWordChecker::ascii_index_find(const common::ObString &word, bool &is_stopword) const
{
  bool decidable = true;
  const char *ptr = word.ptr();
  int64_t len = word.length();
  is_stopword = false;
  // a space padded comparison ignores trailing spaces, so they carry no membership information
  while (len > 0 && ' ' == ptr[len - 1]) {
    --len;
  }
  if (len > ASCII_INDEX_MAX_WORD_LEN) {
    // longer than every indexed entry, and ASCII weights compare one byte at a time
  } else if (0 == len) {
    // the list holds no empty word
  } else {
    char folded[ASCII_INDEX_MAX_WORD_LEN];
    for (int64_t i = 0; decidable && i < len; ++i) {
      const unsigned char c = static_cast<unsigned char>(ptr[i]);
      if (OB_UNLIKELY(c >= 0x80)) {
        // a collation may fold a non-ASCII weight onto an ASCII one: keep the hash set path
        decidable = false;
      } else {
        folded[i] = static_cast<char>((c >= 'A' && c <= 'Z') ? (c + ('a' - 'A')) : c);
      }
    }
    if (decidable) {
      const uint64_t key = pack_ascii_word(folded, len);
      int64_t slot = ascii_index_slot(key);
      while (OB_UNLIKELY(0 != ascii_index_[slot] && key != ascii_index_[slot])) {
        slot = (slot + 1) & (ASCII_INDEX_SLOT_CNT - 1);
      }
      is_stopword = (key == ascii_index_[slot]);
    }
  }
  return decidable;
}

int ObStopWordChecker::init()
{
  int ret = OB_SUCCESS;



  if (inited_) {
    ret = OB_INIT_TWICE;
  } else if (OB_FAIL(stopword_set_.create(DEFAULT_STOPWORD_BUCKET_NUM, "StopWordSet", "StopWordSet"))) {
  } else {
    ObObjMeta stop_meta;
    stop_meta.set_varchar();
    stop_meta.set_collation_type(ObCollationType::CS_TYPE_UTF8MB4_GENERAL_CI);

    stopword_type_.set_meta(stop_meta);
    ascii_index_valid_ = build_ascii_index();
    const int64_t stopword_count = sizeof(ob_stop_word_list) / sizeof(ob_stop_word_list[0]);
    for (int64_t i = 0; OB_SUCC(ret) && i < stopword_count; ++i) {
      ObFTWord stopword(STRLEN(ob_stop_word_list[i]), ob_stop_word_list[i], stopword_type_);
      if (OB_FAIL(stopword_set_.set_refactored(stopword))) {
      }
    }

    if (OB_SUCC(ret)) {
      inited_ = true;
    }
  }
  return ret;
}

void ObStopWordChecker::destroy()
{
  if (inited_) {
    stopword_set_.destroy();
    ascii_index_valid_ = false;
    inited_ = false;
  }
}

int ObStopWordChecker::check_stopword(const ObFTWord &word, bool &is_stopword)
{
  int ret = OB_SUCCESS;
  bool decided = false;
  is_stopword = false;
  const bool same_collation = (word.get_collation_type() == stopword_type_.get_collation_type());
  if (OB_UNLIKELY(!inited_)) {
    ret = OB_NOT_INIT;
  } else if (OB_UNLIKELY(word.empty())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (same_collation && ascii_index_valid_
             && (decided = ascii_index_find(word.get_word().get_string(), is_stopword))) {
    // An ASCII token under utf8mb4_general_ci compares by its case folded bytes, so the
    // index gives the same answer as the hash set without hashing or latching a bucket.
  } else if (same_collation) {
    // A direct lookup is equivalent to the converting path for this case:
    //  - same-collation conversion returns the input bytes unchanged
    //    (ObCharset::charset_convert short-circuits equal charsets), and
    //  - word hashing and comparison of string datums dispatch on the charset's
    //    basic-func table keyed by collation only, not on the concrete string
    //    type, and the tokens here are always in-memory strings without a lob
    //    locator (ObDatumFuncs::get_basic_func / get_datum_cmp_func).
    // The extra ObObjMeta type check is therefore not needed for equivalence.
    ret = stopword_set_.exist_refactored(word);
  } else {
    common::ObArenaAllocator allocator(lib::ObMemAttr("ChkStopWord"));
    common::ObString cmp_str;
    if (OB_FAIL(common::ObCharset::charset_convert(
                                       allocator,
                                       word.get_word().get_string(),
                                       word.get_collation_type(),
                                       stopword_type_.get_collation_type(),
                                       cmp_str))) {
    } else {
      ObFTWord converted(cmp_str.length(), cmp_str.ptr(), stopword_type_);
      ret = stopword_set_.exist_refactored(converted);
    }
  }
  if (decided) {
    // is_stopword is final
  } else if (OB_HASH_NOT_EXIST == ret) {
    is_stopword = false;
    ret = OB_SUCCESS;
  } else if (OB_HASH_EXIST == ret) {
    is_stopword = true;
    ret = OB_SUCCESS;
  } else if (OB_SUCC(ret)) {
    ret = OB_ERR_UNEXPECTED;
  }
  return ret;
}

////////////////////////////////////////////////////////////////////////////////
// class ObAddWord
ObAddWord::ObAddWord(
    const ObFTParserProperty &property,
    const ObObjMeta &meta,
    const ObAddWordFlag &flag,
    common::ObIAllocator &allocator,
    ObFTWordMap &word_map)
  : word_meta_(meta),
    allocator_(allocator),
    word_map_(word_map),
    min_max_word_cnt_(0),
    non_stopword_cnt_(0),
    stopword_cnt_(0),
    min_token_size_(property.min_token_size_),
    max_token_size_(property.max_token_size_),
    flag_(flag)
{
}

int ObAddWord::process_word(
    const char *word,
    const int64_t word_len,
    const int64_t char_cnt,
    const int64_t word_freq)
{
  int ret = OB_SUCCESS;
  bool is_stopword = false;
  ObFTWord src_word(word_len, word, word_meta_);
  ObFTWord dst_word;
  if (OB_ISNULL(word) || OB_UNLIKELY(0 >= word_len || 0 >= char_cnt || 0 >= word_freq)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (is_min_max_word(char_cnt)) {
    ++min_max_word_cnt_;
  } else if (OB_FAIL(casedown_word(src_word, dst_word))) {
  } else if (OB_FAIL(check_stopword(dst_word, is_stopword))) {
  } else if (OB_UNLIKELY(is_stopword)) {
    ++stopword_cnt_;
  } else if (OB_FAIL(groupby_word(dst_word, word_freq))) {
  } else {
    non_stopword_cnt_ += word_freq;
  }
  return ret;
}

bool ObAddWord::is_min_max_word(const int64_t c_len) const
{
  return flag_.min_max_word() && (c_len < min_token_size_ || c_len > max_token_size_);
}

namespace
{
// Mixed Chinese/English text is the common case: ASCII letters fold with a constant
// offset and the CJK ideograph block (U+4000..U+9FFF, lead byte E4..E9) has no case
// mapping, so both fit in one pass. Returns false as soon as a character outside those
// sets appears, in which case the charset lower-casing must be used instead.
bool fold_case_ascii_cjk(const char *src, const int64_t len, char *dst)
{
  bool done = true;
  int64_t i = 0;
  while (done && i < len) {
    const unsigned char c = static_cast<unsigned char>(src[i]);
    if (c < 0x80) {
      dst[i] = (c >= 'A' && c <= 'Z') ? static_cast<char>(c - 'A' + 'a') : static_cast<char>(c);
      i += 1;
    } else if (c >= 0xE4 && c <= 0xE9 && i + 2 < len
               && 0x80 == (static_cast<unsigned char>(src[i + 1]) & 0xC0)
               && 0x80 == (static_cast<unsigned char>(src[i + 2]) & 0xC0)) {
      dst[i] = src[i];
      dst[i + 1] = src[i + 1];
      dst[i + 2] = src[i + 2];
      i += 3;
    } else {
      done = false;
    }
  }
  return done;
}
} // namespace

int ObAddWord::casedown_word(const ObFTWord &src, ObFTWord &dst)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(src.empty())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (flag_.casedown()) {
    const ObString src_str = src.get_word().get_string();
    const ObCollationType collation_type = word_meta_.get_collation_type();
    // Pure-ASCII tokens without uppercase letters are lowercased into an identical byte
    // sequence, so reference the token buffer directly instead of copying it.
    bool need_casedown = false;
    for (int64_t i = 0; !need_casedown && i < src_str.length(); ++i) {
      const unsigned char c = static_cast<unsigned char>(src_str.ptr()[i]);
      need_casedown = c >= 0x80 || (c >= 'A' && c <= 'Z');
    }
    if (!need_casedown && ObCharset::is_valid_collation(collation_type)) {
      dst = src;
    } else {
      char *buf = nullptr;
      if (CHARSET_UTF8MB4 == ObCharset::charset_type_by_coll(collation_type)
          && !ObCharset::is_bin_sort(collation_type)
          && OB_NOT_NULL(buf = static_cast<char *>(allocator_.alloc(src_str.length())))
          && fold_case_ascii_cjk(src_str.ptr(), src_str.length(), buf)) {
        ObFTWord tmp(src_str.length(), buf, word_meta_);
        dst = tmp;
      } else {
        ObString dst_str;
        if (OB_FAIL(ObCharset::tolower(
                        collation_type,
                        src_str,
                        dst_str,
                        allocator_))) {
        } else {
          ObFTWord tmp(dst_str.length(), dst_str.ptr(), word_meta_);
          dst = tmp;
        }
      }
    }
  } else {
    dst = src;
  }
  return ret;
}

int ObAddWord::check_stopword(const ObFTWord &ft_word, bool &is_stopword)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(ft_word.empty())) {
    ret = OB_INVALID_ARGUMENT;
  } else if (flag_.stopword()) {
    ObStopWordChecker *stop_word_checker = ObFTParseData::instance().stop_word_checker();
    if (OB_ISNULL(stop_word_checker)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(stop_word_checker->check_stopword(ft_word, is_stopword))) {
    }
  }
  return ret;
}

int ObAddWord::groupby_word(const ObFTWord &word, const int64_t word_freq)
{
  int ret = OB_SUCCESS;
  // A repeated word keeps the count of its first occurrence and accumulates the
  // frequency of the later ones. Doing that in place needs one hash computation
  // and one bucket walk, where the previous lookup-then-overwrite needed two.
  struct ObFTWordFreqUpdater
  {
    explicit ObFTWordFreqUpdater(const int64_t added) : added_(added) {}
    void operator()(common::hash::HashMapPair<ObFTWord, int64_t> &entry)
    {
      entry.second += added_;
    }
    int64_t added_;
  };
  if (OB_UNLIKELY(word.empty() || word_freq <= 0)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (!flag_.groupby_word()) {
    if (OB_FAIL(word_map_.set_refactored(word, 1/*word count*/))) {
    }
  } else {
    ObFTWordFreqUpdater updater(word_freq);
    ret = word_map_.set_or_update(word, 1/*word count*/, updater);
  }
  return ret;
}

} // end namespace storage
} // end namespace oceanbase
