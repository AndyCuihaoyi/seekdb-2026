//! Row storage: one file per table plus a side file for the row directory.
//!
//! Layout of a table directory:
//!
//! ```text
//! <data_dir>/<table>/meta.dat     row directory (fixed size records)
//! <data_dir>/<table>/vec.dat      vector column, dimension major
//! <data_dir>/<table>/col.dat      non vector columns, packed row images
//! <data_dir>/<table>/ann.dat      approximate index (IVF posting lists)
//! ```
//!
//! The vector column is stored **dimension major**: for a table with dimension
//! `d` and `n` rows, the data is a `d x n` matrix in row major order of the
//! transpose. A query that accumulates the distance of one query vector over
//! all rows can then walk memory linearly and keep eight rows in flight at a
//! time, which is what the SIMD kernel wants. The cost is that appending a row
//! touches `d` separate places in the file; we amortise that by appending in
//! **blocks** of `block_rows` rows, and a partially filled block is padded with
//! zero vectors that are masked out at read time.

use crate::error::{SqlError, SqlResult};
use crate::value::vector::Vector;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

/// Rows per vector block. Must be a multiple of 8 so the SIMD kernel can
/// process whole lanes.
pub const DEFAULT_BLOCK_ROWS: usize = 4096;

/// A stored row: primary key plus the non vector column values.
#[derive(Debug, Clone, PartialEq)]
pub struct Row {
    pub pk: i64,
    /// Values of the non vector columns, in table column order.
    pub cols: Vec<ColValue>,
}

/// A column value.
#[derive(Debug, Clone, PartialEq)]
pub enum ColValue {
    BigInt(i64),
    Text(String),
    /// `None` marks a deleted row; the vector slot still exists on disk.
    Vector(Vector),
}

/// Row directory entry, fixed size.
///
/// The `version` field makes the replay of the write-ahead log idempotent: a
/// record that has already been applied carries a version that is not newer
/// than the one stored here, so it is skipped. That is what allows the log to
/// be replayed after a crash without duplicating rows.
#[derive(Debug, Clone, Copy)]
pub struct RowMeta {
    pub pk: i64,
    /// Byte offset of the row image in `col.dat`.
    pub col_offset: u64,
    /// Length of the row image in `col.dat`.
    pub col_len: u32,
    /// Block index that holds this row's vector.
    pub vec_block: u32,
    /// Slot inside the block.
    pub vec_slot: u32,
    /// Version of the row, incremented on every update.
    pub version: u32,
    /// 1 when the row is live, 0 when deleted.
    pub alive: u8,
    /// Reserved for future use; keeps the record size a round 48 bytes.
    pub _pad: [u8; 11],
}

pub const ROW_META_SIZE: usize = 48;

/// File handles of one table.
pub struct TableStore {
    pub dir: PathBuf,
    pub name: String,
    pub dim: usize,
    pub block_rows: usize,
    meta: File,
    col: File,
    vec: File,
    /// Number of rows currently stored.
    pub row_count: u64,
    /// Number of rows per block in the vector file.
    pub blocks: u64,
}

impl TableStore {
    pub fn open(dir: &Path, name: &str, dim: usize, block_rows: usize) -> io::Result<TableStore> {
        std::fs::create_dir_all(dir)?;
        let meta = OpenOptions::new().create(true).read(true).write(true).open(dir.join("meta.dat"))?;
        let col = OpenOptions::new().create(true).read(true).write(true).open(dir.join("col.dat"))?;
        let vec = OpenOptions::new().create(true).read(true).write(true).open(dir.join("vec.dat"))?;
        let mut store = TableStore {
            dir: dir.to_path_buf(),
            name: name.to_string(),
            dim,
            block_rows,
            meta,
            col,
            vec,
            row_count: 0,
            blocks: 0,
        };
        store.reload_counts()?;
        Ok(store)
    }

    /// Recompute the row count from the directory (used at startup).
    pub fn reload_counts(&mut self) -> io::Result<()> {
        let len = self.meta.metadata()?.len();
        self.row_count = len / ROW_META_SIZE as u64;
        self.blocks = self.row_count.div_ceil(self.block_rows as u64);
        Ok(())
    }

    /// Append a row and return its row id.
    ///
    /// The caller must have already written the vector through
    /// [`TableStore::write_vector`].
    pub fn append_row(
        &mut self,
        pk: i64,
        col_offset: u64,
        col_len: u32,
        vec_block: u32,
        vec_slot: u32,
    ) -> SqlResult<u64> {
        let meta = RowMeta {
            pk,
            col_offset,
            col_len,
            vec_block,
            vec_slot,
            version: 1,
            alive: 1,
            _pad: [0; 11],
        };
        self.write_meta_at(self.row_count, &meta)?;
        self.row_count += 1;
        Ok(self.row_count - 1)
    }

    /// Read one directory entry.
    pub fn read_meta(&mut self, row_id: u64) -> io::Result<Option<RowMeta>> {
        if row_id >= self.row_count {
            return Ok(None);
        }
        let mut buf = [0u8; ROW_META_SIZE];
        self.meta.seek(SeekFrom::Start(row_id * ROW_META_SIZE as u64))?;
        self.meta.read_exact(&mut buf)?;
        let meta = RowMeta {
            pk: i64::from_le_bytes(buf[0..8].try_into().unwrap()),
            col_offset: u64::from_le_bytes(buf[8..16].try_into().unwrap()),
            col_len: u32::from_le_bytes(buf[16..20].try_into().unwrap()),
            vec_block: u32::from_le_bytes(buf[20..24].try_into().unwrap()),
            vec_slot: u32::from_le_bytes(buf[24..28].try_into().unwrap()),
            version: u32::from_le_bytes(buf[28..32].try_into().unwrap()),
            alive: buf[32],
            _pad: [0; 11],
        };
        Ok(Some(meta))
    }

    /// Overwrite the directory entry of a row.
    pub fn write_meta(&mut self, row_id: u64, meta: &RowMeta) -> SqlResult<()> {
        self.write_meta_at(row_id, meta)
    }

    fn write_meta_at(&mut self, row_id: u64, meta: &RowMeta) -> SqlResult<()> {
        let mut buf = [0u8; ROW_META_SIZE];
        buf[0..8].copy_from_slice(&meta.pk.to_le_bytes());
        buf[8..16].copy_from_slice(&meta.col_offset.to_le_bytes());
        buf[16..20].copy_from_slice(&meta.col_len.to_le_bytes());
        buf[20..24].copy_from_slice(&meta.vec_block.to_le_bytes());
        buf[24..28].copy_from_slice(&meta.vec_slot.to_le_bytes());
        buf[28..32].copy_from_slice(&meta.version.to_le_bytes());
        buf[32] = meta.alive;
        self.meta
            .seek(SeekFrom::Start(row_id * ROW_META_SIZE as u64))
            .and_then(|_| self.meta.write_all(&buf))
            .map_err(|e| SqlError::internal(format!("meta write failed: {e}")))
    }

    /// Append a row image to `col.dat` and return its offset.
    pub fn append_columns(&mut self, image: &[u8]) -> SqlResult<u64> {
        let offset = self.col.seek(SeekFrom::End(0))?;
        self.col
            .write_all(image)
            .map_err(|e| SqlError::internal(format!("col write failed: {e}")))?;
        Ok(offset)
    }

    /// Read a row image back.
    pub fn read_columns(&mut self, offset: u64, len: u32) -> SqlResult<Vec<u8>> {
        let mut buf = vec![0u8; len as usize];
        self.col
            .seek(SeekFrom::Start(offset))
            .and_then(|_| self.col.read_exact(&mut buf))
            .map_err(|e| SqlError::internal(format!("col read failed: {e}")))?;
        Ok(buf)
    }

    /// Byte offset of a vector slot inside `vec.dat`.
    ///
    /// Dimension major layout: dimension `k` of block `b` lives at
    /// `((b * dim) + k) * block_rows * 4 + slot * 4`.
    pub fn vector_offset(&self, block: u32, slot: u32) -> u64 {
        let br = self.block_rows as u64;
        let d = self.dim as u64;
        (((block as u64) * d) * br + (slot as u64)) * 4
    }

    /// Write a vector into its slot.
    pub fn write_vector(&mut self, block: u32, slot: u32, v: &[f32]) -> SqlResult<()> {
        let mut buf = Vec::with_capacity(v.len() * 4);
        for x in v {
            buf.extend_from_slice(&x.to_le_bytes());
        }
        let offset = self.vector_offset(block, slot);
        self.vec
            .seek(SeekFrom::Start(offset))
            .and_then(|_| self.vec.write_all(&buf))
            .map_err(|e| SqlError::internal(format!("vec write failed: {e}")))
    }

    /// Read one vector back.
    pub fn read_vector(&mut self, block: u32, slot: u32) -> SqlResult<Vec<f32>> {
        let mut buf = vec![0u8; self.dim * 4];
        let offset = self.vector_offset(block, slot);
        self.vec
            .seek(SeekFrom::Start(offset))
            .and_then(|_| self.vec.read_exact(&mut buf))
            .map_err(|e| SqlError::internal(format!("vec read failed: {e}")))?;
        Ok(buf.chunks_exact(4)
            .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
            .collect())
    }

    /// Read a contiguous run of vectors from one block.
    ///
    /// Because the layout is dimension major, the vectors of one block are
    /// strided by `block_rows * 4` bytes. The returned buffer is a
    /// `block_rows x dim` matrix in row major order.
    pub fn read_block_matrix(&mut self, block: u32) -> SqlResult<Vec<f32>> {
        let br = self.block_rows;
        let d = self.dim;
        let mut out = vec![0f32; br * d];
        for k in 0..d {
            let offset = self.vector_offset(block, k as u32);
            let mut buf = vec![0u8; br * 4];
            let read = self
                .vec
                .seek(SeekFrom::Start(offset))
                .and_then(|_| read_up_to(&mut self.vec, &mut buf));
            match read {
                Ok(n) => {
                    // A partially filled block is padded with zeros.
                    for slot in 0..br {
                        let base = slot * 4;
                        if base + 4 > n {
                            break;
                        }
                        out[slot * d + k] = f32::from_le_bytes(buf[base..base + 4].try_into().unwrap());
                    }
                }
                Err(e) => {
                    return Err(SqlError::internal(format!("vec read failed: {e}")));
                }
            }
        }
        Ok(out)
    }
}

/// Read up to `buf.len()` bytes, tolerating a short file.
fn read_up_to(file: &mut File, buf: &mut [u8]) -> io::Result<usize> {
    let mut total = 0usize;
    while total < buf.len() {
        match file.read(&mut buf[total..]) {
            Ok(0) => break,
            Ok(n) => total += n,
            Err(ref e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(total)
}
