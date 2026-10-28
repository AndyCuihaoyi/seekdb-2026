#include "observer/ai_service/ob_ai_column_worker.h"

#define USING_LOG_PREFIX SHARE

#include "share/ob_server_struct.h"
#include "share/schema/ob_schema_getter_guard.h"
#include "share/schema/ob_multi_version_schema_service.h"
#include "share/schema/ob_table_schema.h"
#include "share/rc/ob_server_runtime.h"
#include "query/ai/ob_ai_endpoint_resolver.h"
#include "sql/engine/expr/ob_expr_ai/ob_ai_func_utils.h"
#include "sql/engine/expr/ob_expr_ai/ob_ai_func_client.h"
#include "common/object/ob_obj_funcs.h"
#include "lib/time/ob_time_utility.h"

namespace oceanbase
{
using namespace common;
namespace observer
{

static const char *WORKER_STATE_PREFIX = "__ai_state_";
static const char *WORKER_REQUEST_PREFIX = "__ai_request_";
static const int64_t WORK_INTERVAL_US = 100L * 1000L;
static const int64_t RETRY_DELAY_US = 500L * 1000L;
static const int64_t CANDIDATE_LIMIT = 128;
static const ObString AI_STATE_RETRY_WAIT("RETRY_WAIT");

static int deep_copy_str(ObIAllocator &allocator, const ObString &src, ObString &dst)
{
  int ret = OB_SUCCESS;
  char *buf = static_cast<char *>(allocator.alloc(src.length()));
  if (OB_ISNULL(buf)) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else {
    if (src.length() > 0) {
      MEMCPY(buf, src.ptr(), src.length());
    }
    dst.assign_ptr(buf, static_cast<int32_t>(src.length()));
  }
  return ret;
}

static int build_hidden_name(ObIAllocator &allocator, const char *prefix,
                             const ObString &column_name, ObString &out)
{
  int ret = OB_SUCCESS;
  const int64_t prefix_len = strlen(prefix);
  char *buf = static_cast<char *>(allocator.alloc(prefix_len + column_name.length()));
  if (OB_ISNULL(buf)) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else {
    MEMCPY(buf, prefix, prefix_len);
    MEMCPY(buf + prefix_len, column_name.ptr(), column_name.length());
    out.assign_ptr(buf, static_cast<int32_t>(prefix_len + column_name.length()));
  }
  return ret;
}

// Read a string field from the flat AI column spec JSON: "key":"value"
static int json_get_string(ObIAllocator &allocator, const ObString &spec,
                           const char *key, ObString &out)
{
  int ret = OB_SUCCESS;
  const int64_t key_len = strlen(key);
  for (int64_t i = 0; i + key_len + 3 <= spec.length(); ++i) {
    if (0 == MEMCMP(spec.ptr() + i, key, key_len)
        && '"' == spec.ptr()[i + key_len]
        && ':' == spec.ptr()[i + key_len + 1]
        && '"' == spec.ptr()[i + key_len + 2]) {
      int64_t start = i + key_len + 3;
      int64_t end = start;
      while (end < spec.length()) {
        if ('\\' == spec.ptr()[end] && end + 1 < spec.length()) {
          end += 2;
        } else if ('"' == spec.ptr()[end]) {
          break;
        } else {
          ++end;
        }
      }
      const int64_t raw_len = end - start;
      char *buf = static_cast<char *>(allocator.alloc(raw_len + 1));
      if (OB_ISNULL(buf)) {
        ret = OB_ALLOCATE_MEMORY_FAILED;
      } else {
        int64_t pos = 0;
        for (int64_t j = 0; j < raw_len; ++j) {
          if ('\\' == spec.ptr()[start + j] && j + 1 < raw_len) {
            ++j;
          }
          buf[pos++] = spec.ptr()[start + j];
        }
        out.assign_ptr(buf, static_cast<int32_t>(pos));
      }
      break;
    }
  }
  return ret;
}

// Read an integer field from the flat AI column spec JSON: "key":123
static int json_get_int(const ObString &spec, const char *key, int64_t &out)
{
  int ret = OB_SUCCESS;
  const int64_t key_len = strlen(key);
  for (int64_t i = 0; i + key_len + 2 <= spec.length(); ++i) {
    if (0 == MEMCMP(spec.ptr() + i, key, key_len)
        && '"' == spec.ptr()[i + key_len]
        && ':' == spec.ptr()[i + key_len + 1]) {
      out = strtoll(spec.ptr() + i + key_len + 2, NULL, 10);
      break;
    }
  }
  return ret;
}

// request column format: <rid>|<retry>|<eligible_us>
static int parse_request_column(const ObString &req, ObString &rid,
                                int64_t &retry, int64_t &eligible_us)
{
  int ret = OB_SUCCESS;
  rid.reset();
  retry = 0;
  eligible_us = 0;
  int64_t field = 0;
  int64_t start = 0;
  for (int64_t i = 0; OB_SUCC(ret) && i <= req.length(); ++i) {
    if (i == req.length() || '|' == req.ptr()[i]) {
      const ObString field_str(static_cast<int32_t>(i - start), req.ptr() + start);
      if (0 == field) {
        rid = field_str;
      } else if (1 == field) {
        retry = field_str.empty() ? 0 : strtol(field_str.ptr(), NULL, 10);
      } else if (2 == field) {
        eligible_us = field_str.empty() ? 0 : strtoll(field_str.ptr(), NULL, 10);
      }
      ++field;
      start = i + 1;
    }
  }
  return ret;
}

// append a quoted, escaped SQL string literal
static int append_str_literal(ObIAllocator &allocator, ObSqlString &sql, const ObString &value)
{
  int ret = OB_SUCCESS;
  const int64_t buf_len = value.length() * 2 + 16;
  char *buf = static_cast<char *>(allocator.alloc(buf_len));
  if (OB_ISNULL(buf)) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
  } else {
    ObHexEscapeSqlStr escaped(value);
    const int64_t pos = escaped.to_string(buf, buf_len);
    if (pos < 0 || pos >= buf_len) {
      ret = OB_SIZE_OVERFLOW;
      LOG_WARN("escape ai result string failed", K(ret), K(pos));
    } else if (OB_FAIL(sql.append_fmt("'%.*s'", static_cast<int32_t>(pos), buf))) {
      LOG_WARN("append escaped literal failed", K(ret));
    }
  }
  return ret;
}

// format an object as a SQL literal (unquoted for numbers, quoted for strings)
static int append_obj_literal(ObIAllocator &allocator, ObSqlString &sql, const ObObj &obj)
{
  int ret = OB_SUCCESS;
  if (obj.is_null()) {
    ret = OB_ERR_UNEXPECTED;
  } else if (obj.is_string_type() || obj.is_lob_storage()) {
    if (OB_FAIL(append_str_literal(allocator, sql, obj.get_string()))) {
      LOG_WARN("append string literal failed", K(ret));
    }
  } else if (ob_is_int_tc(obj.get_type())) {
    if (OB_FAIL(sql.append_fmt("%ld", obj.get_int()))) {
      LOG_WARN("append integer literal failed", K(ret));
    }
  } else if (ob_is_uint_tc(obj.get_type())) {
    if (OB_FAIL(sql.append_fmt("%lu", obj.get_uint64()))) {
      LOG_WARN("append unsigned literal failed", K(ret));
    }
  } else if (ob_is_float_tc(obj.get_type()) || ob_is_double_tc(obj.get_type())) {
    if (OB_FAIL(sql.append_fmt("%g", obj.get_double()))) {
      LOG_WARN("append double literal failed", K(ret));
    }
  } else if (ob_is_number_tc(obj.get_type())) {
    char buf[128] = {0};
    const number::ObNumber &num = obj.get_number();
    const int64_t pos = num.to_string(buf, sizeof(buf));
    if (pos <= 0 || pos >= static_cast<int64_t>(sizeof(buf))) {
      ret = OB_SIZE_OVERFLOW;
      LOG_WARN("number to string failed", K(ret), K(pos));
    } else if (OB_FAIL(sql.append_fmt("%.*s", static_cast<int>(pos), buf))) {
      LOG_WARN("append number literal failed", K(ret));
    }
  } else {
    // ObObj::to_string prints the JSON form {"BIGINT":1}, which is not a SQL literal
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "AI COLUMN on rowkey of this data type is");
  }
  return ret;
}

// convert an evaluated user expr value into AI call content text
static int obj_to_content(ObIAllocator &allocator, const ObObj &obj,
                          bool &is_null, ObString &content)
{
  int ret = OB_SUCCESS;
  is_null = obj.is_null();
  if (!is_null) {
    if (obj.is_string_type() || obj.is_lob_storage()) {
      // the ObObj string points into the result buffer, which is freed once the
      // read result dies; the content outlives it, so copy it into our allocator
      if (OB_FAIL(deep_copy_str(allocator, obj.get_string(), content))) {
        LOG_WARN("copy content failed", K(ret));
      }
    } else {
      char buf[512] = {0};
      const int64_t pos = obj.to_string(buf, sizeof(buf));
      if (pos <= 0 || pos >= static_cast<int64_t>(sizeof(buf))) {
        ret = OB_ERR_UNEXPECTED;
        LOG_WARN("obj to content string failed", K(ret));
      } else if (OB_FAIL(deep_copy_str(allocator, ObString(static_cast<int32_t>(pos), buf), content))) {
        LOG_WARN("copy content failed", K(ret));
      }
    }
  }
  return ret;
}

ObAIColumnWorker::ObAIColumnWorker()
    : sql_proxy_(NULL), allocator_(ObMemAttr("AiColWorker")), inited_(false)
{
}

ObAIColumnWorker::~ObAIColumnWorker()
{
}

int ObAIColumnWorker::init(ObMySQLProxy &sql_proxy, ObTimer &timer)
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(inited_)) {
    ret = OB_INIT_TWICE;
    LOG_WARN("ai column worker already inited", K(ret));
  } else {
    sql_proxy_ = &sql_proxy;
    inited_ = true;
    disable_timeout_check();
    if (OB_FAIL(timer.schedule(*this, WORK_INTERVAL_US, true /*repeat*/))) {
      LOG_WARN("schedule ai column worker failed", K(ret));
    }
  }
  return ret;
}

void ObAIColumnWorker::runTimerTask()
{
  int ret = OB_SUCCESS;
  if (OB_UNLIKELY(!inited_)) {
    ret = OB_NOT_INIT;
  } else if (OB_FAIL(process())) {
    LOG_WARN("ai column worker process failed", K(ret));
  }
}

int ObAIColumnWorker::process()
{
  int ret = OB_SUCCESS;
  allocator_.reuse();
  share::schema::ObSchemaGetterGuard guard;
  ObSEArray<AiColumnMeta, 16> metas;
  if (OB_ISNULL(GCTX.schema_service_) || OB_ISNULL(sql_proxy_)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("ai column worker not ready", K(ret));
  } else if (OB_FAIL(GCTX.schema_service_->get_runtime_schema_guard(guard))) {
    LOG_WARN("get schema guard failed", K(ret));
  } else if (OB_FAIL(load_ai_columns(guard, metas))) {
    LOG_WARN("load ai columns failed", K(ret));
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < metas.count(); ++i) {
    bool processed = false;
    for (int64_t j = 0; !processed && j < i; ++j) {
      processed = (metas.at(j).table_id_ == metas.at(i).table_id_);
    }
    if (OB_SUCC(ret) && !processed) {
      int tmp_ret = process_table(guard, metas.at(i).table_id_, metas);
      if (OB_SUCCESS != tmp_ret) {
        // a stale ai column entry must not block every other table's tasks
        LOG_WARN("process ai column table failed", K(tmp_ret), K(metas.at(i).table_id_));
      }
    }
  }
  return ret;
}

int ObAIColumnWorker::load_ai_columns(share::schema::ObSchemaGetterGuard &guard,
                                      ObIArray<AiColumnMeta> &metas)
{
  int ret = OB_SUCCESS;
  UNUSED(guard);
  common::ObISQLClient::ReadResult read_res;
  if (OB_ISNULL(sql_proxy_)) {
    ret = OB_NOT_INIT;
  } else if (OB_FAIL(sql_proxy_->read(read_res,
      "SELECT table_id, column_id, column_name, max_retries, normalized_expr FROM __all_ai_column"))) {
    LOG_WARN("read __all_ai_column failed", K(ret));
  } else {
    sqlclient::ObMySQLResult *result = read_res.get_result();
    if (OB_ISNULL(result)) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      while (OB_SUCC(ret)) {
        int tmp_ret = result->next();
        if (OB_ITER_END == tmp_ret) {
          break;
        } else if (OB_SUCCESS != tmp_ret) {
          ret = tmp_ret;
          LOG_WARN("iterate __all_ai_column failed", K(ret));
        } else {
          AiColumnMeta meta;
          int64_t table_id = OB_INVALID_ID;
          int64_t column_id = OB_INVALID_ID;
          int64_t max_retries = 1;
          ObString column_name;
          ObString spec_json;
          if (OB_FAIL(result->get_int("table_id", table_id))
              || OB_FAIL(result->get_int("column_id", column_id))
              || OB_FAIL(result->get_int("max_retries", max_retries))
              || OB_FAIL(result->get_varchar("column_name", column_name))
              || OB_FAIL(result->get_varchar("normalized_expr", spec_json))) {
            LOG_WARN("get ai column meta row failed", K(ret));
          } else {
            meta.table_id_ = static_cast<uint64_t>(table_id);
            meta.column_id_ = static_cast<uint64_t>(column_id);
            meta.max_retries_ = max_retries;
            if (OB_FAIL(deep_copy_str(allocator_, column_name, meta.column_name_))) {
            } else if (OB_FAIL(deep_copy_str(allocator_, spec_json, meta.spec_json_))) {
            } else if (OB_FAIL(parse_spec_json(meta))) {
              LOG_WARN("parse ai column spec failed", K(ret));
            } else if (OB_FAIL(metas.push_back(meta))) {
              LOG_WARN("push back ai column meta failed", K(ret));
            }
          }
        }
      }
    }
  }
  return ret;
}

int ObAIColumnWorker::parse_spec_json(AiColumnMeta &meta)
{
  int ret = OB_SUCCESS;
  ObString ai_func;
  if (OB_FAIL(json_get_string(allocator_, meta.spec_json_, "ai_func", ai_func))) {
  } else {
    meta.is_embed_ = (0 == ai_func.case_compare(ObString::make_string("AI_EMBED")));
  }
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(json_get_string(allocator_, meta.spec_json_, "model", meta.model_name_))) {
  } else if (OB_FAIL(json_get_string(allocator_, meta.spec_json_, "system", meta.system_prompt_))) {
  } else if (OB_FAIL(json_get_string(allocator_, meta.spec_json_, "user", meta.user_expr_))) {
  } else if (OB_FAIL(json_get_int(meta.spec_json_, "dim", meta.embed_dim_))) {
  } else if (OB_FAIL(json_get_int(meta.spec_json_, "max_retries", meta.max_retries_))) {
  }
  if (meta.max_retries_ < 0) {
    meta.max_retries_ = 0;
  }
  return ret;
}

int ObAIColumnWorker::process_table(share::schema::ObSchemaGetterGuard &guard,
                                    const uint64_t table_id,
                                    const ObIArray<AiColumnMeta> &metas)
{
  int ret = OB_SUCCESS;
  const share::schema::ObTableSchema *table_schema = NULL;
  const share::schema::ObDatabaseSchema *db_schema = NULL;
  ObString db_name;
  ObString table_name;
  bool table_ok = false;
  if (OB_FAIL(guard.get_table_schema(table_id, table_schema))) {
    LOG_WARN("get table schema failed", K(ret), K(table_id));
  } else if (OB_ISNULL(table_schema) || table_schema->is_in_recyclebin()
             || !table_schema->is_user_table()) {
    // skip: table dropped or not a user table
  } else if (OB_FAIL(guard.get_database_schema(table_schema->get_database_id(), db_schema))) {
    LOG_WARN("get database schema failed", K(ret));
  } else if (OB_ISNULL(db_schema)) {
    ret = OB_ERR_UNEXPECTED;
  } else {
    db_name = db_schema->get_database_name_str();
    table_name = table_schema->get_table_name_str();
    table_ok = true;
  }
  ObSEArray<const AiColumnMeta *, 4> cols;
  ObSEArray<ObString, 4> state_cols;
  ObSEArray<ObString, 4> request_cols;
  ObSqlString select_list;
  ObSqlString pending_pred;
  ObArray<uint64_t> rowkey_ids;
  for (int64_t i = 0; OB_SUCC(ret) && table_ok && i < metas.count(); ++i) {
    if (metas.at(i).table_id_ == table_id && OB_FAIL(cols.push_back(&metas.at(i)))) {
      LOG_WARN("push ai column meta failed", K(ret));
    }
  }
  if (OB_FAIL(ret) || !table_ok || cols.empty()) {
  } else if (OB_FAIL(table_schema->get_rowkey_info().get_column_ids(rowkey_ids))) {
    LOG_WARN("get rowkey column ids failed", K(ret));
  }
  for (int64_t j = 0; OB_SUCC(ret) && table_ok && !cols.empty() && j < rowkey_ids.count(); ++j) {
    const share::schema::ObColumnSchemaV2 *rk_col =
        table_schema->get_column_schema(rowkey_ids.at(j));
    if (OB_ISNULL(rk_col)) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("rowkey column schema is null", K(ret), K(rowkey_ids.at(j)));
    } else if (OB_FAIL(select_list.append_fmt("`%s`, ", rk_col->get_column_name()))) {
      LOG_WARN("append rowkey column failed", K(ret));
    }
  }
  for (int64_t i = 0; OB_SUCC(ret) && table_ok && !cols.empty() && i < cols.count(); ++i) {
    ObString state_col;
    ObString request_col;
    if (OB_FAIL(build_hidden_name(allocator_, WORKER_STATE_PREFIX,
                                  cols.at(i)->column_name_, state_col))) {
      LOG_WARN("build state column name failed", K(ret));
    } else if (OB_FAIL(build_hidden_name(allocator_, WORKER_REQUEST_PREFIX,
                                         cols.at(i)->column_name_, request_col))) {
      LOG_WARN("build request column name failed", K(ret));
    } else if (OB_FAIL(state_cols.push_back(state_col))) {
      LOG_WARN("push state column failed", K(ret));
    } else if (OB_FAIL(request_cols.push_back(request_col))) {
      LOG_WARN("push request column failed", K(ret));
    } else if (OB_FAIL(select_list.append_fmt(
            "%s`%.*s` AS st_%ld, `%.*s` AS rq_%ld",
            0 == i ? "" : ", ",
            static_cast<int32_t>(state_col.length()), state_col.ptr(), i,
            static_cast<int32_t>(request_col.length()), request_col.ptr(), i))) {
      LOG_WARN("append state column failed", K(ret));
    } else if (OB_FAIL(pending_pred.append_fmt(
            "%s`%.*s` IN ('PENDING','RETRY_WAIT')",
            0 == i ? "" : " OR ",
            static_cast<int32_t>(state_col.length()), state_col.ptr()))) {
      LOG_WARN("append pending predicate failed", K(ret));
    }
  }
  if (OB_FAIL(ret) || !table_ok || cols.empty()) {
  } else {
    ObSqlString sql;
    if (OB_FAIL(sql.assign_fmt(
        "SELECT %.*s FROM `%.*s`.`%.*s` WHERE (%s) LIMIT %ld",
        static_cast<int32_t>(select_list.length()), select_list.ptr(),
        static_cast<int32_t>(db_name.length()), db_name.ptr(),
        static_cast<int32_t>(table_name.length()), table_name.ptr(),
        pending_pred.string().ptr(), CANDIDATE_LIMIT))) {
      LOG_WARN("build candidate sql failed", K(ret));
    } else {
      common::ObISQLClient::ReadResult read_res;
      if (OB_FAIL(sql_proxy_->read(read_res, sql.ptr()))) {
        LOG_WARN("select ai column candidates failed", K(ret), K(sql));
      } else {
        sqlclient::ObMySQLResult *result = read_res.get_result();
        if (OB_ISNULL(result)) {
          ret = OB_ERR_UNEXPECTED;
        } else {
          while (OB_SUCC(ret)) {
            int tmp_ret = result->next();
            if (OB_ITER_END == tmp_ret) {
              break;
            } else if (OB_SUCCESS != tmp_ret) {
              ret = tmp_ret;
            } else {
              ObSEArray<ColumnTask, 4> tasks;
              for (int64_t i = 0; OB_SUCC(ret) && i < cols.count(); ++i) {
                ObObj state_obj;
                ObObj request_obj;
                char st_alias[16] = {0};
                char rq_alias[16] = {0};
                snprintf(st_alias, sizeof(st_alias), "st_%ld", i);
                snprintf(rq_alias, sizeof(rq_alias), "rq_%ld", i);
                if (OB_FAIL(result->get_obj(st_alias, state_obj))
                    || OB_FAIL(result->get_obj(rq_alias, request_obj))) {
                  LOG_WARN("get ai state row failed", K(ret));
                } else if (state_obj.is_null()) {
                  // no state for this column, nothing to do
                } else {
                  const ObString state = state_obj.get_varchar();
                  const bool pending = (0 == state.case_compare(ObString::make_string("PENDING"))
                                        || 0 == state.case_compare(AI_STATE_RETRY_WAIT));
                  if (!pending) {
                    // column is not waiting for the worker
                  } else {
                    ColumnTask task;
                    task.meta_ = cols.at(i);
                    task.state_col_ = state_cols.at(i);
                    task.request_col_ = request_cols.at(i);
                    task.state_ = state;
                    // a row that was never claimed yet carries no request token
                    if (!request_obj.is_null()) {
                      task.request_ = request_obj.get_varchar();
                    }
                    if (OB_FAIL(tasks.push_back(task))) {
                      LOG_WARN("push column task failed", K(ret));
                    }
                  }
                }
              }
              // one DML stamps the same request id on every column it touched, so
              // keep only the first task's request group and publish it atomically
              if (OB_SUCC(ret) && !tasks.empty()) {
                ObString group_rid;
                int64_t unused_retry = 0;
                int64_t unused_eligible = 0;
                ObSEArray<ColumnTask, 4> group;
                if (OB_FAIL(parse_request_column(tasks.at(0).request_, group_rid,
                                                 unused_retry, unused_eligible))) {
                  LOG_WARN("parse request column failed", K(ret));
                } else if (OB_FAIL(group.push_back(tasks.at(0)))) {
                  LOG_WARN("push group task failed", K(ret));
                }
                for (int64_t i = 1; OB_SUCC(ret) && i < tasks.count(); ++i) {
                  ObString rid;
                  int64_t retry = 0;
                  int64_t eligible = 0;
                  if (OB_FAIL(parse_request_column(tasks.at(i).request_, rid,
                                                   retry, eligible))) {
                    LOG_WARN("parse request column failed", K(ret));
                  } else if (rid == group_rid && OB_FAIL(group.push_back(tasks.at(i)))) {
                    LOG_WARN("push group task failed", K(ret));
                  }
                }
                ObString rowkey_pred;
                if (OB_FAIL(ret)) {
                } else if (OB_FAIL(build_rowkey_pred(*table_schema, *result, rowkey_pred))) {
                  LOG_WARN("build rowkey predicate failed", K(ret));
                } else if (OB_FAIL(process_row(db_name, table_name, rowkey_pred, group))) {
                  LOG_WARN("process ai column row failed", K(ret));
                }
              }
              tasks.reset();
            }
          }
          if (OB_ITER_END == ret) {
            ret = OB_SUCCESS;
          }
        }
      }
    }
  }
  return ret;
}

int ObAIColumnWorker::build_rowkey_pred(const share::schema::ObTableSchema &table_schema,
                                        sqlclient::ObMySQLResult &result,
                                        ObString &rowkey_pred)
{
  int ret = OB_SUCCESS;
  ObArray<uint64_t> rowkey_ids;
  ObSqlString pred;
  if (OB_FAIL(table_schema.get_rowkey_info().get_column_ids(rowkey_ids))) {
    LOG_WARN("get rowkey column ids failed", K(ret));
  } else if (rowkey_ids.empty()) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("table has no rowkey for ai column", K(ret));
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < rowkey_ids.count(); ++i) {
    const share::schema::ObColumnSchemaV2 *col = table_schema.get_column_schema(rowkey_ids.at(i));
    ObObj value;
    if (OB_ISNULL(col)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(result.get_obj(col->get_column_name(), value))) {
      LOG_WARN("get rowkey value failed", K(ret), K(col->get_column_name()));
    } else if (value.is_null()) {
      ret = OB_ERR_UNEXPECTED;
    } else if (i > 0 && OB_FAIL(pred.append(" AND "))) {
    } else if (OB_FAIL(pred.append_fmt("`%.*s` = ",
                 static_cast<int32_t>(strlen(col->get_column_name())), col->get_column_name()))) {
    } else if (OB_FAIL(append_obj_literal(allocator_, pred, value))) {
    }
  }
  if (OB_SUCC(ret)) {
    // pred's buffer is a stack temporary, the predicate has to outlive this call
    ObString copied;
    if (OB_FAIL(deep_copy_str(allocator_, pred.string(), copied))) {
      LOG_WARN("copy rowkey predicate failed", K(ret));
    } else {
      rowkey_pred = copied;
    }
  }
  return ret;
}

int ObAIColumnWorker::process_row(const ObString &db_name,
                                  const ObString &table_name,
                                  const ObString &rowkey_pred,
                                  const ObIArray<ColumnTask> &tasks)
{
  int ret = OB_SUCCESS;
  const int64_t now = ObTimeUtility::current_time();
  bool not_eligible = false;
  for (int64_t i = 0; OB_SUCC(ret) && !not_eligible && i < tasks.count(); ++i) {
    ObString rid;
    int64_t retry = 0;
    int64_t eligible_us = 0;
    if (OB_FAIL(parse_request_column(tasks.at(i).request_, rid, retry, eligible_us))) {
      LOG_WARN("parse request column failed", K(ret), K(tasks.at(i).request_));
    } else if (0 == tasks.at(i).state_.case_compare(AI_STATE_RETRY_WAIT)
               && eligible_us > now) {
      not_eligible = true;
    }
  }
  if (OB_FAIL(ret) || not_eligible || tasks.empty()) {
  } else {
    // claim the whole request group with one CAS statement: a group is only ever
    // published with one UPDATE, so no column of the group can become visible
    // while another column of the same request is still mid-flight
    ObSqlString claim_sql;
    ObSEArray<ObString, 4> claimed;
    if (OB_FAIL(claim_sql.assign_fmt(
        "UPDATE `%.*s`.`%.*s` SET",
        static_cast<int32_t>(db_name.length()), db_name.ptr(),
        static_cast<int32_t>(table_name.length()), table_name.ptr()))) {
      LOG_WARN("build claim sql failed", K(ret));
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < tasks.count(); ++i) {
      ObString rid;
      int64_t retry = 0;
      int64_t eligible_us = 0;
      ObSqlString token;
      ObString token_copy;
      if (OB_FAIL(parse_request_column(tasks.at(i).request_, rid, retry, eligible_us))) {
        LOG_WARN("parse request column failed", K(ret));
      } else if (OB_FAIL(token.assign_fmt("%ld|%ld|0", now, retry))) {
        LOG_WARN("format claim token failed", K(ret));
      } else if (OB_FAIL(deep_copy_str(allocator_, token.string(), token_copy))) {
        LOG_WARN("copy claim token failed", K(ret));
      } else if (OB_FAIL(claimed.push_back(token_copy))) {
        LOG_WARN("push claim token failed", K(ret));
      } else if (OB_FAIL(claim_sql.append_fmt(
              "%s `%.*s` = 'RUNNING', `%.*s` = ",
              0 == i ? "" : ",",
              static_cast<int32_t>(tasks.at(i).state_col_.length()), tasks.at(i).state_col_.ptr(),
              static_cast<int32_t>(tasks.at(i).request_col_.length()), tasks.at(i).request_col_.ptr()))) {
        LOG_WARN("append claim set failed", K(ret));
      } else if (OB_FAIL(append_str_literal(allocator_, claim_sql, token_copy))) {
        LOG_WARN("append claim token failed", K(ret));
      }
    }
    int64_t affected = 0;
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(claim_sql.append_fmt(" WHERE %.*s",
               static_cast<int32_t>(rowkey_pred.length()), rowkey_pred.ptr()))) {
      LOG_WARN("append claim where failed", K(ret));
    }
    for (int64_t i = 0; OB_SUCC(ret) && i < tasks.count(); ++i) {
      if (OB_FAIL(claim_sql.append_fmt(
              " AND `%.*s` IN ('PENDING','RETRY_WAIT')",
              static_cast<int32_t>(tasks.at(i).state_col_.length()), tasks.at(i).state_col_.ptr()))) {
        LOG_WARN("append claim guard failed", K(ret));
      }
    }
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(sql_proxy_->write(claim_sql.ptr(), affected))) {
      LOG_WARN("claim ai column row failed", K(ret), K(claim_sql));
    } else if (1 != affected) {
      // lost the race or the row changed concurrently, skip the whole group
    } else {
      ObSEArray<bool, 4> call_oks;
      ObSEArray<ObString, 4> results;
      for (int64_t i = 0; OB_SUCC(ret) && i < tasks.count(); ++i) {
        bool is_null_content = false;
        bool call_ok = false;
        ObString content;
        ObString result;
        int eval_ret = eval_user_expr(db_name, table_name, *tasks.at(i).meta_,
                                      rowkey_pred, is_null_content, content);
        if (OB_SUCCESS != eval_ret) {
          // a row stuck in RUNNING would block every later strict read, fold the
          // internal failure into the retry/failure path instead of propagating it
          LOG_WARN("eval ai column user expr failed", K(eval_ret));
        } else if (is_null_content) {
          call_ok = true;
        } else {
          const int call_ret = call_ai(*tasks.at(i).meta_, content, result);
          if (OB_SUCCESS != call_ret) {
            LOG_WARN("call ai failed", K(call_ret), K(tasks.at(i).meta_->column_name_));
          } else {
            call_ok = true;
          }
        }
        if (OB_FAIL(ret)) {
        } else if (OB_FAIL(call_oks.push_back(call_ok))) {
          LOG_WARN("push call ok failed", K(ret));
        } else if (OB_FAIL(results.push_back(result))) {
          LOG_WARN("push result failed", K(ret));
        }
      }
      if (OB_SUCC(ret)) {
        // one publish statement for the whole group, guarded by every claimed token
        ObSqlString sql;
        if (OB_FAIL(sql.assign_fmt(
            "UPDATE `%.*s`.`%.*s` SET",
            static_cast<int32_t>(db_name.length()), db_name.ptr(),
            static_cast<int32_t>(table_name.length()), table_name.ptr()))) {
          LOG_WARN("build publish sql failed", K(ret));
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < tasks.count(); ++i) {
          const AiColumnMeta &meta = *tasks.at(i).meta_;
          ObString rid;
          int64_t retry = 0;
          int64_t eligible_us = 0;
          if (OB_FAIL(parse_request_column(claimed.at(i), rid, retry, eligible_us))) {
            LOG_WARN("parse claimed token failed", K(ret));
          } else if (call_oks.at(i)) {
            if (OB_FAIL(sql.append_fmt(
                    "%s `%.*s` = ",
                    0 == i ? "" : ",",
                    static_cast<int32_t>(meta.column_name_.length()), meta.column_name_.ptr()))) {
              LOG_WARN("append result set failed", K(ret));
            } else if (OB_FAIL(append_str_literal(allocator_, sql, results.at(i)))) {
              LOG_WARN("append result literal failed", K(ret));
            } else if (OB_FAIL(sql.append_fmt(
                    ", `%.*s` = 'READY'",
                    static_cast<int32_t>(tasks.at(i).state_col_.length()), tasks.at(i).state_col_.ptr()))) {
              LOG_WARN("append ready set failed", K(ret));
            }
          } else if (retry + 1 > meta.max_retries_) {
            if (OB_FAIL(sql.append_fmt(
                    "%s `%.*s` = 'FAILED'",
                    0 == i ? "" : ",",
                    static_cast<int32_t>(tasks.at(i).state_col_.length()), tasks.at(i).state_col_.ptr()))) {
              LOG_WARN("append failed set failed", K(ret));
            }
          } else {
            const int64_t eligible = now + RETRY_DELAY_US;
            ObSqlString new_token;
            if (OB_FAIL(new_token.assign_fmt("%.*s|%ld|%ld",
                    static_cast<int32_t>(rid.length()), rid.ptr(), retry + 1, eligible))) {
              LOG_WARN("format retry token failed", K(ret));
            } else if (OB_FAIL(sql.append_fmt(
                    "%s `%.*s` = 'RETRY_WAIT', `%.*s` = ",
                    0 == i ? "" : ",",
                    static_cast<int32_t>(tasks.at(i).state_col_.length()), tasks.at(i).state_col_.ptr(),
                    static_cast<int32_t>(tasks.at(i).request_col_.length()), tasks.at(i).request_col_.ptr()))) {
              LOG_WARN("append retry set failed", K(ret));
            } else if (OB_FAIL(append_str_literal(allocator_, sql, new_token.string()))) {
              LOG_WARN("append retry token failed", K(ret));
            }
          }
        }
        for (int64_t i = 0; OB_SUCC(ret) && i < tasks.count(); ++i) {
          if (OB_FAIL(sql.append_fmt(
                  "%s `%.*s` = ",
                  0 == i ? " WHERE" : " AND",
                  static_cast<int32_t>(tasks.at(i).request_col_.length()), tasks.at(i).request_col_.ptr()))) {
            LOG_WARN("append publish guard failed", K(ret));
          } else if (OB_FAIL(append_str_literal(allocator_, sql, claimed.at(i)))) {
            LOG_WARN("append publish token failed", K(ret));
          }
        }
        if (OB_FAIL(ret)) {
        } else if (OB_FAIL(sql.append_fmt(" AND %.*s",
                   static_cast<int32_t>(rowkey_pred.length()), rowkey_pred.ptr()))) {
          LOG_WARN("append publish where failed", K(ret));
        } else if (OB_FAIL(sql_proxy_->write(sql.ptr(), affected))) {
          LOG_WARN("publish ai result write failed", K(ret), K(sql));
        }
        // a publish that guards out means the row changed concurrently; the new
        // request tokens will be picked up on a later cycle
      }
      // AI call errors have been folded into RETRY_WAIT/FAILED, do not propagate
      ret = OB_SUCCESS;
    }
  }
  return ret;
}

int ObAIColumnWorker::eval_user_expr(const ObString &db_name,
                                     const ObString &table_name,
                                     const AiColumnMeta &meta,
                                     const ObString &rowkey_pred,
                                     bool &is_null_value,
                                     ObString &content)
{
  int ret = OB_SUCCESS;
  is_null_value = false;
  ObSqlString sql;
  if (OB_FAIL(sql.assign_fmt("SELECT (%.*s) AS u FROM `%.*s`.`%.*s` WHERE %.*s",
      static_cast<int32_t>(meta.user_expr_.length()), meta.user_expr_.ptr(),
      static_cast<int32_t>(db_name.length()), db_name.ptr(),
      static_cast<int32_t>(table_name.length()), table_name.ptr(),
      static_cast<int32_t>(rowkey_pred.length()), rowkey_pred.ptr()))) {
    LOG_WARN("build user expr sql failed", K(ret));
  } else {
    common::ObISQLClient::ReadResult read_res;
    if (OB_FAIL(sql_proxy_->read(read_res, sql.ptr()))) {
      LOG_WARN("eval user expr failed", K(ret), K(sql));
    } else {
      sqlclient::ObMySQLResult *result = read_res.get_result();
      if (OB_ISNULL(result)) {
        ret = OB_ERR_UNEXPECTED;
      } else if (OB_ITER_END == result->next()) {
        // row disappeared, treat as null
        is_null_value = true;
      } else {
        ObObj value;
        if (OB_FAIL(result->get_obj("u", value))) {
          LOG_WARN("get user expr value failed", K(ret));
        } else if (OB_FAIL(obj_to_content(allocator_, value, is_null_value, content))) {
          LOG_WARN("convert user expr value failed", K(ret));
        }
      }
    }
  }
  return ret;
}

int ObAIColumnWorker::call_ai(const AiColumnMeta &meta, const ObString &content,
                              ObString &result)
{
  int ret = OB_SUCCESS;
  ObAIFuncExprInfo *info = NULL;
  share::ObAiModelEndpointInfo resolved_endpoint;
  query::ObIAiEndpointResolver *endpoint_resolver =
      ::oceanbase::share::server_service<::oceanbase::query::ObIAiEndpointResolver>();
  if (OB_ISNULL(endpoint_resolver)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (OB_FAIL(ObAIFuncUtils::get_ai_func_info(allocator_, meta.model_name_, info))) {
    LOG_WARN("get ai func info failed", K(ret), K(meta.model_name_));
  } else if (OB_FAIL(endpoint_resolver->resolve_by_model_name(
                 meta.model_name_, allocator_, resolved_endpoint))) {
    LOG_WARN("resolve ai endpoint failed", K(ret), K(meta.model_name_));
  } else if (OB_ISNULL(info)) {
    ret = OB_ERR_UNEXPECTED;
  } else if (meta.is_embed_) {
    ObJsonObject *config = NULL;
    ObString call_content = content;
    if (meta.embed_dim_ > 0) {
      ObJsonInt *dim_json = NULL;
      if (OB_FAIL(ObAIFuncJsonUtils::get_json_object(allocator_, config))) {
        LOG_WARN("alloc embed config failed", K(ret));
      } else if (OB_FAIL(ObAIFuncJsonUtils::get_json_int(allocator_, meta.embed_dim_, dim_json))) {
        LOG_WARN("alloc dimensions failed", K(ret));
      } else if (OB_FAIL(config->add("dimensions", dim_json))) {
        LOG_WARN("add dimensions failed", K(ret));
      }
    }
    if (OB_SUCC(ret)) {
      ObAIFuncModel model(allocator_, *info, resolved_endpoint);
      if (OB_FAIL(model.call_dense_embedding(call_content, config, result))) {
        LOG_WARN("ai embed call failed", K(ret));
      }
    }
  } else {
    // completion: the column system prompt is sent as the system message and the
    // evaluated user expression as the user message
    ObArray<ObString> headers;
    ObJsonObject *body = NULL;
    ObJsonObject *response = NULL;
    ObIJsonBase *result_base = NULL;
    ObAIFuncIComplete *complete_provider = NULL;
    ObString unencrypted_access_key;
    ObString request_model_name = info->model_;
    ObString system_prompt = meta.system_prompt_;
    ObString user_content = content;
    if (!resolved_endpoint.get_request_model_name().empty()) {
      request_model_name = resolved_endpoint.get_request_model_name();
    }
    if (OB_FAIL(ObAIFuncUtils::get_complete_provider(
            allocator_, resolved_endpoint.get_provider(), complete_provider))) {
      LOG_WARN("get complete provider failed", K(ret));
    } else if (OB_ISNULL(complete_provider)) {
      ret = OB_ERR_UNEXPECTED;
    } else if (OB_FAIL(resolved_endpoint.get_unencrypted_access_key(
                   allocator_, unencrypted_access_key))) {
      LOG_WARN("decrypt access key failed", K(ret));
    } else if (OB_FAIL(complete_provider->get_header(
                   allocator_, unencrypted_access_key, headers))) {
      LOG_WARN("build ai header failed", K(ret));
    } else if (OB_FAIL(complete_provider->get_body(
                   allocator_, request_model_name, system_prompt, user_content, NULL, body))) {
      LOG_WARN("build ai body failed", K(ret));
    } else {
      ObAIFuncClient client;
      if (OB_FAIL(client.send_post(allocator_, resolved_endpoint.get_url(), headers, body, response))) {
        LOG_WARN("ai complete call failed", K(ret));
      } else if (OB_FAIL(complete_provider->parse_output(allocator_, response, result_base))) {
        LOG_WARN("parse ai complete output failed", K(ret));
      } else if (OB_FAIL(ObAIFuncJsonUtils::print_json_to_str(allocator_, result_base, result))) {
        LOG_WARN("print ai complete result failed", K(ret));
      }
    }
  }
  return ret;
}

} // namespace observer
} // namespace oceanbase
