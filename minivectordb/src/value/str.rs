//! String helpers with the Unicode semantics the statement requires.
//!
//! * `VARCHAR(n)` length is counted in **Unicode characters** (code points),
//!   not bytes.
//! * String comparison follows **Unicode code point order**, with no
//!   language-specific collation.

use crate::error::{SqlError, SqlResult};

/// Number of Unicode characters (code points) in `s`.
pub fn char_len(s: &str) -> usize {
    s.chars().count()
}

/// Validate a string against a `VARCHAR(n)` declaration.
pub fn check_varchar(s: &str, max_chars: usize) -> SqlResult<()> {
    let n = char_len(s);
    if n > max_chars {
        return Err(SqlError::data_error(format!(
            "string too long: {n} characters exceeds VARCHAR({max_chars})"
        )));
    }
    Ok(())
}

/// Compare two strings by Unicode code point order.
///
/// `str::cmp` on UTF-8 is equivalent to code point order because UTF-8 is
/// designed so that byte-wise ordering matches code point ordering.
pub fn code_point_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    a.cmp(b)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_unicode_characters_not_bytes() {
        // 4 CJK characters = 12 UTF-8 bytes.
        assert_eq!("旅行攻略".len(), 12);
        assert_eq!(char_len("旅行攻略"), 4);
        assert_eq!(char_len("abc"), 3);
    }

    #[test]
    fn enforces_char_length() {
        assert!(check_varchar("旅行攻略", 4).is_ok());
        assert!(check_varchar("旅行攻略", 3).is_err());
    }

    #[test]
    fn compares_by_code_point() {
        use std::cmp::Ordering;
        assert_eq!(code_point_cmp("a", "b"), Ordering::Less);
        assert_eq!(code_point_cmp("旅行", "旅行"), Ordering::Equal);
        assert_eq!(code_point_cmp("Z", "a"), Ordering::Less);
    }
}
