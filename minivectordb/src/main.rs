//! MiniVectorDB entry point.
//!
//! The process is a single node server that speaks the JSON Lines protocol
//! of the competition over stdin and stdout: it announces itself with
//! `{"type":"ready"}`, answers every `execute` request with exactly one
//! response object, and answers the `shutdown` request before it exits with
//! code 0. Diagnostics go to stderr so that stdout stays a clean protocol
//! stream.

mod catalog;
mod config;
mod engine;
mod error;
mod exec;
mod prof;
mod proto;
mod sql;
mod storage;
mod value;
mod vector;

use config::Config;
use engine::Engine;
use error::SqlError;
use proto::session::{self, Session};

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
                    "jsonl" => config::OutputFormat::Jsonl,
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
                    "stdout" => config::ReadySink::Stdout,
                    "stderr" => config::ReadySink::Stderr,
                    _ => config::ReadySink::None,
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
        } else if a.starts_with("--ann-nlist") {
            if let Some(v) = take_value(&mut i) {
                if let Ok(n) = v.parse::<usize>() {
                    cfg.ann_nlist = n.max(1);
                }
            }
        } else if a.starts_with("--ann-nprobe") {
            if let Some(v) = take_value(&mut i) {
                if let Ok(n) = v.parse::<usize>() {
                    cfg.ann_nprobe = Some(n.max(1));
                }
            }
        }
        i += 1;
    }
}

/// Main loop.
fn run(cfg: Config) -> Result<(), Box<dyn std::error::Error>> {
    prof::enable();
    let mut engine = Engine::open(cfg)?;
    let mut session = Session::stdio();

    // The handshake comes first and is flushed on its own, so that the judge
    // sees it even when it sends nothing at all.
    session.announce_ready()?;

    let result = session.run(|req| -> Result<String, SqlError> {
        // The search mode is a property of the request, not of the process,
        // so it is applied before the statement runs.
        engine.mode = session::parse_mode(Some(&req.mode));
        let stmt = prof::measure("parse_sql", || sql::parser::parse(req.sql.trim()))?;
        let query_result = prof::measure("engine_execute", || engine.execute(&stmt))?;
        Ok(session::execute_response(&req.id, &query_result))
    });

    // The judge closes the session with the shutdown request, or by closing
    // stdin. Either way the engine is shut down cleanly: an open transaction
    // is rolled back and the committed data is flushed to the disk.
    engine.shutdown()?;
    prof::dump();
    result.map_err(|e| -> Box<dyn std::error::Error> { Box::new(e) })
}
