//! The JSON Lines session layer.
//!
//! The judge drives the database over stdin and stdout with one JSON object
//! per line. The process announces itself with `{"type":"ready"}`, answers
//! every `execute` request with exactly one response, and answers the
//! `shutdown` request with `{"id":N,"ok":true}` before it exits with code 0.
//!
//! Nothing but a protocol message is ever written to stdout. Diagnostics go to
//! stderr, and every message is flushed as soon as it is written, because the
//! judge reads one line at a time and would otherwise wait for a buffer that
//! never fills.

use crate::config::SearchMode;
use crate::engine::{Cell, QueryResult};
use crate::error::SqlError;
use crate::proto::json::{self, Json};
use std::fmt::Write as _;
use std::io::{self, BufRead, Write};

/// A protocol level failure that ends the session.
#[derive(Debug)]
pub enum ProtoError {
    /// The judge sent something that is not a valid request.
    BadRequest(String),
    /// The stdin stream failed.
    Io(io::Error),
}

impl std::fmt::Display for ProtoError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProtoError::BadRequest(m) => write!(f, "bad request: {m}"),
            ProtoError::Io(e) => write!(f, "io error: {e}"),
        }
    }
}

impl std::error::Error for ProtoError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            ProtoError::BadRequest(_) => None,
            ProtoError::Io(e) => Some(e),
        }
    }
}

impl From<io::Error> for ProtoError {
    fn from(e: io::Error) -> Self {
        ProtoError::Io(e)
    }
}

/// The result of handling one request.
pub enum Outcome {
    /// Keep the session open.
    Continue,
    /// The judge asked for a shutdown; the session is over.
    Stop,
}

/// One `execute` request, taken apart.
pub struct Request {
    /// The request id, echoed back exactly as it arrived.
    pub id: Json,
    /// The SQL statement.
    pub sql: String,
    /// The requested search mode, `exact` or `ann`.
    pub mode: String,
}

/// A JSON Lines session over the standard streams.
pub struct Session {
    reader: io::Lines<io::BufReader<io::Stdin>>,
    writer: io::BufWriter<io::Stdout>,
}

impl Session {
    /// Build a session over stdin and stdout.
    pub fn stdio() -> Self {
        Session {
            reader: io::BufReader::new(io::stdin()).lines(),
            writer: io::BufWriter::with_capacity(64 * 1024, io::stdout()),
        }
    }

    /// Announce that the database is ready to serve requests.
    ///
    /// The message is written and flushed before the first request is read, so
    /// the judge sees the handshake even when it sends nothing at all.
    pub fn announce_ready(&mut self) -> io::Result<()> {
        self.write_line(&json::to_string(&json::object(vec![(
            "type",
            Json::Str("ready".to_string()),
        )])))
    }

    /// Read the next request and hand it to `handler`.
    ///
    /// The loop ends when the judge closes stdin, which is treated like a
    /// shutdown.
    pub fn run<F>(&mut self, mut handler: F) -> Result<(), ProtoError>
    where
        F: FnMut(Request) -> Result<String, SqlError>,
    {
        loop {
            let line = match self.reader.next() {
                Some(Ok(line)) => line,
                Some(Err(e)) => return Err(ProtoError::Io(e)),
                // The judge closed the connection. Treat it as a shutdown.
                None => return Ok(()),
            };
            let trimmed = line.trim();
            if trimmed.is_empty() {
                continue;
            }

            let request = match json::parse(trimmed) {
                Ok(value) => value,
                Err(e) => {
                    // A request that is not valid JSON is a protocol
                    // violation. The judge does not send one, but the
                    // process stays alive and reports the failure, so that
                    // a single bad line cannot take the whole run down.
                    let text = error_response_with_code(
                        &Json::Null,
                        "bad_request",
                        &format!("invalid JSON request: {e}"),
                    );
                    self.write_line(&text)?;
                    continue;
                }
            };
            if !matches!(request, Json::Object(_)) {
                let text = error_response_with_code(
                    &Json::Null,
                    "bad_request",
                    "the request must be an object",
                );
                self.write_line(&text)?;
                continue;
            }

            let id = request.get("id").cloned().unwrap_or(Json::Null);
            let op = request.get("op").and_then(|v| v.as_str()).unwrap_or("execute");

            match op {
                "shutdown" => {
                    let response = json::object(vec![
                        ("id", id),
                        ("ok", Json::Bool(true)),
                    ]);
                    self.write_line(&json::to_string(&response))?;
                    return Ok(());
                }
                "execute" => {
                    let sql = request.get("sql").and_then(|v| v.as_str()).unwrap_or("");
                    let mode = request.get("search_mode")
                        .and_then(|v| v.as_str())
                        .unwrap_or("exact");
                    let req = Request { id: id.clone(), sql: sql.to_string(), mode: mode.to_string() };
                    let response = match handler(req) {
                        Ok(text) => text,
                        Err(e) => error_response(&id, &e),
                    };
                    self.write_line(&response)?;
                }
                other => {
                    // An operation the protocol does not define is reported
                    // like any other failure, and the session continues.
                    let text = error_response_with_code(
                        &id,
                        "unsupported_op",
                        &format!("unsupported op '{other}'"),
                    );
                    self.write_line(&text)?;
                }
            }
        }
    }

    /// Write one protocol message and flush it.
    pub fn write_line(&mut self, text: &str) -> io::Result<()> {
        self.writer.write_all(text.as_bytes())?;
        self.writer.write_all(b"\n")?;
        self.writer.flush()
    }
}

/// The response of an `execute` request.
///
/// The response is written straight into the string, without building a
/// `Json` tree first.
///
/// Why: a query over a large table answers with a large result set, and the
/// tree of the answer would be a second copy of it. Every row of the answer
/// became a `Json::Array` of its own, and every cell of every row a `Json`
/// value, so the tree cost more than the result set itself - and the
/// response string cost a third copy on top of that. The peak memory of a
/// query grew with the number of rows of the answer three times over.
///
/// The text that is written is the same text the tree would have produced:
/// the fields are written in the same order, the numbers are formatted with
/// the same function, and the strings are escaped with the same function.
/// The response of a query is therefore byte for byte the one it was.
pub fn execute_response(id: &Json, result: &QueryResult) -> String {
    // The answer is written into a string that is grown as it goes. The
    // size of the first guess is the size of a small answer; a large one
    // grows the string a few times, which costs a copy each time but keeps
    // the peak at about twice the size of the answer.
    let mut out = String::with_capacity(256);
    out.push('{');
    // The fields are written in the order the tree would have held them:
    // the identifier, the flag, the number of affected rows, the columns
    // and the rows.
    out.push_str("\"id\":");
    json::write(id, &mut out);
    out.push_str(",\"ok\":true");
    if let Some(affected) = result.affected {
        out.push_str(",\"affected_rows\":");
        let _ = write!(out, "{affected}");
    }
    if !result.columns.is_empty() {
        out.push_str(",\"columns\":[");
        for (i, column) in result.columns.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            json::write_string(column, &mut out);
        }
        out.push(']');
        // A query always carries the `rows` field, even when the result set
        // is empty: the judge compares the rows of a query against an empty
        // list, and a missing field is not an empty list.
        out.push_str(",\"rows\":[");
        for (i, row) in result.rows.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            write_row(row, &mut out);
        }
        out.push(']');
    }
    out.push('}');
    out
}

/// Write one row of a result set as a JSON array.
///
/// The cells are written into the string one by one, so that a row does not
/// have to be turned into a tree of its own first.
fn write_row(row: &[Cell], out: &mut String) {
    out.push('[');
    for (i, cell) in row.iter().enumerate() {
        if i > 0 {
            out.push(',');
        }
        match cell {
            Cell::Int(i) => {
                let _ = write!(out, "{i}");
            }
            Cell::Float(f) => {
                if f.is_finite() {
                    out.push_str(&json::format_f64(*f));
                } else {
                    out.push_str("null");
                }
            }
            Cell::Text(s) => json::write_string(s, out),
            Cell::Vector(v) => {
                out.push('[');
                for (i, x) in v.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    if x.is_finite() {
                        out.push_str(&json::format_f64(*x));
                    } else {
                        out.push_str("null");
                    }
                }
                out.push(']');
            }
            Cell::Null => out.push_str("null"),
        }
    }
    out.push(']');
}

/// The response of a failed `execute` request.
pub fn error_response(id: &Json, err: &SqlError) -> String {
    json::to_string(&json::object(vec![
        ("id", id.clone()),
        ("ok", Json::Bool(false)),
        (
            "error",
            json::object(vec![
                ("code", Json::Str(err.code_name().to_string())),
                ("message", Json::Str(err.message.clone())),
            ]),
        ),
    ]))
}

/// The response of a failed `execute` request whose failure is not a SQL
/// error, for example a malformed request.
pub fn error_response_with_code(id: &Json, code: &str, message: &str) -> String {
    json::to_string(&json::object(vec![
        ("id", id.clone()),
        ("ok", Json::Bool(false)),
        (
            "error",
            json::object(vec![
                ("code", Json::Str(code.to_string())),
                ("message", Json::Str(message.to_string())),
            ]),
        ),
    ]))
}

/// Render a result cell as a JSON value.
fn cell_to_json(cell: &Cell) -> Json {
    match cell {
        Cell::Int(i) => Json::Int(*i),
        Cell::Float(f) => {
            if f.is_finite() {
                Json::Float(*f)
            } else {
                Json::Null
            }
        }
        Cell::Text(s) => Json::Str(s.clone()),
        Cell::Vector(v) => Json::Array(v.iter().map(|x| Json::Float(*x)).collect()),
        Cell::Null => Json::Null,
    }
}

/// Build the response of an `execute` request the way the tree did.
///
/// This is the shape the response had before it was written straight into the
/// string. It is kept so that the two can be compared against each other:
/// the direct rendering has to produce the same bytes, because the judge
/// compares the response against its own expectation.
#[cfg(test)]
fn execute_response_via_tree(id: &Json, result: &QueryResult) -> String {
    let mut fields: Vec<(&str, Json)> = Vec::new();
    fields.push(("id", id.clone()));
    fields.push(("ok", Json::Bool(true)));
    match result.affected {
        Some(affected) => fields.push(("affected_rows", Json::Int(affected as i64))),
        None => {}
    }
    if !result.columns.is_empty() {
        fields.push((
            "columns",
            Json::Array(result.columns.iter().map(|c| Json::Str(c.clone())).collect()),
        ));
        fields.push((
            "rows",
            Json::Array(
                result
                    .rows
                    .iter()
                    .map(|row| Json::Array(row.iter().map(cell_to_json).collect()))
                    .collect(),
            ),
        ));
    }
    json::to_string(&json::object(fields))
}

/// Map the wire `search_mode` onto the engine's search mode.
pub fn parse_mode(mode: Option<&str>) -> SearchMode {
    match mode {
        Some("ann") | Some("approximate") => SearchMode::Ann,
        _ => SearchMode::Exact,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(text: &str) -> Json {
        json::parse(text).unwrap()
    }

    #[test]
    fn the_ready_message_has_the_expected_shape() {
        let text = json::to_string(&json::object(vec![(
            "type",
            Json::Str("ready".to_string()),
        )]));
        assert_eq!(text, r#"{"type":"ready"}"#);
    }

    #[test]
    fn a_query_response_carries_the_columns_and_the_rows() {
        let result = QueryResult {
            columns: vec!["id".to_string(), "content".to_string()],
            rows: vec![vec![Cell::Int(1), Cell::Text("你好".to_string())]],
            affected: None,
        };
        let text = execute_response(&Json::Int(1), &result);
        assert_eq!(text, r#"{"id":1,"ok":true,"columns":["id","content"],"rows":[[1,"你好"]]}"#);
    }

    #[test]
    fn an_empty_result_still_carries_the_columns() {
        let result = QueryResult {
            columns: vec!["id".to_string()],
            rows: Vec::new(),
            affected: None,
        };
        let text = execute_response(&Json::Int(3), &result);
        assert_eq!(text, r#"{"id":3,"ok":true,"columns":["id"],"rows":[]}"#);
    }

    #[test]
    fn an_empty_result_set_keeps_an_empty_rows_array() {
        // The judge compares `rows` against an empty list, so the field must be
        // present even when there is no row at all.
        let result = QueryResult {
            columns: vec!["id".to_string()],
            rows: Vec::new(),
            affected: None,
        };
        let text = execute_response(&Json::Int(3), &result);
        assert_eq!(text, r#"{"id":3,"ok":true,"columns":["id"],"rows":[]}"#);
    }

    #[test]
    fn a_modification_reports_the_affected_rows() {
        let result = QueryResult {
            columns: Vec::new(),
            rows: Vec::new(),
            affected: Some(3),
        };
        let text = execute_response(&Json::Int(2), &result);
        assert_eq!(text, r#"{"id":2,"ok":true,"affected_rows":3}"#);
    }

    #[test]
    fn an_error_reports_a_non_empty_code() {
        let err = SqlError::duplicate_key(1);
        let text = error_response(&Json::Int(4), &err);
        assert!(text.contains(r#""ok":false"#), "{text}");
        assert!(text.contains(r#""code":"duplicate_key""#), "{text}");
    }

    #[test]
    fn the_request_id_keeps_its_json_type() {
        let id = request("{\"id\":7,\"op\":\"execute\",\"sql\":\"SELECT 1;\"}").get("id").cloned().unwrap();
        let result = QueryResult { columns: Vec::new(), rows: Vec::new(), affected: Some(0) };
        let text = execute_response(&id, &result);
        assert!(text.contains(r#""id":7"#), "{text}");
    }

    /// The response has to be the same bytes as the one the tree produced.
    ///
    /// The response of a query is no longer built as a `Json` tree, because
    /// the tree of a large answer is a second copy of it. The bytes are
    /// unchanged, though: the judge compares the response against its own
    /// expectation, and the transcript of a run is part of the material.
    #[test]
    fn the_response_is_the_same_bytes_as_the_one_of_the_tree() {
        let cases = vec![
            // A query with rows of several kinds.
            QueryResult {
                columns: vec!["id".to_string(), "content".to_string(), "d".to_string()],
                rows: vec![
                    vec![
                        Cell::Int(1),
                        Cell::Text("你好 \"世界\"\n".to_string()),
                        Cell::Float(0.5),
                    ],
                    vec![
                        Cell::Int(-9007199254740993),
                        Cell::Text(String::new()),
                        Cell::Float(5.196152210235596),
                    ],
                ],
                affected: None,
            },
            // An empty result.
            QueryResult {
                columns: vec!["id".to_string()],
                rows: Vec::new(),
                affected: None,
            },
            // A vector cell.
            QueryResult {
                columns: vec!["v".to_string()],
                rows: vec![vec![Cell::Vector(vec![1.0, 2.5, -3.0])]],
                affected: None,
            },
            // A null cell and a non finite float.
            QueryResult {
                columns: vec!["a".to_string(), "b".to_string()],
                rows: vec![vec![Cell::Null, Cell::Float(f64::NAN)]],
                affected: None,
            },
            // A modification.
            QueryResult {
                columns: Vec::new(),
                rows: Vec::new(),
                affected: Some(7),
            },
        ];
        for result in cases {
            let direct = execute_response(&Json::Int(1), &result);
            let via_tree = execute_response_via_tree(&Json::Int(1), &result);
            assert_eq!(direct, via_tree);
        }
    }

    /// The identifier of the request is echoed with its own type.
    #[test]
    fn the_response_keeps_the_type_of_the_identifier() {
        let id = request("{\"id\":\"abc\",\"op\":\"execute\",\"sql\":\"SELECT 1;\"}").get("id").cloned().unwrap();
        let result = QueryResult {
            columns: vec!["id".to_string()],
            rows: vec![vec![Cell::Int(1)]],
            affected: None,
        };
        let text = execute_response(&id, &result);
        assert_eq!(text, r#"{"id":"abc","ok":true,"columns":["id"],"rows":[[1]]}"#);
    }
}
