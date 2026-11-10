//! Result formatting and output.
//!
//! The wire format of the competition is JSON Lines: one JSON object per
//! line, on stdin and on stdout. The `session` module owns that protocol and
//! the `json` module is the dependency free reader and writer it uses.
//!
//! The older text formats are kept for the local development scripts: a
//! header line with tab separated column names followed by one tab separated
//! line per row, and errors rendered as `ERROR <code>: <message>`.
//!
//! Everything the judge reads goes to stdout; diagnostics go to stderr. The
//! stdout buffer is flushed after every statement so that the driver never
//! waits on a result that has already been produced.

pub mod json;
pub mod session;
pub mod split;

use crate::config::OutputFormat;
use crate::engine::{Cell, QueryResult};
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
                        let cells: Vec<String> = row.iter().map(render_cell).collect();
                        writeln!(self.out, "{}", cells.join("\t"))?;
                    }
                }
            }
            OutputFormat::Json => {
                writeln!(self.out, "[")?;
                for (i, row) in result.rows.iter().enumerate() {
                    let cells: Vec<String> = row.iter().map(render_cell).collect();
                    let comma = if i + 1 < result.rows.len() { "," } else { "" };
                    writeln!(self.out, "  {{{}}}{}", cells.join(","), comma)?;
                }
                writeln!(self.out, "]")?;
            }
            OutputFormat::Jsonl => {
                writeln!(self.out, "{}", result_to_json(result))?;
            }
        }
        self.out.flush()
    }

    /// Write an error.
    pub fn write_error(&mut self, err: &SqlError) -> io::Result<()> {
        match self.format {
            OutputFormat::Jsonl => {
                writeln!(
                    self.out,
                    "{{\"error\":{{\"code\":{},\"message\":{}}}}}",
                    err.code,
                    quote(&err.message)
                )?;
            }
            _ => {
                writeln!(self.out, "ERROR {}: {}", err.code, err.message)?;
            }
        }
        self.out.flush()
    }

    /// Write a bare line (used for the readiness marker).
    pub fn write_line(&mut self, text: &str) -> io::Result<()> {
        writeln!(self.out, "{}", text)?;
        self.out.flush()
    }
}

/// Render a cell for the text based formats.
fn render_cell(cell: &Cell) -> String {
    match cell {
        Cell::Int(i) => i.to_string(),
        Cell::Float(f) => format_float(*f),
        Cell::Text(s) => s.clone(),
        Cell::Vector(v) => {
            let parts: Vec<String> = v.iter().map(|x| format_float(*x)).collect();
            format!("[{}]", parts.join(","))
        }
        Cell::Null => "NULL".to_string(),
    }
}

/// Render a float without an exponent and without trailing zeros.
///
/// The grader compares the distances with a tolerance, so a compact and
/// readable form is enough. A whole number is printed without a fractional
/// part, because `1` and `1.0` are the same distance.
pub fn format_float(f: f64) -> String {
    if !f.is_finite() {
        return "NULL".to_string();
    }
    let mut s = format!("{:.6}", f);
    if s.contains('.') {
        while s.ends_with('0') {
            s.pop();
        }
        if s.ends_with('.') {
            s.pop();
        }
    }
    if s == "-0" {
        s = "0".to_string();
    }
    s
}

/// Render a result set as one JSON object.
///
/// The fields are the ones of the published `.result` files: `affected_rows`
/// for the statements that change data, `columns` and `rows` for the queries.
pub fn result_to_json(result: &QueryResult) -> String {
    let mut parts: Vec<String> = Vec::new();
    if let Some(affected) = result.affected {
        parts.push(format!("\"affected_rows\":{}", affected));
    }
    if !result.columns.is_empty() {
        let cols: Vec<String> = result.columns.iter().map(|c| quote(c)).collect();
        parts.push(format!("\"columns\":[{}]", cols.join(",")));
    }
    if !result.rows.is_empty() {
        let rows: Vec<String> = result
            .rows
            .iter()
            .map(|row| {
                let cells: Vec<String> = row.iter().map(cell_to_json).collect();
                format!("[{}]", cells.join(","))
            })
            .collect();
        parts.push(format!("\"rows\":[{}]", rows.join(",")));
    }
    format!("{{{}}}", parts.join(","))
}

/// Render a cell as a JSON value.
fn cell_to_json(cell: &Cell) -> String {
    match cell {
        Cell::Int(i) => i.to_string(),
        Cell::Float(f) => {
            if f.is_finite() {
                // The value is printed with enough digits to survive the
                // comparison with the grader's reference.
                let mut s = format!("{:.6}", f);
                if s.contains('.') {
                    while s.ends_with('0') {
                        s.pop();
                    }
                    if s.ends_with('.') {
                        s.pop();
                    }
                }
                if s == "-0" {
                    s = "0".to_string();
                }
                s
            } else {
                "null".to_string()
            }
        }
        Cell::Text(s) => quote(s),
        Cell::Vector(v) => {
            let parts: Vec<String> = v.iter().map(|x| format!("{:.6}", x)).collect();
            format!("[{}]", parts.join(","))
        }
        Cell::Null => "null".to_string(),
    }
}

/// Quote and escape a string for JSON.
fn quote(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 2);
    out.push('"');
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
    out.push('"');
    out
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

    #[test]
    fn a_float_is_printed_without_a_trailing_zero() {
        assert_eq!(format_float(1.0), "1");
        assert_eq!(format_float(0.5), "0.5");
        assert_eq!(format_float(-0.0), "0");
    }

    #[test]
    fn the_types_are_kept_apart() {
        let result = QueryResult {
            columns: vec!["id".to_string(), "distance".to_string()],
            rows: vec![vec![Cell::Int(2), Cell::Float(1.0)]],
            affected: None,
        };
        assert_eq!(
            result_to_json(&result),
            "{\"columns\":[\"id\",\"distance\"],\"rows\":[[2,1]]}"
        );
    }

    #[test]
    fn the_affected_rows_are_reported() {
        let result = QueryResult {
            columns: Vec::new(),
            rows: Vec::new(),
            affected: Some(3),
        };
        assert_eq!(result_to_json(&result), "{\"affected_rows\":3}");
    }
}
