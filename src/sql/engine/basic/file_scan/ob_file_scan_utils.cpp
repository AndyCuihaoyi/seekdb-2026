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

#include <cerrno>
#include <climits>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fnmatch.h>
#include "lib/allocator/page_arena.h"
#include "lib/string/ob_sql_string.h"
#include "lib/string/ob_string_buffer.h"
#include "lib/utility/ob_print_utils.h"
#include "lib/container/ob_se_array.h"
#include "common/timezone/ob_time_convert.h"
#include "sql/engine/basic/file_scan/ob_file_scan_reader.h"
#include "sql/engine/basic/file_scan/ob_parquet_reader.h"

namespace oceanbase
{
using namespace common;
namespace sql
{

OB_SERIALIZE_MEMBER(ObFileScanInfo,
                    format_,
                    path_,
                    delimiter_,
                    quote_,
                    escape_,
                    has_header_,
                    null_str_,
                    json_format_,
                    column_names_,
                    column_types_,
                    column_nullable_,
                    column_null_count_,
                    row_cnt_,
                    file_size_,
                    file_mtime_us_,
                    file_ino_,
                    file_dev_);

namespace file_scan
{

int ObLocalFile::open(const common::ObString &path)
{
  int ret = OB_SUCCESS;
  char path_buf[PATH_MAX + 1];
  close();
  if (OB_UNLIKELY(path.empty()) || OB_UNLIKELY(path.length() > PATH_MAX)) {
    ret = OB_INVALID_ARGUMENT;
    LOG_WARN("invalid file path", K(ret), K(path));
  } else {
    MEMCPY(path_buf, path.ptr(), path.length());
    path_buf[path.length()] = '\0';
    int fd = -1;
    do {
      fd = ::open(path_buf, O_RDONLY);
    } while (fd < 0 && EINTR == errno);
    if (fd < 0) {
      ret = (ENOENT == errno || ENOTDIR == errno) ? OB_FILE_NOT_EXIST : OB_IO_ERROR;
      LOG_WARN("failed to open file", K(ret), "errno", errno, K(path));
    } else {
      fd_ = fd;
      path_ = path;
      struct stat file_stat;
      if (0 != ::fstat(fd_, &file_stat)) {
        ret = OB_IO_ERROR;
        LOG_WARN("failed to stat file", K(ret), "errno", errno, K(path));
        close();
      } else if (!S_ISREG(file_stat.st_mode)) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("not a regular file", K(ret), K(path));
        close();
      } else {
        file_size_ = file_stat.st_size;
        mtime_us_ = static_cast<int64_t>(file_stat.st_mtim.tv_sec) * 1000000LL +
                    static_cast<int64_t>(file_stat.st_mtim.tv_nsec) / 1000LL;
        ino_ = static_cast<int64_t>(file_stat.st_ino);
        dev_ = static_cast<int64_t>(file_stat.st_dev);
      }
    }
  }
  return ret;
}

int ObLocalFile::read(char *buf, const int64_t len, int64_t &nread)
{
  int ret = OB_SUCCESS;
  nread = 0;
  if (OB_UNLIKELY(fd_ < 0 || nullptr == buf || len <= 0)) {
    ret = OB_INVALID_ARGUMENT;
    LOG_WARN("invalid argument for file read", K(ret), K_(fd), KP(buf), K(len));
  } else {
    int64_t total = 0;
    while (OB_SUCC(ret) && total < len) {
      const ssize_t n = ::read(fd_, buf + total, static_cast<size_t>(len - total));
      if (n < 0) {
        if (EINTR == errno) {
          continue;
        }
        ret = OB_IO_ERROR;
        LOG_WARN("failed to read file", K(ret), "errno", errno);
      } else if (0 == n) {
        break;
      } else {
        total += n;
      }
    }
    nread = total;
  }
  return ret;
}

int ObLocalFile::seek(const int64_t offset)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(fd_ < 0 || offset < 0)) {
    ret = OB_INVALID_ARGUMENT;
    LOG_WARN("invalid argument for file seek", K(ret), K_(fd), K(offset));
  } else if (static_cast<int64_t>(::lseek(fd_, static_cast<off_t>(offset), SEEK_SET)) != offset) {
    ret = OB_IO_ERROR;
    LOG_WARN("failed to seek file", K(ret), K(offset), "errno", errno);
  }
  return ret;
}

int ObLocalFile::refresh_stat()
{
  int ret = OB_SUCCESS;
  struct stat file_stat;
  if (OB_UNLIKELY(fd_ < 0)) {
    ret = OB_INVALID_ARGUMENT;
  } else if (0 != ::fstat(fd_, &file_stat)) {
    ret = OB_IO_ERROR;
    LOG_WARN("failed to stat file", K(ret), "errno", errno);
  } else {
    file_size_ = file_stat.st_size;
    mtime_us_ = static_cast<int64_t>(file_stat.st_mtim.tv_sec) * 1000000LL +
                static_cast<int64_t>(file_stat.st_mtim.tv_nsec) / 1000LL;
    ino_ = static_cast<int64_t>(file_stat.st_ino);
    dev_ = static_cast<int64_t>(file_stat.st_dev);
  }
  return ret;
}

void ObLocalFile::close()
{
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
  file_size_ = -1;
  mtime_us_ = -1;
  ino_ = -1;
  dev_ = -1;
  path_.reset();
}

int stat_local_path(const common::ObString &path, ObFileStatInfo &stat_info)
{
  int ret = OB_SUCCESS;
  char path_buf[PATH_MAX + 1];
  stat_info = ObFileStatInfo();
  if (OB_UNLIKELY(path.empty()) || OB_UNLIKELY(path.length() > PATH_MAX)) {
    ret = OB_INVALID_ARGUMENT;
  } else {
    MEMCPY(path_buf, path.ptr(), path.length());
    path_buf[path.length()] = '\0';
    struct stat file_stat;
    if (0 != ::stat(path_buf, &file_stat)) {
      stat_info.exists_ = false;
      if (ENOENT != errno && ENOTDIR != errno) {
        ret = OB_IO_ERROR;
        LOG_WARN("failed to stat path", K(ret), "errno", errno, K(path));
      }
    } else {
      stat_info.exists_ = true;
      stat_info.is_dir_ = S_ISDIR(file_stat.st_mode);
      stat_info.size_ = file_stat.st_size;
      stat_info.mtime_us_ = static_cast<int64_t>(file_stat.st_mtim.tv_sec) * 1000000LL +
                            static_cast<int64_t>(file_stat.st_mtim.tv_nsec) / 1000LL;
      stat_info.ino_ = static_cast<int64_t>(file_stat.st_ino);
      stat_info.dev_ = static_cast<int64_t>(file_stat.st_dev);
    }
  }
  return ret;
}

int parse_bool_text(const char *ptr, const int64_t len, bool &value)
{
  int ret = OB_SUCCESS;
  if (4 == len && 0 == strncasecmp(ptr, "true", 4)) {
    value = true;
  } else if (5 == len && 0 == strncasecmp(ptr, "false", 5)) {
    value = false;
  } else if (1 == len && '1' == ptr[0]) {
    value = true;
  } else if (1 == len && '0' == ptr[0]) {
    value = false;
  } else {
    ret = OB_INVALID_ARGUMENT;
  }
  return ret;
}

int parse_int_text(const char *ptr, const int64_t len, int64_t &value, bool &overflow)
{
  int ret = OB_SUCCESS;
  overflow = false;
  value = 0;
  if (OB_UNLIKELY(nullptr == ptr || len <= 0)) {
    ret = OB_INVALID_ARGUMENT;
  } else {
    int64_t i = 0;
    bool negative = false;
    if ('+' == ptr[0] || '-' == ptr[0]) {
      negative = ('-' == ptr[0]);
      i = 1;
    }
    if (OB_UNLIKELY(i >= len)) {
      ret = OB_INVALID_ARGUMENT;
    }
    const uint64_t limit = negative
        ? static_cast<uint64_t>(INT64_MAX) + 1ULL
        : static_cast<uint64_t>(INT64_MAX);
    uint64_t acc = 0;
    for (; OB_SUCC(ret) && i < len; ++i) {
      const char c = ptr[i];
      if (c < '0' || c > '9') {
        ret = OB_INVALID_ARGUMENT;
      } else if (overflow) {
        // keep validating
      } else {
        const uint64_t digit = static_cast<uint64_t>(c - '0');
        if (acc > (limit - digit) / 10ULL) {
          overflow = true;
        } else {
          acc = acc * 10ULL + digit;
        }
      }
    }
    if (OB_SUCC(ret) && !overflow) {
      value = negative ? -static_cast<int64_t>(acc) : static_cast<int64_t>(acc);
    }
  }
  return ret;
}

int parse_double_text(const char *ptr, const int64_t len, double &value, bool &is_numeric)
{
  int ret = OB_SUCCESS;
  is_numeric = false;
  value = 0;
  if (OB_UNLIKELY(nullptr == ptr || len <= 0)) {
    ret = OB_INVALID_ARGUMENT;
  } else {
    int64_t i = 0;
    bool has_digit = false;
    bool valid = true;
    if ('+' == ptr[i] || '-' == ptr[i]) {
      ++i;
    }
    while (i < len && ptr[i] >= '0' && ptr[i] <= '9') {
      has_digit = true;
      ++i;
    }
    if (i < len && '.' == ptr[i]) {
      ++i;
      while (i < len && ptr[i] >= '0' && ptr[i] <= '9') {
        has_digit = true;
        ++i;
      }
    }
    if (!has_digit) {
      valid = false;
    }
    if (valid && i < len && ('e' == ptr[i] || 'E' == ptr[i])) {
      ++i;
      if (i < len && ('+' == ptr[i] || '-' == ptr[i])) {
        ++i;
      }
      bool exp_digit = false;
      while (i < len && ptr[i] >= '0' && ptr[i] <= '9') {
        exp_digit = true;
        ++i;
      }
      if (!exp_digit) {
        valid = false;
      }
    }
    if (valid && i != len) {
      valid = false;
    }
    if (valid && len < 512) {
      char buf[512];
      MEMCPY(buf, ptr, len);
      buf[len] = '\0';
      char *end_ptr = nullptr;
      errno = 0;
      value = strtod(buf, &end_ptr);
      if (ERANGE == errno || nullptr == end_ptr || end_ptr != buf + len) {
        valid = false;
      } else {
        is_numeric = true;
      }
    } else {
      valid = false;
    }
  }
  return ret;
}

int parse_date_text(const char *ptr, const int64_t len, int32_t &value, bool &is_date)
{
  int ret = OB_SUCCESS;
  is_date = false;
  value = 0;
  // strict shape: YYYY-MM-DD
  if (nullptr != ptr && 10 == len && '-' == ptr[4] && '-' == ptr[7]) {
    bool digits_ok = true;
    for (int64_t i = 0; digits_ok && i < len; ++i) {
      if (4 == i || 7 == i) {
        continue;
      }
      digits_ok = (ptr[i] >= '0' && ptr[i] <= '9');
    }
    if (digits_ok) {
      int32_t date_val = 0;
      const int tmp_ret = ObTimeConverter::str_to_date(ObString(static_cast<int32_t>(len), ptr),
                                                      date_val, 0);
      if (OB_SUCCESS == tmp_ret) {
        value = date_val;
        is_date = true;
      }
    }
  }
  return ret;
}

int parse_datetime_text(const char *ptr, const int64_t len, int64_t &value, bool &is_datetime)
{
  int ret = OB_SUCCESS;
  is_datetime = false;
  value = 0;
  // strict shape: YYYY-MM-DD[ T]HH:MM:SS[.ffffff]
  if (nullptr != ptr && len >= 19 && len < 64 && '-' == ptr[4] && '-' == ptr[7] &&
      (' ' == ptr[10] || 'T' == ptr[10]) && ':' == ptr[13] && ':' == ptr[16]) {
    bool digits_ok = true;
    for (int64_t i = 0; digits_ok && i < 19; ++i) {
      if (4 == i || 7 == i || 10 == i || 13 == i || 16 == i) {
        continue;
      }
      digits_ok = (ptr[i] >= '0' && ptr[i] <= '9');
    }
    if (digits_ok && len > 19) {
      const int64_t frac_len = len - 20;
      digits_ok = '.' == ptr[19] && frac_len > 0 && frac_len <= 6;
      for (int64_t i = 20; digits_ok && i < len; ++i) {
        digits_ok = (ptr[i] >= '0' && ptr[i] <= '9');
      }
    }
    if (digits_ok) {
      char buf[64];
      MEMCPY(buf, ptr, len);
      buf[len] = '\0';
      if ('T' == buf[10]) {
        buf[10] = ' ';
      }
      const ObTimeConvertCtx cvrt_ctx(nullptr, false);
      int64_t datetime_val = 0;
      const int tmp_ret = ObTimeConverter::str_to_datetime(
          ObString(static_cast<int32_t>(len), buf), cvrt_ctx, datetime_val);
      if (OB_SUCCESS == tmp_ret) {
        value = datetime_val;
        is_datetime = true;
      }
    }
  }
  return ret;
}

int widen_logical_type(const int64_t cur_type, const int64_t new_type, int64_t &result_type)
{
  int ret = OB_SUCCESS;
  if (OB_FILE_TYPE_NULL == new_type || cur_type == new_type) {
    result_type = cur_type;
  } else if (OB_FILE_TYPE_NULL == cur_type) {
    result_type = new_type;
  } else if ((OB_FILE_TYPE_BIGINT == cur_type && OB_FILE_TYPE_DOUBLE == new_type) ||
             (OB_FILE_TYPE_DOUBLE == cur_type && OB_FILE_TYPE_BIGINT == new_type)) {
    result_type = OB_FILE_TYPE_DOUBLE;
  } else if ((OB_FILE_TYPE_DATE == cur_type && OB_FILE_TYPE_DATETIME == new_type) ||
             (OB_FILE_TYPE_DATETIME == cur_type && OB_FILE_TYPE_DATE == new_type)) {
    result_type = OB_FILE_TYPE_DATETIME;
  } else {
    result_type = OB_FILE_TYPE_VARCHAR;
  }
  return ret;
}

} // namespace file_scan

const char *ob_file_logical_type_name(const int64_t type)
{
  const char *name = "VARCHAR";
  switch (type) {
    case OB_FILE_TYPE_NULL: name = "NULL"; break;
    case OB_FILE_TYPE_BOOLEAN: name = "BOOLEAN"; break;
    case OB_FILE_TYPE_BIGINT: name = "BIGINT"; break;
    case OB_FILE_TYPE_DOUBLE: name = "DOUBLE"; break;
    case OB_FILE_TYPE_VARCHAR: name = "VARCHAR"; break;
    case OB_FILE_TYPE_DATE: name = "DATE"; break;
    case OB_FILE_TYPE_DATETIME: name = "DATETIME"; break;
    default: name = "VARCHAR"; break;
  }
  return name;
}

common::ObObjType ob_file_logical_type_to_obj_type(const int64_t type)
{
  ObObjType obj_type = ObVarcharType;
  switch (type) {
    case OB_FILE_TYPE_NULL: obj_type = ObNullType; break;
    case OB_FILE_TYPE_BOOLEAN: obj_type = ObTinyIntType; break;
    case OB_FILE_TYPE_BIGINT: obj_type = ObIntType; break;
    case OB_FILE_TYPE_DOUBLE: obj_type = ObDoubleType; break;
    case OB_FILE_TYPE_VARCHAR: obj_type = ObVarcharType; break;
    case OB_FILE_TYPE_DATE: obj_type = ObDateType; break;
    case OB_FILE_TYPE_DATETIME: obj_type = ObDateTimeType; break;
    default: obj_type = ObVarcharType; break;
  }
  return obj_type;
}

void ObFileScanInfo::reset()
{
  format_ = OB_FILE_FORMAT_CSV;
  path_.reset();
  delimiter_ = ',';
  quote_ = '"';
  escape_ = '"';
  has_header_ = true;
  null_str_.reset();
  json_format_.reset();
  column_names_.reset();
  column_types_.reset();
  column_nullable_.reset();
  column_null_count_.reset();
  row_cnt_ = 0;
  file_size_ = -1;
  file_mtime_us_ = -1;
  file_ino_ = -1;
  file_dev_ = -1;
}

int ObFileScanInfo::assign(const ObFileScanInfo &other, common::ObIAllocator &alloc)
{
  int ret = OB_SUCCESS;
  reset();
  format_ = other.format_;
  delimiter_ = other.delimiter_;
  quote_ = other.quote_;
  escape_ = other.escape_;
  has_header_ = other.has_header_;
  row_cnt_ = other.row_cnt_;
  file_size_ = other.file_size_;
  file_mtime_us_ = other.file_mtime_us_;
  file_ino_ = other.file_ino_;
  file_dev_ = other.file_dev_;
  if (OB_FAIL(ob_write_string(alloc, other.path_, path_))) {
  } else if (OB_FAIL(ob_write_string(alloc, other.null_str_, null_str_))) {
  } else if (OB_FAIL(ob_write_string(alloc, other.json_format_, json_format_))) {
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < other.column_names_.count(); ++i) {
    ObString name;
    if (OB_FAIL(ob_write_string(alloc, other.column_names_.at(i), name))) {
    } else if (OB_FAIL(column_names_.push_back(name))) {
    }
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < other.column_types_.count(); ++i) {
    if (OB_FAIL(column_types_.push_back(other.column_types_.at(i)))) {
    }
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < other.column_nullable_.count(); ++i) {
    if (OB_FAIL(column_nullable_.push_back(other.column_nullable_.at(i)))) {
    }
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < other.column_null_count_.count(); ++i) {
    if (OB_FAIL(column_null_count_.push_back(other.column_null_count_.at(i)))) {
    }
  }
  return ret;
}

int ObFileScanInfo::add_column(const common::ObString &name, const int64_t type,
                               const bool nullable, const int64_t null_count)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(column_names_.push_back(name)) ||
      OB_FAIL(column_types_.push_back(type)) ||
      OB_FAIL(column_nullable_.push_back(nullable ? 1 : 0)) ||
      OB_FAIL(column_null_count_.push_back(null_count))) {
  }
  return ret;
}

namespace
{

const int64_t FILE_SCAN_BUFFER_SIZE = 64 * 1024;

// Buffered byte reader over a local file. Tracks the absolute file offset so
// that a scanner can be rewound cheaply.
class ObFileByteReader
{
public:
  ObFileByteReader()
    : file_(nullptr), buf_(nullptr), capacity_(0), pos_(0), end_(0),
      buf_start_offset_(0), eof_(false) {}
  int init(file_scan::ObLocalFile *file, common::ObIAllocator &alloc)
  {
    int ret = OB_SUCCESS;
    if (OB_ISNULL(file)) {
      ret = OB_INVALID_ARGUMENT;
    } else if (OB_ISNULL(buf_ = static_cast<char *>(alloc.alloc(FILE_SCAN_BUFFER_SIZE)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
      LOG_WARN("failed to allocate file read buffer", K(ret));
    } else {
      file_ = file;
      capacity_ = FILE_SCAN_BUFFER_SIZE;
    }
    return ret;
  }
  // Returns OB_ITER_END at end of file.
  int peek(int &c)
  {
    int ret = OB_SUCCESS;
    c = -1;
    if (pos_ >= end_) {
      ret = fill();
    }
    if (OB_SUCCESS == ret) {
      c = static_cast<unsigned char>(buf_[pos_]);
    }
    return ret;
  }
  int get(int &c)
  {
    int ret = OB_SUCCESS;
    c = -1;
    if (pos_ >= end_) {
      ret = fill();
    }
    if (OB_SUCCESS == ret) {
      c = static_cast<unsigned char>(buf_[pos_++]);
    }
    return ret;
  }
  int64_t current_offset() const { return buf_start_offset_ + pos_; }
  int rewind(const int64_t offset)
  {
    int ret = OB_SUCCESS;
    if (OB_FAIL(file_->seek(offset))) {
    } else {
      pos_ = 0;
      end_ = 0;
      buf_start_offset_ = offset;
      eof_ = false;
    }
    return ret;
  }
  bool is_eof() const { return eof_ && pos_ >= end_; }
private:
  int fill()
  {
    int ret = OB_SUCCESS;
    if (eof_) {
      ret = OB_ITER_END;
    } else {
      int64_t nread = 0;
      buf_start_offset_ += end_;
      pos_ = 0;
      end_ = 0;
      if (OB_FAIL(file_->read(buf_, capacity_, nread))) {
      } else if (0 == nread) {
        eof_ = true;
        ret = OB_ITER_END;
      } else {
        end_ = nread;
      }
    }
    return ret;
  }
  file_scan::ObLocalFile *file_;
  char *buf_;
  int64_t capacity_;
  int64_t pos_;
  int64_t end_;
  int64_t buf_start_offset_;
  bool eof_;
};

// Streaming CSV scanner with configurable delimiter/quote/escape characters.
class ObCsvScanner
{
public:
  ObCsvScanner()
    : reader_(), content_(), data_start_offset_(0), line_no_(0),
      delimiter_(','), quote_('"'), escape_('"') {}
  int init(file_scan::ObLocalFile *file, const char delimiter, const char quote,
           const char escape, common::ObIAllocator &alloc)
  {
    int ret = OB_SUCCESS;
    content_.reset();
    content_.set_allocator(&alloc);
    if (OB_FAIL(reader_.init(file, alloc))) {
    } else {
      delimiter_ = delimiter;
      quote_ = quote;
      escape_ = escape;
    }
    return ret;
  }
  void mark_data_start() { data_start_offset_ = reader_.current_offset(); }
  int rewind()
  {
    int ret = OB_SUCCESS;
    if (OB_FAIL(reader_.rewind(data_start_offset_))) {
    } else {
      line_no_ = 0;
    }
    return ret;
  }
  int64_t line_no() const { return line_no_; }
  int next_record(common::ObIArray<common::ObString> &fields,
                  common::ObIArray<bool> &quoted,
                  int64_t &start_line)
  {
    int ret = OB_SUCCESS;
    for (;;) {
      content_.reuse();
      field_offsets_.reuse();
      fields.reuse();
      quoted.reuse();
      start_line = line_no_ + 1;
      bool has_record = false;
      if (OB_FAIL(next_record_internal(fields, quoted, has_record))) {
        break;
      } else if (!has_record) {
        ret = OB_ITER_END;
        break;
      } else if (1 == fields.count() && !quoted.at(0) && fields.at(0).empty()) {
        // skip blank line
      } else {
        break;
      }
    }
    return ret;
  }
private:
  int next_record_internal(common::ObIArray<common::ObString> &fields,
                           common::ObIArray<bool> &quoted, bool &has_record)
  {
    int ret = OB_SUCCESS;
    has_record = false;
    bool in_quotes = false;
    bool after_quote = false;
    bool at_field_start = true;
    bool field_quoted = false;
    bool first_char = true;
    int64_t field_start = 0;
    for (bool done = false; OB_SUCC(ret) && !done;) {
      int c = -1;
      const int get_ret = reader_.get(c);
      if (OB_ITER_END == get_ret) {
        done = true;
        if (in_quotes) {
          ret = OB_INVALID_ARGUMENT;
          LOG_WARN("unterminated quoted CSV field", K(ret), K_(line_no));
        } else if (first_char && fields.empty() && content_.empty()) {
          ret = OB_ITER_END;
        } else if (!append_field(fields, quoted, field_start, field_quoted)) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
        } else {
          has_record = true;
        }
      } else if (OB_FAIL(get_ret)) {
        ret = get_ret;
      } else {
        first_char = false;
        if ('\n' == c) {
          ++line_no_;
        }
        if (in_quotes) {
          if (escape_ != quote_ && c == escape_) {
            int c2 = -1;
            if (OB_FAIL(reader_.get(c2))) {
              ret = OB_INVALID_ARGUMENT;
              LOG_WARN("unterminated escape in CSV field", K(ret), K_(line_no));
            } else {
              if ('\n' == c2) {
                ++line_no_;
              }
              if (!append_char(c2)) {
                ret = OB_ALLOCATE_MEMORY_FAILED;
              }
            }
          } else if (c == quote_) {
            in_quotes = false;
            after_quote = true;
          } else if (!append_char(c)) {
            ret = OB_ALLOCATE_MEMORY_FAILED;
          }
        } else if (after_quote) {
          if (c == delimiter_) {
            if (!append_field(fields, quoted, field_start, field_quoted)) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              at_field_start = true;
              after_quote = false;
              field_quoted = false;
            }
          } else if ('\r' == c || '\n' == c) {
            if (!append_field(fields, quoted, field_start, field_quoted)) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              if ('\r' == c) {
                consume_lf();
              }
              has_record = true;
              done = true;
            }
          } else if (c == quote_) {
            // Doubled quote inside a quoted field, e.g. "say ""hi""" -> say "hi"
            if (!append_char(c)) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              in_quotes = true;
              after_quote = false;
            }
          } else {
            ret = OB_INVALID_ARGUMENT;
            LOG_WARN("unexpected character after closing quote in CSV", K(ret),
                     K_(line_no), K(c));
          }
        } else {
          if (at_field_start && c == quote_) {
            in_quotes = true;
            field_quoted = true;
            at_field_start = false;
          } else if (c == delimiter_) {
            if (!append_field(fields, quoted, field_start, field_quoted)) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              at_field_start = true;
              field_quoted = false;
            }
          } else if ('\r' == c || '\n' == c) {
            if (!append_field(fields, quoted, field_start, field_quoted)) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              if ('\r' == c) {
                consume_lf();
              }
              has_record = true;
            }
            done = true;
          } else if (!append_char(c)) {
            ret = OB_ALLOCATE_MEMORY_FAILED;
          } else {
            at_field_start = false;
          }
        }
      }
    }
    if (OB_SUCC(ret) && has_record) {
      // content_ may have grown while the record was being parsed, which
      // invalidates the pointers handed out by earlier append_field() calls.
      for (int64_t i = 0; OB_SUCC(ret) && i < fields.count(); ++i) {
        ObString &field = fields.at(i);
        if (OB_UNLIKELY(i >= field_offsets_.count())) {
          ret = OB_ERR_UNEXPECTED;
        } else {
          field.assign_ptr(content_.ptr() + field_offsets_.at(i), field.length());
        }
      }
    }
    return ret;
  }
  void consume_lf()
  {
    int next_c = -1;
    const int peek_ret = reader_.peek(next_c);
    if (OB_SUCCESS == peek_ret && '\n' == next_c) {
      int tmp_c = -1;
      reader_.get(tmp_c);
      ++line_no_;
    }
  }
  bool append_char(const int c)
  {
    const char ch = static_cast<char>(c);
    return OB_SUCCESS == content_.append(&ch, 1);
  }
  bool append_field(common::ObIArray<common::ObString> &fields,
                    common::ObIArray<bool> &quoted,
                    int64_t &field_start, const bool field_quoted)
  {
    bool ok = true;
    const int64_t field_len = static_cast<int64_t>(content_.length()) - field_start;
    ObString field(static_cast<int32_t>(field_len), content_.ptr() + field_start);
    if (OB_SUCCESS != fields.push_back(field) || OB_SUCCESS != quoted.push_back(field_quoted) ||
        OB_SUCCESS != field_offsets_.push_back(field_start)) {
      ok = false;
    } else {
      field_start = static_cast<int64_t>(content_.length());
    }
    return ok;
  }
  ObFileByteReader reader_;
  ObStringBuffer content_;
  common::ObSEArray<int64_t, 16> field_offsets_;
  int64_t data_start_offset_;
  int64_t line_no_;
  char delimiter_;
  char quote_;
  char escape_;
};

// ---------------------------------------------------------------------------
// Minimal flat JSON parser used by the JSONL reader. Rejects nested
// objects/arrays because the file-as-table feature only supports flat records.
// ---------------------------------------------------------------------------
struct ObFlatJsonValue
{
  enum Kind { JSON_NULL = 0, JSON_BOOL = 1, JSON_INT = 2, JSON_DOUBLE = 3, JSON_STRING = 4 };
  ObFlatJsonValue() : kind_(JSON_NULL), bool_(false), int_(0), double_(0), str_() {}
  Kind kind_;
  bool bool_;
  int64_t int_;
  double double_;
  ObString str_;
};

class ObFlatJsonParser
{
public:
  ObFlatJsonParser(char *buf, const int64_t len, common::ObStringBuffer &str_buf)
    : buf_(buf), len_(len), pos_(0), str_buf_(str_buf) {}
  int parse_object(common::ObIArray<ObString> &keys, common::ObIArray<ObFlatJsonValue> &values)
  {
    int ret = OB_SUCCESS;
    skip_ws();
    if (OB_FAIL(expect('{'))) {
    } else {
      skip_ws();
      if (pos_ < len_ && '}' == buf_[pos_]) {
        ++pos_;
      } else {
        for (bool done = false; OB_SUCC(ret) && !done;) {
          ObString key;
          ObFlatJsonValue value;
          if (OB_FAIL(parse_string(key))) {
          } else if (OB_FAIL(expect(':'))) {
          } else if (OB_FAIL(parse_value(value))) {
          } else if (OB_FAIL(keys.push_back(key))) {
          } else if (OB_FAIL(values.push_back(value))) {
          } else {
            skip_ws();
            if (pos_ < len_ && ',' == buf_[pos_]) {
              ++pos_;
            } else if (pos_ < len_ && '}' == buf_[pos_]) {
              ++pos_;
              done = true;
            } else {
              ret = OB_INVALID_ARGUMENT;
              LOG_WARN("malformed JSON object, expect ',' or '}'");
            }
          }
        }
      }
      skip_ws();
      if (OB_SUCC(ret) && pos_ != len_) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("unexpected trailing characters after JSON object");
      }
    }
    return ret;
  }
private:
  void skip_ws()
  {
    while (pos_ < len_ && (' ' == buf_[pos_] || '\t' == buf_[pos_] ||
                           '\r' == buf_[pos_] || '\n' == buf_[pos_])) {
      ++pos_;
    }
  }
  int expect(const char c)
  {
    int ret = OB_SUCCESS;
    skip_ws();
    if (pos_ >= len_ || buf_[pos_] != c) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("malformed JSON, unexpected token", K(c));
    } else {
      ++pos_;
    }
    return ret;
  }
  int parse_string(ObString &value)
  {
    int ret = OB_SUCCESS;
    skip_ws();
    if (pos_ >= len_ || '"' != buf_[pos_]) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("malformed JSON, expect string");
    } else {
      ++pos_;
      const int64_t start = static_cast<int64_t>(str_buf_.length());
      for (bool done = false; OB_SUCC(ret) && !done;) {
        if (pos_ >= len_) {
          ret = OB_INVALID_ARGUMENT;
          LOG_WARN("unterminated JSON string");
        } else {
          const char c = buf_[pos_++];
          if ('"' == c) {
            done = true;
          } else if ('\\' == c) {
            if (OB_FAIL(parse_escape())) {
            }
          } else if (static_cast<unsigned char>(c) < 0x20) {
            ret = OB_INVALID_ARGUMENT;
            LOG_WARN("invalid control character in JSON string");
          } else if (OB_SUCCESS != str_buf_.append(&c, 1)) {
            ret = OB_ALLOCATE_MEMORY_FAILED;
          }
        }
      }
      if (OB_SUCC(ret)) {
        const int64_t str_len = static_cast<int64_t>(str_buf_.length()) - start;
        value.assign_ptr(str_buf_.ptr() + start, static_cast<int32_t>(str_len));
      }
    }
    return ret;
  }
  int parse_escape()
  {
    int ret = OB_SUCCESS;
    if (pos_ >= len_) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      const char c = buf_[pos_++];
      char out = 0;
      switch (c) {
        case '"': out = '"'; break;
        case '\\': out = '\\'; break;
        case '/': out = '/'; break;
        case 'b': out = '\b'; break;
        case 'f': out = '\f'; break;
        case 'n': out = '\n'; break;
        case 'r': out = '\r'; break;
        case 't': out = '\t'; break;
        case 'u': {
          uint32_t cp = 0;
          if (OB_FAIL(parse_hex4(cp))) {
          } else if (cp >= 0xD800 && cp <= 0xDBFF) {
            // surrogate pair
            uint32_t low = 0;
            if (pos_ + 1 < len_ && '\\' == buf_[pos_] && 'u' == buf_[pos_ + 1]) {
              pos_ += 2;
              if (OB_FAIL(parse_hex4(low))) {
              } else if (low >= 0xDC00 && low <= 0xDFFF) {
                cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00);
                if (OB_FAIL(append_utf8(cp))) {
                }
              } else {
                ret = OB_INVALID_ARGUMENT;
                LOG_WARN("invalid low surrogate in JSON string");
              }
            } else {
              ret = OB_INVALID_ARGUMENT;
              LOG_WARN("missing low surrogate in JSON string");
            }
          } else if (OB_FAIL(append_utf8(cp))) {
          }
          break;
        }
        default:
          ret = OB_INVALID_ARGUMENT;
          LOG_WARN("invalid escape in JSON string", K(c));
          break;
      }
      if (OB_SUCC(ret) && 0 != out) {
        if (OB_SUCCESS != str_buf_.append(&out, 1)) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
        }
      }
    }
    return ret;
  }
  int parse_hex4(uint32_t &cp)
  {
    int ret = OB_SUCCESS;
    cp = 0;
    if (pos_ + 4 > len_) {
      ret = OB_INVALID_ARGUMENT;
    } else {
      for (int i = 0; OB_SUCC(ret) && i < 4; ++i) {
        const char c = buf_[pos_++];
        cp <<= 4;
        if (c >= '0' && c <= '9') {
          cp |= static_cast<uint32_t>(c - '0');
        } else if (c >= 'a' && c <= 'f') {
          cp |= static_cast<uint32_t>(c - 'a' + 10);
        } else if (c >= 'A' && c <= 'F') {
          cp |= static_cast<uint32_t>(c - 'A' + 10);
        } else {
          ret = OB_INVALID_ARGUMENT;
        }
      }
    }
    return ret;
  }
  int append_utf8(const uint32_t cp)
  {
    int ret = OB_SUCCESS;
    char out[4];
    int64_t out_len = 0;
    if (cp < 0x80) {
      out[0] = static_cast<char>(cp);
      out_len = 1;
    } else if (cp < 0x800) {
      out[0] = static_cast<char>(0xC0 | (cp >> 6));
      out[1] = static_cast<char>(0x80 | (cp & 0x3F));
      out_len = 2;
    } else if (cp < 0x10000) {
      out[0] = static_cast<char>(0xE0 | (cp >> 12));
      out[1] = static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      out[2] = static_cast<char>(0x80 | (cp & 0x3F));
      out_len = 3;
    } else {
      out[0] = static_cast<char>(0xF0 | (cp >> 18));
      out[1] = static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
      out[2] = static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
      out[3] = static_cast<char>(0x80 | (cp & 0x3F));
      out_len = 4;
    }
    if (OB_SUCCESS != str_buf_.append(out, out_len)) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    }
    return ret;
  }
  int parse_value(ObFlatJsonValue &value)
  {
    int ret = OB_SUCCESS;
    skip_ws();
    if (pos_ >= len_) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("malformed JSON, missing value");
    } else if ('"' == buf_[pos_]) {
      value.kind_ = ObFlatJsonValue::JSON_STRING;
      if (OB_FAIL(parse_string(value.str_))) {
      }
    } else if ('{' == buf_[pos_] || '[' == buf_[pos_]) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("nested JSON object/array is not supported by file sql");
    } else if ('t' == buf_[pos_] && pos_ + 4 <= len_ && 0 == strncmp(buf_ + pos_, "true", 4)) {
      pos_ += 4;
      value.kind_ = ObFlatJsonValue::JSON_BOOL;
      value.bool_ = true;
    } else if ('f' == buf_[pos_] && pos_ + 5 <= len_ && 0 == strncmp(buf_ + pos_, "false", 5)) {
      pos_ += 5;
      value.kind_ = ObFlatJsonValue::JSON_BOOL;
      value.bool_ = false;
    } else if ('n' == buf_[pos_] && pos_ + 4 <= len_ && 0 == strncmp(buf_ + pos_, "null", 4)) {
      pos_ += 4;
      value.kind_ = ObFlatJsonValue::JSON_NULL;
    } else if (OB_FAIL(parse_number(value))) {
    }
    return ret;
  }
  int parse_number(ObFlatJsonValue &value)
  {
    int ret = OB_SUCCESS;
    const int64_t start = pos_;
    if (pos_ < len_ && '-' == buf_[pos_]) {
      ++pos_;
    }
    while (pos_ < len_ && buf_[pos_] >= '0' && buf_[pos_] <= '9') {
      ++pos_;
    }
    bool is_float = false;
    if (pos_ < len_ && '.' == buf_[pos_]) {
      is_float = true;
      ++pos_;
      while (pos_ < len_ && buf_[pos_] >= '0' && buf_[pos_] <= '9') {
        ++pos_;
      }
    }
    if (pos_ < len_ && ('e' == buf_[pos_] || 'E' == buf_[pos_])) {
      is_float = true;
      ++pos_;
      if (pos_ < len_ && ('+' == buf_[pos_] || '-' == buf_[pos_])) {
        ++pos_;
      }
      while (pos_ < len_ && buf_[pos_] >= '0' && buf_[pos_] <= '9') {
        ++pos_;
      }
    }
    const int64_t token_len = pos_ - start;
    if (0 == token_len) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("malformed JSON number");
    } else if (!is_float) {
      bool overflow = false;
      if (OB_FAIL(file_scan::parse_int_text(buf_ + start, token_len, value.int_, overflow))) {
      } else if (overflow) {
        is_float = true;
      } else {
        value.kind_ = ObFlatJsonValue::JSON_INT;
      }
    }
    if (OB_SUCC(ret) && is_float) {
      bool is_numeric = false;
      if (OB_FAIL(file_scan::parse_double_text(buf_ + start, token_len, value.double_, is_numeric))) {
      } else if (!is_numeric) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("malformed JSON number");
      } else {
        value.kind_ = ObFlatJsonValue::JSON_DOUBLE;
      }
    }
    return ret;
  }
  char *buf_;
  int64_t len_;
  int64_t pos_;
  common::ObStringBuffer &str_buf_;
};

// Classify a CSV field into the logical type candidate used for inference.
int classify_csv_field(const ObString &field, int64_t &type)
{
  int ret = OB_SUCCESS;
  if (field.empty()) {
    type = OB_FILE_TYPE_NULL;
  } else {
    int64_t int_val = 0;
    bool overflow = false;
    double double_val = 0;
    bool is_numeric = false;
    int32_t date_val = 0;
    bool is_date = false;
    int64_t datetime_val = 0;
    bool is_datetime = false;
    // Only literal true/false denote a boolean column; bare 0/1 are integers,
    // otherwise a column like 1,2,3 would mix boolean and bigint and widen to varchar.
    if ((4 == field.length() && 0 == strncasecmp(field.ptr(), "true", 4)) ||
        (5 == field.length() && 0 == strncasecmp(field.ptr(), "false", 5))) {
      type = OB_FILE_TYPE_BOOLEAN;
    } else if (OB_SUCCESS == file_scan::parse_int_text(field.ptr(), field.length(), int_val, overflow) &&
               !overflow) {
      type = OB_FILE_TYPE_BIGINT;
    } else if (OB_SUCCESS == file_scan::parse_double_text(field.ptr(), field.length(),
                                                          double_val, is_numeric) && is_numeric) {
      type = OB_FILE_TYPE_DOUBLE;
    } else if (OB_SUCCESS == file_scan::parse_date_text(field.ptr(), field.length(),
                                                        date_val, is_date) && is_date) {
      type = OB_FILE_TYPE_DATE;
    } else if (OB_SUCCESS == file_scan::parse_datetime_text(field.ptr(), field.length(),
                                                            datetime_val, is_datetime) && is_datetime) {
      type = OB_FILE_TYPE_DATETIME;
    } else {
      type = OB_FILE_TYPE_VARCHAR;
    }
  }
  return ret;
}

// Append a unique column name, deriving "name__N" on duplicates.
int make_column_name(const int64_t idx, common::ObIAllocator &alloc, common::ObString &name);

int add_unique_column_name(common::ObIArray<common::ObString> &names,
                           const common::ObString &candidate,
                           common::ObIAllocator &alloc,
                           common::ObString &final_name)
{
  int ret = OB_SUCCESS;
  ObString base = candidate;
  if (base.empty()) {
    if (OB_FAIL(make_column_name(names.count(), alloc, base))) {
    }
  }
  for (int64_t suffix = 1; OB_SUCC(ret); ++suffix) {
    ObSqlString tmp;
    ObString try_name = base;
    if (suffix > 1) {
      if (OB_FAIL(tmp.append_fmt("%.*s__%ld", base.length(), base.ptr(), suffix))) {
      } else {
        try_name = tmp.string();
      }
    }
    bool used = false;
    for (int64_t i = 0; OB_SUCC(ret) && !used && i < names.count(); ++i) {
      used = (0 == names.at(i).case_compare(try_name));
    }
    if (OB_SUCC(ret) && !used) {
      if (OB_FAIL(ob_write_string(alloc, try_name, final_name))) {
      }
      break;
    }
  }
  return ret;
}

int make_column_name(const int64_t idx, common::ObIAllocator &alloc, common::ObString &name)
{
  int ret = OB_SUCCESS;
  ObSqlString tmp;
  if (OB_FAIL(tmp.append_fmt("column%ld", idx))) {
  } else if (OB_FAIL(ob_write_string(alloc, tmp.string(), name))) {
  }
  return ret;
}

int convert_csv_field_to_obj(const int64_t type, const ObString &field, const bool quoted,
                             const ObString &null_str, ObObj &obj)
{
  int ret = OB_SUCCESS;
  bool is_null = false;
  if (field.empty() && (!quoted || OB_FILE_TYPE_VARCHAR != type)) {
    is_null = true;
  } else if (!quoted && !null_str.empty() &&
             0 == field.case_compare(null_str)) {
    is_null = true;
  }
  if (is_null) {
    obj.set_null();
  } else {
    switch (type) {
      case OB_FILE_TYPE_NULL: {
        obj.set_null();
        break;
      }
      case OB_FILE_TYPE_BOOLEAN: {
        bool bool_val = false;
        if (OB_FAIL(file_scan::parse_bool_text(field.ptr(), field.length(), bool_val))) {
        } else {
          obj.set_bool(bool_val);
        }
        break;
      }
      case OB_FILE_TYPE_BIGINT: {
        int64_t int_val = 0;
        bool overflow = false;
        if (OB_FAIL(file_scan::parse_int_text(field.ptr(), field.length(), int_val, overflow))) {
        } else if (overflow) {
          double double_val = 1.0;
          bool is_numeric = false;
          if (OB_FAIL(file_scan::parse_double_text(field.ptr(), field.length(), double_val,
                                                   is_numeric)) || !is_numeric) {
          } else {
            obj.set_double(double_val);
          }
        } else {
          obj.set_int(int_val);
        }
        break;
      }
      case OB_FILE_TYPE_DOUBLE: {
        double double_val = 0;
        bool is_numeric = false;
        if (OB_FAIL(file_scan::parse_double_text(field.ptr(), field.length(), double_val,
                                                 is_numeric))) {
        } else if (!is_numeric) {
          int64_t int_val = 0;
          bool overflow = false;
          if (OB_FAIL(file_scan::parse_int_text(field.ptr(), field.length(), int_val, overflow))) {
          } else if (!overflow) {
            obj.set_double(static_cast<double>(int_val));
          }
        } else {
          obj.set_double(double_val);
        }
        break;
      }
      case OB_FILE_TYPE_DATE: {
        int32_t date_val = 0;
        bool is_date = false;
        if (OB_FAIL(file_scan::parse_date_text(field.ptr(), field.length(), date_val, is_date))) {
        } else if (!is_date) {
          ret = OB_INVALID_ARGUMENT;
        } else {
          obj.set_date(date_val);
        }
        break;
      }
      case OB_FILE_TYPE_DATETIME: {
        int64_t datetime_val = 0;
        bool is_datetime = false;
        if (OB_FAIL(file_scan::parse_datetime_text(field.ptr(), field.length(), datetime_val,
                                                   is_datetime))) {
        } else if (!is_datetime) {
          ret = OB_INVALID_ARGUMENT;
        } else {
          obj.set_datetime(datetime_val);
        }
        break;
      }
      case OB_FILE_TYPE_VARCHAR:
      default: {
        obj.set_varchar(field);
        obj.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
        break;
      }
    }
  }
  return ret;
}

class ObCsvFileReader : public ObFileScanReader
{
public:
  ObCsvFileReader() : info_(), file_(), scanner_(), opened_(false) {}
  virtual ~ObCsvFileReader() { close(); }
  virtual int open(const ObFileScanInfo &info, common::ObIAllocator &alloc) override
  {
    int ret = OB_SUCCESS;
    ObSEArray<ObString, 16> fields;
    ObSEArray<bool, 16> quoted;
    int64_t start_line = 0;
    if (opened_) {
      close();
    }
    if (OB_FAIL(info_.assign(info, alloc))) {
    } else if (OB_FAIL(file_.open(info_.path_))) {
    } else if (OB_FAIL(check_file_stat(info_))) {
    } else if (OB_FAIL(scanner_.init(&file_, info_.delimiter_, info_.quote_, info_.escape_, alloc))) {
    } else if (info_.has_header_ && OB_FAIL(scanner_.next_record(fields, quoted, start_line))) {
      if (OB_ITER_END == ret) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("empty CSV file", K(ret), K(info_.path_));
      }
    } else {
      scanner_.mark_data_start();
      row_cnt_ = info_.row_cnt_;
      opened_ = true;
    }
    if (OB_FAIL(ret) && OB_UNLIKELY(!opened_)) {
      file_.close();
    }
    return ret;
  }
  virtual int next_row(common::ObIAllocator &alloc,
                       common::ObIArray<ObObj> &cells) override
  {
    int ret = OB_SUCCESS;
    UNUSED(alloc);
    ObSEArray<ObString, 16> fields;
    ObSEArray<bool, 16> quoted;
    int64_t start_line = 0;
    cells.reuse();
    if (OB_UNLIKELY(!opened_)) {
      ret = OB_INVALID_ARGUMENT;
    } else if (OB_FAIL(scanner_.next_record(fields, quoted, start_line))) {
      if (OB_ITER_END != ret) {
        LOG_WARN("failed to read CSV record", K(ret), K(start_line));
      }
    } else if (fields.count() != info_.column_types_.count()) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("CSV field count mismatch", K(ret), K(start_line),
               "expected", info_.column_types_.count(), "actual", fields.count());
    } else {
      for (int64_t i = 0; OB_SUCC(ret) && i < fields.count(); ++i) {
        ObObj obj;
        if (OB_FAIL(convert_csv_field_to_obj(info_.column_types_.at(i), fields.at(i),
                                             quoted.at(i), info_.null_str_, obj))) {
          LOG_WARN("failed to convert CSV field", K(ret), K(start_line), K(i), K(fields.at(i)));
        } else if (OB_FAIL(cells.push_back(obj))) {
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
    } else if (OB_FAIL(scanner_.rewind())) {
    }
    return ret;
  }
  virtual void close() override
  {
    file_.close();
    opened_ = false;
  }
  virtual void destroy() override { this->~ObCsvFileReader(); }
private:
  int check_file_stat(const ObFileScanInfo &info)
  {
    int ret = OB_SUCCESS;
    if (info.file_size_ >= 0 &&
        (file_.size() != info.file_size_ ||
         file_.mtime_us() != info.file_mtime_us_ ||
         file_.ino() != info.file_ino_)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("local file was modified after the schema was inferred", K(ret),
               K(info.path_), "expect_size", info.file_size_, "actual_size", file_.size(),
               "expect_mtime", info.file_mtime_us_, "actual_mtime", file_.mtime_us());
    }
    return ret;
  }
  ObFileScanInfo info_;
  file_scan::ObLocalFile file_;
  ObCsvScanner scanner_;
  bool opened_;
};

class ObJsonlFileReader : public ObFileScanReader
{
public:
  ObJsonlFileReader() : info_(), file_(), reader_(), line_buf_(), str_buf_(),
                        line_no_(0), opened_(false), obj_bool_(false) {}
  virtual ~ObJsonlFileReader() { close(); }
  virtual int open(const ObFileScanInfo &info, common::ObIAllocator &alloc) override
  {
    int ret = OB_SUCCESS;
    if (opened_) {
      close();
    }
    line_buf_.reset();
    line_buf_.set_allocator(&alloc);
    str_buf_.reset();
    str_buf_.set_allocator(&alloc);
    if (OB_FAIL(info_.assign(info, alloc))) {
    } else if (OB_FAIL(file_.open(info_.path_))) {
    } else if (info_.file_size_ >= 0 && (file_.size() != info_.file_size_ ||
                                         file_.mtime_us() != info_.file_mtime_us_ ||
                                         file_.ino() != info_.file_ino_)) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("local file was modified after the schema was inferred", K(ret), K(info_.path_));
    } else if (OB_FAIL(reader_.init(&file_, alloc))) {
    } else {
      row_cnt_ = info_.row_cnt_;
      opened_ = true;
    }
    if (OB_FAIL(ret) && OB_UNLIKELY(!opened_)) {
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
      do {
        ret = read_line();
        if (OB_SUCC(ret) && !line_buf_.empty()) {
          ret = convert_line_to_cells(cells);
        }
      } while (OB_SUCC(ret) && line_buf_.empty());
    }
    return ret;
  }
  virtual int rewind() override
  {
    int ret = OB_SUCCESS;
    if (OB_UNLIKELY(!opened_)) {
      ret = OB_INVALID_ARGUMENT;
    } else if (OB_FAIL(reader_.rewind(0))) {
    } else {
      line_no_ = 0;
    }
    return ret;
  }
  virtual void close() override
  {
    file_.close();
    opened_ = false;
  }
  virtual void destroy() override { this->~ObJsonlFileReader(); }
private:
  int read_line()
  {
    int ret = OB_SUCCESS;
    line_buf_.reuse();
    bool got_any = false;
    for (bool done = false; OB_SUCC(ret) && !done;) {
      int c = -1;
      const int get_ret = reader_.get(c);
      if (OB_ITER_END == get_ret) {
        done = true;
        if (!got_any) {
          ret = OB_ITER_END;
        }
      } else if (OB_FAIL(get_ret)) {
        ret = get_ret;
      } else if ('\n' == c) {
        done = true;
        got_any = true;
        ++line_no_;
      } else {
        got_any = true;
        if ('\r' != c && OB_SUCCESS != line_buf_.append(reinterpret_cast<char *>(&c), 1)) {
          ret = OB_ALLOCATE_MEMORY_FAILED;
        }
      }
    }
    return ret;
  }
  int convert_line_to_cells(common::ObIArray<ObObj> &cells)
  {
    int ret = OB_SUCCESS;
    str_buf_.reuse();
    ObFlatJsonParser parser(const_cast<char *>(line_buf_.ptr()),
                            static_cast<int64_t>(line_buf_.length()), str_buf_);
    ObSEArray<ObString, 16> keys;
    ObSEArray<ObFlatJsonValue, 16> values;
    if (OB_FAIL(parser.parse_object(keys, values))) {
      LOG_WARN("failed to parse JSON line", K(ret), K_(line_no));
    } else {
      for (int64_t i = 0; OB_SUCC(ret) && i < info_.column_types_.count(); ++i) {
        const ObFlatJsonValue *value = nullptr;
        for (int64_t j = 0; nullptr == value && j < keys.count(); ++j) {
          if (0 == keys.at(j).case_compare(info_.column_names_.at(i))) {
            value = &values.at(j);
          }
        }
        ObObj obj;
        if (OB_FAIL(convert_json_value(info_.column_types_.at(i), value, obj))) {
          LOG_WARN("failed to convert JSON value", K(ret), K_(line_no), K(i));
        } else if (OB_FAIL(cells.push_back(obj))) {
        }
      }
    }
    return ret;
  }
  int convert_json_value(const int64_t type, const ObFlatJsonValue *value, ObObj &obj)
  {
    int ret = OB_SUCCESS;
    if (nullptr == value || ObFlatJsonValue::JSON_NULL == value->kind_ ||
        OB_FILE_TYPE_NULL == type) {
      obj.set_null();
    } else {
      switch (type) {
        case OB_FILE_TYPE_BOOLEAN: {
          if (ObFlatJsonValue::JSON_BOOL == value->kind_) {
            obj.set_bool(value->bool_);
          } else if (ObFlatJsonValue::JSON_INT == value->kind_ && (0 == value->int_ || 1 == value->int_)) {
            obj.set_bool(0 != value->int_);
          } else if (ObFlatJsonValue::JSON_STRING == value->kind_ &&
                     OB_SUCC(file_scan::parse_bool_text(value->str_.ptr(), value->str_.length(),
                                                       obj_bool_))) {
            obj.set_bool(obj_bool_);
          } else {
            ret = OB_INVALID_ARGUMENT;
          }
          break;
        }
        case OB_FILE_TYPE_BIGINT: {
          if (ObFlatJsonValue::JSON_INT == value->kind_) {
            obj.set_int(value->int_);
          } else if (ObFlatJsonValue::JSON_DOUBLE == value->kind_) {
            obj.set_int(static_cast<int64_t>(value->double_));
          } else if (ObFlatJsonValue::JSON_STRING == value->kind_) {
            int64_t int_val = 0;
            bool overflow = false;
            if (OB_FAIL(file_scan::parse_int_text(value->str_.ptr(), value->str_.length(),
                                                  int_val, overflow)) || overflow) {
            } else {
              obj.set_int(int_val);
            }
          } else {
            ret = OB_INVALID_ARGUMENT;
          }
          break;
        }
        case OB_FILE_TYPE_DOUBLE: {
          if (ObFlatJsonValue::JSON_DOUBLE == value->kind_) {
            obj.set_double(value->double_);
          } else if (ObFlatJsonValue::JSON_INT == value->kind_) {
            obj.set_double(static_cast<double>(value->int_));
          } else if (ObFlatJsonValue::JSON_STRING == value->kind_) {
            double double_val = 0;
            bool is_numeric = false;
            if (OB_FAIL(file_scan::parse_double_text(value->str_.ptr(), value->str_.length(),
                                                     double_val, is_numeric)) || !is_numeric) {
            } else {
              obj.set_double(double_val);
            }
          } else {
            ret = OB_INVALID_ARGUMENT;
          }
          break;
        }
        case OB_FILE_TYPE_DATE: {
          int32_t date_val = 0;
          bool is_date = false;
          if (ObFlatJsonValue::JSON_STRING != value->kind_ ||
              OB_FAIL(file_scan::parse_date_text(value->str_.ptr(), value->str_.length(),
                                                 date_val, is_date)) || !is_date) {
            ret = OB_INVALID_ARGUMENT;
          } else {
            obj.set_date(date_val);
          }
          break;
        }
        case OB_FILE_TYPE_DATETIME: {
          int64_t datetime_val = 0;
          bool is_datetime = false;
          if (ObFlatJsonValue::JSON_STRING != value->kind_ ||
              OB_FAIL(file_scan::parse_datetime_text(value->str_.ptr(), value->str_.length(),
                                                     datetime_val, is_datetime)) || !is_datetime) {
            ret = OB_INVALID_ARGUMENT;
          } else {
            obj.set_datetime(datetime_val);
          }
          break;
        }
        case OB_FILE_TYPE_VARCHAR:
        default: {
          if (ObFlatJsonValue::JSON_STRING == value->kind_) {
            obj.set_varchar(value->str_);
          } else if (ObFlatJsonValue::JSON_BOOL == value->kind_) {
            const char *text = value->bool_ ? "true" : "false";
            if (OB_SUCCESS != str_buf_.append(text, strlen(text))) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              obj.set_varchar(ObString(static_cast<int32_t>(strlen(text)),
                                       str_buf_.ptr() + str_buf_.length() - strlen(text)));
            }
          } else {
            ObSqlString tmp;
            if (ObFlatJsonValue::JSON_INT == value->kind_) {
              ret = tmp.append_fmt("%ld", value->int_);
            } else {
              ret = tmp.append_fmt("%.15g", value->double_);
            }
            if (OB_FAIL(ret)) {
            } else if (OB_SUCCESS != str_buf_.append(tmp.ptr(), tmp.length())) {
              ret = OB_ALLOCATE_MEMORY_FAILED;
            } else {
              obj.set_varchar(ObString(static_cast<int32_t>(tmp.length()),
                                       str_buf_.ptr() + str_buf_.length() - tmp.length()));
            }
          }
          if (OB_SUCC(ret)) {
            obj.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
          }
          break;
        }
      }
    }
    return ret;
  }
  ObFileScanInfo info_;
  file_scan::ObLocalFile file_;
  ObFileByteReader reader_;
  ObStringBuffer line_buf_;
  ObStringBuffer str_buf_;
  int64_t line_no_;
  bool opened_;
  bool obj_bool_;
};

class ObGlobFileReader : public ObFileScanReader
{
public:
  ObGlobFileReader() : files_(), cur_idx_(0), opened_(false) {}
  virtual ~ObGlobFileReader() { close(); }
  virtual int open(const ObFileScanInfo &info, common::ObIAllocator &alloc) override
  {
    int ret = OB_SUCCESS;
    files_.reset();
    cur_idx_ = 0;
    if (OB_FAIL(expand_glob_paths(alloc, info.path_, files_))) {
    } else {
      row_cnt_ = files_.count();
      opened_ = true;
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
    } else if (cur_idx_ >= files_.count()) {
      ret = OB_ITER_END;
    } else {
      ObObj obj;
      obj.set_varchar(files_.at(cur_idx_));
      obj.set_collation_type(CS_TYPE_UTF8MB4_GENERAL_CI);
      if (OB_FAIL(cells.push_back(obj))) {
      } else {
        ++cur_idx_;
      }
    }
    return ret;
  }
  virtual int rewind() override
  {
    cur_idx_ = 0;
    return OB_SUCCESS;
  }
  virtual void close() override
  {
    files_.reset();
    cur_idx_ = 0;
    opened_ = false;
  }
  virtual void destroy() override { this->~ObGlobFileReader(); }
private:
  ObSEArray<ObString, 16> files_;
  int64_t cur_idx_;
  bool opened_;
};

int infer_csv_schema(common::ObIAllocator &alloc,
                     file_scan::ObLocalFile &file,
                     const ObFileScanOptions &options,
                     ObFileScanInfo &info)
{
  int ret = OB_SUCCESS;
  ObCsvScanner scanner;
  ObSEArray<ObString, 16> fields;
  ObSEArray<bool, 16> quoted;
  int64_t start_line = 0;
  int64_t column_cnt = 0;
  int64_t row_cnt = 0;
  if (OB_FAIL(scanner.init(&file, options.delimiter_, options.quote_, options.escape_, alloc))) {
  } else if (OB_FAIL(scanner.next_record(fields, quoted, start_line))) {
    if (OB_ITER_END == ret) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("empty CSV file", K(ret), K(info.path_));
    }
  } else {
    column_cnt = fields.count();
    for (int64_t i = 0; OB_SUCC(ret) && i < column_cnt; ++i) {
      ObString final_name;
      if (OB_FAIL(add_unique_column_name(info.column_names_,
                                         options.has_header_ ? fields.at(i) : ObString(),
                                         alloc, final_name))) {
      } else if (OB_FAIL(info.add_column(final_name, OB_FILE_TYPE_NULL, false, 0))) {
      }
    }
    // Without a header the first record belongs to the data set.
    if (OB_SUCC(ret) && !options.has_header_) {
      for (int64_t i = 0; OB_SUCC(ret) && i < column_cnt; ++i) {
        int64_t cell_type = OB_FILE_TYPE_NULL;
        if (OB_FAIL(classify_csv_field(fields.at(i), cell_type))) {
        } else {
          int64_t widened = info.column_types_.at(i);
          if (OB_FAIL(file_scan::widen_logical_type(info.column_types_.at(i), cell_type,
                                                    widened))) {
          } else {
            info.column_types_.at(i) = widened;
            if (OB_FILE_TYPE_NULL == cell_type) {
              info.column_null_count_.at(i) += 1;
            }
          }
        }
      }
      ++row_cnt;
    }
  }
  for (bool done = false; OB_SUCC(ret) && !done;) {
    if (OB_FAIL(scanner.next_record(fields, quoted, start_line))) {
      if (OB_ITER_END == ret) {
        ret = OB_SUCCESS;
        done = true;
      }
    } else if (fields.count() != column_cnt) {
      ret = OB_INVALID_ARGUMENT;
      LOG_WARN("CSV field count mismatch", K(ret), K(start_line),
               "expected", column_cnt, "actual", fields.count());
    } else {
      for (int64_t i = 0; OB_SUCC(ret) && i < column_cnt; ++i) {
        int64_t cell_type = OB_FILE_TYPE_NULL;
        if (OB_FAIL(classify_csv_field(fields.at(i), cell_type))) {
        } else {
          int64_t widened = info.column_types_.at(i);
          if (OB_FAIL(file_scan::widen_logical_type(info.column_types_.at(i), cell_type,
                                                    widened))) {
          } else {
            info.column_types_.at(i) = widened;
            if (OB_FILE_TYPE_NULL == cell_type) {
              info.column_null_count_.at(i) += 1;
            }
          }
        }
      }
      ++row_cnt;
    }
  }
  if (OB_SUCC(ret)) {
    for (int64_t i = 0; OB_SUCC(ret) && i < column_cnt; ++i) {
      info.column_nullable_.at(i) = (info.column_null_count_.at(i) > 0) ? 1 : 0;
    }
    info.row_cnt_ = row_cnt;
  }
  return ret;
}

int infer_jsonl_schema(common::ObIAllocator &alloc,
                       file_scan::ObLocalFile &file,
                       const ObFileScanOptions &options,
                       ObFileScanInfo &info)
{
  int ret = OB_SUCCESS;
  UNUSED(options);
  if (!info.json_format_.empty() && 0 != info.json_format_.case_compare("newline_delimited")) {
    ret = OB_NOT_SUPPORTED;
    LOG_WARN("unsupported json format", K(ret), K(info.json_format_));
  } else {
    ObFileByteReader reader;
    ObArenaAllocator tmp_alloc;
    ObStringBuffer line_buf(&tmp_alloc);
    ObStringBuffer str_buf(&tmp_alloc);
    int64_t line_no = 0;
    int64_t row_cnt = 0;
    if (OB_FAIL(reader.init(&file, alloc))) {
    }
    for (bool eof = false; OB_SUCC(ret) && !eof;) {
      line_buf.reuse();
      bool got_any = false;
      int read_ret = OB_SUCCESS;
      // NOTE: do not wrap read_ret with OB_SUCC/OB_FAIL here, those macros assign
      // their argument to 'ret' and would leak OB_ITER_END out of this function.
      for (bool done = false; OB_SUCCESS == read_ret && !done;) {
        int c = -1;
        read_ret = reader.get(c);
        if (OB_ITER_END == read_ret) {
          done = true;
          eof = true;
        } else if (OB_SUCCESS != read_ret) {
        } else if ('\n' == c) {
          done = true;
          got_any = true;
          ++line_no;
        } else {
          got_any = true;
          if ('\r' != c && OB_SUCCESS != line_buf.append(reinterpret_cast<char *>(&c), 1)) {
            read_ret = OB_ALLOCATE_MEMORY_FAILED;
          }
        }
      }
      if (OB_SUCCESS != read_ret && OB_ITER_END != read_ret) {
        ret = read_ret;
      } else if (OB_SUCC(ret) && got_any && !line_buf.empty()) {
        str_buf.reuse();
        ObFlatJsonParser parser(const_cast<char *>(line_buf.ptr()),
                                static_cast<int64_t>(line_buf.length()), str_buf);
        ObSEArray<ObString, 16> keys;
        ObSEArray<ObFlatJsonValue, 16> values;
        if (OB_FAIL(parser.parse_object(keys, values))) {
          LOG_WARN("failed to parse JSON line", K(ret), K(line_no));
        } else {
          for (int64_t j = 0; OB_SUCC(ret) && j < keys.count(); ++j) {
            int64_t col_idx = -1;
            for (int64_t i = 0; -1 == col_idx && i < info.column_names_.count(); ++i) {
              if (0 == info.column_names_.at(i).case_compare(keys.at(j))) {
                col_idx = i;
              }
            }
            if (-1 == col_idx) {
              ObString name;
              if (OB_FAIL(ob_write_string(alloc, keys.at(j), name))) {
              } else if (OB_FAIL(info.add_column(name, OB_FILE_TYPE_NULL, true, 0))) {
              } else {
                col_idx = info.column_names_.count() - 1;
              }
            }
            if (OB_SUCC(ret)) {
              int64_t cell_type = OB_FILE_TYPE_NULL;
              const ObFlatJsonValue &value = values.at(j);
              switch (value.kind_) {
                case ObFlatJsonValue::JSON_NULL: cell_type = OB_FILE_TYPE_NULL; break;
                case ObFlatJsonValue::JSON_BOOL: cell_type = OB_FILE_TYPE_BOOLEAN; break;
                case ObFlatJsonValue::JSON_INT: cell_type = OB_FILE_TYPE_BIGINT; break;
                case ObFlatJsonValue::JSON_DOUBLE: cell_type = OB_FILE_TYPE_DOUBLE; break;
                case ObFlatJsonValue::JSON_STRING: cell_type = OB_FILE_TYPE_VARCHAR; break;
                default: cell_type = OB_FILE_TYPE_VARCHAR; break;
              }
              int64_t widened = info.column_types_.at(col_idx);
              if (OB_FAIL(file_scan::widen_logical_type(info.column_types_.at(col_idx),
                                                        cell_type, widened))) {
              } else {
                info.column_types_.at(col_idx) = widened;
              }
            }
          }
          ++row_cnt;
        }
      }
    }
    if (OB_SUCC(ret)) {
      info.row_cnt_ = row_cnt;
    }
  }
  return ret;
}

} // end anonymous namespace

int create_file_scan_reader(const int64_t format, common::ObIAllocator &alloc,
                            ObFileScanReader *&reader)
{
  int ret = OB_SUCCESS;
  reader = nullptr;
  void *buf = nullptr;
  if (OB_FILE_FORMAT_CSV == format) {
    if (OB_ISNULL(buf = alloc.alloc(sizeof(ObCsvFileReader)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else {
      reader = new (buf) ObCsvFileReader();
    }
  } else if (OB_FILE_FORMAT_JSONL == format) {
    if (OB_ISNULL(buf = alloc.alloc(sizeof(ObJsonlFileReader)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else {
      reader = new (buf) ObJsonlFileReader();
    }
  } else if (OB_FILE_FORMAT_GLOB == format) {
    if (OB_ISNULL(buf = alloc.alloc(sizeof(ObGlobFileReader)))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
    } else {
      reader = new (buf) ObGlobFileReader();
    }
  } else if (OB_FILE_FORMAT_PARQUET == format) {
    if (OB_FAIL(create_parquet_file_reader(alloc, reader))) {
    }
  } else {
    ret = OB_NOT_SUPPORTED;
    LOG_WARN("unsupported file format", K(ret), K(format));
  }
  return ret;
}

int infer_file_scan_schema(common::ObIAllocator &alloc,
                           const common::ObString &path,
                           const ObFileScanOptions &options,
                           ObFileScanInfo &info)
{
  int ret = OB_SUCCESS;
  file_scan::ObLocalFile file;
  info.reset();
  info.format_ = options.format_;
  info.delimiter_ = options.delimiter_;
  info.quote_ = options.quote_;
  info.escape_ = options.escape_;
  info.has_header_ = options.has_header_;
  if (OB_FAIL(ob_write_string(alloc, path, info.path_))) {
  } else if (OB_FAIL(ob_write_string(alloc, options.null_str_, info.null_str_))) {
  } else if (OB_FAIL(ob_write_string(alloc, options.json_format_, info.json_format_))) {
  } else if (OB_FILE_FORMAT_GLOB == options.format_) {
    // Glob patterns are not opened as regular files; schema is a single column.
    ObString name;
    if (OB_FAIL(ob_write_string(alloc, "file", name))) {
    } else if (OB_FAIL(info.add_column(name, OB_FILE_TYPE_VARCHAR, false, 0))) {
    } else {
      ObSEArray<ObString, 16> files;
      if (OB_FAIL(expand_glob_paths(alloc, path, files))) {
      } else {
        info.row_cnt_ = files.count();
      }
    }
  } else if (OB_FAIL(file.open(path))) {
  } else {
    info.file_size_ = file.size();
    info.file_mtime_us_ = file.mtime_us();
    info.file_ino_ = file.ino();
    info.file_dev_ = file.dev();
    if (OB_FILE_FORMAT_CSV == options.format_) {
      if (OB_FAIL(infer_csv_schema(alloc, file, options, info))) {
      }
    } else if (OB_FILE_FORMAT_JSONL == options.format_) {
      if (OB_FAIL(infer_jsonl_schema(alloc, file, options, info))) {
      }
    } else if (OB_FILE_FORMAT_PARQUET == options.format_) {
      file.close();
      if (OB_FAIL(infer_parquet_file_schema(alloc, path, info))) {
      }
    } else {
      ret = OB_NOT_SUPPORTED;
      LOG_WARN("unsupported file format", K(ret), K(options.format_));
    }
  }
  return ret;
}

int expand_glob_paths(common::ObIAllocator &alloc,
                      const common::ObString &pattern,
                      common::ObIArray<common::ObString> &files)
{
  int ret = OB_SUCCESS;
  char pattern_buf[PATH_MAX + 1];
  files.reset();
  if (OB_UNLIKELY(pattern.empty()) || OB_UNLIKELY(pattern.length() > PATH_MAX)) {
    ret = OB_INVALID_ARGUMENT;
    LOG_WARN("invalid glob pattern", K(ret), K(pattern));
  } else {
    MEMCPY(pattern_buf, pattern.ptr(), pattern.length());
    pattern_buf[pattern.length()] = '\0';
    char *slash = strrchr(pattern_buf, '/');
    const char *dir_path = ".";
    const char *file_pattern = pattern_buf;
    ObSqlString dir_str;
    if (nullptr != slash) {
      *slash = '\0';
      dir_path = pattern_buf;
      file_pattern = slash + 1;
      if (OB_FAIL(dir_str.append(dir_path))) {
      }
    } else if (OB_FAIL(dir_str.append("."))) {
    }
    if (OB_SUCC(ret)) {
      // Directories in the pattern (other than the last component) must be static.
      if (nullptr != strpbrk(dir_path, "*?[")) {
        ret = OB_NOT_SUPPORTED;
        LOG_WARN("glob wildcards in directory components are not supported", K(ret), K(pattern));
      } else if (strlen(file_pattern) == 0) {
        ret = OB_INVALID_ARGUMENT;
        LOG_WARN("empty glob file pattern", K(ret), K(pattern));
      } else {
        DIR *dir = ::opendir(dir_path);
        if (nullptr == dir) {
          if (ENOENT == errno || ENOTDIR == errno) {
            // no match
          } else {
            ret = OB_IO_ERROR;
            LOG_WARN("failed to open directory for glob", K(ret), "errno", errno, K(pattern));
          }
        } else {
          struct dirent *entry = nullptr;
          while (OB_SUCC(ret) && nullptr != (entry = ::readdir(dir))) {
            if (0 == strcmp(entry->d_name, ".") || 0 == strcmp(entry->d_name, "..")) {
              continue;
            }
            if (0 != ::fnmatch(file_pattern, entry->d_name, 0)) {
              continue;
            }
            ObSqlString full_path;
            if (OB_FAIL(full_path.append_fmt("%s/%s", dir_str.ptr(), entry->d_name))) {
            } else {
              file_scan::ObFileStatInfo stat_info;
              if (OB_FAIL(file_scan::stat_local_path(full_path.string(), stat_info))) {
              } else if (!stat_info.exists_ || stat_info.is_dir_) {
                // only regular files are reported
              } else {
                ObString file_path;
                if (OB_FAIL(ob_write_string(alloc, full_path.string(), file_path))) {
                } else if (OB_FAIL(files.push_back(file_path))) {
                }
              }
            }
          }
          ::closedir(dir);
        }
      }
    }
  }
  // Sort ascending for deterministic output.
  for (int64_t i = 1; OB_SUCC(ret) && i < files.count(); ++i) {
    ObString key = files.at(i);
    int64_t j = i - 1;
    while (j >= 0 && files.at(j).compare(key) > 0) {
      files.at(j + 1) = files.at(j);
      --j;
    }
    files.at(j + 1) = key;
  }
  return ret;
}

} // end namespace sql
} // end namespace oceanbase
