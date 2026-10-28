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

#define USING_LOG_PREFIX SERVER

#include "observer/virtual_table/ob_ai_column_task_iterator.h"

#include "query/session/ob_session_inner_sql.h"
#include "sql/session/ob_sql_session_info.h"
#include "share/schema/ob_schema_struct.h"
#include "common/mysqlclient/ob_mysql_result.h"

namespace oceanbase
{
using namespace common;
using namespace share::schema;
namespace observer
{

enum AiColumnTaskColumn
{
  AI_TASK_TABLE_ID_COL = OB_APP_MIN_COLUMN_ID,
  AI_TASK_COLUMN_ID_COL,
  AI_TASK_COLUMN_NAME_COL,
  AI_TASK_REQUEST_ID_COL,
  AI_TASK_STATE_COL,
  AI_TASK_RETRY_COUNT_COL,
  AI_TASK_ELIGIBLE_TS_COL,
  AI_TASK_ROWKEY_PREDICATE_COL
};

static const char *AI_TASK_STATE_READY = "READY";
static const char *AI_TASK_STATE_FAILED = "FAILED";

static int deep_copy_str(ObIAllocator &allocator, const ObString &src, ObString &dst)
{
  int ret = OB_SUCCESS;
  char *buf = NULL;
  if (src.length() > 0) {
    if (OB_ISNULL(buf = static_cast<char *>(allocator.alloc(src.length())))) {
      ret = OB_ALLOCATE_MEMORY_FAILED;
      LOG_WARN("alloc for string failed", K(ret), K(src.length()));
    } else {
      MEMCPY(buf, src.ptr(), src.length());
    }
  }
  if (OB_SUCC(ret)) {
    dst.assign_ptr(buf, static_cast<int32_t>(src.length()));
  }
  return ret;
}

static int64_t ai_state_to_int(const ObString &state)
{
  int64_t val = 0; // PENDING by default
  if (0 == state.case_compare(ObString::make_string("RUNNING"))) {
    val = 1;
  } else if (0 == state.case_compare(ObString::make_string("RETRY_WAIT"))) {
    val = 3;
  } else if (0 == state.case_compare(ObString::make_string("READY"))) {
    val = 4;
  } else if (0 == state.case_compare(ObString::make_string("FAILED"))) {
    val = 5;
  }
  return val;
}

// request column format: "<rid>|<retry>|<eligible_us>"
static void parse_request_column(const ObString &req, int64_t &rid, int64_t &retry, int64_t &eligible_us)
{
  rid = 0;
  retry = 0;
  eligible_us = 0;
  int64_t field = 0;
  int64_t start = 0;
  for (int64_t i = 0; i <= req.length(); ++i) {
    if (i == req.length() || req.ptr()[i] == '|') {
      ObString part(static_cast<int32_t>(i - start), req.ptr() + start);
      if (part.length() > 0) {
        int64_t v = strtoll(part.ptr(), NULL, 10);
        if (0 == field) {
          rid = v;
        } else if (1 == field) {
          retry = v;
        } else if (2 == field) {
          eligible_us = v;
        }
      }
      ++field;
      start = i + 1;
    }
  }
}

int ObAIColumnTaskIterator::inner_get_next_row(ObNewRow *&row)
{
  int ret = OB_SUCCESS;
  if (!start_to_read_) {
    if (OB_FAIL(fill_scanner_())) {
      LOG_WARN("fill ai column task scanner failed", K(ret));
    } else {
      start_to_read_ = true;
    }
  }
  if (OB_SUCC(ret) && start_to_read_) {
    if (OB_FAIL(scanner_it_.get_next_row(cur_row_))) {
      if (OB_ITER_END != ret) {
        LOG_WARN("get next row from ai column task scanner failed", K(ret));
      }
    } else {
      row = &cur_row_;
    }
  }
  return ret;
}

int ObAIColumnTaskIterator::fill_scanner_()
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(allocator_) || OB_ISNULL(session_) || OB_ISNULL(schema_guard_)) {
    ret = OB_NOT_INIT;
    LOG_WARN("ai column task iterator not inited", K(ret));
  } else {
    query::ObSessionInnerSql inner_sql(static_cast<void *>(session_));
    ObSEArray<AiColMeta, 8> ai_cols;
    // the per-row state/request columns are schema-hidden; resolve them through
    // the inner-session path like the background worker does
    const bool was_inner = session_->is_inner();
    if (!was_inner) {
      session_->set_inner_session();
    }
    if (OB_FAIL(load_ai_columns_(inner_sql, ai_cols))) {
      LOG_WARN("load ai columns failed", K(ret));
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < ai_cols.count(); ++i) {
      if (OB_FAIL(fill_tasks_for_column_(inner_sql, ai_cols.at(i)))) {
        LOG_WARN("fill tasks for ai column failed", K(ret), K(i));
      }
    }
    if (!was_inner) {
      session_->set_user_session();
    }
  }
  if (OB_SUCC(ret)) {
    scanner_it_ = scanner_.begin();
    start_to_read_ = true;
  }
  return ret;
}

int ObAIColumnTaskIterator::load_ai_columns_(query::ObSessionInnerSql &inner_sql,
                                             ObIArray<AiColMeta> &ai_cols)
{
  int ret = OB_SUCCESS;
  ObSqlString sql;
  ObISQLClient::ReadResult read_res;
  sqlclient::ObMySQLResult *result = NULL;
  if (OB_FAIL(sql.assign("SELECT table_id, column_id, column_name, "
                         "state_column_id, request_column_id FROM oceanbase.__all_ai_column"))) {
    LOG_WARN("build ai column meta sql failed", K(ret));
  } else if (OB_FAIL(inner_sql.execute_read(sql, read_res))) {
    LOG_WARN("read __all_ai_column failed", K(ret), K(sql));
  } else if (OB_ISNULL(result = read_res.get_result())) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("empty result for __all_ai_column", K(ret));
  }
  while (OB_SUCC(ret)) {
    if (OB_FAIL(result->next())) {
      if (OB_ITER_END == ret) {
        ret = OB_SUCCESS;
      }
      break;
    }
    AiColMeta meta;
    int64_t table_id = 0;
    int64_t column_id = 0;
    int64_t state_cid = 0;
    int64_t request_cid = 0;
    ObString col_name;
    if (OB_FAIL(result->get_int("table_id", table_id))
        || OB_FAIL(result->get_int("column_id", column_id))
        || OB_FAIL(result->get_int("state_column_id", state_cid))
        || OB_FAIL(result->get_int("request_column_id", request_cid))
        || OB_FAIL(result->get_varchar("column_name", col_name))) {
      LOG_WARN("get ai column meta row failed", K(ret));
    } else if (OB_FAIL(deep_copy_str(*allocator_, col_name, meta.column_name_))) {
      LOG_WARN("copy ai column name failed", K(ret));
    } else {
      meta.table_id_ = static_cast<uint64_t>(table_id);
      meta.column_id_ = static_cast<uint64_t>(column_id);
      meta.state_column_id_ = static_cast<uint64_t>(state_cid);
      meta.request_column_id_ = static_cast<uint64_t>(request_cid);
      if (OB_FAIL(ai_cols.push_back(meta))) {
        LOG_WARN("push back ai column meta failed", K(ret));
      }
    }
  }
  return ret;
}

int ObAIColumnTaskIterator::fill_tasks_for_column_(query::ObSessionInnerSql &inner_sql,
                                                   const AiColMeta &ai_col)
{
  int ret = OB_SUCCESS;
  const ObTableSchema *table_schema = NULL;
  const ObDatabaseSchema *db_schema = NULL;
  ObSEArray<uint64_t, 4> pk_ids;
  ObSqlString sql;
  ObISQLClient::ReadResult read_res;
  sqlclient::ObMySQLResult *result = NULL;
  if (OB_FAIL(schema_guard_->get_table_schema(ai_col.table_id_, table_schema))) {
    LOG_WARN("get ai table schema failed", K(ret), K(ai_col.table_id_));
  } else if (OB_ISNULL(table_schema)) {
    ret = OB_TABLE_NOT_EXIST;
    LOG_WARN("ai table schema is null", K(ret), K(ai_col.table_id_));
  } else if (OB_FAIL(schema_guard_->get_database_schema(table_schema->get_database_id(), db_schema))) {
    LOG_WARN("get ai table database schema failed", K(ret));
  } else if (OB_ISNULL(db_schema)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("ai table database schema is null", K(ret));
  } else if (OB_FAIL(table_schema->get_rowkey_column_ids(pk_ids))) {
    LOG_WARN("get rowkey column ids failed", K(ret));
  }

  ObString state_name;
  ObString request_name;
  ObString db_name;
  ObString table_name;
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(deep_copy_str(*allocator_, db_schema->get_database_name_str(), db_name))) {
  } else if (OB_FAIL(deep_copy_str(*allocator_, table_schema->get_table_name_str(), table_name))) {
  } else if (OB_FAIL(deep_copy_str(*allocator_, ai_col.column_name_, state_name))) {
  } else if (OB_FAIL(deep_copy_str(*allocator_, ai_col.column_name_, request_name))) {
  }

  if (OB_SUCC(ret)) {
    if (OB_FAIL(sql.append("SELECT /*+OPT_PARAM('hidden_column_visible','true')*/ "))) {
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < pk_ids.count(); ++i) {
      const ObColumnSchemaV2 *pk_col = table_schema->get_column_schema(pk_ids.at(i));
      if (OB_ISNULL(pk_col)) {
        ret = OB_ERR_UNEXPECTED;
        LOG_WARN("rowkey column schema is null", K(ret));
      } else if (i > 0 && OB_FAIL(sql.append(", "))) {
      } else if (OB_FAIL(sql.append_fmt("`%.*s` AS pk_%ld",
                                        pk_col->get_column_name_str().length(),
                                        pk_col->get_column_name_str().ptr(), i))) {
        LOG_WARN("append pk column failed", K(ret));
      }
    }
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(sql.append_fmt(", `__ai_state_%.*s` AS ai_state, `__ai_request_%.*s` AS ai_req "
                                      "FROM `%.*s`.`%.*s` WHERE `__ai_state_%.*s` <> '%s' "
                                      "AND `__ai_state_%.*s` <> '%s'",
                                      state_name.length(), state_name.ptr(),
                                      state_name.length(), state_name.ptr(),
                                      db_name.length(), db_name.ptr(),
                                      table_name.length(), table_name.ptr(),
                                      state_name.length(), state_name.ptr(), AI_TASK_STATE_READY,
                                      state_name.length(), state_name.ptr(), AI_TASK_STATE_FAILED))) {
      LOG_WARN("append ai task scan sql failed", K(ret));
    }
  }

  if (OB_SUCC(ret)) {
    if (OB_FAIL(inner_sql.execute_read(sql, read_res))) {
      LOG_WARN("scan ai column tasks failed", K(ret), K(sql));
    } else if (OB_ISNULL(result = read_res.get_result())) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("empty result for ai task scan", K(ret), K(sql));
    }
  }
  while (OB_SUCC(ret)) {
    if (OB_FAIL(result->next())) {
      if (OB_ITER_END == ret) {
        ret = OB_SUCCESS;
      }
      break;
    }
    ObString state;
    ObString req;
    if (OB_FAIL(result->get_varchar("ai_state", state))
        || OB_FAIL(result->get_varchar("ai_req", req))) {
      LOG_WARN("get ai hidden column values failed", K(ret));
    } else {
      int64_t rid = 0;
      int64_t retry = 0;
      int64_t eligible_us = 0;
      parse_request_column(req, rid, retry, eligible_us);
      // build rowkey predicate text, e.g. `id` = 2
      ObSqlString predicate;
      for (int64_t i = 0; OB_SUCC(ret) && i < pk_ids.count(); ++i) {
        ObObj pk_obj;
        const ObColumnSchemaV2 *pk_col = table_schema->get_column_schema(pk_ids.at(i));
        ObString pk_name;
        if (OB_ISNULL(pk_col)) {
          ret = OB_ERR_UNEXPECTED;
          LOG_WARN("rowkey column schema is null", K(ret));
        } else if (FALSE_IT(pk_name = pk_col->get_column_name_str())) {
        } else if (i > 0 && OB_FAIL(predicate.append(" AND "))) {
        } else if (OB_FAIL(result->get_obj(static_cast<int64_t>(i), pk_obj))) {
          LOG_WARN("get pk value failed", K(ret));
        } else if (OB_FAIL(predicate.append_fmt("`%.*s` = ", pk_name.length(), pk_name.ptr()))) {
        } else if (pk_obj.is_null()) {
          ret = OB_ERR_UNEXPECTED;
          LOG_WARN("rowkey value is null", K(ret));
        } else if (ob_is_int_tc(pk_obj.get_type()) || ob_is_uint_tc(pk_obj.get_type())) {
          int64_t v = pk_obj.get_int();
          if (OB_FAIL(predicate.append_fmt("%ld", v))) {
          }
        } else if (pk_obj.is_varchar() || pk_obj.is_char()) {
          ObString v = pk_obj.get_varchar();
          if (OB_FAIL(predicate.append_fmt("'%.*s'", v.length(), v.ptr()))) {
          }
        } else {
          ret = OB_NOT_SUPPORTED;
          LOG_USER_ERROR(OB_NOT_SUPPORTED, "AI COLUMN on rowkey of this data type is");
        }
      }
      if (OB_FAIL(ret)) {
      } else {
        ObObj *cells = cur_row_.cells_;
        if (OB_ISNULL(cells)) {
          ret = OB_ERR_UNEXPECTED;
          LOG_WARN("cells of cur row is null", K(ret));
        }
        for (int64_t j = 0; OB_SUCC(ret) && j < output_column_ids_.count(); ++j) {
          const uint64_t col_id = output_column_ids_.at(j);
          switch (col_id) {
            case AI_TASK_TABLE_ID_COL: {
              cells[j].set_int(static_cast<int64_t>(ai_col.table_id_));
              break;
            }
            case AI_TASK_COLUMN_ID_COL: {
              cells[j].set_int(static_cast<int64_t>(ai_col.column_id_));
              break;
            }
            case AI_TASK_COLUMN_NAME_COL: {
              cells[j].set_varchar(ai_col.column_name_);
              cells[j].set_collation_type(ObCharset::get_default_collation(ObCharset::get_default_charset()));
              break;
            }
            case AI_TASK_REQUEST_ID_COL: {
              cells[j].set_int(rid);
              break;
            }
            case AI_TASK_STATE_COL: {
              cells[j].set_int(ai_state_to_int(state));
              break;
            }
            case AI_TASK_RETRY_COUNT_COL: {
              cells[j].set_int(retry);
              break;
            }
            case AI_TASK_ELIGIBLE_TS_COL: {
              cells[j].set_timestamp(eligible_us);
              break;
            }
            case AI_TASK_ROWKEY_PREDICATE_COL: {
              ObString pred_str;
              if (OB_FAIL(deep_copy_str(*allocator_, ObString(
                      static_cast<int32_t>(predicate.length()), predicate.ptr()), pred_str))) {
                LOG_WARN("copy rowkey predicate failed", K(ret));
              } else {
                cells[j].set_varchar(pred_str);
                cells[j].set_collation_type(ObCharset::get_default_collation(ObCharset::get_default_charset()));
              }
              break;
            }
            default: {
              ret = OB_ERR_UNEXPECTED;
              LOG_WARN("invalid column id", K(ret), K(col_id));
              break;
            }
          }
        }
        if (OB_SUCC(ret) && OB_FAIL(scanner_.add_row(cur_row_))) {
          LOG_WARN("add ai task row failed", K(ret));
        }
      }
    }
  }
  return ret;
}

} // namespace observer
} // namespace oceanbase
