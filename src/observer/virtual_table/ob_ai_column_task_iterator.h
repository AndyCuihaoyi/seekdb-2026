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

#ifndef OCEANBASE_OBSERVER_VIRTUAL_TABLE_OB_AI_COLUMN_TASK_ITERATOR_H_
#define OCEANBASE_OBSERVER_VIRTUAL_TABLE_OB_AI_COLUMN_TASK_ITERATOR_H_

#include "observer/virtual_table/ob_virtual_table_scanner_iterator.h"

namespace oceanbase
{
namespace query
{
class ObSessionInnerSql;
}
namespace observer
{
// __all_ai_column_task derives its rows from the per-row hidden state/request
// columns of user tables that own AI columns, so it must read through the
// querying session's inner connection to keep in-transaction visibility.
class ObAIColumnTaskIterator : public common::ObVirtualTableScannerIterator
{
public:
  ObAIColumnTaskIterator() {}
  virtual ~ObAIColumnTaskIterator() {}
  virtual int inner_get_next_row(common::ObNewRow *&row) override;

private:
  struct AiColMeta
  {
    uint64_t table_id_;
    uint64_t column_id_;
    common::ObString column_name_;
    uint64_t state_column_id_;
    uint64_t request_column_id_;
    AiColMeta() : table_id_(0), column_id_(0), state_column_id_(0), request_column_id_(0) {}
  };
  int fill_scanner_();
  int load_ai_columns_(query::ObSessionInnerSql &inner_sql,
                       common::ObIArray<AiColMeta> &ai_cols);
  int fill_tasks_for_column_(query::ObSessionInnerSql &inner_sql,
                             const AiColMeta &ai_col);

private:
  DISALLOW_COPY_AND_ASSIGN(ObAIColumnTaskIterator);
};

} // namespace observer
} // namespace oceanbase

#endif // OCEANBASE_OBSERVER_VIRTUAL_TABLE_OB_AI_COLUMN_TASK_ITERATOR_H_
