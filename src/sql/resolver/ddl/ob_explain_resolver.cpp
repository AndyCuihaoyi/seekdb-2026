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

#define USING_LOG_PREFIX SQL_RESV
#include "sql/resolver/ddl/ob_explain_resolver.h"
#include "lib/string/ob_sql_string.h"
#include "sql/parser/ob_parser.h"
#include "sql/resolver/ob_resolver.h"

using namespace oceanbase;
using namespace sql;
using namespace common;
const static int64_t EXPLAIN_FORMAT = 0;
const static int64_t EXPLAIN_DISPLAY_OPTION = 1;
const static int64_t EXPLAIN_INTO_TABLE = 2;
const static int64_t EXPLAIN_STMT_ID = 3;
const static int64_t EXPLAIN_CHILD_STMT = 4;
const static int64_t EXPLAIN_IS_DESCRIBE = 5;

static int append_sql_string_literal(ObSqlString &sql, const ObString &value)
{
  int ret = OB_SUCCESS;
  if (OB_FAIL(sql.append("'"))) {
    LOG_WARN("failed to append sql literal", K(ret));
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < value.length(); ++i) {
    const char c = value.ptr()[i];
    if ('\'' == c || '\\' == c) {
      if (OB_FAIL(sql.append(&c, 1))) {
        LOG_WARN("failed to append sql literal", K(ret));
      }
    }
    if (OB_SUCC(ret) && OB_FAIL(sql.append(&c, 1))) {
      LOG_WARN("failed to append sql literal", K(ret));
    }
  }
  if (OB_SUCC(ret) && OB_FAIL(sql.append("'"))) {
    LOG_WARN("failed to append sql literal", K(ret));
  }
  return ret;
}

// DESCRIBE of a file table returns the inferred schema instead of a plan,
// mirroring SHOW COLUMNS output (column_name/column_type/null/key/default/extra).
static int build_describe_file_sql(const ObFileScanInfo &info, ObSqlString &sql)
{
  int ret = OB_SUCCESS;
  const int64_t column_cnt = info.get_column_count();
  if (OB_UNLIKELY(column_cnt <= 0)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("file table has no inferred column", K(ret));
  } else if (OB_FAIL(sql.append(
      "select column_name, column_type, `null`, `key`, `default`, extra from ("))) {
    LOG_WARN("failed to build describe sql", K(ret));
  }
  for (int64_t i = 0; OB_SUCC(ret) && i < column_cnt; ++i) {
    if (i > 0 && OB_FAIL(sql.append(" union all "))) {
      LOG_WARN("failed to build describe sql", K(ret));
    } else if (OB_FAIL(sql.append_fmt("select %ld as rn, ", i))) {
      LOG_WARN("failed to build describe sql", K(ret));
    } else if (OB_FAIL(append_sql_string_literal(sql, info.column_names_.at(i)))) {
    } else if (OB_FAIL(sql.append(" as column_name, "))) {
      LOG_WARN("failed to build describe sql", K(ret));
    } else if (OB_FAIL(append_sql_string_literal(
                 sql, ObString(ob_file_logical_type_name(info.column_types_.at(i)))))) {
    } else if (OB_FAIL(sql.append(" as column_type, "))) {
      LOG_WARN("failed to build describe sql", K(ret));
    } else if (OB_FAIL(append_sql_string_literal(
                 sql, info.column_nullable_.at(i) ? ObString("YES") : ObString("NO")))) {
    } else if (OB_FAIL(sql.append(" as `null`, null as `key`, null as `default`, null as extra"))) {
      LOG_WARN("failed to build describe sql", K(ret));
    }
  }
  if (OB_SUCC(ret) && OB_FAIL(sql.append(") describe_file_schema order by rn"))) {
    LOG_WARN("failed to build describe sql", K(ret));
  }
  return ret;
}

const ObFileScanInfo *ObExplainResolver::get_describe_file_info(ObDMLStmt *child_stmt)
{
  const ObFileScanInfo *file_info = NULL;
  ObSelectStmt *select_stmt = NULL;
  if (OB_NOT_NULL(child_stmt) && child_stmt->is_select_stmt()) {
    select_stmt = static_cast<ObSelectStmt*>(child_stmt);
    if (1 == select_stmt->get_table_items().count()) {
      TableItem *table_item = select_stmt->get_table_items().at(0);
      if (OB_NOT_NULL(table_item) && table_item->is_values_table() &&
          OB_NOT_NULL(table_item->values_table_def_) &&
          ObValuesTableDef::ACCESS_FILE == table_item->values_table_def_->access_type_ &&
          OB_NOT_NULL(table_item->values_table_def_->file_info_)) {
        file_info = table_item->values_table_def_->file_info_;
      }
    }
  }
  return file_info;
}

int ObExplainResolver::resolve_describe_file_table(ObDMLStmt *child_stmt, ObDMLStmt *&describe_stmt)
{
  int ret = OB_SUCCESS;
  describe_stmt = NULL;
  const ObFileScanInfo *file_info = get_describe_file_info(child_stmt);
  if (OB_ISNULL(file_info)) {
    // not a file table select, keep the regular explain behavior
  } else if (OB_ISNULL(params_.allocator_) || OB_ISNULL(session_info_)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("resolver is not inited", K(ret));
  } else {
    ObSqlString sql;
    ObParser parser(*params_.allocator_, session_info_->get_sql_mode(),
                    session_info_->get_charsets4parser());
    ParseResult parse_result;
    if (OB_FAIL(build_describe_file_sql(*file_info, sql))) {
    } else if (OB_FAIL(parser.parse(sql.string(), parse_result))) {
      LOG_WARN("failed to parse describe sql", K(ret), K(sql));
    } else if (OB_ISNULL(parse_result.result_tree_) ||
               OB_UNLIKELY(parse_result.result_tree_->num_child_ < 1) ||
               OB_ISNULL(parse_result.result_tree_->children_[0]) ||
               OB_UNLIKELY(T_SELECT != parse_result.result_tree_->children_[0]->type_)) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("unexpected describe sql parse tree", K(ret), K(sql));
    } else {
      ObResolver resolver(params_);
      ObStmt *resolved_stmt = NULL;
      if (OB_FAIL(resolver.resolve(ObResolver::IS_NOT_PREPARED_STMT,
                                   *parse_result.result_tree_->children_[0],
                                   resolved_stmt))) {
      } else if (OB_ISNULL(resolved_stmt) || OB_UNLIKELY(!resolved_stmt->is_select_stmt())) {
        ret = OB_ERR_UNEXPECTED;
        LOG_WARN("unexpected describe stmt", K(ret), K(sql));
      } else {
        describe_stmt = static_cast<ObDMLStmt*>(resolved_stmt);
      }
    }
  }
  return ret;
}

int ObExplainResolver::resolve(const ParseNode &parse_tree)
{
  int ret = OB_SUCCESS;
  ObExplainStmt *explain_stmt = NULL;
  if (!(T_EXPLAIN == parse_tree.type_  && 6 == parse_tree.num_child_)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("invalid EXPLAIN syntax",
             "type", parse_tree.type_,
             "# of child", parse_tree.num_child_);
  } else if (NULL == (explain_stmt = create_stmt<ObExplainStmt>())) {
    ret = OB_SQL_RESOLVER_NO_MEMORY;
    LOG_WARN("failed to create explain stmt");
  } else {
    ParseNode *child_node = parse_tree.children_[EXPLAIN_CHILD_STMT];
    ObStmt *child_stmt = NULL;
    ObExplainDisplayOpt opt;
    opt.with_tree_line_ = true;
    if (OB_ISNULL(child_node)) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("empty parse tree for stmt");
    } else if (!(T_SELECT == child_node->type_
                 || T_DELETE == child_node->type_
                 || T_INSERT == child_node->type_
                 || T_UPDATE == child_node->type_
                 || T_REPLACE == child_node->type_)) {
      ret = OB_NOT_SUPPORTED;
      LOG_WARN("invalid stmt type for EXPLAIN", "type", child_node->type_);
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "stmt type for EXPLAIN");
    } else {
      ObResolver resolver(params_);
      if (OB_FAIL(resolver.resolve(ObResolver::IS_NOT_PREPARED_STMT,
                                   *child_node,
                                   child_stmt))) {
      } else if (OB_ISNULL(child_stmt)) {
        ret = OB_ERR_UNEXPECTED;
        LOG_WARN("failed to generate stmt for child stmt");
      } else {
        explain_stmt->set_explain_query_stmt(static_cast<ObDMLStmt *>(child_stmt));
        ParseNode *format = parse_tree.children_[EXPLAIN_FORMAT];
        if (NULL != format) {
          switch (format->type_) {
          case T_OUTLINE:
          {
            explain_stmt->set_explain_format(EXPLAIN_OUTLINE);
            opt.with_tree_line_ = true;
          } break;
          case T_EXTENDED:
          {
            explain_stmt->set_explain_format(EXPLAIN_EXTENDED);
            opt.with_tree_line_ = true;
          } break;
          case T_PARTITIONS:
          {
            explain_stmt->set_explain_format(EXPLAIN_PARTITIONS);
            opt.with_tree_line_ = true;
          } break;
          case T_TRADITIONAL:
          {
            explain_stmt->set_explain_format(EXPLAIN_TRADITIONAL);
            opt.with_tree_line_ = true;
          } break;
          case T_FORMAT_JSON:
          {
            explain_stmt->set_explain_format(EXPLAIN_FORMAT_JSON);
            opt.with_tree_line_ = false;
          } break;
          case T_BASIC: {
            explain_stmt->set_explain_format(EXPLAIN_BASIC);
            opt.with_tree_line_ = true;
            break;
          }
          case T_EXTENDED_NOADDR: {
            explain_stmt->set_explain_format(EXPLAIN_EXTENDED_NOADDR);
            opt.with_tree_line_ = true;
            break;
          }
          case T_PLANREGRESS: {
            explain_stmt->set_explain_format(EXPLAIN_PLANREGRESS);
            opt.with_tree_line_ = true;
            break;
          }
          default:
            ret = OB_ERROR;
            LOG_WARN("unknown explain format", "format", format->type_);
            break;
          }
        }
      }
    }
    ParseNode *opt_node = parse_tree.children_[EXPLAIN_DISPLAY_OPTION];
    if (OB_SUCC(ret) && NULL != opt_node) {
      switch (opt_node->type_) {
      case T_PRETTY_COLOR:
        opt.with_color_ = true;
        // pass
      case T_PRETTY:
        opt.with_tree_line_ = true;
        break;
      default:
        ret = OB_ERR_UNEXPECTED;
      }
    }
    if (OB_SUCC(ret)) {
      explain_stmt->set_display_opt(opt);
    }
    ParseNode *into_table_node = parse_tree.children_[EXPLAIN_INTO_TABLE];
    if (OB_SUCC(ret) && NULL != into_table_node) {
      ObString into_table(into_table_node->str_len_, into_table_node->str_value_);
      explain_stmt->set_into_table(into_table);
    }
    ParseNode *statement_id_node = parse_tree.children_[EXPLAIN_STMT_ID];
    if (OB_SUCC(ret) && NULL != statement_id_node) {
      ObString statement_id(statement_id_node->str_len_, statement_id_node->str_value_);
      explain_stmt->set_statement_id(statement_id);
    }
    if (OB_SUCC(ret) && NULL != parse_tree.children_[EXPLAIN_IS_DESCRIBE]) {
      ObDMLStmt *describe_stmt = NULL;
      if (OB_FAIL(resolve_describe_file_table(explain_stmt->get_explain_query_stmt(),
                                              describe_stmt))) {
      } else if (NULL != describe_stmt) {
        set_basic_stmt(describe_stmt);
      }
    }
  }

  return ret;
}
