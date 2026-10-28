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

#ifndef OCEANBASE_SQL_RESOLVER_DDL_OB_AI_COLUMN_RESOLVER_H_
#define OCEANBASE_SQL_RESOLVER_DDL_OB_AI_COLUMN_RESOLVER_H_

#include "sql/resolver/ob_resolver.h"
#include "share/schema/ob_table_schema.h"

namespace oceanbase
{
namespace sql
{
const char *const AI_COLUMN_STATE_PREFIX = "__ai_state_";
const char *const AI_COLUMN_REQUEST_PREFIX = "__ai_request_";
const char *const AI_COLUMN_SPEC_PREFIX = "__ai_spec_";
const char *const AI_COLUMN_JSON_SYSTEM_PROMPT = "Return exactly one valid JSON value";
const char *const AI_COLUMN_INTEGER_SYSTEM_PROMPT = "base-10 integer";
const char *const AI_COLUMN_STATE_PENDING = "PENDING";

struct ObAIColumnMeta
{
  ObAIColumnMeta()
      : column_id_(OB_INVALID_ID),
        state_column_id_(OB_INVALID_ID),
        request_column_id_(OB_INVALID_ID),
        spec_column_id_(OB_INVALID_ID),
        max_retries_(1),
        is_embed_(false),
        embed_dim_(-1)
  {}
  common::ObString column_name_;
  common::ObString model_name_;
  uint64_t column_id_;
  uint64_t state_column_id_;
  uint64_t request_column_id_;
  uint64_t spec_column_id_;
  int64_t max_retries_;
  bool is_embed_;
  int64_t embed_dim_;
  common::ObString spec_json_;
  common::ObString user_expr_;
};

// Resolve the T_AI_COLUMN_ATTR parse node attached to a column definition.
// Validates the AI expression, dependency columns and embedding dimension,
// and records an ObAIColumnMeta for hidden column generation and metadata persistence.
int resolve_ai_column_attribute(ObSQLSessionInfo *session_info,
                                common::ObIAllocator &allocator,
                                share::schema::ObColumnSchemaV2 &column,
                                ParseNode *attr_node,
                                const common::ObIArray<share::schema::ObColumnSchemaV2> *declared_cols,
                                const common::ObIArray<common::ObString> &existing_ai_columns,
                                ObAIColumnMeta &meta);

// Create the three hidden state columns for one AI column and append them to resolved_cols.
int build_ai_hidden_columns(common::ObIAllocator &allocator,
                            const ObAIColumnMeta &meta,
                            uint64_t state_column_id,
                            uint64_t request_column_id,
                            uint64_t spec_column_id,
                            common::ObIArray<share::schema::ObColumnSchemaV2> &resolved_cols);

// Build "prefix + column_name" with allocator; used for hidden column names.
int build_ai_hidden_column_name(common::ObIAllocator &allocator,
                                const char *prefix,
                                const common::ObString &column_name,
                                common::ObString &out_name);

// True if column name starts with the AI state/request/spec prefix.
bool is_ai_hidden_column_name(const common::ObString &name, const char *prefix);

} // namespace sql
} // namespace oceanbase

#endif // OCEANBASE_SQL_RESOLVER_DDL_OB_AI_COLUMN_RESOLVER_H_
