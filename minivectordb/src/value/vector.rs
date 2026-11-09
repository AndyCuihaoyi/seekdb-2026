//! Vector values: parsing, validation and storage.
//!
//! The statement demands that a vector be a fixed-dimension float32 array whose
//! elements are finite: NaN, Infinity, dimension mismatch and conversion
//! overflow must all be rejected rather than silently coerced.

use crate::error::{SqlError, SqlResult};

#[derive(Debug, Clone, PartialEq)]
pub struct Vector {
    pub dim: usize,
    pub data: Vec<f32>,
}

impl Vector {
    pub fn new(dim: usize, data: Vec<f32>) -> Self {
        Vector { dim, data }
    }

    pub fn zeros(dim: usize) -> Self {
        Vector { dim, data: vec![0.0f32; dim] }
    }

    pub fn as_slice(&self) -> &[f32] {
        &self.data
    }

    /// Parse a vector literal such as `[1, 0, 0, 0]`.
    ///
    /// Accepts optional surrounding whitespace and an optional trailing comma.
    /// Every element must parse to a finite float32.
    pub fn parse_literal(text: &str) -> SqlResult<Vector> {
        let trimmed = text.trim();
        let inner = trimmed
            .strip_prefix('[')
            .and_then(|s| s.strip_suffix(']'))
            .ok_or_else(|| {
                SqlError::data_error(format!(
                    "invalid vector literal '{text}': expected a bracketed list"
                ))
            })?;

        if inner.trim().is_empty() {
            return Err(SqlError::data_error(
                "invalid vector literal: empty vector is not allowed",
            ));
        }

        let mut data = Vec::new();
        for part in split_top_level(inner) {
            let p = part.trim();
            if p.is_empty() {
                return Err(SqlError::data_error(
                    "invalid vector literal: empty element",
                ));
            }
            let v = parse_f32(p)?;
            if !v.is_finite() {
                return Err(SqlError::data_error(format!(
                    "invalid vector element '{p}': value must be finite"
                )));
            }
            data.push(v);
        }
        let dim = data.len();
        Ok(Vector { dim, data })
    }

    /// Check the dimension against the column declaration.
    pub fn check_dim(&self, expected: usize) -> SqlResult<()> {
        if self.dim != expected {
            return Err(SqlError::data_error(format!(
                "vector dimension mismatch: expected {expected}, got {}",
                self.dim
            )));
        }
        Ok(())
    }
}

/// Split on commas that are not nested inside brackets.
fn split_top_level(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut depth = 0i32;
    let mut start = 0usize;
    for (i, c) in s.char_indices() {
        match c {
            '[' | '(' => depth += 1,
            ']' | ')' => depth -= 1,
            ',' if depth == 0 => {
                out.push(&s[start..i]);
                start = i + 1;
            }
            _ => {}
        }
    }
    out.push(&s[start..]);
    out
}

/// Parse a numeric literal into a finite float32.
///
/// Rejects NaN / Infinity spellings and values that overflow float32.
pub fn parse_f32(text: &str) -> SqlResult<f32> {
    let t = text.trim();
    if t.is_empty() {
        return Err(SqlError::data_error("invalid numeric literal: empty"));
    }
    let lower = t.to_ascii_lowercase();
    if lower.contains("nan") || lower.contains("inf") {
        return Err(SqlError::data_error(format!(
            "invalid numeric literal '{t}': NaN and Infinity are not allowed"
        )));
    }
    let v: f64 = t.parse().map_err(|_| {
        SqlError::data_error(format!("invalid numeric literal '{t}'"))
    })?;
    if !v.is_finite() {
        return Err(SqlError::data_error(format!(
            "invalid numeric literal '{t}': value must be finite"
        )));
    }
    let f = v as f32;
    if !f.is_finite() {
        return Err(SqlError::data_error(format!(
            "numeric literal '{t}' overflows float32"
        )));
    }
    Ok(f)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_simple_vector() {
        let v = Vector::parse_literal("[1, 0, 0, 0]").unwrap();
        assert_eq!(v.dim, 4);
        assert_eq!(v.data[0], 1.0);
    }

    #[test]
    fn parses_negative_and_float() {
        let v = Vector::parse_literal("[-1.5, 2e2]").unwrap();
        assert_eq!(v.data[0], -1.5);
        assert_eq!(v.data[1], 200.0);
    }

    #[test]
    fn rejects_nan_and_inf() {
        assert!(Vector::parse_literal("[NaN, 1]").is_err());
        assert!(Vector::parse_literal("[Infinity, 1]").is_err());
        assert!(Vector::parse_literal("[-inf, 1]").is_err());
    }

    #[test]
    fn rejects_overflow() {
        // 1e40 is finite in f64 but overflows float32.
        assert!(Vector::parse_literal("[1e40]").is_err());
    }

    #[test]
    fn rejects_bad_shape() {
        assert!(Vector::parse_literal("1,2,3").is_err());
        assert!(Vector::parse_literal("[]").is_err());
        assert!(Vector::parse_literal("[1,,2]").is_err());
    }

    #[test]
    fn dimension_mismatch_detected() {
        let v = Vector::parse_literal("[1,2]").unwrap();
        assert!(v.check_dim(4).is_err());
        assert!(v.check_dim(2).is_ok());
    }
}
