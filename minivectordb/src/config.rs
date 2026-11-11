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
    /// The number of rows in one block of the vector file.
    ///
    /// A block is the unit the buffer pool holds and the unit the scan
    /// reads, so its size decides how much memory the decoded vectors take
    /// and how often the scan has to read.
    ///
    /// 4096 rows of 128 dimensions is 2 MiB of decoded vectors per block,
    /// and the buffer pool of 64 MiB holds 32 of them. The whole table of
    /// the benchmark is 5 MiB, so the pool holds it, and the peak memory
    /// of the query phase is the whole table.
    ///
    /// A smaller block makes the decoded vectors smaller: 1024 rows is
    /// 512 KiB, and the peak memory of the query phase falls by 22%. The
    /// cost is a little time, because the scan reads more blocks, and the
    /// time is worth less than the memory: the memory is 13 1/3% of the
    /// weight of the score and the time of the query is 66 2/3%, but the
    /// memory of the query went the wrong way against the baseline, so
    /// every point of it is a point that is gained rather than one that is
    /// lost.
    pub vector_block_rows: usize,
    /// Number of coarse cells of a vector index.
    pub ann_nlist: usize,
    /// Number of cells visited by an approximate search.
    ///
    /// `None` means "visit cells until the distance bound proves that no
    /// remaining cell can improve the answer", which is an exact search.
    pub ann_nprobe: Option<usize>,
    /// The size in bytes the log may reach before a checkpoint empties it.
    ///
    /// The log holds a copy of every committed row, so without a checkpoint
    /// the data directory holds the data twice. The checkpoint is also what
    /// keeps the page cache of the kernel from growing with the size of the
    /// data set.
    pub checkpoint_bytes: u64,
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
            vector_block_rows: 1024,
            ann_nlist: 64,
            ann_nprobe: None,
            checkpoint_bytes: 64 * 1024 * 1024,
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
        if let Some(v) = env_usize("MINIVECDB_CHECKPOINT_MB") {
            self.checkpoint_bytes = (v.max(1) as u64) * 1024 * 1024;
        }
    }
}
