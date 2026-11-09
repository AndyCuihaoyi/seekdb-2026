//! MiniVectorDB entry point.
//!
//! The process is a single node server: it reads SQL statements from stdin,
//! writes results to stdout and keeps its data under the configured data
//! directory. Diagnostics go to stderr so that stdout stays a clean result
//! stream for the test driver.

mod catalog;
mod config;
mod engine;
mod error;
mod exec;
mod proto;
mod sql;
mod storage;
mod value;
mod vector;

use config::{Config, ReadySink};
use engine::Engine;
use error::SqlError;
use proto::split::strip_comments;
use proto::Output;
use std::io::{self, Read, Write};

fn main() {
    let mut cfg = Config::default();
    parse_args(&mut cfg);
    cfg.apply_env();

    if let Err(e) = run(cfg) {
        eprintln!("minivectordb: fatal: {e}");
        std::process::exit(1);
    }
}

/// Parse the command line.
///
/// Unknown flags are ignored rather than fatal: the driver may pass extra
/// arguments we do not know about, and refusing to start would be fatal for
/// the whole run.
    fn parse_args(cfg: &mut Config) {
        let args: Vec<String> = std::env::args().skip(1).collect();
        let mut i = 0usize;
        while i < args.len() {
            let a = args[i].clone();
            let take_value = |i: &mut usize| -> Option<String> {
                if let Some((_, v)) = a.split_once('=') {
                    return Some(v.to_string());
                }
                if *i + 1 < args.len() {
                    *i += 1;
                    return Some(args[*i].clone());
                }
                None
            };
        if a.starts_with("--data-dir") {
            if let Some(v) = take_value(&mut i) {
                cfg.data_dir = std::path::PathBuf::from(v);
            }
        } else if a.starts_with("--output-format") {
            if let Some(v) = take_value(&mut i) {
                cfg.output_format = match v.to_ascii_lowercase().as_str() {
                    "json" => config::OutputFormat::Json,
                    _ => config::OutputFormat::Tsv,
                };
            }
        } else if a.starts_with("--ready-token") {
            if let Some(v) = take_value(&mut i) {
                cfg.ready_token = v;
            }
        } else if a.starts_with("--ready-sink") {
            if let Some(v) = take_value(&mut i) {
                cfg.ready_sink = match v.to_ascii_lowercase().as_str() {
                    "stdout" => ReadySink::Stdout,
                    "stderr" => ReadySink::Stderr,
                    _ => ReadySink::None,
                };
            }
        } else if a.starts_with("--mode") {
            if let Some(v) = take_value(&mut i) {
                cfg.mode = match v.to_ascii_lowercase().as_str() {
                    "ann" | "approximate" => config::SearchMode::Ann,
                    _ => config::SearchMode::Exact,
                };
            }
        } else if a.starts_with("--durability") {
            if let Some(v) = take_value(&mut i) {
                cfg.durability = match v.to_ascii_lowercase().as_str() {
                    "full" | "fsync" => config::Durability::Full,
                    _ => config::Durability::Os,
                };
            }
        } else if a.starts_with("--buffer-pool-mb") {
            if let Some(v) = take_value(&mut i) {
                if let Ok(n) = v.parse() {
                    cfg.buffer_pool_mb = n;
                }
            }
        } else if a.starts_with("--vector-block-rows") {
            if let Some(v) = take_value(&mut i) {
                if let Ok(n) = v.parse() {
                    cfg.vector_block_rows = n;
                }
            }
        }
        i += 1;
    }
}

/// Main loop.
fn run(cfg: Config) -> Result<(), Box<dyn std::error::Error>> {
    let ready_sink = cfg.ready_sink;
    let ready_token = cfg.ready_token.clone();
    let output_format = cfg.output_format;

    let mut engine = Engine::open(cfg)?;
    let mut out = Output::new(output_format);

    // Signal readiness. The token is written to stderr by default so that the
    // stdout result stream stays clean; the driver may switch it to stdout.
    match ready_sink {
        ReadySink::Stdout => {
            out.write_line(&ready_token)?;
        }
        ReadySink::Stderr => {
            let mut e = io::stderr();
            writeln!(e, "{}", ready_token)?;
            e.flush()?;
        }
        ReadySink::None => {}
    }

    let stdin = io::stdin();
    let mut reader = stdin.lock();
    let mut buf = String::new();
    let mut chunk = [0u8; 64 * 1024];

    loop {
        let n = match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => n,
            Err(ref e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(Box::new(e)),
        };
        buf.push_str(&String::from_utf8_lossy(&chunk[..n]));

        // Process every complete statement in the buffer. A statement is
        // complete when it is terminated by a semicolon; the trailing
        // fragment stays buffered until more input arrives.
        while let Some(end) = find_statement_end(&buf) {
            let stmt = buf[..end].trim().to_string();
            buf.drain(..end + 1);
            run_statement(&mut engine, &mut out, &stmt)?;
        }
    }

    // A driver that does not terminate its last statement with a semicolon
    // still gets its result.
    let trailing = buf.trim().to_string();
    if !trailing.is_empty() {
        run_statement(&mut engine, &mut out, &trailing)?;
    }
    Ok(())
}

/// Find the offset of the first statement terminator that is not inside a
/// literal or a comment.
fn find_statement_end(buf: &str) -> Option<usize> {
    let bytes = buf.as_bytes();
    let mut i = 0usize;
    while i < bytes.len() {
        match bytes[i] {
            b'\'' | b'"' => i = skip_quoted(bytes, i, bytes[i]),
            b'`' => i = skip_quoted(bytes, i, b'`'),
            b'-' if i + 1 < bytes.len() && bytes[i + 1] == b'-' => {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
            }
            b'#' => {
                while i < bytes.len() && bytes[i] != b'\n' {
                    i += 1;
                }
            }
            b'/' if i + 1 < bytes.len() && bytes[i + 1] == b'*' => {
                i += 2;
                while i + 1 < bytes.len() && !(bytes[i] == b'*' && bytes[i + 1] == b'/') {
                    i += 1;
                }
                i = (i + 2).min(bytes.len());
            }
            b';' => return Some(i),
            _ => i += 1,
        }
    }
    None
}

/// Advance past a quoted run.
fn skip_quoted(bytes: &[u8], start: usize, quote: u8) -> usize {
    let mut i = start + 1;
    while i < bytes.len() {
        match bytes[i] {
            b'\\' if quote != b'`' => i += 2,
            c if c == quote => {
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

/// Parse and execute one statement, writing the result.
fn run_statement(
    engine: &mut Engine,
    out: &mut Output,
    stmt: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    let cleaned = strip_comments(stmt);
    if cleaned.is_empty() {
        return Ok(());
    }
    let result = match sql::parser::parse(&cleaned) {
        Ok(parsed) => engine.execute(&parsed),
        Err(e) => Err(e),
    };
    match result {
        Ok(r) => out.write_result(&r)?,
        Err(e) => out.write_error(&e)?,
    }
    Ok(())
}

/// Unused helper kept for symmetry with the error type.
#[allow(dead_code)]
fn describe_error(e: &SqlError) -> String {
    e.to_string()
}
