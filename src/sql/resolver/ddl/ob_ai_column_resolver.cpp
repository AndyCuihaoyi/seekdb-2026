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

#include "sql/resolver/ddl/ob_ai_column_resolver.h"

#include "sql/resolver/ddl/ob_ddl_resolver.h"
#include "sql/parser/parse_node.h"
#include "query/vector/ob_vector_index_util.h"

namespace oceanbase
{
using namespace common;
namespace sql
{

bool is_ai_hidden_column_name(const ObString &name, const char *prefix)
{
  const int64_t prefix_len = strlen(prefix);
  return name.length() > prefix_len
      && 0 == MEMCMP(name.ptr(), prefix, prefix_len);
}

int build_ai_hidden_column_name(ObIAllocator &allocator,
                                const char *prefix,
                                const ObString &column_name,
                                ObString &out_name)
{
  int ret = OB_SUCCESS;
  const int64_t prefix_len = strlen(prefix);
  const int64_t total = prefix_len + column_name.length();
  char *buf = static_cast<char *>(allocator.alloc(total));
  if (OB_ISNULL(buf)) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
    LOG_WARN("alloc memory for ai hidden column name failed", K(ret), K(total));
  } else {
    MEMCPY(buf, prefix, prefix_len);
    MEMCPY(buf + prefix_len, column_name.ptr(), column_name.length());
    out_name.assign_ptr(buf, static_cast<int32_t>(total));
  }
  return ret;
}

static int write_escaped_json_string(char *buf, const int64_t buf_len, int64_t &pos,
                                     const ObString &value)
{
  int ret = OB_SUCCESS;
  if (pos + value.length() * 2 + 3 > buf_len) {
    ret = OB_SIZE_OVERFLOW;
  } else {
    buf[pos++] = '"';
    for (int64_t i = 0; i < value.length(); ++i) {
      const char c = value.ptr()[i];
      if (c == '"' || c == '\\') {
        buf[pos++] = '\\';
      }
      buf[pos++] = c;
    }
    buf[pos++] = '"';
  }
  return ret;
}

static int write_deps_json(char *buf, const int64_t buf_len, int64_t &pos,
                           const ObIArray<ObString> &deps)
{
  int ret = OB_SUCCESS;
  if (pos + 9 > buf_len) {
    ret = OB_SIZE_OVERFLOW;
  } else {
    MEMCPY(buf + pos, ",\"deps\":[", 9);
    pos += 9;
    for (int64_t i = 0; OB_SUCC(ret) && i < deps.count(); ++i) {
      if (i > 0) {
        if (pos + 1 > buf_len) {
          ret = OB_SIZE_OVERFLOW;
        } else {
          buf[pos++] = ',';
        }
      }
      if (OB_FAIL(ret)) {
      } else if (OB_FAIL(write_escaped_json_string(buf, buf_len, pos, deps.at(i)))) {
        LOG_WARN("write dep name json failed", K(ret), K(i));
      }
    }
    if (OB_SUCC(ret)) {
      if (pos + 1 > buf_len) {
        ret = OB_SIZE_OVERFLOW;
      } else {
        buf[pos++] = ']';
      }
    }
  }
  return ret;
}

static int build_spec_json(ObIAllocator &allocator, const ObAIColumnMeta &meta,
                           const ObString &system_prompt, const ObString &user_expr,
                           const ObIArray<ObString> &deps,
                           ObString &spec_json)
{
  int ret = OB_SUCCESS;
  int64_t deps_len = 16;
  for (int64_t i = 0; i < deps.count(); ++i) {
    deps_len += deps.at(i).length() * 2 + 8;
  }
  const int64_t buf_len = user_expr.length() + system_prompt.length() + meta.model_name_.length() + deps_len + 256;
  char *buf = static_cast<char *>(allocator.alloc(buf_len));
  if (OB_ISNULL(buf)) {
    ret = OB_ALLOCATE_MEMORY_FAILED;
    LOG_WARN("alloc memory for ai column spec failed", K(ret), K(buf_len));
  } else {
    int64_t pos = 0;
    const char *func = meta.is_embed_ ? "AI_EMBED" : "AI_COMPLETE";
    const ObString func_str = ObString::make_string(func);
    if (OB_FAIL(ret)) {
    } else if (pos + 12 > buf_len) {
      ret = OB_SIZE_OVERFLOW;
    } else {
      MEMCPY(buf + pos, "{\"ai_func\":\"", 12);
      pos += 12;
      if (OB_FAIL(write_escaped_json_string(buf, buf_len, pos, func_str))) {
      } else if (pos + 9 > buf_len) {
        ret = OB_SIZE_OVERFLOW;
      } else {
        MEMCPY(buf + pos, ",\"model\":", 9);
        pos += 9;
        if (OB_FAIL(write_escaped_json_string(buf, buf_len, pos, meta.model_name_))) {
        } else if (pos + 10 > buf_len) {
          ret = OB_SIZE_OVERFLOW;
        } else {
          MEMCPY(buf + pos, ",\"system\":", 10);
          pos += 10;
          if (OB_FAIL(write_escaped_json_string(buf, buf_len, pos, system_prompt))) {
          } else if (pos + 8 > buf_len) {
            ret = OB_SIZE_OVERFLOW;
          } else {
            MEMCPY(buf + pos, ",\"user\":", 8);
            pos += 8;
            if (OB_FAIL(write_escaped_json_string(buf, buf_len, pos, user_expr))) {
            } else {
              // dependency column names, e.g. "deps":["col_a","col_b"]
              if (deps.count() > 0 && OB_FAIL(write_deps_json(buf, buf_len, pos, deps))) {
                LOG_WARN("write ai column deps json failed", K(ret));
              } else {
              char tail[64] = {0};
              int64_t tail_len = 0;
              if (meta.embed_dim_ >= 0) {
                tail_len = snprintf(tail, sizeof(tail), ",\"max_retries\":%ld,\"dim\":%ld}",
                                    meta.max_retries_, meta.embed_dim_);
              } else {
                tail_len = snprintf(tail, sizeof(tail), ",\"max_retries\":%ld}",
                                    meta.max_retries_);
              }
              if (pos + tail_len > buf_len) {
                ret = OB_SIZE_OVERFLOW;
              } else {
                MEMCPY(buf + pos, tail, tail_len);
                pos += tail_len;
                spec_json.assign_ptr(buf, static_cast<int32_t>(pos));
              }
              }
            }
          }
        }
      }
    }
  }
  return ret;
}

// Extract the raw SQL text of the second argument of FUNC(arg0, arg1[, arg2]) from
// the full raw text of the AI function call. Only our own validated call syntax is scanned.
static int extract_second_arg_text(const ObString &raw, ObString &out)
{
  int ret = OB_SUCCESS;
  int64_t paren_depth = 0;
  bool in_quote = false;
  int64_t start = -1;
  int64_t end = -1;
  int64_t top_commas = 0;
  for (int64_t i = 0; OB_SUCC(ret) && i < raw.length(); ++i) {
    const char c = raw.ptr()[i];
    if (in_quote) {
      if (c == '\\' && i + 1 < raw.length()) {
        ++i;
      } else if (c == '\'') {
        in_quote = false;
      }
    } else if (c == '\'') {
      in_quote = true;
    } else if (c == '(') {
      ++paren_depth;
    } else if (c == ')') {
      if (0 == paren_depth) {
        break;
      }
      --paren_depth;
      if (0 == paren_depth && end < 0) {
        // this paren closes the ai function call itself, it is not part of the argument
        end = i;
      }
    } else if (c == ',' && 1 == paren_depth) {
      ++top_commas;
      if (1 == top_commas) {
        start = i + 1;
      } else if (2 == top_commas) {
        end = i;
        break;
      }
    }
  }
  if (OB_SUCC(ret)) {
    if (start < 0) {
      ret = OB_ERR_UNEXPECTED;
      LOG_WARN("cannot locate second argument in ai column expression", K(raw));
    } else if (end < 0) {
      end = raw.length();
    }
    // trim spaces
    while (start < end && isspace(static_cast<unsigned char>(raw.ptr()[start]))) {
      ++start;
    }
    while (end > start && isspace(static_cast<unsigned char>(raw.ptr()[end - 1]))) {
      --end;
    }
    out.assign_ptr(raw.ptr() + start, static_cast<int32_t>(end - start));
  }
  return ret;
}

static int collect_column_refs(ParseNode *node, ObIArray<ObString> &refs)
{
  int ret = OB_SUCCESS;
  if (OB_ISNULL(node)) {
    // nothing
  } else if (T_COLUMN_REF == node->type_) {
    // children: [db, table, column]; the column name is the last child
    ParseNode *col_node = node->children_[node->num_child_ - 1];
    if (OB_ISNULL(col_node)) {
      ret = OB_ERR_UNEXPECTED;
    } else {
      ObString name(static_cast<int32_t>(col_node->str_len_), col_node->str_value_);
      bool found = false;
      for (int64_t i = 0; !found && i < refs.count(); ++i) {
        found = (0 == refs.at(i).case_compare(name));
      }
      if (!found && OB_FAIL(refs.push_back(name))) {
        LOG_WARN("push back column ref failed", K(ret));
      }
    }
  } else {
    for (int64_t i = 0; OB_SUCC(ret) && i < node->num_child_; ++i) {
      if (OB_FAIL(SMART_CALL(collect_column_refs(node->children_[i], refs)))) {
        LOG_WARN("collect column refs failed", K(ret));
      }
    }
  }
  return ret;
}

int resolve_ai_column_attribute(ObSQLSessionInfo *session_info,
                                ObIAllocator &allocator,
                                share::schema::ObColumnSchemaV2 &column,
                                ParseNode *attr_node,
                                const ObIArray<share::schema::ObColumnSchemaV2> *declared_cols,
                                const ObIArray<ObString> &existing_ai_columns,
                                ObAIColumnMeta &meta)
{
  int ret = OB_SUCCESS;
  ParseNode *expr_node = NULL;
  ParseNode *opts_node = NULL;
  if (OB_ISNULL(attr_node) || T_AI_COLUMN_ATTR != attr_node->type_ || 2 != attr_node->num_child_) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("invalid ai column attr node", K(ret));
  } else {
    expr_node = attr_node->children_[0];
    opts_node = attr_node->children_[1];
  }
  // AI_COMPLETE/AI_EMBED are not reserved words: the parser produces a generic
  // T_FUN_SYS call whose children_[0] is the function name node and children_[1]
  // is the T_EXPR_LIST of arguments.
  ParseNode *name_node = NULL;
  ParseNode *args_node = NULL;
  if (OB_FAIL(ret)) {
  } else if (OB_ISNULL(expr_node) || T_FUN_SYS != expr_node->type_
             || expr_node->num_child_ < 2 || OB_ISNULL(expr_node->children_[0])
             || OB_ISNULL(expr_node->children_[1])) {
    ret = OB_NOT_SUPPORTED;
    LOG_USER_ERROR(OB_NOT_SUPPORTED, "AI COLUMN expression, expect AI_COMPLETE or AI_EMBED");
  } else {
    name_node = expr_node->children_[0];
    args_node = expr_node->children_[1];
    if (OB_ISNULL(name_node->str_value_)) {
      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "AI COLUMN expression, expect AI_COMPLETE or AI_EMBED");
    } else if (args_node->num_child_ < 2
               || OB_ISNULL(args_node->children_[0]) || OB_ISNULL(args_node->children_[1])) {
      ret = OB_ERR_PARSER_SYNTAX;
      LOG_USER_ERROR(OB_ERR_PARSER_SYNTAX, "AI COLUMN expression");
    }
  }
  if (OB_FAIL(ret)) {
  } else {
    ObString func_name(static_cast<int32_t>(name_node->str_len_), name_node->str_value_);
    meta.is_embed_ = (0 == func_name.case_compare(ObString::make_string("ai_embed")));
    if (!meta.is_embed_
        && 0 != func_name.case_compare(ObString::make_string("ai_complete"))) {
      ret = OB_NOT_SUPPORTED;
      LOG_USER_ERROR(OB_NOT_SUPPORTED, "AI COLUMN expression, expect AI_COMPLETE or AI_EMBED");
    }
  }
  if (OB_FAIL(ret)) {
  } else if (T_VARCHAR != args_node->children_[0]->type_ || OB_ISNULL(args_node->children_[0]->str_value_)) {
    ret = OB_INVALID_ARGUMENT;
    LOG_USER_ERROR(OB_INVALID_ARGUMENT, "AI COLUMN model name must be a string literal");
  } else if (OB_ISNULL(expr_node->str_value_)) {
    ret = OB_ERR_UNEXPECTED;
    LOG_WARN("ai column expression raw text is missing", K(ret));
  }

  if (OB_SUCC(ret)) {
    meta.column_name_.assign_ptr(
        const_cast<char *>(column.get_column_name_str().ptr()),
        column.get_column_name_str().length());
    meta.column_id_ = column.get_column_id();
    meta.model_name_.assign_ptr(
        const_cast<char *>(args_node->children_[0]->str_value_),
        static_cast<int32_t>(args_node->children_[0]->str_len_));
  }

  // embedding dimension argument and vector column dimension check
  if (OB_FAIL(ret)) {
  } else if (meta.is_embed_ && args_node->num_child_ >= 3 && NULL != args_node->children_[2]) {
    ParseNode *dim_node = args_node->children_[2];
    if (T_INT != dim_node->type_ && T_UINT64 != dim_node->type_ && T_NUMBER != dim_node->type_) {
      ret = OB_INVALID_ARGUMENT;
      LOG_USER_ERROR(OB_INVALID_ARGUMENT, "AI_EMBED dimension must be an integer literal");
    } else {
      meta.embed_dim_ = static_cast<int64_t>(dim_node->value_);
    }
  }
  if (OB_FAIL(ret)) {
  } else if (meta.is_embed_) {
    int64_t col_dim = 0;
    if (OB_FAIL(ObVectorIndexUtil::get_vector_dim_from_extend_type_info(
            column.get_extended_type_info(), col_dim))) {
      ret = OB_ERR_BAD_VEC_INDEX_COLUMN;
      LOG_USER_ERROR(OB_ERR_BAD_VEC_INDEX_COLUMN, column.get_column_name_str().length(),
                     column.get_column_name_str().ptr());
    } else if (meta.embed_dim_ >= 0 && col_dim != meta.embed_dim_) {
      ret = OB_ERR_INVALID_VECTOR_DIM;
      LOG_USER_ERROR(OB_ERR_INVALID_VECTOR_DIM, static_cast<int32_t>(col_dim),
                     static_cast<int32_t>(meta.embed_dim_));
    }
  }

  // dependency column references
  ObSEArray<ObString, 4> refs;
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(collect_column_refs(args_node, refs))) {
  } else {
    for (int64_t i = 0; OB_SUCC(ret) && i < refs.count(); ++i) {
      const ObString &ref = refs.at(i);
      bool is_ai_column = false;
      for (int64_t j = 0; !is_ai_column && j < existing_ai_columns.count(); ++j) {
        is_ai_column = (0 == existing_ai_columns.at(j).case_compare(ref));
      }
      if (is_ai_column) {
        ret = OB_NOT_SUPPORTED;
        LOG_USER_ERROR(OB_NOT_SUPPORTED, "AI COLUMN depending on another AI column is");
      } else {
        bool found = false;
        if (NULL != declared_cols) {
          for (int64_t j = 0; !found && j < declared_cols->count(); ++j) {
            found = (0 == declared_cols->at(j).get_column_name_str().case_compare(ref));
          }
          if (!found) {
            ret = OB_ERR_BAD_FIELD_ERROR;
            LOG_USER_ERROR(OB_ERR_BAD_FIELD_ERROR, ref.length(), ref.ptr(),
                           column.get_column_name_str().length(), column.get_column_name_str().ptr());
          }
        }
      }
    }
  }

  // options
  int64_t max_retries = 1;
  if (OB_FAIL(ret)) {
  } else if (NULL != opts_node) {
    ParseNode *opt_list = opts_node;
    while (OB_SUCC(ret) && NULL != opt_list) {
      ParseNode *opt = NULL;
      if (T_LINK_NODE == opt_list->type_) {
        opt = opt_list->children_[1];
        opt_list = opt_list->children_[0];
      } else {
        opt = opt_list;
        opt_list = NULL;
      }
      if (OB_ISNULL(opt) || T_AI_COLUMN_OPTION != opt->type_ || 2 != opt->num_child_) {
        ret = OB_ERR_UNEXPECTED;
      } else {
        ObString key(static_cast<int32_t>(opt->children_[0]->str_len_), opt->children_[0]->str_value_);
        ParseNode *value = opt->children_[1];
        if (0 == key.case_compare(ObString("sync_mode"))) {
          ObString mode(static_cast<int32_t>(value->str_len_), value->str_value_);
          if (0 != mode.case_compare(ObString("async"))) {
            ret = OB_NOT_SUPPORTED;
            LOG_USER_ERROR(OB_NOT_SUPPORTED, "AI COLUMN sync_mode other than async is");
          }
        } else if (0 == key.case_compare(ObString("max_retries"))) {
          max_retries = static_cast<int64_t>(value->value_);
          if (max_retries < 0) {
            ret = OB_INVALID_ARGUMENT;
            LOG_USER_ERROR(OB_INVALID_ARGUMENT, "AI COLUMN max_retries must be non-negative");
          }
        } else {
          ret = OB_INVALID_ARGUMENT;
          LOG_USER_ERROR(OB_INVALID_ARGUMENT, "unknown AI COLUMN option");
        }
      }
    }
  }
  if (OB_SUCC(ret)) {
    meta.max_retries_ = max_retries;
  }

  // system prompt by result column type and spec json
  ObString system_prompt;
  ObString user_expr;
  if (OB_FAIL(ret)) {
  } else if (OB_FAIL(extract_second_arg_text(
                 ObString(static_cast<int32_t>(expr_node->str_len_), expr_node->str_value_),
                 user_expr))) {
  } else if (ob_is_json_tc(column.get_data_type())) {
    system_prompt = ObString::make_string(AI_COLUMN_JSON_SYSTEM_PROMPT);
  } else if (column.get_meta_type().is_integer_type()) {
    system_prompt = ObString::make_string(AI_COLUMN_INTEGER_SYSTEM_PROMPT);
  }
  if (OB_SUCC(ret)) {
    ObString spec_json;
    if (OB_FAIL(build_spec_json(allocator, meta, system_prompt, user_expr, refs, spec_json))) {
      LOG_WARN("build ai column spec json failed", K(ret));
    } else {
      meta.spec_json_ = spec_json;
      meta.user_expr_ = user_expr;
    }
  }
  return ret;
}

static int set_hidden_column_common(share::schema::ObColumnSchemaV2 &col,
                                    uint64_t column_id,
                                    const ObString &name,
                                    ObObjType type,
                                    int64_t length)
{
  int ret = OB_SUCCESS;
  col.reset();
  col.set_column_id(column_id);
  if (OB_FAIL(col.set_column_name(name))) {
    LOG_WARN("set ai hidden column name failed", K(ret));
  } else {
    col.set_data_type(type);
    ObAccuracy accuracy(ObAccuracy::DDL_DEFAULT_ACCURACY[type]);
    accuracy.set_length(static_cast<int32_t>(length));
    col.set_accuracy(accuracy);
    col.set_charset_type(CHARSET_UTF8MB4);
    col.set_collation_type(CS_TYPE_UTF8MB4_BIN);
    col.set_is_hidden(true);
    col.set_nullable(true);
  }
  return ret;
}

int build_ai_hidden_columns(ObIAllocator &allocator,
                            const ObAIColumnMeta &meta,
                            uint64_t state_column_id,
                            uint64_t request_column_id,
                            uint64_t spec_column_id,
                            ObIArray<share::schema::ObColumnSchemaV2> &resolved_cols)
{
  int ret = OB_SUCCESS;
  ObString state_name;
  ObString request_name;
  ObString spec_name;
  if (OB_FAIL(build_ai_hidden_column_name(allocator, AI_COLUMN_STATE_PREFIX, meta.column_name_, state_name))) {
  } else if (OB_FAIL(build_ai_hidden_column_name(allocator, AI_COLUMN_REQUEST_PREFIX, meta.column_name_, request_name))) {
  } else if (OB_FAIL(build_ai_hidden_column_name(allocator, AI_COLUMN_SPEC_PREFIX, meta.column_name_, spec_name))) {
  }
  if (OB_SUCC(ret)) {
    share::schema::ObColumnSchemaV2 state_col;
    share::schema::ObColumnSchemaV2 request_col;
    share::schema::ObColumnSchemaV2 spec_col;
    if (OB_FAIL(set_hidden_column_common(state_col, state_column_id, state_name, ObVarcharType, 32))) {
    } else {
      ObObj default_state;
      default_state.set_varchar(ObString::make_string(AI_COLUMN_STATE_PENDING));
      default_state.set_collation_type(CS_TYPE_UTF8MB4_BIN);
      default_state.set_collation_level(CS_LEVEL_IMPLICIT);
      if (OB_FAIL(state_col.set_cur_default_value(default_state, false))) {
        LOG_WARN("set state default failed", K(ret));
      }
    }
    if (OB_FAIL(ret)) {
    } else if (OB_FAIL(set_hidden_column_common(request_col, request_column_id, request_name, ObVarcharType, 128))) {
    } else if (OB_FAIL(set_hidden_column_common(spec_col, spec_column_id, spec_name, ObVarcharType, 4096))) {
    } else {
      ObObj default_spec;
      default_spec.set_varchar(meta.spec_json_);
      default_spec.set_collation_type(CS_TYPE_UTF8MB4_BIN);
      default_spec.set_collation_level(CS_LEVEL_IMPLICIT);
      if (OB_FAIL(spec_col.set_cur_default_value(default_spec, false))) {
        LOG_WARN("set spec default failed", K(ret));
      }
    }
    if (OB_SUCC(ret)) {
      if (OB_FAIL(resolved_cols.push_back(state_col))) {
      } else if (OB_FAIL(resolved_cols.push_back(request_col))) {
      } else if (OB_FAIL(resolved_cols.push_back(spec_col))) {
      }
    }
  }
  return ret;
}

} // namespace sql
} // namespace oceanbase
