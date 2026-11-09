//! Statement splitting.
//!
//! The driver feeds SQL over stdin. Statement boundaries are terminated by a
//! semicolon that is not inside a string literal, a quoted identifier, or a
//! comment. We deliberately support the common dialects so that the official
//! test package can use whichever quoting style it prefers:
//!
//! * single quoted strings `'...'` with `\\` and `''` escapes
//! * double quoted strings `"..."` (MySQL default) with the same escapes
//! * backtick quoted identifiers `` `...` ``
//! * `-- ` line comments and `#` line comments
//! * `/* ... */` block comments (nesting is not supported, matching MySQL)

#![allow(dead_code)]

/// Split a raw input chunk into individual statements.
///
/// Trailing whitespace is trimmed. A trailing fragment without a terminating
/// semicolon is returned as its own statement so that a driver which does not
/// use semicolons still works.
pub fn split_statements(input: &str) -> Vec<String> {
    let bytes = input.as_bytes();
    let mut out = Vec::new();
    let mut start = 0usize;
    let mut i = 0usize;

    while i < bytes.len() {
        match bytes[i] {
            b'\'' | b'"' => {
                i = skip_quoted(bytes, i, bytes[i]);
            }
            b'`' => {
                i = skip_quoted(bytes, i, b'`');
            }
            b'-' if i + 1 < bytes.len() && bytes[i + 1] == b'-' => {
                i = skip_line_comment(bytes, i);
            }
            b'#' => {
                i = skip_line_comment(bytes, i);
            }
            b'/' if i + 1 < bytes.len() && bytes[i + 1] == b'*' => {
                i = skip_block_comment(bytes, i);
            }
            b';' => {
                let stmt = input[start..i].trim();
                if !stmt.is_empty() {
                    out.push(stmt.to_string());
                }
                i += 1;
                start = i;
            }
            _ => i += 1,
        }
    }

    let tail = input[start..].trim();
    if !tail.is_empty() {
        out.push(tail.to_string());
    }
    out
}

/// Advance past a quoted run starting at `start`; returns the index just after
/// the closing quote. If the literal is unterminated we stop at end of input.
fn skip_quoted(bytes: &[u8], start: usize, quote: u8) -> usize {
    let mut i = start + 1;
    while i < bytes.len() {
        match bytes[i] {
            b'\\' if quote != b'`' => {
                // Backslash escape: skip the escaped byte.
                i += 2;
            }
            c if c == quote => {
                // Doubled quote ('' or "") is an escaped quote, not a terminator.
                if i + 1 < bytes.len() && bytes[i + 1] == quote {
                    i += 2;
                } else {
                    return i + 1;
                }
            }
            _ => i += 1,
        }
    }
    bytes.len()
}

fn skip_line_comment(bytes: &[u8], start: usize) -> usize {
    let mut i = start;
    while i < bytes.len() && bytes[i] != b'\n' {
        i += 1;
    }
    i
}

fn skip_block_comment(bytes: &[u8], start: usize) -> usize {
    let mut i = start + 2;
    while i + 1 < bytes.len() {
        if bytes[i] == b'*' && bytes[i + 1] == b'/' {
            return i + 2;
        }
        i += 1;
    }
    bytes.len()
}

/// Remove comments and surrounding whitespace from a single statement.
///
/// The parser works on the cleaned text so that trailing `--` comments do not
/// break tokenization.
pub fn strip_comments(stmt: &str) -> String {
    let bytes = stmt.as_bytes();
    let mut out = String::with_capacity(stmt.len());
    let mut i = 0usize;
    while i < bytes.len() {
        match bytes[i] {
            b'\'' | b'"' => {
                let end = skip_quoted(bytes, i, bytes[i]);
                out.push_str(&stmt[i..end]);
                i = end;
            }
            b'`' => {
                let end = skip_quoted(bytes, i, b'`');
                out.push_str(&stmt[i..end]);
                i = end;
            }
            b'-' if i + 1 < bytes.len() && bytes[i + 1] == b'-' => {
                i = skip_line_comment(bytes, i);
                out.push(' ');
            }
            b'#' => {
                i = skip_line_comment(bytes, i);
                out.push(' ');
            }
            b'/' if i + 1 < bytes.len() && bytes[i + 1] == b'*' => {
                i = skip_block_comment(bytes, i);
                out.push(' ');
            }
            c => {
                out.push(c as char);
                i += 1;
            }
        }
    }
    out.trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splits_on_semicolon() {
        let stmts = split_statements("SELECT 1; SELECT 2;");
        assert_eq!(stmts, vec!["SELECT 1", "SELECT 2"]);
    }

    #[test]
    fn ignores_semicolon_inside_string() {
        let stmts = split_statements("INSERT INTO t VALUES ('a;b'); SELECT 1;");
        assert_eq!(stmts, vec!["INSERT INTO t VALUES ('a;b')", "SELECT 1"]);
    }

    #[test]
    fn ignores_semicolon_inside_backtick_identifier() {
        let stmts = split_statements("SELECT `a;b` FROM t;");
        assert_eq!(stmts, vec!["SELECT `a;b` FROM t"]);
    }

    #[test]
    fn handles_line_and_block_comments() {
        let stmts = split_statements("-- comment; here\nSELECT 1; /* block; comment */ SELECT 2;");
        assert_eq!(stmts, vec!["-- comment; here\nSELECT 1", "/* block; comment */ SELECT 2"]);
    }

    #[test]
    fn keeps_trailing_fragment() {
        let stmts = split_statements("SELECT 1");
        assert_eq!(stmts, vec!["SELECT 1"]);
    }

    #[test]
    fn escaped_quote_does_not_end_string() {
        let stmts = split_statements("SELECT 'it\\'s; fine'; SELECT 2;");
        assert_eq!(stmts, vec!["SELECT 'it\\'s; fine'", "SELECT 2"]);
    }

    #[test]
    fn doubled_quote_is_escape() {
        let stmts = split_statements("SELECT 'it''s; fine';");
        assert_eq!(stmts, vec!["SELECT 'it''s; fine'"]);
    }

    #[test]
    fn strips_trailing_comment() {
        assert_eq!(strip_comments("SELECT 1 -- tail").trim(), "SELECT 1");
    }
}
