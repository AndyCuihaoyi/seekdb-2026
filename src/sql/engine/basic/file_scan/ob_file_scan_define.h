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

#pragma once

#include "lib/ob_define.h"
#include "lib/string/ob_string.h"
#include "lib/container/ob_se_array.h"
#include "lib/allocator/ob_allocator.h"
#include "common/object/ob_obj_type.h"

namespace oceanbase
{
namespace sql
{

// File formats supported by the file-as-table feature.
enum ObFileFormat : int64_t
{
  OB_FILE_FORMAT_CSV = 0,
  OB_FILE_FORMAT_JSONL = 1,
  OB_FILE_FORMAT_PARQUET = 2,
  OB_FILE_FORMAT_GLOB = 3,
  OB_FILE_FORMAT_MAX
};

// Logical column types produced by schema inference. They are mapped to
// OceanBase object types when column expressions are built.
enum ObFileLogicalType : int64_t
{
  OB_FILE_TYPE_NULL = 0,   // all values are NULL
  OB_FILE_TYPE_BOOLEAN = 1,
  OB_FILE_TYPE_BIGINT = 2,
  OB_FILE_TYPE_DOUBLE = 3,
  OB_FILE_TYPE_VARCHAR = 4,
  OB_FILE_TYPE_DATE = 5,
  OB_FILE_TYPE_DATETIME = 6,
  OB_FILE_TYPE_MAX
};

const char *ob_file_logical_type_name(const int64_t type);
common::ObObjType ob_file_logical_type_to_obj_type(const int64_t type);

struct ObFileScanColumn
{
  ObFileScanColumn() : name_(), type_(OB_FILE_TYPE_NULL), nullable_(true), null_count_(0) {}
  TO_STRING_KV(K_(name), K_(type), K_(nullable), K_(null_count));
  common::ObString name_;
  int64_t type_;
  bool nullable_;
  int64_t null_count_;
};

struct ObFileScanOptions
{
  ObFileScanOptions()
    : format_(OB_FILE_FORMAT_CSV),
      delimiter_(','),
      quote_('"'),
      escape_('"'),
      has_header_(true),
      null_str_(),
      json_format_()
  {}
  int64_t format_;
  char delimiter_;
  char quote_;
  char escape_;
  bool has_header_;
  common::ObString null_str_;   // cell text interpreted as NULL, empty means "empty field only"
  common::ObString json_format_; // newline_delimited / array
};

// Everything needed to re-open and scan a file: path, options, inferred
// schema and the file identity captured when the schema was inferred.
// The same layout is used by the resolver (schema inference) and the
// physical operator spec (execution).
struct ObFileScanInfo
{
  OB_UNIS_VERSION(1);
public:
  ObFileScanInfo()
    : format_(OB_FILE_FORMAT_CSV),
      path_(),
      delimiter_(','),
      quote_('"'),
      escape_('"'),
      has_header_(true),
      null_str_(),
      json_format_(),
      column_names_(),
      column_types_(),
      column_nullable_(),
      column_null_count_(),
      row_cnt_(0),
      file_size_(-1),
      file_mtime_us_(-1),
      file_ino_(-1),
      file_dev_(-1)
  {}
  void reset();
  int assign(const ObFileScanInfo &other, common::ObIAllocator &alloc);
  int add_column(const common::ObString &name, const int64_t type, const bool nullable,
                 const int64_t null_count);
  int64_t get_column_count() const { return column_types_.count(); }
  TO_STRING_KV(K_(format), K_(path), K_(delimiter), K_(quote), K_(escape), K_(has_header),
               K_(row_cnt), K_(column_names), K_(column_types), K_(column_nullable));
  int64_t format_;
  common::ObString path_;
  char delimiter_;
  char quote_;
  char escape_;
  bool has_header_;
  common::ObString null_str_;
  common::ObString json_format_;
  common::ObSEArray<common::ObString, 8, common::ModulePageAllocator, true> column_names_;
  common::ObSEArray<int64_t, 8, common::ModulePageAllocator, true> column_types_;
  common::ObSEArray<int64_t, 8, common::ModulePageAllocator, true> column_nullable_;
  common::ObSEArray<int64_t, 8, common::ModulePageAllocator, true> column_null_count_;
  int64_t row_cnt_;
  int64_t file_size_;
  int64_t file_mtime_us_;
  int64_t file_ino_;
  int64_t file_dev_;
};

} // end namespace sql
} // end namespace oceanbase
