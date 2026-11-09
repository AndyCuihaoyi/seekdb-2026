//! Runtime configuration.
//!
//! Every protocol-relevant knob is a flag or environment variable so that we
//! can align with the official test package once it is published without
//! touching the engine. Defaults follow the MySQL `mysqltest` result style
//! used by seekdb's own test suite.

use std::path::PathBuf;

/// How result sets are rendered on stdout.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OutputFormat {
    /// `col1\tcol2` header line followed by tab separated data lines.
    Tsv,
    /// One JSON object per row, then a JSON array terminator.
    Json,
    /// One JSON object per statement, with the fields of the published
    /// `.result` files.
    Jsonl,
}

/// Where the readiness marker is emitted.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadySink {
    Stdout,
    Stderr,
    None,
}

/// Vector search mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SearchMode {
    /// Always return exact results, even when an approximate index exists.
    Exact,
    /// Use the approximate index for vector TopK queries.
    Ann,
}

/// Durability level at COMMIT.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Durability {
    /// Hand data to the OS (write) and rely on the process-kill crash model.
    Os,
    /// Also fsync data and log files before acknowledging COMMIT.
    Full,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub data_dir: PathBuf,
    pub output_format: OutputFormat,
    pub ready_sink: ReadySink,
    pub ready_token: String,
    pub mode: SearchMode,
    pub durability: Durability,
    pub buffer_pool_mb: usize,
    pub vector_block_rows: usize,
    /// Number of coarse cells of a vector index.
    pub ann_nlist: usize,
    /// Number of cells visited by an approximate search.
    ///
    /// `None` means "visit cells until the distance bound proves that no
    /// remaining cell can improve the answer", which is an exact search.
    pub ann_nprobe: Option<usize>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            data_dir: PathBuf::from("/data"),
            output_format: OutputFormat::Tsv,
            ready_sink: ReadySink::Stderr,
            ready_token: "ready".to_string(),
            mode: SearchMode::Exact,
            durability: Durability::Os,
            buffer_pool_mb: 64,
            vector_block_rows: 4096,
            ann_nlist: 64,
            ann_nprobe: None,
        }
    }
}

fn env_string(key: &str) -> Option<String> {
    std::env::var(key).ok().filter(|v| !v.is_empty())
}

fn env_usize(key: &str) -> Option<usize> {
    env_string(key).and_then(|v| v.parse().ok())
}

impl Config {
    /// Apply environment variable overrides. CLI flags are parsed separately and
    /// take precedence over the environment.
    pub fn apply_env(&mut self) {
        if let Some(v) = env_string("MINIVECDB_DATA_DIR") {
            self.data_dir = PathBuf::from(v);
        }
        if let Some(v) = env_string("MINIVECDB_OUTPUT_FORMAT") {
            self.output_format = match v.to_ascii_lowercase().as_str() {
                "json" => OutputFormat::Json,
                "jsonl" => OutputFormat::Jsonl,
                _ => OutputFormat::Tsv,
            };
        }
        if let Some(v) = env_string("MINIVECDB_READY_SINK") {
            self.ready_sink = match v.to_ascii_lowercase().as_str() {
                "stdout" => ReadySink::Stdout,
                "stderr" => ReadySink::Stderr,
                _ => ReadySink::None,
            };
        }
        if let Some(v) = env_string("MINIVECDB_READY_TOKEN") {
            self.ready_token = v;
        }
        if let Some(v) = env_string("MINIVECDB_MODE") {
            self.mode = match v.to_ascii_lowercase().as_str() {
                "ann" | "approximate" => SearchMode::Ann,
                _ => SearchMode::Exact,
            };
        }
        if let Some(v) = env_string("MINIVECDB_DURABILITY") {
            self.durability = match v.to_ascii_lowercase().as_str() {
                "full" | "fsync" => Durability::Full,
                _ => Durability::Os,
            };
        }
        if let Some(v) = env_usize("MINIVECDB_BUFFER_POOL_MB") {
            self.buffer_pool_mb = v;
        }
        if let Some(v) = env_usize("MINIVECDB_VECTOR_BLOCK_ROWS") {
            self.vector_block_rows = v;
        }
        if let Some(v) = env_usize("MINIVECDB_ANN_NLIST") {
            self.ann_nlist = v.max(1);
        }
        if let Some(v) = env_usize("MINIVECDB_ANN_NPROBE") {
            self.ann_nprobe = Some(v.max(1));
        }
    }
}
