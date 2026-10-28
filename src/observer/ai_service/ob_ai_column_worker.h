#pragma once

#include "lib/task/ob_timer.h"
#include "lib/allocator/page_arena.h"
#include "common/mysqlclient/ob_mysql_proxy.h"

namespace oceanbase
{
namespace share
{
namespace schema
{
class ObSchemaGetterGuard;
class ObTableSchema;
}
}
namespace observer
{

// Background worker that scans AI column hidden state, invokes the AI service for
// PENDING/RETRY_WAIT rows and publishes results back into the hidden state columns.
class ObAIColumnWorker : public common::ObTimerTask
{
public:
  ObAIColumnWorker();
  virtual ~ObAIColumnWorker();

  int init(common::ObMySQLProxy &sql_proxy, common::ObTimer &timer);
  virtual void runTimerTask() override;

private:
  struct AiColumnMeta
  {
    uint64_t table_id_;
    uint64_t column_id_;
    common::ObString column_name_;
    common::ObString spec_json_;
    int64_t max_retries_;
    common::ObString model_name_;
    common::ObString system_prompt_;
    common::ObString user_expr_;
    bool is_embed_;
    int64_t embed_dim_;

    AiColumnMeta() { reset(); }
    void reset()
    {
      table_id_ = OB_INVALID_ID;
      column_id_ = OB_INVALID_ID;
      max_retries_ = 1;
      is_embed_ = false;
      embed_dim_ = -1;
    }
  };

  int process();
  int load_ai_columns(share::schema::ObSchemaGetterGuard &guard,
                      common::ObIArray<AiColumnMeta> &metas);
  int parse_spec_json(AiColumnMeta &meta);
  int process_table(share::schema::ObSchemaGetterGuard &guard,
                    const uint64_t table_id,
                    const common::ObIArray<AiColumnMeta> &metas);
  int process_row(const share::schema::ObTableSchema &table_schema,
                  const common::ObString &db_name,
                  const common::ObString &table_name,
                  const AiColumnMeta &meta,
                  const common::ObString &state_col,
                  const common::ObString &request_col,
                  const common::ObString &rowkey_pred,
                  const common::ObString &state,
                  const common::ObString &request);
  int build_rowkey_pred(const share::schema::ObTableSchema &table_schema,
                        sqlclient::ObMySQLResult &result,
                        common::ObString &rowkey_pred);
  int eval_user_expr(const common::ObString &db_name,
                     const common::ObString &table_name,
                     const AiColumnMeta &meta,
                     const common::ObString &rowkey_pred,
                     bool &is_null_value,
                     common::ObString &content);
  int call_ai(const AiColumnMeta &meta, const common::ObString &content,
              common::ObString &result);
  int publish_result(const common::ObString &db_name,
                     const common::ObString &table_name,
                     const AiColumnMeta &meta,
                     const common::ObString &state_col,
                     const common::ObString &request_col,
                     const common::ObString &rowkey_pred,
                     const common::ObString &claimed_request,
                     const int64_t retry,
                     bool call_ok,
                     const common::ObString &result);

  common::ObMySQLProxy *sql_proxy_;
  common::ObArenaAllocator allocator_;
  bool inited_;
  DISALLOW_COPY_AND_ASSIGN(ObAIColumnWorker);
};

} // namespace observer
} // namespace oceanbase
