//! Statement execution: DDL, DML and queries.

#![allow(dead_code)]

pub mod query;

use crate::engine::{Engine, QueryResult, TxnState};
use crate::error::{SqlError, SqlResult};
use crate::exec::query::describe_expr;
use crate::sql::ast::*;
use crate::storage::format::encode_row;
use crate::storage::table::ColValue;
use crate::value::str as vstr;

impl Engine {
    /// Execute one parsed statement, dispatching to the right handler.
    pub(crate) fn dispatch(&mut self, stmt: &Statement) -> SqlResult<QueryResult> {
        match stmt {
            Statement::CreateTable(ct) => self.exec_create_table(ct),
            Statement::CreateVectorIndex(ci) => self.exec_create_vector_index(ci),
            Statement::Insert(i) => self.exec_insert(i),
            Statement::Select(s) => self.exec_select(s),
            Statement::Update(u) => self.exec_update(u),
            Statement::Delete(d) => self.exec_delete(d),
            Statement::Begin => self.exec_begin(),
            Statement::Commit => self.exec_commit(),
            Statement::Rollback => self.exec_rollback(),
            Statement::SetMode { ann } => {
                self.mode = if *ann {
                    crate::config::SearchMode::Ann
                } else {
                    crate::config::SearchMode::Exact
                };
                Ok(QueryResult::ok())
            }
        }
    }

    // -------------------------------------------------------------------- DDL

    fn exec_create_table(&mut self, ct: &CreateTable) -> SqlResult<QueryResult> {
        // `CREATE TABLE IF NOT EXISTS` is idempotent: when the table is
        // already there the statement succeeds and the existing table is
        // left exactly as it is.
        if ct.if_not_exists && self.catalog.get(&ct.table).is_some() {
            return Ok(QueryResult::ok());
        }
        let def = self.catalog.build_table_def(&ct.table, ct.columns.clone())?;
        let table_id = self.next_table_id;
        self.next_table_id += 1;

        let schema = crate::engine::encode_schema(&def);
        self.wal
            .log_ddl(table_id, def.name.as_bytes(), &schema)
            .map_err(|e| SqlError::internal(format!("wal write failed: {e}")))?;
        self.install_table(table_id, def.name.clone(), def)?;
        // The catalog is written after the log record, so that a crash in
        // between leaves the table known through the log.
        self.write_catalog()?;
        Ok(QueryResult::ok())
    }

    fn exec_create_vector_index(&mut self, ci: &CreateVectorIndex) -> SqlResult<QueryResult> {
        let key = ci.table.to_ascii_lowercase();
        let state = self
            .tables
            .get(&key)
            .ok_or_else(|| SqlError::no_such_table(&ci.table))?;
        let vec_idx = state.def.vector_column.ok_or_else(|| {
            SqlError::not_supported("CREATE VECTOR INDEX requires a vector column")
        })?;
        let col_idx = state
            .def
            .column_index(&ci.column)
            .ok_or_else(|| SqlError::no_such_column(&ci.column))?;
        if col_idx != vec_idx {
            return Err(SqlError::not_supported(
                "the index column must be the table's vector column",
            ));
        }
        let dim = state.def.vector_dim().unwrap_or(0);
        let pk_index = state.def.pk_index;
        let table_id = state.id;
        let nlist = self.config.ann_nlist;

        // Build the index over the rows that are currently visible.
        let index = {
            let state = self
                .tables
                .get_mut(&key)
                .ok_or_else(|| SqlError::no_such_table(&ci.table))?;
            crate::vector::index::build_index(
                dim,
                nlist,
                &mut state.store,
                pk_index,
                vec_idx,
            )?
        };

        // Record the existence of the index so that it is rebuilt after a
        // restart. The index itself is not persisted: the statement allows
        // it to be rebuilt from the persisted records.
        let dir = self.table_dir(&ci.table);
        crate::vector::index::write_marker(&dir)?;

        if let Some(state) = self.tables.get_mut(&key) {
            state.index = Some(index);
        }
        let _ = table_id;
        Ok(QueryResult::ok())
    }

    // -------------------------------------------------------------------- DML

    fn exec_insert(&mut self, ins: &Insert) -> SqlResult<QueryResult> {
        let key = ins.table.to_ascii_lowercase();
        let def = self
            .catalog
            .get(&ins.table)
            .ok_or_else(|| SqlError::no_such_table(&ins.table))?
            .clone();
        let table_id = self
            .tables
            .get(&key)
            .map(|t| t.id)
            .ok_or_else(|| SqlError::no_such_table(&ins.table))?;

        let mut count = 0u64;
        for row_exprs in &ins.rows {
            if row_exprs.len() != def.columns.len() {
                return Err(SqlError::syntax(format!(
                    "column count mismatch: table '{}' has {} columns, {} values given",
                    def.name,
                    def.columns.len(),
                    row_exprs.len()
                )));
            }
            let mut cols: Vec<ColValue> = Vec::with_capacity(def.columns.len());
            for (i, e) in row_exprs.iter().enumerate() {
                let v = self.eval_literal(e, &def.columns[i])?;
                cols.push(v);
            }
            let pk = match &cols[def.pk_index] {
                ColValue::BigInt(i) => *i,
                _ => return Err(SqlError::internal("primary key is not an integer")),
            };

            // Duplicate primary key: against committed rows and against rows
            // staged in the current transaction.
            if self
                .pk_index
                .get(&table_id)
                .map(|m| m.contains_key(&pk))
                .unwrap_or(false)
            {
                return Err(SqlError::duplicate_key(pk));
            }
            // The staged rows are checked through the key set of the buffer,
            // so the cost does not depend on how many rows the transaction
            // has staged so far. Scanning the log would make a multi-row
            // insert quadratic in the number of rows.
            if self.txn_buffer.holds_key(table_id, pk) {
                return Err(SqlError::duplicate_key(pk));
            }

            let image = encode_row(&cols);
            // The row id is reserved now so that the log record identifies the
            // row; the data files are written at commit time.
            let row_id = self.next_row_id(table_id);
            self.wal
                .log_insert(table_id, row_id, &image)
                .map_err(|e| SqlError::internal(format!("wal write failed: {e}")))?;
            self.txn_buffer.stage_insert(table_id, row_id, pk, cols);
            count += 1;
        }
        Ok(QueryResult { columns: Vec::new(), rows: Vec::new(), affected: Some(count) })
    }

    fn exec_update(&mut self, upd: &Update) -> SqlResult<QueryResult> {
        let key = upd.table.to_ascii_lowercase();
        let def = self
            .catalog
            .get(&upd.table)
            .ok_or_else(|| SqlError::no_such_table(&upd.table))?
            .clone();
        let table_id = self
            .tables
            .get(&key)
            .map(|t| t.id)
            .ok_or_else(|| SqlError::no_such_table(&upd.table))?;

        let mut targets = Vec::new();
        for (name, expr) in &upd.assignments {
            let idx = def
                .column_index(name)
                .ok_or_else(|| SqlError::no_such_column(name))?;
            if idx == def.pk_index {
                return Err(SqlError::not_supported(
                    "updating the primary key is not supported",
                ));
            }
            targets.push((idx, expr.clone()));
        }

        // The rows that the filter keeps are the only ones that are read
        // whole, so the memory of the statement is proportional to the
        // number of rows it changes and not to the size of the table.
        //
        // The vector of a row is read as well: the statement may change it,
        // and a row whose vector is missing would be written back without
        // it.
        //
        // The rows are already filtered, so the filter is not applied a
        // second time below.
        let (rows, _already_filtered) =
            self.collect_rows_filtered(&def, upd.filter.as_ref(), true)?;
        let mut affected = 0u64;
        let mut updates: Vec<(u64, Vec<ColValue>)> = Vec::new();
        for row in rows {
            let mut cols = row.cols.clone();
            for (idx, expr) in &targets {
                let v = self.eval_literal(expr, &def.columns[*idx])?;
                cols[*idx] = v;
            }
            updates.push((row.row_id, cols));
            affected += 1;
        }

        for (row_id, cols) in updates {
            let image = encode_row(&cols);
            self.wal
                .log_update(table_id, row_id, &image)
                .map_err(|e| SqlError::internal(format!("wal write failed: {e}")))?;
            let pk = match cols.get(def.pk_index) {
                Some(ColValue::BigInt(value)) => *value,
                _ => return Err(SqlError::internal("row image without primary key")),
            };
            self.txn_buffer.stage_update(table_id, row_id, pk, cols);
        }
        Ok(QueryResult { columns: Vec::new(), rows: Vec::new(), affected: Some(affected) })
    }

    fn exec_delete(&mut self, del: &Delete) -> SqlResult<QueryResult> {
        let key = del.table.to_ascii_lowercase();
        let def = self
            .catalog
            .get(&del.table)
            .ok_or_else(|| SqlError::no_such_table(&del.table))?
            .clone();
        let table_id = self
            .tables
            .get(&key)
            .map(|t| t.id)
            .ok_or_else(|| SqlError::no_such_table(&del.table))?;

        // The rows that the filter keeps are the only ones that are read, so
        // the memory of the statement is proportional to the number of rows
        // it deletes and not to the size of the table.
        //
        // The vector of a row is not read: a delete does not change it, and
        // the statement only needs the identity of the row.
        //
        // The rows are already filtered, so the filter is not applied a
        // second time below.
        let (rows, _already_filtered) =
            self.collect_rows_filtered(&def, del.filter.as_ref(), false)?;
        let mut affected = 0u64;
        let mut victims = Vec::new();
        for row in rows {
            victims.push(row.row_id);
            affected += 1;
        }
        for row_id in victims {
            self.wal
                .log_delete(table_id, row_id)
                .map_err(|e| SqlError::internal(format!("wal write failed: {e}")))?;
            self.txn_buffer.stage_delete(table_id, row_id);
        }
        Ok(QueryResult { columns: Vec::new(), rows: Vec::new(), affected: Some(affected) })
    }

    // ------------------------------------------------------------ transactions

    fn exec_begin(&mut self) -> SqlResult<QueryResult> {
        if self.txn_state == TxnState::InProgress {
            return Err(SqlError::transaction_state("nested transactions are not supported"));
        }
        self.txn_state = TxnState::InProgress;
        self.wal.set_txn_id(self.next_txn_id);
        Ok(QueryResult::ok())
    }

    fn exec_commit(&mut self) -> SqlResult<QueryResult> {
        if self.txn_state != TxnState::InProgress {
            return Err(SqlError::transaction_state("there is no active transaction"));
        }
        self.commit_buffer()?;
        self.txn_state = TxnState::Auto;
        self.next_txn_id += 1;
        self.wal.set_txn_id(0);
        Ok(QueryResult::ok())
    }

    fn exec_rollback(&mut self) -> SqlResult<QueryResult> {
        if self.txn_state != TxnState::InProgress {
            return Err(SqlError::transaction_state("there is no active transaction"));
        }
        self.wal
            .log_rollback()
            .map_err(|e| SqlError::internal(format!("wal write failed: {e}")))?;
        self.txn_buffer.clear();
        self.txn_state = TxnState::Auto;
        self.next_txn_id += 1;
        self.wal.set_txn_id(0);
        Ok(QueryResult::ok())
    }

    /// Evaluate a constant expression into a value for a specific column.
    ///
    /// Only literals are accepted on the right hand side of an assignment and
    /// in the value list of an INSERT: the statement does not require
    /// expressions there, and accepting arbitrary expressions would make the
    /// "unsupported statement" contract ambiguous.
    ///
    /// The target column type decides how a string literal is interpreted: a
    /// vector column parses it as a vector literal, a VARCHAR column keeps it
    /// as text.
    pub(crate) fn eval_literal(
        &mut self,
        expr: &Expr,
        column: &ColumnDef,
    ) -> SqlResult<ColValue> {
        match (&column.ty, expr) {
            (ColumnType::Vector(d), Expr::Str(s)) => {
                let v = crate::value::vector::Vector::parse_literal(s)?;
                v.check_dim(*d)?;
                Ok(ColValue::Vector(v))
            }
            (ColumnType::Vector(d), other) => Err(SqlError::data_error(format!(
                "column '{}' expects a VECTOR({}) literal but got {}",
                column.name,
                d,
                describe_expr(other)
            ))),
            (ColumnType::BigInt, Expr::Int(i)) => Ok(ColValue::BigInt(*i)),
            (ColumnType::BigInt, other) => Err(SqlError::data_error(format!(
                "column '{}' expects a BIGINT literal but got {}",
                column.name,
                describe_expr(other)
            ))),
            (ColumnType::VarChar(n), Expr::Str(s)) => {
                vstr::check_varchar(s, *n)?;
                Ok(ColValue::Text(s.clone()))
            }
            (ColumnType::VarChar(n), other) => Err(SqlError::data_error(format!(
                "column '{}' expects a VARCHAR({}) literal but got {}",
                column.name,
                n,
                describe_expr(other)
            ))),
        }
    }
}

/// Placeholder to keep the error message shape consistent.
/// Validate that a value matches the declared column type.
pub fn check_column_type(col: &crate::sql::ast::ColumnDef, v: &ColValue) -> SqlResult<()> {
    match (&col.ty, v) {
        (ColumnType::BigInt, ColValue::BigInt(_)) => Ok(()),
        (ColumnType::VarChar(n), ColValue::Text(s)) => vstr::check_varchar(s, *n),
        (ColumnType::Vector(d), ColValue::Vector(vec)) => vec.check_dim(*d),
        (ColumnType::BigInt, other) => Err(SqlError::data_error(format!(
            "column '{}' expects BIGINT but got {}",
            col.name,
            type_name(other)
        ))),
        (ColumnType::VarChar(n), other) => Err(SqlError::data_error(format!(
            "column '{}' expects VARCHAR({}) but got {}",
            col.name,
            n,
            type_name(other)
        ))),
        (ColumnType::Vector(d), other) => Err(SqlError::data_error(format!(
            "column '{}' expects VECTOR({}) but got {}",
            col.name,
            d,
            type_name(other)
        ))),
    }
}

/// Human readable type name used in error messages.
pub fn type_name(v: &ColValue) -> &'static str {
    match v {
        ColValue::BigInt(_) => "BIGINT",
        ColValue::Text(_) => "VARCHAR",
        ColValue::Vector(_) => "VECTOR",
    }
}
