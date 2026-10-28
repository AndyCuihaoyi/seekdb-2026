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

#include <cstring>
#include "lib/allocator/page_arena.h"
#include "lib/string/ob_sql_string.h"
#include "lib/utility/ob_print_utils.h"
#include "sql/engine/basic/file_scan/ob_parquet_reader.h"

namespace oceanbase
{
using namespace common;
namespace sql
{
namespace
{

// ---------------------------------------------------------------------------
// Little endian helpers
// ---------------------------------------------------------------------------
inline uint16_t load_le16(const uint8_t *p)
{
  return static_cast<uint16_t>(p[0]) | (static_cast<uint16_t>(p[1]) << 8);
}

inline uint32_t load_le32(const uint8_t *p)
{
  return static_cast<uint32_t>(p[0]) | (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) | (static_cast<uint32_t>(p[3]) << 24);
}

inline uint64_t load_le64(const uint8_t *p)
{
  return static_cast<uint64_t>(load_le32(p)) |
         (static_cast<uint64_t>(load_le32(p + 4)) << 32);
}

// ---------------------------------------------------------------------------
// Thrift compact protocol decoder (only the subset needed by Parquet metadata).
// ---------------------------------------------------------------------------
enum ObThriftType : uint8_t
{
  THRIFT_STOP = 0,
  THRIFT_BOOL_TRUE = 1,
  THRIFT_BOOL_FALSE = 2,
  THRIFT_BYTE = 3,
  THRIFT_I16 = 4,
  THRIFT_I32 = 5,
  THRIFT_I64 = 6,
  THRIFT_DOUBLE = 7,
  THRIFT_BINARY = 8,
  THRIFT_LIST = 9,
  THRIFT_SET = 10,
  THRIFT_MAP = 11,
  THRIFT_STRUCT = 12
};

class ObThriftCompactReader
{
public:
  ObThriftCompactReader(const uint8_t *buf, const int64_t len)
      : buf_(buf), len_(len), pos_(0), last_field_id_(0), depth_(0) {}
  int64_t pos() const { return pos_; }
  int read_byte(uint8_t &b)
  {
    int ret = OB_SUCCESS;
    if (OB_UNLIKELY(pos_ >= len_)) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      b = buf_[pos_++];
    }
    return ret;
  }
  int read_varint(uint64_t &v)
  {
    int ret = OB_SUCCESS;
    v = 0;
    bool done = false;
    for (int shift = 0; OB_SUCC(ret) && !done; ++shift) {
      if (OB_UNLIKELY(shift >= 10 || pos_ >= len_)) {
        ret = OB_INVALID_ARGUMENT;
      } else {
        const uint8_t b = buf_[pos_++];
        v |= static_cast<uint64_t>(b & 0x7F) << (shift * 7);
        done = (0 == (b & 0x80));
      }
    }
    return ret;
  }
  int read_zigzag(int64_t &v)
  {
    int ret = OB_SUCCESS;
    uint64_t u = 0;
    if (OB_FAIL(read_varint(u))) {
    } else {
      v = static_cast<int64_t>((u >> 1) ^ (~(u & 1ULL) + 1ULL));
    }
    return ret;
  }
  int read_i64(int64_t &v) { return read_zigzag(v); }
  int read_double(double &v)
  {
    int ret = OB_SUCCESS;
    if (OB_UNLIKELY(pos_ + 8 > len_)) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      const uint64_t bits = load_le64(buf_ + pos_);
      pos_ += 8;
      MEMCPY(&v, &bits, sizeof(v));
    }
    return ret;
  }
  int read_binary(ObString &s)
  {
    int ret = OB_SUCCESS;
    uint64_t n = 0;
    if (OB_FAIL(read_varint(n))) {
    } else if (OB_UNLIKELY(n > static_cast<uint64_t>(len_ - pos_))) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      s.assign_ptr(reinterpret_cast<const char *>(buf_ + pos_), static_cast<int32_t>(n));
      pos_ += n;
    }
    return ret;
  }
  int read_list_begin(int64_t &size, uint8_t &elem_type)
  {
    int ret = OB_SUCCESS;
    uint8_t b = 0;
    if (OB_FAIL(read_byte(b))) {
    } else {
      size = (b >> 4) & 0x0F;
      elem_type = b & 0x0F;
      if (15 == size) {
        uint64_t sz = 0;
        if (OB_FAIL(read_varint(sz))) {
        } else if (OB_UNLIKELY(sz > static_cast<uint64_t>(len_ - pos_))) {
          ret = OB_INVALID_ARGUMENT;
        } else {
          size = static_cast<int64_t>(sz);
        }
      }
    }
    return ret;
  }
  int read_field_begin(uint8_t &type, int16_t &field_id)
  {
    int ret = OB_SUCCESS;
    uint8_t b = 0;
    if (OB_FAIL(read_byte(b))) {
    } else {
      type = b & 0x0F;
      if (THRIFT_STOP == type) {
        field_id = 0;
      } else {
        const int16_t delta = static_cast<int16_t>((b >> 4) & 0x0F);
        if (0 == delta) {
          int64_t v = 0;
          if (OB_FAIL(read_zigzag(v))) {
          } else {
            field_id = static_cast<int16_t>(v);
            last_field_id_ = field_id;
          }
        } else {
          field_id = static_cast<int16_t>(last_field_id_ + delta);
          last_field_id_ = field_id;
        }
      }
    }
    return ret;
  }
  void begin_struct()
  {
    if (depth_ < 16) {
      stack_[depth_] = last_field_id_;
    }
    ++depth_;
    last_field_id_ = 0;
  }
  void end_struct()
  {
    --depth_;
    if (depth_ >= 0 && depth_ < 16) {
      last_field_id_ = stack_[depth_];
    } else {
      last_field_id_ = 0;
    }
  }
  int skip(const uint8_t type)
  {
    int ret = OB_SUCCESS;
    switch (type) {
      case THRIFT_STOP:
      // A bool field carries its value in the type nibble, no payload.
      case THRIFT_BOOL_TRUE:
      case THRIFT_BOOL_FALSE:
        break;
      case THRIFT_BYTE: {
        uint8_t b = 0;
        ret = read_byte(b);
        break;
      }
      case THRIFT_I16:
      case THRIFT_I32:
      case THRIFT_I64: {
        uint64_t v = 0;
        ret = read_varint(v);
        break;
      }
      case THRIFT_DOUBLE: {
        if (OB_UNLIKELY(pos_ + 8 > len_)) {
          ret = OB_INVALID_ARGUMENT;
        } else {
          pos_ += 8;
        }
        break;
      }
      case THRIFT_BINARY: {
        uint64_t n = 0;
        if (OB_FAIL(read_varint(n))) {
        } else if (OB_UNLIKELY(n > static_cast<uint64_t>(len_ - pos_))) {
          ret = OB_INVALID_ARGUMENT;
        } else {
          pos_ += n;
        }
        break;
      }
      case THRIFT_LIST:
      case THRIFT_SET: {
        int64_t size = 0;
        uint8_t et = 0;
        if (OB_FAIL(read_list_begin(size, et))) {
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
          if (THRIFT_BOOL_TRUE == et || THRIFT_BOOL_FALSE == et) {
            // bool elements are encoded as a one byte payload
            uint8_t b = 0;
            if (OB_FAIL(read_byte(b))) {
            }
          } else if (OB_FAIL(skip(et))) {
          }
        }
        break;
      }
      case THRIFT_MAP: {
        uint64_t size = 0;
        if (OB_FAIL(read_varint(size))) {
        } else if (size > 0) {
          uint8_t b = 0;
          if (OB_FAIL(read_byte(b))) {
          } else {
            const uint8_t kt = (b >> 4) & 0x0F;
            const uint8_t vt = b & 0x0F;
            for (uint64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
              if (OB_FAIL(skip(kt))) {
              } else if (OB_FAIL(skip(vt))) {
              }
            }
          }
        }
        break;
      }
      case THRIFT_STRUCT: {
        begin_struct();
        for (;;) {
          uint8_t ft = 0;
          int16_t fid = 0;
          if (OB_FAIL(read_field_begin(ft, fid))) {
            break;
          } else if (THRIFT_STOP == ft) {
            break;
          } else if (OB_FAIL(skip(ft))) {
            break;
          }
        }
        end_struct();
        break;
      }
      default:
        ret = OB_INVALID_ARGUMENT;
        break;
    }
    return ret;
  }
private:
  const uint8_t *buf_;
  int64_t len_;
  int64_t pos_;
  int16_t last_field_id_;
  int16_t stack_[16];
  int depth_;
};

// ---------------------------------------------------------------------------
// Parquet metadata structures (only the fields we need).
// ---------------------------------------------------------------------------
enum ObParquetPhysicalType : int64_t
{
  PARQUET_BOOLEAN = 0,
  PARQUET_INT32 = 1,
  PARQUET_INT64 = 2,
  PARQUET_INT96 = 3,
  PARQUET_FLOAT = 4,
  PARQUET_DOUBLE = 5,
  PARQUET_BYTE_ARRAY = 6,
  PARQUET_FIXED_LEN_BYTE_ARRAY = 7
};

enum ObParquetPageType : int64_t
{
  PARQUET_PAGE_DATA = 0,
  PARQUET_PAGE_INDEX = 1,
  PARQUET_PAGE_DICTIONARY = 2,
  PARQUET_PAGE_DATA_V2 = 3
};

enum ObParquetEncoding : int64_t
{
  PARQUET_ENCODING_PLAIN = 0,
  PARQUET_ENCODING_PLAIN_DICTIONARY = 2,
  PARQUET_ENCODING_RLE = 3,
  PARQUET_ENCODING_BIT_PACKED = 4,
  PARQUET_ENCODING_RLE_DICTIONARY = 8
};

enum ObParquetCodec : int64_t
{
  PARQUET_CODEC_NONE = 0,
  PARQUET_CODEC_SNAPPY = 1
};

struct ObParquetSchemaElement
{
  ObParquetSchemaElement()
      : physical_type_(-1), type_length_(0), repetition_type_(-1), name_(),
        num_children_(-1), converted_type_(-1), logical_type_(-1), time_unit_(-1) {}
  int64_t physical_type_;
  int64_t type_length_;
  int64_t repetition_type_;  // 0 required, 1 optional, 2 repeated, -1 unset
  ObString name_;
  int64_t num_children_;
  int64_t converted_type_;
  int64_t logical_type_;
  int64_t time_unit_;        // 1 millis, 2 micros, 3 nanos, -1 unset
};

struct ObParquetColumn
{
  ObParquetColumn()
      : name_(), physical_type_(-1), type_length_(0), converted_type_(-1),
        logical_type_(-1), time_unit_(-1), max_def_level_(0), max_rep_level_(0),
        logical_(-1) {}
  ObString name_;
  int64_t physical_type_;
  int64_t type_length_;
  int64_t converted_type_;
  int64_t logical_type_;
  int64_t time_unit_;
  int64_t max_def_level_;
  int64_t max_rep_level_;
  int64_t logical_;  // ObFileLogicalType
};

struct ObParquetChunk
{
  ObParquetChunk()
      : num_values_(0), codec_(-1), data_page_offset_(-1), dictionary_page_offset_(-1) {}
  int64_t num_values_;
  int64_t codec_;
  int64_t data_page_offset_;
  int64_t dictionary_page_offset_;
};

struct ObParquetFileMeta
{
  ObParquetFileMeta() : num_rows_(0), schema_(), columns_(), chunks_(),
                        rg_first_chunk_(), rg_num_rows_() {}
  int64_t num_rows_;
  ObSEArray<ObParquetSchemaElement, 16> schema_;
  ObSEArray<ObParquetColumn, 8> columns_;
  ObSEArray<ObParquetChunk, 16> chunks_;
  ObSEArray<int64_t, 8> rg_first_chunk_;
  ObSEArray<int64_t, 8> rg_num_rows_;
};

struct ObParquetPageHeader
{
  ObParquetPageHeader()
      : type_(-1), uncompressed_page_size_(0), compressed_page_size_(0),
        num_values_(0), encoding_(-1), dict_num_values_(0),
        def_levels_byte_length_(0), rep_levels_byte_length_(0), is_compressed_(true) {}
  int64_t type_;
  int64_t uncompressed_page_size_;
  int64_t compressed_page_size_;
  int64_t num_values_;
  int64_t encoding_;
  int64_t dict_num_values_;
  int64_t def_levels_byte_length_;
  int64_t rep_levels_byte_length_;
  bool is_compressed_;
};

int parse_time_type(ObThriftCompactReader &r, ObParquetSchemaElement &el)
{
  int ret = OB_SUCCESS;
  uint8_t ftype = 0;
  int16_t fid = 0;
  r.begin_struct();
  while (OB_SUCC(ret)) {
    if (OB_FAIL(r.read_field_begin(ftype, fid))) {
      break;
    } else if (THRIFT_STOP == ftype) {
      break;
    } else if (2 == fid && THRIFT_STRUCT == ftype) {
      // TimeUnit is a union; the field id selects the unit.
      r.begin_struct();
      while (OB_SUCC(ret)) {
        uint8_t ut = 0;
        int16_t ufid = 0;
        if (OB_FAIL(r.read_field_begin(ut, ufid))) {
          break;
        } else if (THRIFT_STOP == ut) {
          break;
        } else {
          if (1 == ufid) {
            el.time_unit_ = 1;
          } else if (2 == ufid) {
            el.time_unit_ = 2;
          } else if (3 == ufid) {
            el.time_unit_ = 3;
          }
          if (OB_FAIL(r.skip(ut))) {
            break;
          }
        }
      }
      r.end_struct();
    } else if (OB_FAIL(r.skip(ftype))) {
    }
  }
  r.end_struct();
  return ret;
}

int parse_logical_type(ObThriftCompactReader &r, ObParquetSchemaElement &el)
{
  int ret = OB_SUCCESS;
  uint8_t ftype = 0;
  int16_t fid = 0;
  r.begin_struct();
  while (OB_SUCC(ret)) {
    if (OB_FAIL(r.read_field_begin(ftype, fid))) {
      break;
    } else if (THRIFT_STOP == ftype) {
      break;
    } else {
      if (fid >= 1 && fid <= 16 && THRIFT_STRUCT == ftype) {
        el.logical_type_ = fid;
        if (7 == fid || 8 == fid) {
          // TIME / TIMESTAMP carry a TimeUnit
          if (OB_FAIL(parse_time_type(r, el))) {
          }
        } else if (OB_FAIL(r.skip(ftype))) {
        }
      } else if (OB_FAIL(r.skip(ftype))) {
      }
    }
  }
  r.end_struct();
  return ret;
}

int parse_schema_element(ObThriftCompactReader &r, ObParquetSchemaElement &el)
{
  int ret = OB_SUCCESS;
  uint8_t ftype = 0;
  int16_t fid = 0;
  r.begin_struct();
  while (OB_SUCC(ret)) {
    if (OB_FAIL(r.read_field_begin(ftype, fid))) {
      break;
    } else if (THRIFT_STOP == ftype) {
      break;
    } else if (4 == fid && THRIFT_BINARY == ftype) {
      if (OB_FAIL(r.read_binary(el.name_))) {
      }
    } else if (10 == fid && THRIFT_STRUCT == ftype) {
      if (OB_FAIL(parse_logical_type(r, el))) {
      }
    } else if (THRIFT_I32 == ftype || THRIFT_I64 == ftype || THRIFT_I16 == ftype) {
      int64_t v = 0;
      if (OB_FAIL(r.read_i64(v))) {
      } else if (1 == fid) {
        el.physical_type_ = v;
      } else if (2 == fid) {
        el.type_length_ = v;
      } else if (3 == fid) {
        el.repetition_type_ = v;
      } else if (5 == fid) {
        el.num_children_ = v;
      } else if (6 == fid) {
        el.converted_type_ = v;
      }
    } else if (OB_FAIL(r.skip(ftype))) {
    }
  }
  r.end_struct();
  return ret;
}

int parse_column_metadata(ObThriftCompactReader &r, ObParquetChunk &chunk)
{
  int ret = OB_SUCCESS;
  uint8_t ftype = 0;
  int16_t fid = 0;
  r.begin_struct();
  while (OB_SUCC(ret)) {
    if (OB_FAIL(r.read_field_begin(ftype, fid))) {
      break;
    } else if (THRIFT_STOP == ftype) {
      break;
    } else if ((THRIFT_I32 == ftype || THRIFT_I64 == ftype) &&
               (4 == fid || 5 == fid || 9 == fid || 11 == fid)) {
      int64_t v = 0;
      if (OB_FAIL(r.read_i64(v))) {
      } else if (4 == fid) {
        chunk.codec_ = v;
      } else if (5 == fid) {
        chunk.num_values_ = v;
      } else if (9 == fid) {
        chunk.data_page_offset_ = v;
      } else if (11 == fid) {
        chunk.dictionary_page_offset_ = v;
      }
    } else if (OB_FAIL(r.skip(ftype))) {
    }
  }
  r.end_struct();
  return ret;
}

int parse_column_chunk(ObThriftCompactReader &r, ObParquetChunk &chunk)
{
  int ret = OB_SUCCESS;
  uint8_t ftype = 0;
  int16_t fid = 0;
  r.begin_struct();
  while (OB_SUCC(ret)) {
    if (OB_FAIL(r.read_field_begin(ftype, fid))) {
      break;
    } else if (THRIFT_STOP == ftype) {
      break;
    } else if (3 == fid && THRIFT_STRUCT == ftype) {
      if (OB_FAIL(parse_column_metadata(r, chunk))) {
      }
    } else if (OB_FAIL(r.skip(ftype))) {
    }
  }
  r.end_struct();
  return ret;
}

int parse_row_group(ObThriftCompactReader &r, ObParquetFileMeta &meta)
{
  int ret = OB_SUCCESS;
  uint8_t ftype = 0;
  int16_t fid = 0;
  int64_t num_rows = 0;
  if (OB_FAIL(meta.rg_first_chunk_.push_back(meta.chunks_.count()))) {
  } else {
    r.begin_struct();
    while (OB_SUCC(ret)) {
      if (OB_FAIL(r.read_field_begin(ftype, fid))) {
        break;
      } else if (THRIFT_STOP == ftype) {
        break;
      } else if (1 == fid && THRIFT_LIST == ftype) {
        int64_t size = 0;
        uint8_t et = 0;
        if (OB_FAIL(r.read_list_begin(size, et))) {
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
          ObParquetChunk chunk;
          if (OB_FAIL(parse_column_chunk(r, chunk))) {
          } else if (OB_FAIL(meta.chunks_.push_back(chunk))) {
          }
        }
      } else if (3 == fid && (THRIFT_I32 == ftype || THRIFT_I64 == ftype)) {
        if (OB_FAIL(r.read_i64(num_rows))) {
        }
      } else if (OB_FAIL(r.skip(ftype))) {
      }
    }
    r.end_struct();
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(meta.rg_num_rows_.push_back(num_rows))) {
    }
  }
  return ret;
}

int parse_file_metadata(ObThriftCompactReader &r, ObParquetFileMeta &meta)
{
  int ret = OB_SUCCESS;
  uint8_t ftype = 0;
  int16_t fid = 0;
  r.begin_struct();
  while (OB_SUCC(ret)) {
    if (OB_FAIL(r.read_field_begin(ftype, fid))) {
      break;
    } else if (THRIFT_STOP == ftype) {
      break;
    } else if (3 == fid && (THRIFT_I32 == ftype || THRIFT_I64 == ftype)) {
      if (OB_FAIL(r.read_i64(meta.num_rows_))) {
      }
    } else if (2 == fid && THRIFT_LIST == ftype) {
      int64_t size = 0;
      uint8_t et = 0;
      if (OB_FAIL(r.read_list_begin(size, et))) {
      }
      for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
        ObParquetSchemaElement el;
        if (OB_FAIL(parse_schema_element(r, el))) {
        } else if (OB_FAIL(meta.schema_.push_back(el))) {
        }
      }
    } else if (4 == fid && THRIFT_LIST == ftype) {
      int64_t size = 0;
      uint8_t et = 0;
      if (OB_FAIL(r.read_list_begin(size, et))) {
      }
      for (int64_t i = 0; OB_SUCC(ret) && i < size; ++i) {
        if (OB_FAIL(parse_row_group(r, meta))) {
        }
      }
    } else if (OB_FAIL(r.skip(ftype))) {
    }
  }
  r.end_struct();
  return ret;
}

int parse_page_header(ObThriftCompactReader &r, ObParquetPageHeader &ph)
{
  int ret = OB_SUCCESS;
  uint8_t ftype = 0;
  int16_t fid = 0;
  r.begin_struct();
  while (OB_SUCC(ret)) {
    if (OB_FAIL(r.read_field_begin(ftype, fid))) {
      break;
    } else if (THRIFT_STOP == ftype) {
      break;
    } else if ((THRIFT_I32 == ftype || THRIFT_I64 == ftype) &&
               (1 == fid || 2 == fid || 3 == fid)) {
      int64_t v = 0;
      if (OB_FAIL(r.read_i64(v))) {
      } else if (1 == fid) {
        ph.type_ = v;
      } else if (2 == fid) {
        ph.uncompressed_page_size_ = v;
      } else if (3 == fid) {
        ph.compressed_page_size_ = v;
      }
    } else if (5 == fid && THRIFT_STRUCT == ftype) {
      // DataPageHeader
      r.begin_struct();
      while (OB_SUCC(ret)) {
        uint8_t t = 0;
        int16_t f = 0;
        if (OB_FAIL(r.read_field_begin(t, f))) {
          break;
        } else if (THRIFT_STOP == t) {
          break;
        } else if ((THRIFT_I32 == t || THRIFT_I64 == t) && (1 == f || 2 == f)) {
          int64_t v = 0;
          if (OB_FAIL(r.read_i64(v))) {
          } else if (1 == f) {
            ph.num_values_ = v;
          } else {
            ph.encoding_ = v;
          }
        } else if (OB_FAIL(r.skip(t))) {
        }
      }
      r.end_struct();
    } else if (7 == fid && THRIFT_STRUCT == ftype) {
      // DictionaryPageHeader
      r.begin_struct();
      while (OB_SUCC(ret)) {
        uint8_t t = 0;
        int16_t f = 0;
        if (OB_FAIL(r.read_field_begin(t, f))) {
          break;
        } else if (THRIFT_STOP == t) {
          break;
        } else if (1 == f && (THRIFT_I32 == t || THRIFT_I64 == t)) {
          if (OB_FAIL(r.read_i64(ph.dict_num_values_))) {
          }
        } else if (OB_FAIL(r.skip(t))) {
        }
      }
      r.end_struct();
    } else if (8 == fid && THRIFT_STRUCT == ftype) {
      // DataPageHeaderV2
      r.begin_struct();
      while (OB_SUCC(ret)) {
        uint8_t t = 0;
        int16_t f = 0;
        if (OB_FAIL(r.read_field_begin(t, f))) {
          break;
        } else if (THRIFT_STOP == t) {
          break;
        } else if ((THRIFT_BOOL_TRUE == t || THRIFT_BOOL_FALSE == t) && 7 == f) {
          ph.is_compressed_ = (THRIFT_BOOL_TRUE == t);
        } else if ((THRIFT_I32 == t || THRIFT_I64 == t) &&
                   (1 == f || 5 == f || 6 == f)) {
          int64_t v = 0;
          if (OB_FAIL(r.read_i64(v))) {
          } else if (1 == f) {
            ph.num_values_ = v;
          } else if (5 == f) {
            ph.def_levels_byte_length_ = v;
          } else {
            ph.rep_levels_byte_length_ = v;
          }
        } else if ((THRIFT_I32 == t || THRIFT_I64 == t) && 4 == f) {
          if (OB_FAIL(r.read_i64(ph.encoding_))) {
          }
        } else if (OB_FAIL(r.skip(t))) {
        }
      }
      r.end_struct();
    } else if (OB_FAIL(r.skip(ftype))) {
    }
  }
  r.end_struct();
  return ret;
}

// ---------------------------------------------------------------------------
// Type mapping
// ---------------------------------------------------------------------------
int64_t parquet_physical_to_logical(const ObParquetColumn &col)
{
  int64_t logical = OB_FILE_TYPE_VARCHAR;
  // LogicalType (new style) takes precedence.
  switch (col.logical_type_) {
    case 1:  // STRING
    case 11: // JSON
    case 12: // BSON
      logical = OB_FILE_TYPE_VARCHAR;
      return logical;
    case 5:  // DECIMAL (not supported, mapped to VARCHAR)
      logical = OB_FILE_TYPE_VARCHAR;
      return logical;
    case 6:  // DATE
      logical = OB_FILE_TYPE_DATE;
      return logical;
    case 8:  // TIMESTAMP
      logical = OB_FILE_TYPE_DATETIME;
      return logical;
    case 9:  // INTEGER
      logical = OB_FILE_TYPE_BIGINT;
      return logical;
    case 10: // UNKNOWN (null type)
      logical = OB_FILE_TYPE_NULL;
      return logical;
    default:
      break;
  }
  switch (col.converted_type_) {
    case 0:  // UTF8
    case 19: // JSON
    case 20: // BSON
      logical = OB_FILE_TYPE_VARCHAR;
      return logical;
    case 6:  // DATE
      logical = OB_FILE_TYPE_DATE;
      return logical;
    case 9:  // TIMESTAMP_MILLIS
    case 10: // TIMESTAMP_MICROS
      logical = OB_FILE_TYPE_DATETIME;
      return logical;
    default:
      break;
  }
  switch (col.physical_type_) {
    case PARQUET_BOOLEAN:
      logical = OB_FILE_TYPE_BOOLEAN;
      break;
    case PARQUET_INT32:
    case PARQUET_INT64:
      logical = OB_FILE_TYPE_BIGINT;
      break;
    case PARQUET_INT96:
      logical = OB_FILE_TYPE_DATETIME;
      break;
    case PARQUET_FLOAT:
    case PARQUET_DOUBLE:
      logical = OB_FILE_TYPE_DOUBLE;
      break;
    case PARQUET_BYTE_ARRAY:
    case PARQUET_FIXED_LEN_BYTE_ARRAY:
      logical = OB_FILE_TYPE_VARCHAR;
      break;
    default:
      logical = OB_FILE_TYPE_VARCHAR;
      break;
  }
  return logical;
}

int flatten_schema(const ObSEArray<ObParquetSchemaElement, 16> &schema, int64_t &idx,
                   const int64_t parent_def, const int64_t parent_rep, const bool is_root,
                   ObSEArray<ObParquetColumn, 8> &columns)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(idx < 0 || idx >= schema.count())) {
    ret = OB_INVALID_ARGUMENT;
    LOG_WARN("invalid parquet schema", K(ret), K(idx), "count", schema.count());
  } else {
    const ObParquetSchemaElement &el = schema.at(idx);
    ++idx;
    const int64_t def = parent_def +
        ((1 == el.repetition_type_ || 2 == el.repetition_type_) ? 1 : 0);
    const int64_t rep = parent_rep + ((2 == el.repetition_type_) ? 1 : 0);
    if (el.num_children_ > 0) {
      for (int64_t i = 0; OB_SUCC(ret) && i < el.num_children_; ++i) {
        if (OB_FAIL(flatten_schema(schema, idx, def, rep, false, columns))) {
        }
      }
    } else if (is_root) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("parquet root schema has no children", K(ret));
    } else {
      ObParquetColumn col;
      col.name_ = el.name_;
      col.physical_type_ = el.physical_type_;
      col.type_length_ = el.type_length_;
      col.converted_type_ = el.converted_type_;
      col.logical_type_ = el.logical_type_;
      col.time_unit_ = el.time_unit_;
      col.max_def_level_ = def;
      col.max_rep_level_ = rep;
      col.logical_ = parquet_physical_to_logical(col);
      if (OB_FAIL(columns.push_back(col))) {
      }
    }
  }
  return ret;
}

// ---------------------------------------------------------------------------
// Snappy raw block decompression
// ---------------------------------------------------------------------------
int snappy_decompress(const uint8_t *src, const int64_t src_len, uint8_t *dst,
                      const int64_t dst_capacity, int64_t &written)
{
  int ret = OB_SUCCESS;
  written = 0;
  int64_t pos = 0;
  uint64_t want = 0;
  bool done = false;
  for (int shift = 0; OB_SUCC(ret) && !done; ++shift) {
    if (OB_UNLIKELY(shift >= 10 || pos >= src_len)) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      const uint8_t b = src[pos++];
      want |= static_cast<uint64_t>(b & 0x7F) << (shift * 7);
      done = (0 == (b & 0x80));
    }
  }
  if (OB_FAIL(ret)) {
  } else if (OB_UNLIKELY(want > static_cast<uint64_t>(dst_capacity))) {
    ret = OB_INVALID_ARGUMENT;
    LOG_WARN("snappy: uncompressed length exceeds buffer", K(ret), K(want), K(dst_capacity));
  } else {
    const int64_t expected = static_cast<int64_t>(want);
    while (OB_SUCC(ret) && pos < src_len && written < expected) {
      const uint8_t tag = src[pos++];
      const int kind = tag & 0x3;
      if (0 == kind) {
        int64_t len = (tag >> 2) + 1;
        if (len > 60) {
          const int extra = static_cast<int>(len - 60);
          if (OB_UNLIKELY(pos + extra > src_len)) {
            ret = OB_INVALID_ARGUMENT;
          } else {
            int64_t l = 0;
            for (int i = 0; i < extra; ++i) {
              l |= static_cast<int64_t>(src[pos + i]) << (8 * i);
            }
            pos += extra;
            len = l + 1;
          }
        }
        if (OB_FAIL(ret)) {
        } else if (OB_UNLIKELY(pos + len > src_len || written + len > expected)) {
          ret = OB_INVALID_ARGUMENT;
          LOG_WARN("snappy: literal overflows", K(ret), K(len), K(written), K(expected));
        } else {
          MEMCPY(dst + written, src + pos, len);
          pos += len;
          written += len;
        }
      } else {
        int64_t len = 0;
        int64_t offset = 0;
        if (1 == kind) {
          len = 4 + ((tag >> 2) & 0x7);
          if (OB_UNLIKELY(pos >= src_len)) {
            ret = OB_INVALID_ARGUMENT;
          } else {
            offset = (static_cast<int64_t>(tag >> 5) << 8) | src[pos++];
          }
        } else if (2 == kind) {
          len = 1 + (tag >> 2);
          if (OB_UNLIKELY(pos + 2 > src_len)) {
            ret = OB_INVALID_ARGUMENT;
          } else {
            offset = load_le16(src + pos);
            pos += 2;
          }
        } else {
          len = 1 + (tag >> 2);
          if (OB_UNLIKELY(pos + 4 > src_len)) {
            ret = OB_INVALID_ARGUMENT;
          } else {
            offset = load_le32(src + pos);
            pos += 4;
          }
        }
        if (OB_FAIL(ret)) {
        } else if (OB_UNLIKELY(offset <= 0 || offset > written || written + len > expected)) {
          ret = OB_INVALID_ARGUMENT;
          LOG_WARN("snappy: invalid copy", K(ret), K(offset), K(len), K(written));
        } else {
          for (int64_t i = 0; i < len; ++i) {
            dst[written + i] = dst[written - offset + i];
          }
          written += len;
        }
      }
    }
    if (OB_SUCC(ret) && OB_UNLIKELY(written != expected)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("snappy: unexpected output length", K(ret), K(written), K(expected));
    }
  }
  return ret;
}

// ---------------------------------------------------------------------------
// RLE / bit-packed hybrid decoder
// ---------------------------------------------------------------------------
int decode_hybrid(const uint8_t *buf, const int64_t len, const int bit_width,
                  const int64_t value_count, ObIArray<uint32_t> &values)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(bit_width < 0 || bit_width > 32 || value_count < 0)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (0 == bit_width) {
    for (int64_t i = 0; OB_SUCC(ret) && i < value_count; ++i) {
      if (OB_FAIL(values.push_back(0))) {
      }
    }
  } else {
    int64_t pos = 0;
    while (OB_SUCC(ret) && values.count() < value_count) {
      uint64_t header = 0;
      bool varint_done = false;
      for (int shift = 0; OB_SUCC(ret) && !varint_done; ++shift) {
        if (OB_UNLIKELY(shift >= 10 || pos >= len)) {
          ret = OB_INVALID_ARGUMENT;
        } else {
          const uint8_t b = buf[pos++];
          header |= static_cast<uint64_t>(b & 0x7F) << (shift * 7);
          varint_done = (0 == (b & 0x80));
        }
      }
      if (OB_FAIL(ret)) {
      } else if (header & 1ULL) {
        const int64_t groups = static_cast<int64_t>(header >> 1);
        const int64_t nvalues = groups * 8;
        const int64_t nbytes = groups * bit_width;
        if (OB_UNLIKELY(nbytes < 0 || pos + nbytes > len)) {
          ret = OB_INVALID_ARGUMENT;
          LOG_WARN("rle: bit-packed run overflows", K(ret), K(nbytes), K(len), K(pos));
        } else {
          for (int64_t i = 0; OB_SUCC(ret) && i < nvalues && values.count() < value_count; ++i) {
            uint32_t v = 0;
            for (int b = 0; b < bit_width; ++b) {
              const int64_t bit = i * bit_width + b;
              v |= static_cast<uint32_t>((buf[pos + bit / 8] >> (bit % 8)) & 1) << b;
            }
            if (OB_FAIL(values.push_back(v))) {
            }
          }
          pos += nbytes;
        }
      } else {
        const int64_t run = static_cast<int64_t>(header >> 1);
        const int64_t nbytes = (bit_width + 7) / 8;
        if (OB_UNLIKELY(pos + nbytes > len)) {
          ret = OB_INVALID_ARGUMENT;
          LOG_WARN("rle: rle run overflows", K(ret), K(nbytes), K(len), K(pos));
        } else {
          uint32_t v = 0;
          for (int i = 0; i < nbytes; ++i) {
            v |= static_cast<uint32_t>(buf[pos + i]) << (8 * i);
          }
          pos += nbytes;
          for (int64_t i = 0; OB_SUCC(ret) && i < run && values.count() < value_count; ++i) {
            if (OB_FAIL(values.push_back(v))) {
            }
          }
        }
      }
    }
    if (OB_SUCC(ret) && OB_UNLIKELY(values.count() != value_count)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("rle: not enough values", K(ret), K(values.count()), K(value_count));
    }
  }
  return ret;
}

int bit_width_of(const int64_t max_value)
{
  int width = 0;
  while (width < 32 && (static_cast<int64_t>(1) << width) <= max_value) {
    ++width;
  }
  return width;
}

// Converts INT96 timestamp (julian day + nanoseconds of day) to microseconds.
int64_t int96_to_usec(const uint8_t *p)
{
  uint64_t nanos = 0;
  for (int i = 0; i < 8; ++i) {
    nanos |= static_cast<uint64_t>(p[i]) << (8 * i);
  }
  const int64_t julian = static_cast<int64_t>(load_le32(p + 8));
  const int64_t usec = (julian - 2440588LL) * 86400000000LL +
                       static_cast<int64_t>(nanos / 1000ULL);
  return usec;
}

int64_t timestamp_unit_to_usec(const ObParquetColumn &col)
{
  int64_t factor = 1;
  if (9 == col.converted_type_) {
    factor = 1000;
  } else if (10 == col.converted_type_) {
    factor = 1;
  } else if (8 == col.logical_type_ || 7 == col.logical_type_) {
    if (1 == col.time_unit_) {
      factor = 1000;
    } else if (2 == col.time_unit_) {
      factor = 1;
    } else if (3 == col.time_unit_) {
      factor = 1;  // nanoseconds are divided below
    }
  }
  return factor;
}

int set_varchar_deep_copy(ObObj &obj, const char *ptr, const int64_t len, ObIAllocator &alloc)
{
  int ret = OB_SUCCESS;
  ObString src(static_cast<int32_t>(len), ptr);
  ObString dst;
  if (OB_UNLIKELY(len > INT32_MAX)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(ob_write_string(alloc, src, dst))) {
  } else {
    obj.set_varchar(dst);
    obj.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
  }
  return ret;
}

int decode_plain_values(const uint8_t *buf, const int64_t len, const int64_t count,
                        const ObParquetColumn &col, ObIAllocator &alloc,
                        ObIArray<ObObj> &out)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(count < 0)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (0 == count) {
    // nothing to do
  } else {
    switch (col.physical_type_) {
      case PARQUET_BOOLEAN: {
        const int64_t need = (count + 7) / 8;
        if (OB_UNLIKELY(need > len)) {
          ret = OB_INVALID_ARGUMENT;
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
          ObObj obj;
          obj.set_bool(0 != ((buf[i / 8] >> (i % 8)) & 1));
          if (OB_FAIL(out.push_back(obj))) {
          }
        }
        break;
      }
      case PARQUET_INT32: {
        if (OB_UNLIKELY(count * 4 > len)) {
          ret = OB_INVALID_ARGUMENT;
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
          const int32_t v = static_cast<int32_t>(load_le32(buf + i * 4));
          ObObj obj;
          if (OB_FILE_TYPE_DATE == col.logical_) {
            obj.set_date(v);
          } else if (OB_FILE_TYPE_DATETIME == col.logical_) {
            obj.set_datetime(static_cast<int64_t>(v) * timestamp_unit_to_usec(col));
          } else {
            obj.set_int(v);
          }
          if (OB_FAIL(out.push_back(obj))) {
          }
        }
        break;
      }
      case PARQUET_INT64: {
        if (OB_UNLIKELY(count * 8 > len)) {
          ret = OB_INVALID_ARGUMENT;
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
          const int64_t v = static_cast<int64_t>(load_le64(buf + i * 8));
          ObObj obj;
          if (OB_FILE_TYPE_DATETIME == col.logical_) {
            int64_t usec = v * timestamp_unit_to_usec(col);
            if (3 == col.time_unit_ && 8 == col.logical_type_) {
              usec = v / 1000;
            }
            obj.set_datetime(usec);
          } else if (OB_FILE_TYPE_DATE == col.logical_) {
            obj.set_date(static_cast<int32_t>(v));
          } else {
            obj.set_int(v);
          }
          if (OB_FAIL(out.push_back(obj))) {
          }
        }
        break;
      }
      case PARQUET_INT96: {
        if (OB_UNLIKELY(count * 12 > len)) {
          ret = OB_INVALID_ARGUMENT;
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
          ObObj obj;
          obj.set_datetime(int96_to_usec(buf + i * 12));
          if (OB_FAIL(out.push_back(obj))) {
          }
        }
        break;
      }
      case PARQUET_FLOAT: {
        if (OB_UNLIKELY(count * 4 > len)) {
          ret = OB_INVALID_ARGUMENT;
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
          const uint32_t bits = load_le32(buf + i * 4);
          float f = 0;
          MEMCPY(&f, &bits, sizeof(f));
          ObObj obj;
          obj.set_double(static_cast<double>(f));
          if (OB_FAIL(out.push_back(obj))) {
          }
        }
        break;
      }
      case PARQUET_DOUBLE: {
        if (OB_UNLIKELY(count * 8 > len)) {
          ret = OB_INVALID_ARGUMENT;
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
          const uint64_t bits = load_le64(buf + i * 8);
          double d = 0;
          MEMCPY(&d, &bits, sizeof(d));
          ObObj obj;
          obj.set_double(d);
          if (OB_FAIL(out.push_back(obj))) {
          }
        }
        break;
      }
      case PARQUET_BYTE_ARRAY: {
        int64_t pos = 0;
        for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
          if (OB_UNLIKELY(pos + 4 > len)) {
            ret = OB_INVALID_ARGUMENT;
            break;
          }
          const int64_t slen = static_cast<int64_t>(load_le32(buf + pos));
          pos += 4;
          if (OB_UNLIKELY(slen < 0 || pos + slen > len)) {
            ret = OB_INVALID_ARGUMENT;
          } else {
            ObObj obj;
            if (OB_FAIL(set_varchar_deep_copy(obj, reinterpret_cast<const char *>(buf + pos),
                                              slen, alloc))) {
            } else if (OB_FAIL(out.push_back(obj))) {
            }
            pos += slen;
          }
        }
        break;
      }
      case PARQUET_FIXED_LEN_BYTE_ARRAY: {
        const int64_t flen = col.type_length_;
        if (OB_UNLIKELY(flen <= 0 || count * flen > len)) {
          ret = OB_INVALID_ARGUMENT;
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < count; ++i) {
          ObObj obj;
          if (OB_FAIL(set_varchar_deep_copy(obj,
                                            reinterpret_cast<const char *>(buf + i * flen),
                                            flen, alloc))) {
          } else if (OB_FAIL(out.push_back(obj))) {
          }
        }
        break;
      }
      default:
        ret = OB_NOT_SUPPORTED;
        LOG_WARN("unsupported parquet physical type", K(ret), K(col.physical_type_));
        break;
    }
  }
  return ret;
}

// ---------------------------------------------------------------------------
// Footer parsing shared by schema inference and the scan reader
// ---------------------------------------------------------------------------
int read_file_exact(file_scan::ObLocalFile &file, const int64_t offset, uint8_t *buf,
                    const int64_t len)
{
  int ret = OB_SUCCESS;
  int64_t nread = 0;
  if (OB_UNLIKELY(len <= 0)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (OB_FAIL(file.seek(offset))) {
  } else if (OB_FAIL(file.read(reinterpret_cast<char *>(buf), len, nread))) {
  } else if (OB_UNLIKELY(nread != len)) {
    ret = OB_IO_ERROR;
    LOG_WARN("failed to read enough bytes", K(ret), K(offset), K(len), K(nread));
  }
  return ret;
}

int parse_parquet_footer(file_scan::ObLocalFile &file, common::ObIAllocator &alloc,
                         ObParquetFileMeta &meta, uint8_t *&footer_buf)
{
  int ret = OB_SUCCESS;
  const int64_t file_size = file.size();
  meta.schema_.reset();
  meta.columns_.reset();
  meta.chunks_.reset();
  meta.rg_first_chunk_.reset();
  meta.rg_num_rows_.reset();
  if (OB_UNLIKELY(file_size < 12)) {
    ret = OB_INVALID_ARGUMENT;
    LOG_WARN("parquet file is too small", K(ret), K(file_size));
  } else {
    uint8_t head[4];
    uint8_t tail[8];
    if (OB_FAIL(read_file_exact(file, 0, head, 4))) {
    } else if (0 != MEMCMP(head, "PAR1", 4)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("invalid parquet magic at file head", K(ret));
    } else if (OB_FAIL(read_file_exact(file, file_size - 8, tail, 8))) {
    } else if (0 != MEMCMP(tail + 4, "PAR1", 4)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("invalid parquet magic at file tail", K(ret));
    } else {
      const int64_t footer_len = static_cast<int64_t>(load_le32(tail));
      if (OB_UNLIKELY(footer_len <= 0 || footer_len > file_size - 12)) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("invalid parquet footer length", K(ret), K(footer_len), K(file_size));
      } else if (OB_ISNULL(footer_buf = static_cast<uint8_t *>(alloc.alloc(footer_len)))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
        LOG_WARN("failed to allocate parquet footer buffer", K(ret), K(footer_len));
      } else if (OB_FAIL(read_file_exact(file, file_size - 8 - footer_len, footer_buf,
                                         footer_len))) {
      } else {
        ObThriftCompactReader reader(footer_buf, footer_len);
        if (OB_FAIL(parse_file_metadata(reader, meta))) {
          LOG_WARN("failed to parse parquet footer metadata", K(ret));
        } else {
          int64_t idx = 0;
          if (OB_UNLIKELY(meta.schema_.empty())) {
            ret = OB_INVALID_ARGUMENT;
            LOG_WARN("empty parquet schema", K(ret));
          } else if (OB_FAIL(flatten_schema(meta.schema_, idx, 0, 0, true, meta.columns_))) {
          }
        }
      }
    }
  }
  return ret;
}

// ---------------------------------------------------------------------------
// File reader
// ---------------------------------------------------------------------------
class ObParquetFileReader : public ObFileScanReader
{
public:
  ObParquetFileReader()
      : info_(), file_(), rg_alloc_(), meta_(), values_(), dict_(),
        rg_idx_(0), row_pos_(0), rg_rows_(0), loaded_(false), opened_(false) {}
  virtual ~ObParquetFileReader() { close(); }
  virtual int open(const ObFileScanInfo &info, common::ObIAllocator &alloc) override
  {
    int ret = OB_SUCCESS;
    if (opened_) {
      close();
    }
    if (OB_FAIL(info_.assign(info, alloc))) {
    } else if (OB_FILE_FORMAT_PARQUET != info_.format_) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("parquet reader requires the parquet format", K(ret), K(info_.format_));
    } else if (OB_FAIL(file_.open(info_.path_))) {
    } else if (info_.file_size_ >= 0 && (file_.size() != info_.file_size_ ||
                                         file_.mtime_us() != info_.file_mtime_us_ ||
                                         file_.ino() != info_.file_ino_)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("local file was modified after the schema was inferred", K(ret), K(info_.path_));
    } else if (OB_FAIL(parse_footer(alloc))) {
    } else if (OB_FAIL(check_schema())) {
    } else {
      row_cnt_ = info_.row_cnt_;
      rg_idx_ = 0;
      row_pos_ = 0;
      rg_rows_ = 0;
      loaded_ = false;
      opened_ = true;
    }
    if (OB_FAIL(ret)) {
      file_.close();
    }
    return ret;
  }
  virtual int next_row(common::ObIAllocator &alloc,
                       common::ObIArray<ObObj> &cells) override
  {
    int ret = OB_SUCCESS;
    UNUSED(alloc);
    cells.reuse();
    if (OB_UNLIKELY(!opened_)) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      bool found = false;
      for (; OB_SUCC(ret) && !found;) {
        if (!loaded_) {
          if (OB_FAIL(load_row_group())) {
            if (OB_ITER_END != ret) {
              LOG_WARN("failed to load parquet row group", K(ret), K(rg_idx_));
            }
          }
        }
        if (OB_FAIL(ret)) {
        } else if (row_pos_ >= rg_rows_) {
          loaded_ = false;
          ++rg_idx_;
        } else {
          const int64_t colcnt = meta_.columns_.count();
          const int64_t base = row_pos_ * colcnt;
          for (int64_t i = 0; OB_SUCC(ret) && i < colcnt; ++i) {
            if (OB_FAIL(cells.push_back(values_.at(base + i)))) {
            }
          }
          if (OB_SUCC(ret)) {
            ++row_pos_;
            found = true;
          }
        }
      }
    }
    return ret;
  }
  virtual int rewind() override
  {
    int ret = OB_SUCCESS;
    if (OB_UNLIKELY(!opened_)) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      rg_idx_ = 0;
      row_pos_ = 0;
      rg_rows_ = 0;
      loaded_ = false;
      rg_alloc_.reset();
      values_.reset();
      dict_.reset();
    }
    return ret;
  }
  virtual void close() override
  {
    file_.close();
    opened_ = false;
    loaded_ = false;
  }
  virtual void destroy() override { this->~ObParquetFileReader(); }
private:
  int read_exact(const int64_t offset, uint8_t *buf, const int64_t len)
  {
    return read_file_exact(file_, offset, buf, len);
  }
  int parse_footer(common::ObIAllocator &alloc)
  {
    return parse_parquet_footer(file_, alloc, meta_, footer_buf_);
  }
  int check_schema()
  {
    int ret = OB_SUCCESS;
    if (OB_UNLIKELY(meta_.columns_.count() != info_.get_column_count() ||
                    info_.row_cnt_ != meta_.num_rows_)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("parquet file changed after schema inference", K(ret),
               "meta_columns", meta_.columns_.count(),
               "info_columns", info_.get_column_count(),
               "meta_rows", meta_.num_rows_, "info_rows", info_.row_cnt_);
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < meta_.columns_.count(); ++i) {
      const ObParquetColumn &col = meta_.columns_.at(i);
      if (OB_UNLIKELY(0 != col.name_.case_compare(info_.column_names_.at(i)))) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("parquet column name mismatch", K(ret), K(i), K(col.name_));
      } else if (OB_UNLIKELY(col.logical_ != info_.column_types_.at(i))) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("parquet column type mismatch", K(ret), K(i),
                 K(col.logical_), K(info_.column_types_.at(i)));
      } else if (OB_UNLIKELY(col.max_rep_level_ > 0)) {
        ret = OB_NOT_SUPPORTED;
        LOG_WARN("repeated parquet columns are not supported", K(ret), K(col.name_));
      }
    }
    return ret;
  }
  int load_row_group()
  {
    int ret = OB_SUCCESS;
    rg_alloc_.reset();
    values_.reset();
    dict_.reset();
    if (rg_idx_ >= meta_.rg_first_chunk_.count()) {
      ret = OB_ITER_END;
    } else {
      const int64_t first = meta_.rg_first_chunk_.at(rg_idx_);
      const int64_t last = (rg_idx_ + 1 < meta_.rg_first_chunk_.count())
          ? meta_.rg_first_chunk_.at(rg_idx_ + 1) : meta_.chunks_.count();
      const int64_t colcnt = meta_.columns_.count();
      rg_rows_ = meta_.rg_num_rows_.at(rg_idx_);
      if (OB_UNLIKELY(last - first != colcnt)) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("parquet row group column count mismatch", K(ret),
                 "chunks", last - first, K(colcnt));
      } else if (OB_UNLIKELY(rg_rows_ < 0 || (colcnt > 0 && rg_rows_ > INT64_MAX / colcnt))) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("invalid parquet row group size", K(ret), K(rg_rows_), K(colcnt));
      } else if (OB_FAIL(values_.reserve(rg_rows_ * colcnt))) {
      } else {
        ObObj null_obj;
        null_obj.set_null();
        for (int64_t i = 0; OB_SUCC(ret) && i < rg_rows_ * colcnt; ++i) {
          if (OB_FAIL(values_.push_back(null_obj))) {
          }
        }
        for (int64_t c = 0; OB_SUCC(ret) && c < colcnt; ++c) {
          if (OB_FAIL(decode_chunk(meta_.chunks_.at(first + c), c))) {
            LOG_WARN("failed to decode parquet column chunk", K(ret), K(c), K(rg_idx_));
          }
        }
      }
      if (OB_SUCC(ret)) {
        loaded_ = true;
        row_pos_ = 0;
      }
    }
    return ret;
  }
  int decode_chunk(const ObParquetChunk &chunk, const int64_t col_idx)
  {
    int ret = OB_SUCCESS;
    const ObParquetColumn &col = meta_.columns_.at(col_idx);
    dict_.reset();
    int64_t page_offset = chunk.dictionary_page_offset_ >= 0
        ? chunk.dictionary_page_offset_ : chunk.data_page_offset_;
    if (chunk.dictionary_page_offset_ >= 0 && chunk.data_page_offset_ >= 0 &&
        chunk.data_page_offset_ < page_offset) {
      page_offset = chunk.data_page_offset_;
    }
    const int64_t file_size = file_.size();
    int64_t values_seen = 0;
    int64_t rows_written = 0;
    while (OB_SUCC(ret) && values_seen < chunk.num_values_) {
      if (OB_UNLIKELY(page_offset < 0 || page_offset >= file_size)) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("parquet page offset out of range", K(ret), K(page_offset), K(file_size));
      } else {
        ObParquetPageHeader ph;
        int64_t header_len = 0;
        if (OB_FAIL(read_page_header(page_offset, ph, header_len))) {
        } else if (OB_UNLIKELY(ph.compressed_page_size_ < 0 ||
                               page_offset + header_len + ph.compressed_page_size_ > file_size)) {
          ret = OB_INVALID_ARGUMENT;
          LOG_WARN("invalid parquet page size", K(ret), K(page_offset), K(header_len),
                   K(ph.compressed_page_size_), K(file_size));
        } else {
          const int64_t data_offset = page_offset + header_len;
          if (PARQUET_PAGE_DICTIONARY == ph.type_) {
            if (OB_FAIL(decode_dictionary_page(ph, data_offset, chunk.codec_, col))) {
              LOG_WARN("failed to decode parquet dictionary page", K(ret), K(col_idx));
            }
          } else if (PARQUET_PAGE_DATA == ph.type_ || PARQUET_PAGE_DATA_V2 == ph.type_) {
            if (OB_FAIL(decode_data_page(ph, data_offset, col_idx, col, chunk, rows_written))) {
              LOG_WARN("failed to decode parquet data page", K(ret), K(col_idx));
            } else {
              values_seen += ph.num_values_;
            }
          }
          page_offset = data_offset + ph.compressed_page_size_;
        }
      }
    }
    if (OB_SUCC(ret) && OB_UNLIKELY(rows_written > rg_rows_)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("parquet column has more rows than the row group", K(ret),
               K(rows_written), K(rg_rows_));
    }
    return ret;
  }
  int read_page_header(const int64_t page_offset, ObParquetPageHeader &ph,
                       int64_t &header_len)
  {
    int ret = OB_SUCCESS;
    const int64_t remain = file_.size() - page_offset;
    if (OB_UNLIKELY(remain <= 0)) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      const int64_t want = remain < 64 * 1024 ? remain : 64 * 1024;
      uint8_t *buf = static_cast<uint8_t *>(rg_alloc_.alloc(want));
      if (OB_ISNULL(buf)) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
        LOG_WARN("failed to allocate page header buffer", K(ret), K(want));
      } else if (OB_FAIL(read_exact(page_offset, buf, want))) {
      } else {
        ObThriftCompactReader reader(buf, want);
        if (OB_FAIL(parse_page_header(reader, ph))) {
          LOG_WARN("failed to parse parquet page header", K(ret), K(page_offset));
        } else {
          header_len = reader.pos();
        }
      }
    }
    return ret;
  }
  int read_page_data(const int64_t offset, const int64_t len, const uint8_t *&data)
  {
    int ret = OB_SUCCESS;
    uint8_t *buf = nullptr;
    if (OB_UNLIKELY(len < 0)) {
      ret = OB_INVALID_ARGUMENT;
    } else if (0 == len) {
      data = nullptr;
    } else if (OB_ISNULL(buf = static_cast<uint8_t *>(rg_alloc_.alloc(len)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
      LOG_WARN("failed to allocate page buffer", K(ret), K(len));
    } else if (OB_FAIL(read_exact(offset, buf, len))) {
    } else {
      data = buf;
    }
    return ret;
  }
  int decompress_page(const int64_t codec, const uint8_t *src, const int64_t src_len,
                      const int64_t expected_len, const uint8_t *&out)
  {
    int ret = OB_SUCCESS;
    if (PARQUET_CODEC_NONE == codec) {
      if (OB_UNLIKELY(src_len != expected_len)) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("uncompressed page size mismatch", K(ret), K(src_len), K(expected_len));
      } else {
        out = src;
      }
    } else if (PARQUET_CODEC_SNAPPY == codec) {
      uint8_t *buf = nullptr;
      if (OB_UNLIKELY(expected_len < 0)) {
        ret = OB_INVALID_ARGUMENT;
      } else if (0 == expected_len) {
        out = nullptr;
      } else if (OB_ISNULL(buf = static_cast<uint8_t *>(rg_alloc_.alloc(expected_len)))) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else {
        int64_t written = 0;
        if (OB_FAIL(snappy_decompress(src, src_len, buf, expected_len, written))) {
          LOG_WARN("failed to decompress snappy page", K(ret), K(src_len), K(expected_len));
        } else if (OB_UNLIKELY(written != expected_len)) {
          ret = OB_INVALID_ARGUMENT;
        } else {
          out = buf;
        }
      }
    } else {
      ret = OB_NOT_SUPPORTED;
      LOG_WARN("unsupported parquet compression codec", K(ret), K(codec));
    }
    return ret;
  }
  int decode_dictionary_page(const ObParquetPageHeader &ph, const int64_t data_offset,
                             const int64_t codec, const ObParquetColumn &col)
  {
    int ret = OB_SUCCESS;
    const uint8_t *raw = nullptr;
    const uint8_t *data = nullptr;
    if (OB_FAIL(read_page_data(data_offset, ph.compressed_page_size_, raw))) {
    } else if (OB_FAIL(decompress_page(codec, raw, ph.compressed_page_size_,
                                       ph.uncompressed_page_size_, data))) {
    } else if (OB_FAIL(decode_plain_values(data, ph.uncompressed_page_size_,
                                           ph.dict_num_values_, col, rg_alloc_, dict_))) {
      LOG_WARN("failed to decode dictionary page values", K(ret), K(col.name_));
    }
    return ret;
  }
  int decode_dictionary_indices(const uint8_t *buf, const int64_t len, const int64_t count,
                                ObIArray<ObObj> &out)
  {
    int ret = OB_SUCCESS;
    if (0 == count) {
    } else if (OB_UNLIKELY(nullptr == buf || len < 1)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("empty dictionary index payload", K(ret), K(len), K(count));
    } else {
      const int bit_width = buf[0];
      ObSEArray<uint32_t, 64> indices;
      if (OB_FAIL(decode_hybrid(buf + 1, len - 1, bit_width, count, indices))) {
      }
      for (int64_t i = 0; OB_SUCC(ret) && i < indices.count(); ++i) {
        const uint32_t idx = indices.at(i);
        if (OB_UNLIKELY(idx >= static_cast<uint64_t>(dict_.count()))) {
          ret = OB_INVALID_ARGUMENT;
          LOG_WARN("dictionary index out of range", K(ret), K(idx), "dict", dict_.count());
        } else if (OB_FAIL(out.push_back(dict_.at(idx)))) {
        }
      }
    }
    return ret;
  }
  int decode_data_page(const ObParquetPageHeader &ph, const int64_t data_offset,
                       const int64_t col_idx, const ObParquetColumn &col,
                       const ObParquetChunk &chunk, int64_t &rows_written)
  {
    int ret = OB_SUCCESS;
    const int64_t num_values = ph.num_values_;
    if (OB_UNLIKELY(num_values < 0)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("invalid parquet page value count", K(ret), K(num_values));
    } else {
      const uint8_t *raw = nullptr;
      if (OB_FAIL(read_page_data(data_offset, ph.compressed_page_size_, raw))) {
      } else {
        ObSEArray<uint32_t, 64> defs;
        const uint8_t *values_ptr = nullptr;
        int64_t values_len = 0;
        if (PARQUET_PAGE_DATA_V2 == ph.type_) {
          const int64_t rep_len = ph.rep_levels_byte_length_;
          const int64_t def_len = ph.def_levels_byte_length_;
          if (OB_UNLIKELY(rep_len < 0 || def_len < 0 ||
                          rep_len + def_len > ph.compressed_page_size_ ||
                          rep_len + def_len > ph.uncompressed_page_size_)) {
            ret = OB_INVALID_ARGUMENT;
            LOG_WARN("invalid level byte lengths", K(ret), K(rep_len), K(def_len),
                     K(ph.compressed_page_size_), K(ph.uncompressed_page_size_));
          } else if (col.max_def_level_ > 0 &&
                     OB_FAIL(decode_hybrid(raw + rep_len, def_len,
                                           bit_width_of(col.max_def_level_),
                                           num_values, defs))) {
            LOG_WARN("failed to decode definition levels", K(ret), K(col.name_));
          } else {
            const uint8_t *data = raw + rep_len + def_len;
            const int64_t data_len = ph.compressed_page_size_ - rep_len - def_len;
            const int64_t expected = ph.uncompressed_page_size_ - rep_len - def_len;
            if (ph.is_compressed_) {
              if (OB_FAIL(decompress_page(chunk.codec_, data, data_len, expected,
                                          values_ptr))) {
              } else {
                values_len = expected;
              }
            } else if (OB_UNLIKELY(data_len != expected)) {
              ret = OB_INVALID_ARGUMENT;
              LOG_WARN("v2 page data length mismatch", K(ret), K(data_len), K(expected));
            } else {
              values_ptr = data;
              values_len = data_len;
            }
          }
        } else {
          const uint8_t *page = nullptr;
          if (OB_FAIL(decompress_page(chunk.codec_, raw, ph.compressed_page_size_,
                                      ph.uncompressed_page_size_, page))) {
          } else {
            int64_t pos = 0;
            const int64_t total = ph.uncompressed_page_size_;
            if (col.max_rep_level_ > 0) {
              if (OB_UNLIKELY(pos + 4 > total)) {
                ret = OB_INVALID_ARGUMENT;
              } else {
                const int64_t rep_len = static_cast<int64_t>(load_le32(page + pos));
                pos += 4;
                if (OB_UNLIKELY(rep_len < 0 || pos + rep_len > total)) {
                  ret = OB_INVALID_ARGUMENT;
                } else {
                  pos += rep_len;
                }
              }
            } else if (total >= 4 && 0 == load_le32(page)) {
              // parquet-mr writes an empty repetition level section
              pos += 4;
            }
            if (OB_FAIL(ret)) {
            } else if (col.max_def_level_ > 0) {
              if (OB_UNLIKELY(pos + 4 > total)) {
                ret = OB_INVALID_ARGUMENT;
              } else {
                const int64_t def_len = static_cast<int64_t>(load_le32(page + pos));
                pos += 4;
                if (OB_UNLIKELY(def_len < 0 || pos + def_len > total)) {
                  ret = OB_INVALID_ARGUMENT;
                } else if (OB_FAIL(decode_hybrid(page + pos, def_len,
                                                 bit_width_of(col.max_def_level_),
                                                 num_values, defs))) {
                  LOG_WARN("failed to decode definition levels", K(ret), K(col.name_));
                } else {
                  pos += def_len;
                }
              }
            }
            if (OB_SUCC(ret)) {
              values_ptr = page + pos;
              values_len = total - pos;
            }
          }
        }
        if (OB_SUCC(ret)) {
          int64_t non_null = num_values;
          if (col.max_def_level_ > 0) {
            non_null = 0;
            for (int64_t i = 0; i < defs.count(); ++i) {
              if (static_cast<int64_t>(defs.at(i)) == col.max_def_level_) {
                ++non_null;
              }
            }
          }
          ObSEArray<ObObj, 64> page_values;
          if (non_null > 0) {
            if (PARQUET_ENCODING_PLAIN == ph.encoding_) {
              if (OB_FAIL(decode_plain_values(values_ptr, values_len, non_null, col,
                                              rg_alloc_, page_values))) {
                LOG_WARN("failed to decode plain values", K(ret), K(col.name_));
              }
            } else if (PARQUET_ENCODING_PLAIN_DICTIONARY == ph.encoding_ ||
                       PARQUET_ENCODING_RLE_DICTIONARY == ph.encoding_) {
              if (OB_FAIL(decode_dictionary_indices(values_ptr, values_len, non_null,
                                                    page_values))) {
              }
            } else {
              ret = OB_NOT_SUPPORTED;
              LOG_WARN("unsupported parquet value encoding", K(ret), K(ph.encoding_),
                       K(col.name_));
            }
          }
          if (OB_SUCC(ret)) {
            const int64_t colcnt = meta_.columns_.count();
            if (OB_UNLIKELY(rows_written + num_values > rg_rows_)) {
              ret = OB_INVALID_ARGUMENT;
              LOG_WARN("parquet page overflows the row group", K(ret), K(rows_written),
                       K(num_values), K(rg_rows_));
            }
            int64_t v = 0;
            for (int64_t i = 0; OB_SUCC(ret) && i < num_values; ++i) {
              const int64_t idx = (rows_written + i) * colcnt + col_idx;
              if (col.max_def_level_ > 0 &&
                  static_cast<int64_t>(defs.at(i)) != col.max_def_level_) {
                values_.at(idx).set_null();
              } else if (OB_UNLIKELY(v >= page_values.count())) {
                ret = OB_INVALID_ARGUMENT;
                LOG_WARN("not enough decoded values", K(ret), K(v), K(page_values.count()));
              } else {
                values_.at(idx) = page_values.at(v);
                ++v;
              }
            }
            if (OB_SUCC(ret)) {
              rows_written += num_values;
            }
          }
        }
      }
    }
    return ret;
  }
  ObFileScanInfo info_;
  file_scan::ObLocalFile file_;
  ObArenaAllocator rg_alloc_;
  ObParquetFileMeta meta_;
  ObSEArray<ObObj, 64> values_;
  ObSEArray<ObObj, 64> dict_;
  uint8_t *footer_buf_ = nullptr;
  int64_t rg_idx_;
  int64_t row_pos_;
  int64_t rg_rows_;
  bool loaded_;
  bool opened_;
};

} // end anonymous namespace

int create_parquet_file_reader(common::ObIAllocator &alloc, ObFileScanReader *&reader)
{
  int ret = OB_SUCCESS;
  reader = nullptr;
  void *buf = alloc.alloc(sizeof(ObParquetFileReader));
  if (OB_ISNULL(buf)) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
    LOG_WARN("failed to allocate parquet file reader", K(ret));
  } else {
    reader = new (buf) ObParquetFileReader();
  }
  return ret;
}

int infer_parquet_file_schema(common::ObIAllocator &alloc,
                              const common::ObString &path,
                              ObFileScanInfo &info)
{
  int ret = OB_SUCCESS;
  file_scan::ObLocalFile file;
  if (OB_FAIL(file.open(path))) {
  } else {
    ObParquetFileMeta meta;
    uint8_t *footer_buf = nullptr;
    if (OB_FAIL(parse_parquet_footer(file, alloc, meta, footer_buf))) {
    } else {
      info.row_cnt_ = meta.num_rows_;
      info.file_size_ = file.size();
      info.file_mtime_us_ = file.mtime_us();
      info.file_ino_ = file.ino();
      info.file_dev_ = file.dev();
      for (int64_t i = 0; OB_SUCC(ret) && i < meta.columns_.count(); ++i) {
        const ObParquetColumn &col = meta.columns_.at(i);
        if (OB_FAIL(info.add_column(col.name_, col.logical_, col.max_def_level_ > 0, 0))) {
        }
      }
    }
  }
  return ret;
}

} // end namespace sql
} // end namespace oceanbase
