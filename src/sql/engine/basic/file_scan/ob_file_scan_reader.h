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

#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include "common/object/ob_object.h"
#include "sql/engine/basic/file_scan/ob_file_scan_define.h"

namespace oceanbase
{
namespace sql
{
namespace file_scan
{

// Buffered sequential reader over a local file. All IO errors are converted
// to OceanBase error codes; the class never throws.
class ObLocalFile
{
public:
  ObLocalFile() : fd_(-1), file_size_(-1), mtime_us_(-1), ino_(-1), dev_(-1) {}
  ~ObLocalFile() { close(); }
  int open(const common::ObString &path);
  // Reads up to len bytes. Returns the number of bytes read, 0 at EOF.
  int read(char *buf, const int64_t len, int64_t &nread);
  int seek(const int64_t offset);
  int64_t size() const { return file_size_; }
  int64_t mtime_us() const { return mtime_us_; }
  int64_t ino() const { return ino_; }
  int64_t dev() const { return dev_; }
  int refresh_stat();
  void close();
  bool is_opened() const { return fd_ >= 0; }
  const char *error_path() const { return path_.ptr(); }
private:
  int fd_;
  int64_t file_size_;
  int64_t mtime_us_;
  int64_t ino_;
  int64_t dev_;
  common::ObString path_;
};

struct ObFileStatInfo
{
  ObFileStatInfo() : size_(-1), mtime_us_(-1), ino_(-1), dev_(-1), is_dir_(false), exists_(false) {}
  int64_t size_;
  int64_t mtime_us_;
  int64_t ino_;
  int64_t dev_;
  bool is_dir_;
  bool exists_;
};

int stat_local_path(const common::ObString &path, ObFileStatInfo &stat_info);

// Text to value conversions shared by schema inference and the scan operator.
int parse_bool_text(const char *ptr, const int64_t len, bool &value);
int parse_int_text(const char *ptr, const int64_t len, int64_t &value, bool &overflow);
int parse_double_text(const char *ptr, const int64_t len, double &value, bool &is_numeric);
int parse_date_text(const char *ptr, const int64_t len, int32_t &value, bool &is_date);
int parse_datetime_text(const char *ptr, const int64_t len, int64_t &value, bool &is_datetime);

int widen_logical_type(const int64_t cur_type, const int64_t new_type, int64_t &result_type);

} // namespace file_scan

class ObFileScanReader
{
public:
  ObFileScanReader() : row_cnt_(-1) {}
  virtual ~ObFileScanReader() {}
  // Open the file described by info; the schema inside info must be the one
  // inferred for this file.
  virtual int open(const ObFileScanInfo &info, common::ObIAllocator &alloc) = 0;
  // Produce one row as target-typed ObObj cells (string buffers are valid
  // until the next call). Returns OB_ITER_END when the file is exhausted.
  virtual int next_row(common::ObIAllocator &alloc,
                       common::ObIArray<common::ObObj> &cells) = 0;
  virtual int rewind() = 0;
  virtual void close() = 0;
  // Readers are created inside the execution allocator, which never invokes
  // destructors; destroy() must be called explicitly before the allocator dies.
  virtual void destroy() = 0;
  int64_t get_row_cnt() const { return row_cnt_; }
protected:
  int64_t row_cnt_;
};

int create_file_scan_reader(const int64_t format, common::ObIAllocator &alloc,
                            ObFileScanReader *&reader);

// Full-file schema inference. Also fills file identity used for change detection.
int infer_file_scan_schema(common::ObIAllocator &alloc,
                           const common::ObString &path,
                           const ObFileScanOptions &options,
                           ObFileScanInfo &info);

// Expand a glob pattern (supports *, ? and [...] in the last path component).
int expand_glob_paths(common::ObIAllocator &alloc,
                      const common::ObString &pattern,
                      common::ObIArray<common::ObString> &files);

} // namespace sql
} // namespace oceanbase
