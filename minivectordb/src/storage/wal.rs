//! Write-ahead log.
//!
//! Every mutation is recorded before it becomes visible, and a transaction
//! only becomes durable-visible when its commit marker is written. The crash
//! model of the competition is a forced process termination, so a commit that
//! has been acknowledged must survive, and a transaction that was not
//! acknowledged must be either fully applied or fully absent - never partial.
//!
//! Log record layout (all integers little endian):
//!
//! ```text
//! header:  magic u32 | payload_len u32 | kind u8 | txn_id u64
//! payload: kind specific bytes
//! ```
//!
//! Kinds:
//!
//! * `1` insert  : table_id u32, row_id u64, value blob
//! * `2` update  : table_id u32, row_id u64, value blob
//! * `3` delete  : table_id u32, row_id u64
//! * `4` commit  : txn_id u64
//! * `5` rollback: txn_id u64
//! * `6` ddl     : table_id u32, name blob, schema blob

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

pub const WAL_MAGIC: u32 = 0x4d56_4442; // "MVDB"

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RecordKind {
    Insert = 1,
    Update = 2,
    Delete = 3,
    Commit = 4,
    Rollback = 5,
    Ddl = 6,
}

impl RecordKind {
    fn from_u8(v: u8) -> Option<Self> {
        Some(match v {
            1 => RecordKind::Insert,
            2 => RecordKind::Update,
            3 => RecordKind::Delete,
            4 => RecordKind::Commit,
            5 => RecordKind::Rollback,
            6 => RecordKind::Ddl,
            _ => return None,
        })
    }
}

/// One decoded log record.
#[derive(Debug, Clone)]
pub enum Record {
    Insert { table_id: u32, row_id: u64, value: Vec<u8> },
    Update { table_id: u32, row_id: u64, value: Vec<u8> },
    Delete { table_id: u32, row_id: u64 },
    Commit { txn_id: u64 },
    Rollback { txn_id: u64 },
    Ddl { table_id: u32, name: Vec<u8>, schema: Vec<u8> },
}

/// Append-only log file.
pub struct Wal {
    file: File,
    /// Bytes written so far.
    len: u64,
    /// Current transaction id, 0 means "no explicit transaction".
    txn_id: u64,
    /// fsync on commit (durability level `full`).
    sync_on_commit: bool,
    /// Records of the statement that is currently executing.
    ///
    /// A statement must be atomic, so its log records are only appended to the
    /// file once the statement has succeeded. A statement that fails leaves no
    /// trace in the log at all.
    staged: Vec<u8>,
}

impl Wal {
    pub fn open(path: &Path, sync_on_commit: bool) -> io::Result<Wal> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(path)?;
        let len = file.metadata()?.len();
        Ok(Wal { file, len, txn_id: 0, sync_on_commit, staged: Vec::new() })
    }

    pub fn len(&self) -> u64 {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    pub fn txn_id(&self) -> u64 {
        self.txn_id
    }

    pub fn set_txn_id(&mut self, id: u64) {
        self.txn_id = id;
    }

    /// Write one record into the staging area.
    fn write_record(&mut self, kind: RecordKind, payload: &[u8]) -> io::Result<()> {
        let mut rec = Vec::with_capacity(17 + payload.len());
        rec.extend_from_slice(&WAL_MAGIC.to_le_bytes());
        rec.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        rec.push(kind as u8);
        rec.extend_from_slice(&self.txn_id.to_le_bytes());
        rec.extend_from_slice(payload);
        self.staged.extend_from_slice(&rec);
        Ok(())
    }

    /// Stage an insert record.
    pub fn log_insert(&mut self, table_id: u32, row_id: u64, value: &[u8]) -> io::Result<()> {
        let mut p = Vec::with_capacity(12 + value.len());
        p.extend_from_slice(&table_id.to_le_bytes());
        p.extend_from_slice(&row_id.to_le_bytes());
        p.extend_from_slice(value);
        self.write_record(RecordKind::Insert, &p)
    }

    /// Stage an update record.
    pub fn log_update(&mut self, table_id: u32, row_id: u64, value: &[u8]) -> io::Result<()> {
        let mut p = Vec::with_capacity(12 + value.len());
        p.extend_from_slice(&table_id.to_le_bytes());
        p.extend_from_slice(&row_id.to_le_bytes());
        p.extend_from_slice(value);
        self.write_record(RecordKind::Update, &p)
    }

    /// Stage a delete record.
    pub fn log_delete(&mut self, table_id: u32, row_id: u64) -> io::Result<()> {
        let mut p = Vec::with_capacity(12);
        p.extend_from_slice(&table_id.to_le_bytes());
        p.extend_from_slice(&row_id.to_le_bytes());
        self.write_record(RecordKind::Delete, &p)
    }

    /// Append the staged records to the log file.
    ///
    /// Called after a statement succeeded, or when a transaction commits.
    pub fn flush_staged(&mut self) -> io::Result<()> {
        if self.staged.is_empty() {
            return Ok(());
        }
        self.file.seek(SeekFrom::End(0))?;
        self.file.write_all(&self.staged)?;
        self.len += self.staged.len() as u64;
        self.staged.clear();
        Ok(())
    }

    /// Drop the staged records, used when a statement fails.
    pub fn discard_staged(&mut self) {
        self.staged.clear();
    }

    /// Number of staged bytes.
    pub fn staged_len(&self) -> usize {
        self.staged.len()
    }

    /// Write a DDL record directly to the log file.
    ///
    /// Schema changes are not transactional in this engine: they take effect
    /// immediately, so their log record must be durable before the statement
    /// is acknowledged.
    pub fn log_ddl(&mut self, table_id: u32, name: &[u8], schema: &[u8]) -> io::Result<()> {
        let mut p = Vec::with_capacity(8 + name.len() + schema.len());
        p.extend_from_slice(&table_id.to_le_bytes());
        p.extend_from_slice(name);
        p.push(0);
        p.extend_from_slice(schema);
        let mut rec = Vec::with_capacity(17 + p.len());
        rec.extend_from_slice(&WAL_MAGIC.to_le_bytes());
        rec.extend_from_slice(&(p.len() as u32).to_le_bytes());
        rec.push(RecordKind::Ddl as u8);
        rec.extend_from_slice(&self.txn_id.to_le_bytes());
        rec.extend_from_slice(&p);
        self.file.seek(SeekFrom::End(0))?;
        self.file.write_all(&rec)?;
        self.len += rec.len() as u64;
        Ok(())
    }

    /// Write the commit marker for the current transaction.
    ///
    /// The marker is written after the transaction's data records, so a crash
    /// in between leaves the transaction unacknowledged and the recovery will
    /// discard it.
    pub fn log_commit(&mut self) -> io::Result<()> {
        let mut rec = Vec::with_capacity(25);
        rec.extend_from_slice(&WAL_MAGIC.to_le_bytes());
        rec.extend_from_slice(&0u32.to_le_bytes());
        rec.push(RecordKind::Commit as u8);
        rec.extend_from_slice(&self.txn_id.to_le_bytes());
        self.file.seek(SeekFrom::End(0))?;
        self.file.write_all(&rec)?;
        self.len += rec.len() as u64;
        self.staged.clear();
        if self.sync_on_commit {
            self.file.sync_data()?;
        }
        Ok(())
    }

    /// Write a rollback marker.
    pub fn log_rollback(&mut self) -> io::Result<()> {
        let mut rec = Vec::with_capacity(25);
        rec.extend_from_slice(&WAL_MAGIC.to_le_bytes());
        rec.extend_from_slice(&0u32.to_le_bytes());
        rec.push(RecordKind::Rollback as u8);
        rec.extend_from_slice(&self.txn_id.to_le_bytes());
        self.file.seek(SeekFrom::End(0))?;
        self.file.write_all(&rec)?;
        self.len += rec.len() as u64;
        self.staged.clear();
        Ok(())
    }

    /// Flush user space buffers into the kernel.
    pub fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }

    /// Truncate the log, used after a checkpoint has made it obsolete.
    pub fn truncate(&mut self) -> io::Result<()> {
        self.file.set_len(0)?;
        self.file.seek(SeekFrom::Start(0))?;
        self.len = 0;
        Ok(())
    }

    /// The number of bytes the log occupies on the disk.
    pub fn size(&self) -> u64 {
        self.len
    }
}

/// Read every record from a log file, stopping at the first truncated or
/// corrupt record (which is exactly the tail a crash can leave behind).
pub fn read_all(path: &Path) -> io::Result<Vec<(u64, Record)>> {
    let mut out = Vec::new();
    let mut file = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(out),
        Err(e) => return Err(e),
    };
    let mut buf = Vec::new();
    file.read_to_end(&mut buf)?;
    let mut pos = 0usize;
    while pos + 17 <= buf.len() {
        let magic = u32::from_le_bytes(buf[pos..pos + 4].try_into().unwrap());
        if magic != WAL_MAGIC {
            break;
        }
        let payload_len = u32::from_le_bytes(buf[pos + 4..pos + 8].try_into().unwrap()) as usize;
        let kind = match RecordKind::from_u8(buf[pos + 8]) {
            Some(k) => k,
            None => break,
        };
        let txn_id = u64::from_le_bytes(buf[pos + 9..pos + 17].try_into().unwrap());
        let start = pos + 17;
        let end = start + payload_len;
        if end > buf.len() {
            // Torn tail from a crash: stop here.
            break;
        }
        let payload = &buf[start..end];
        let record = match kind {
            RecordKind::Insert => {
                if payload.len() < 12 {
                    break;
                }
                let table_id = u32::from_le_bytes(payload[0..4].try_into().unwrap());
                let row_id = u64::from_le_bytes(payload[4..12].try_into().unwrap());
                Record::Insert { table_id, row_id, value: payload[12..].to_vec() }
            }
            RecordKind::Update => {
                if payload.len() < 12 {
                    break;
                }
                let table_id = u32::from_le_bytes(payload[0..4].try_into().unwrap());
                let row_id = u64::from_le_bytes(payload[4..12].try_into().unwrap());
                Record::Update { table_id, row_id, value: payload[12..].to_vec() }
            }
            RecordKind::Delete => {
                if payload.len() < 12 {
                    break;
                }
                let table_id = u32::from_le_bytes(payload[0..4].try_into().unwrap());
                let row_id = u64::from_le_bytes(payload[4..12].try_into().unwrap());
                Record::Delete { table_id, row_id }
            }
            RecordKind::Commit => Record::Commit { txn_id },
            RecordKind::Rollback => Record::Rollback { txn_id },
            RecordKind::Ddl => {
                if payload.len() < 4 {
                    break;
                }
                let table_id = u32::from_le_bytes(payload[0..4].try_into().unwrap());
                let (name, schema) = split_at_nul(&payload[4..]);
                Record::Ddl { table_id, name, schema }
            }
        };
        out.push((txn_id, record));
        pos = end;
    }
    Ok(out)
}

/// Split a byte slice at the first NUL byte.
fn split_at_nul(data: &[u8]) -> (Vec<u8>, Vec<u8>) {
    match data.iter().position(|b| *b == 0) {
        Some(i) => (data[..i].to_vec(), data[i + 1..].to_vec()),
        None => (data.to_vec(), Vec::new()),
    }
}
