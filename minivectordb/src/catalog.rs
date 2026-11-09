//! Table catalog: table definitions and their validation.

#![allow(dead_code)]

use crate::error::{SqlError, SqlResult};
use crate::sql::ast::{ColumnDef, ColumnType};
use std::collections::HashMap;

/// A table definition.
#[derive(Debug, Clone)]
pub struct TableDef {
    pub name: String,
    pub columns: Vec<ColumnDef>,
    /// Index of the vector column, if any.
    pub vector_column: Option<usize>,
    /// Index of the primary key column.
    pub pk_index: usize,
}

impl TableDef {
    pub fn column_index(&self, name: &str) -> Option<usize> {
        self.columns.iter().position(|c| c.name.eq_ignore_ascii_case(name))
    }

    pub fn vector_dim(&self) -> Option<usize> {
        self.vector_column.and_then(|i| match &self.columns[i].ty {
            ColumnType::Vector(d) => Some(*d),
            _ => None,
        })
    }
}

/// All tables known to the database.
#[derive(Debug, Default)]
pub struct Catalog {
    tables: HashMap<String, TableDef>,
    /// Insertion order, so that output is deterministic.
    order: Vec<String>,
}

impl Catalog {
    pub fn new() -> Self {
        Catalog::default()
    }

    pub fn get(&self, name: &str) -> Option<&TableDef> {
        self.tables.get(&name.to_ascii_lowercase())
    }

    pub fn insert(&mut self, def: TableDef) {
        let key = def.name.to_ascii_lowercase();
        if !self.tables.contains_key(&key) {
            self.order.push(key.clone());
        }
        self.tables.insert(key, def);
    }

    pub fn remove(&mut self, name: &str) {
        let key = name.to_ascii_lowercase();
        self.tables.remove(&key);
        self.order.retain(|k| k != &key);
    }

    pub fn names(&self) -> &[String] {
        &self.order
    }

    /// Validate a CREATE TABLE definition and normalise it.
    ///
    /// The statement requires exactly one BIGINT primary key and at most one
    /// vector column per table.
    pub fn build_table_def(
        &self,
        name: &str,
        columns: Vec<ColumnDef>,
    ) -> SqlResult<TableDef> {
        if self.get(name).is_some() {
            return Err(SqlError::table_exists(name));
        }
        if columns.is_empty() {
            return Err(SqlError::syntax("table must have at least one column"));
        }

        let mut pk_index: Option<usize> = None;
        let mut vector_column: Option<usize> = None;

        for (i, cd) in columns.iter().enumerate() {
            if columns[..i]
                .iter()
                .any(|c| c.name.eq_ignore_ascii_case(&cd.name))
            {
                return Err(SqlError::syntax(format!(
                    "duplicate column name '{}'",
                    cd.name
                )));
            }
            if cd.primary_key {
                if pk_index.is_some() {
                    return Err(SqlError::not_supported(
                        "a table may declare at most one primary key",
                    ));
                }
                if !matches!(cd.ty, ColumnType::BigInt) {
                    return Err(SqlError::not_supported(
                        "the primary key must be a BIGINT column",
                    ));
                }
                pk_index = Some(i);
            }
            if matches!(cd.ty, ColumnType::Vector(_)) {
                if vector_column.is_some() {
                    return Err(SqlError::not_supported(
                        "a table may declare at most one vector column",
                    ));
                }
                vector_column = Some(i);
            }
        }

        let pk_index = pk_index.ok_or_else(|| {
            SqlError::not_supported("every table must declare a BIGINT primary key")
        })?;

        Ok(TableDef { name: name.to_string(), columns, vector_column, pk_index })
    }
}
