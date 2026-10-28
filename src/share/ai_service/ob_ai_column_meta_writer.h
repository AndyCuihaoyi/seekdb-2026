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

#ifndef OCEANBASE_SHARE_AI_SERVICE_OB_AI_COLUMN_META_WRITER_H_
#define OCEANBASE_SHARE_AI_SERVICE_OB_AI_COLUMN_META_WRITER_H_

#include "common/mysqlclient/ob_isql_client.h"
#include "share/schema/ob_table_schema.h"

namespace oceanbase
{
namespace share
{
class ObAIColumnMetaWriter
{
public:
  // Persist one row per AI column of the table into __all_ai_column.
  // A column is an AI column when its "__ai_state_<name>" hidden column exists.
  static int write_meta_rows(common::ObISQLClient &client,
                             uint64_t table_id,
                             const share::schema::ObTableSchema &schema,
                             int64_t &affected_rows);
  static int delete_meta_rows(common::ObISQLClient &client,
                              uint64_t table_id,
                              int64_t &affected_rows);
};

} // namespace share
} // namespace oceanbase

#endif // OCEANBASE_SHARE_AI_SERVICE_OB_AI_COLUMN_META_WRITER_H_
