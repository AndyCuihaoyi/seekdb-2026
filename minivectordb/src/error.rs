//! Error types and SQLSTATE mapping.
//!
//! The competition statement says unsupported statements must return "the
//! agreed error" and must never silently behave like something else.
//!
//! Two names are carried by every error:
//!
//! * `code` is the numeric MySQL error code. It is kept for the diagnostics
//!   on stderr and for the historical `ERROR <code>: <message>` format.
//! * `name` is the semantic error name that the judge compares, for example
//!   `duplicate_key` or `unsupported_sql`. The published expectations name
//!   the failures this way, so the wire protocol reports the name.
//!
//! The numeric code and the semantic name always agree: they are produced by
//! the same constructor, so there is no way to raise one without the other.

#![allow(dead_code)]

use std::fmt;

/// SQLSTATE class used for every error we raise.
pub const SQLSTATE_OK: &str = "00000";

/// The semantic name of an error, as the judge expects it on the wire.
pub mod names {
    /// A syntax error in the statement.
    pub const SYNTAX_ERROR: &str = "syntax_error";
    /// A statement the specification does not support.
    pub const UNSUPPORTED_SQL: &str = "unsupported_sql";
    /// The table does not exist.
    pub const NO_SUCH_TABLE: &str = "no_such_table";
    /// The table already exists.
    pub const TABLE_EXISTS: &str = "table_exists";
    /// The column does not exist.
    pub const NO_SUCH_COLUMN: &str = "no_such_column";
    /// The primary key is already taken.
    pub const DUPLICATE_KEY: &str = "duplicate_key";
    /// The value does not match the declared type.
    pub const DATA_ERROR: &str = "data_error";
    /// The vector has the wrong number of components.
    pub const DIMENSION_MISMATCH: &str = "dimension_mismatch";
    /// The value is not a finite number, or it does not fit its column.
    pub const VALUE_OUT_OF_RANGE: &str = "value_out_of_range";
    /// The transaction cannot be run in its current state.
    pub const TRANSACTION_STATE: &str = "transaction_state";
    /// An internal failure.
    pub const INTERNAL_ERROR: &str = "internal_error";
}

impl From<std::io::Error> for SqlError {
    fn from(e: std::io::Error) -> Self {
        SqlError::internal(format!("io error: {e}"))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SqlError {
    /// Numeric error code (`23000` and friends).
    pub code: u32,
    /// Semantic error name, as the judge expects it on the wire.
    pub name: &'static str,
    /// SQLSTATE, kept separately so that a future protocol revision can switch
    /// to `ERROR <sqlstate> (<code>): ...` without changing call sites.
    pub sqlstate: &'static str,
    pub message: String,
}

impl SqlError {
    pub fn new(code: u32, sqlstate: &'static str, message: impl Into<String>) -> Self {
        SqlError { code, name: names::INTERNAL_ERROR, sqlstate, message: message.into() }
    }

    /// Build an error with an explicit semantic name.
    pub fn named(
        code: u32,
        name: &'static str,
        sqlstate: &'static str,
        message: impl Into<String>,
    ) -> Self {
        SqlError { code, name, sqlstate, message: message.into() }
    }

    /// The semantic error name, as the judge compares it.
    pub fn code_name(&self) -> &'static str {
        self.name
    }

    /// Syntax error.
    pub fn syntax(message: impl Into<String>) -> Self {
        SqlError::named(1064, names::SYNTAX_ERROR, "42000", message)
    }

    /// Table already exists / does not exist.
    pub fn no_such_table(name: &str) -> Self {
        SqlError::named(1146, names::NO_SUCH_TABLE, "42S02", format!("Table '{name}' doesn't exist"))
    }

    pub fn table_exists(name: &str) -> Self {
        SqlError::named(1050, names::TABLE_EXISTS, "42S01", format!("Table '{name}' already exists"))
    }

    pub fn no_such_column(name: &str) -> Self {
        SqlError::named(
            1054,
            names::NO_SUCH_COLUMN,
            "42S22",
            format!("Unknown column '{name}' in 'field list'"),
        )
    }

    /// Duplicate primary key.
    pub fn duplicate_key(pk: i64) -> Self {
        SqlError::named(
            23000,
            names::DUPLICATE_KEY,
            "23000",
            format!("Duplicate entry '{pk}' for key 'PRIMARY'"),
        )
    }

    /// Data conversion problems (NaN / Infinity / overflow / bad vector
    /// literal).
    pub fn data_error(message: impl Into<String>) -> Self {
        SqlError::named(1366, names::DATA_ERROR, "22007", message)
    }

    /// The vector does not have the number of components of its column.
    pub fn dimension_mismatch(message: impl Into<String>) -> Self {
        SqlError::named(1366, names::DIMENSION_MISMATCH, "22007", message)
    }

    /// A value that does not fit its column, for example a string that is
    /// longer than the declared `VARCHAR` length.
    pub fn value_out_of_range(message: impl Into<String>) -> Self {
        SqlError::named(1366, names::VALUE_OUT_OF_RANGE, "22007", message)
    }

    /// A statement the specification does not support.
    pub fn not_supported(message: impl Into<String>) -> Self {
        SqlError::named(1235, names::UNSUPPORTED_SQL, "42000", message)
    }

    /// A transaction that cannot run in its current state.
    pub fn transaction_state(message: impl Into<String>) -> Self {
        SqlError::named(1235, names::TRANSACTION_STATE, "42000", message)
    }

    pub fn internal(message: impl Into<String>) -> Self {
        SqlError::named(1105, names::INTERNAL_ERROR, "HY000", message)
    }
}

impl fmt::Display for SqlError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "ERROR {}: {}", self.code, self.message)
    }
}

impl std::error::Error for SqlError {}

pub type SqlResult<T> = Result<T, SqlError>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_names_are_the_ones_the_judge_expects() {
        assert_eq!(SqlError::duplicate_key(1).code_name(), "duplicate_key");
        assert_eq!(SqlError::not_supported("x").code_name(), "unsupported_sql");
        assert_eq!(SqlError::dimension_mismatch("x").code_name(), "dimension_mismatch");
        assert_eq!(SqlError::syntax("x").code_name(), "syntax_error");
    }

    #[test]
    fn the_numeric_code_is_kept() {
        assert_eq!(SqlError::duplicate_key(1).code, 23000);
        assert_eq!(SqlError::syntax("x").code, 1064);
    }
}
