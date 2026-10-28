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

#include "sql/engine/basic/file_scan/ob_file_scan_reader.h"

namespace oceanbase
{
namespace sql
{

// Implemented in ob_parquet_reader.cpp.
int create_parquet_file_reader(common::ObIAllocator &alloc, ObFileScanReader *&reader);
int infer_parquet_file_schema(common::ObIAllocator &alloc,
                              const common::ObString &path,
                              ObFileScanInfo &info);

} // namespace sql
} // namespace oceanbase
