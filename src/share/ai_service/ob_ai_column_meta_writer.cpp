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

#define USING_LOG_PREFIX SHARE

#include "share/ai_service/ob_ai_column_meta_writer.h"

#include "lib/time/ob_time_utility.h"
#include "share/inner_table/ob_inner_table_schema.h"
#include "share/ob_dml_sql_splicer.h"

namespace oceanbase
{
namespace share
{
static const char *AI_STATE_PREFIX = "__ai_state_";
static const char *AI_REQUEST_PREFIX = "__ai_request_";
static const char *AI_SPEC_PREFIX = "__ai_spec_";
static const char *AI_RETRIES_KEY = "\"max_retries\":";

static bool starts_with(const common::ObString &name, const char *prefix)
{
  const int64_t len = strlen(prefix);
  return name.length() > len && 0 == MEMCMP(name.ptr(), prefix, len);
}

static int build_hidden_name(char *buf, const int64_t buf_len, const char *prefix,
                             const common::ObString &column_name, common::ObString &out)
{
  int ret = common::OB_SUCCESS;
  const int64_t prefix_len = strlen(prefix);
  if (prefix_len + column_name.length() >= buf_len) {
    ret = common::OB_SIZE_OVERFLOW;
  } else {
    MEMCPY(buf, prefix, prefix_len);
    MEMCPY(buf + prefix_len, column_name.ptr(), column_name.length());
    out.assign_ptr(buf, static_cast<int32_t>(prefix_len + column_name.length()));
  }
  return ret;
}

static int64_t parse_max_retries(const common::ObString &spec_json)
{
  int64_t retries = 1;
  const int64_t key_len = strlen(AI_RETRIES_KEY);
  for (int64_t i = 0; i + key_len < spec_json.length(); ++i) {
    if (0 == MEMCMP(spec_json.ptr() + i, AI_RETRIES_KEY, key_len)) {
      retries = strtoll(spec_json.ptr() + i + key_len, NULL, 10);
      if (retries < 0) {
        retries = 0;
      }
      break;
    }
  }
  return retries;
}

int ObAIColumnMetaWriter::write_meta_rows(common::ObISQLClient &client,
                                          uint64_t table_id,
                                          const share::schema::ObTableSchema &schema,
                                          int64_t &affected_rows)
{
  int ret = common::OB_SUCCESS;
  affected_rows = 0;
  SHARE_LOG(INFO, "AI_COL_TRACE write_meta_rows enter", K(table_id), "col_cnt", schema.get_column_count());
  for (int64_t i = 0; OB_SUCC(ret) && i < schema.get_column_count(); ++i) {
    const share::schema::ObColumnSchemaV2 *column = schema.get_column_schema_by_idx(i);
    if (OB_ISNULL(column) || column->is_hidden()) {
      // skip
    } else {
      char name_buf[3][256];
      common::ObString state_name;
      common::ObString request_name;
      common::ObString spec_name;
      const share::schema::ObColumnSchemaV2 *state_col = NULL;
      const share::schema::ObColumnSchemaV2 *request_col = NULL;
      const share::schema::ObColumnSchemaV2 *spec_col = NULL;
      if (OB_FAIL(build_hidden_name(name_buf[0], sizeof(name_buf[0]), AI_STATE_PREFIX,
                                    column->get_column_name_str(), state_name))) {
      } else if (OB_FAIL(build_hidden_name(name_buf[1], sizeof(name_buf[1]), AI_REQUEST_PREFIX,
                                           column->get_column_name_str(), request_name))) {
      } else if (OB_FAIL(build_hidden_name(name_buf[2], sizeof(name_buf[2]), AI_SPEC_PREFIX,
                                           column->get_column_name_str(), spec_name))) {
      } else if (OB_ISNULL(state_col = schema.get_column_schema(state_name))
                 || OB_ISNULL(request_col = schema.get_column_schema(request_name))
                 || OB_ISNULL(spec_col = schema.get_column_schema(spec_name))) {
        // not an AI column
        SHARE_LOG(INFO, "AI_COL_TRACE hidden col lookup miss", K(i),
               "col", common::ObString(column->get_column_name_str()),
               "state_name", state_name, "request_name", request_name, "spec_name", spec_name,
               "state_found", NULL != state_col, "request_found", NULL != request_col,
               "spec_found", NULL != spec_col);
      } else {
        common::ObString spec_json;
        const common::ObObj &default_spec = spec_col->get_cur_default_value();
        if (!default_spec.is_null() && common::ObVarcharType == default_spec.get_type()) {
          spec_json = default_spec.get_varchar();
        }
        share::ObDMLSqlSplicer dml;
        share::ObDMLExecHelper exec(client);
        int64_t row_affected = 0;
        if (OB_FAIL(dml.add_gmt_create(ObTimeUtility::current_time()))) {
        } else if (OB_FAIL(dml.add_gmt_modified(ObTimeUtility::current_time()))) {
        } else if (OB_FAIL(dml.add_pk_column("table_id", static_cast<int64_t>(table_id)))) {
        } else if (OB_FAIL(dml.add_pk_column("column_id", static_cast<int64_t>(column->get_column_id())))) {
        } else if (OB_FAIL(dml.add_column("column_name", column->get_column_name_str()))) {
        } else if (OB_FAIL(dml.add_column("max_retries", parse_max_retries(spec_json)))) {
        } else if (OB_FAIL(dml.add_column("state_column_id", static_cast<int64_t>(state_col->get_column_id())))) {
        } else if (OB_FAIL(dml.add_column("request_column_id", static_cast<int64_t>(request_col->get_column_id())))) {
        } else if (OB_FAIL(dml.add_column("result_spec_column_id", static_cast<int64_t>(spec_col->get_column_id())))) {
        } else if (OB_FAIL(dml.add_column("normalized_expr", ObHexEscapeSqlStr(spec_json)))) {
        } else if (OB_FAIL(exec.exec_replace(OB_ALL_AI_COLUMN_TNAME, dml, row_affected))) {
          LOG_WARN("write ai column meta row failed", K(ret), K(table_id), K(column->get_column_name_str()));
        } else {
          affected_rows += row_affected;
        }
      }
    }
  }
  return ret;
}

int ObAIColumnMetaWriter::delete_meta_rows(common::ObISQLClient &client,
                                           uint64_t table_id,
                                           int64_t &affected_rows)
{
  int ret = common::OB_SUCCESS;
  common::ObSqlString sql;
  if (OB_FAIL(sql.assign_fmt("DELETE FROM %s WHERE table_id = %lu",
                             OB_ALL_AI_COLUMN_TNAME, table_id))) {
  } else if (OB_FAIL(client.write(sql.ptr(), affected_rows))) {
    LOG_WARN("delete ai column meta rows failed", K(ret), K(table_id));
  }
  return ret;
}

} // namespace share
} // namespace oceanbase
