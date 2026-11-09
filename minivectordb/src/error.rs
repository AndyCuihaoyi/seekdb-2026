//! Error types and SQLSTATE mapping.
//!
//! The competition statement says unsupported statements must return "the
//! agreed error" and must never silently behave like something else. The exact
//! numeric codes are published together with the test package; until then we
//! use MySQL-compatible SQLSTATE-derived numeric codes so that the wire
//! format stays `ERROR <code>: <message>`.

#![allow(dead_code)]

use std::fmt;

/// SQLSTATE class used for every error we raise.
pub const SQLSTATE_OK: &str = "00000";

impl From<std::io::Error> for SqlError {
    fn from(e: std::io::Error) -> Self {
        SqlError::internal(format!("io error: {e}"))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SqlError {
    /// Numeric error code rendered on stdout (`ERROR 23000: ...`).
    pub code: u32,
    /// SQLSTATE, kept separately so a future protocol revision can switch to
    /// `ERROR <sqlstate> (<code>): ...` without changing call sites.
    pub sqlstate: &'static str,
    pub message: String,
}

impl SqlError {
    pub fn new(code: u32, sqlstate: &'static str, message: impl Into<String>) -> Self {
        SqlError { code, sqlstate, message: message.into() }
    }

    /// Syntax error / unsupported statement.
    pub fn syntax(message: impl Into<String>) -> Self {
        SqlError::new(1064, "42000", message)
    }

    /// Table already exists / does not exist.
    pub fn no_such_table(name: &str) -> Self {
        SqlError::new(1146, "42S02", format!("Table '{name}' doesn't exist"))
    }

    pub fn table_exists(name: &str) -> Self {
        SqlError::new(1050, "42S01", format!("Table '{name}' already exists"))
    }

    pub fn no_such_column(name: &str) -> Self {
        SqlError::new(1054, "42S22", format!("Unknown column '{name}' in 'field list'"))
    }

    /// Duplicate primary key.
    pub fn duplicate_key(pk: i64) -> Self {
        SqlError::new(
            23000,
            "23000",
            format!("Duplicate entry '{pk}' for key 'PRIMARY'"),
        )
    }

    /// Data conversion problems (NaN / Infinity / overflow / bad vector literal).
    pub fn data_error(message: impl Into<String>) -> Self {
        SqlError::new(1366, "22007", message)
    }

    pub fn not_supported(message: impl Into<String>) -> Self {
        SqlError::new(1235, "42000", message)
    }

    pub fn internal(message: impl Into<String>) -> Self {
        SqlError::new(1105, "HY000", message)
    }
}

impl fmt::Display for SqlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "ERROR {}: {}", self.code, self.message)
    }
}

impl std::error::Error for SqlError {}

pub type SqlResult<T> = Result<T, SqlError>;
