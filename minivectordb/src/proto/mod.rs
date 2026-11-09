//! Result formatting and output.
//!
//! The wire format follows the MySQL `mysqltest` style used by seekdb's own
//! test suite: a header line with tab separated column names followed by one
//! tab separated line per row, and errors rendered as
//! `ERROR <code>: <message>`.
//!
//! Everything the driver reads goes to stdout; diagnostics go to stderr. The
//! stdout buffer is flushed after every statement so that the driver never
//! waits on a result that has already been produced.

pub mod split;

use crate::config::OutputFormat;
use crate::engine::QueryResult;
use crate::error::SqlError;
use std::io::{self, BufWriter, Write};

/// Writer wrapper that guarantees a flush after every statement.
pub struct Output {
    out: BufWriter<io::Stdout>,
    format: OutputFormat,
}

impl Output {
    pub fn new(format: OutputFormat) -> Self {
        Output { out: BufWriter::with_capacity(64 * 1024, io::stdout()), format }
    }

    /// Write a result set.
    pub fn write_result(&mut self, result: &QueryResult) -> io::Result<()> {
        match self.format {
            OutputFormat::Tsv => {
                if !result.columns.is_empty() {
                    writeln!(self.out, "{}", result.columns.join("\t"))?;
                    for row in &result.rows {
                        let cells: Vec<String> = row
                            .iter()
                            .map(|c| c.clone().unwrap_or_else(|| "NULL".to_string()))
                            .collect();
                        writeln!(self.out, "{}", cells.join("\t"))?;
                    }
                }
            }
            OutputFormat::Json => {
                writeln!(self.out, "[")?;
                for (i, row) in result.rows.iter().enumerate() {
                    let cells: Vec<String> = row
                        .iter()
                        .map(|c| {
                            let text = c.clone().unwrap_or_else(|| "NULL".to_string());
                            format!("\"{}\"", escape_json(&text))
                        })
                        .collect();
                    let comma = if i + 1 < result.rows.len() { "," } else { "" };
                    writeln!(self.out, "  {{{}}}{}", cells.join(","), comma)?;
                }
                writeln!(self.out, "]")?;
            }
        }
        self.out.flush()
    }

    /// Write an error.
    pub fn write_error(&mut self, err: &SqlError) -> io::Result<()> {
        writeln!(self.out, "ERROR {}: {}", err.code, err.message)?;
        self.out.flush()
    }

    /// Write a bare line (used for the readiness marker).
    pub fn write_line(&mut self, text: &str) -> io::Result<()> {
        writeln!(self.out, "{}", text)?;
        self.out.flush()
    }
}

/// Escape a string for the JSON output format.
fn escape_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn escapes_json_specials() {
        assert_eq!(escape_json("a\"b"), "a\\\"b");
        assert_eq!(escape_json("a\nb"), "a\\nb");
    }
}
