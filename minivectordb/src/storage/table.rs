//! Row storage: one file per table plus a side file for the row directory.
//!
//! Layout of a table directory:
//!
//! ```text
//! <data_dir>/<table>/meta.dat     row directory (fixed size records)
//! <data_dir>/<table>/vec.dat      vector column, row major inside a block
//! <data_dir>/<table>/col.dat      non vector columns, packed row images
//! <data_dir>/<table>/ann.dat      approximate index (IVF posting lists)
//! ```
//!
//! The vector column is stored **row major inside a block**: the vector of
//! slot `s` of block `b` occupies `dim` consecutive floats, and the next slot
//! follows it. A scan of one block is therefore a single contiguous read of
//! `block_rows * dim * 4` bytes, and a write of one vector is a single
//! contiguous write. The dimension major alternative would turn every vector
//! write into `dim` scattered writes and every block scan into `dim` strided
//! reads; the transpose it would save in the distance kernel is far cheaper
//! than those scattered accesses.
//!
//! The file starts with a header that describes the layout. A file whose
//! header does not match the current one is stale and is rebuilt from the row
//! images of `col.dat`, which are the authoritative copy of the data.
//!
//! # The block cache
//!
//! The vector blocks and the directory blocks are read through a bounded
//! cache, shared by every table of the database. The cache holds the
//! *decoded* form of a block, because the decoding is the expensive part:
//! turning the bytes of a vector block into floats costs more than the read
//! out of the file, and a scan of ten thousand rows repeats that work for
//! every query.
//!
//! A write invalidates the block it touches, so that the cache never holds a
//! value that is older than the file.

use crate::error::{SqlError, SqlResult};
use crate::storage::bufferpool::BufferPool;
use crate::value::vector::Vector;
use std::cell::RefCell;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::FileExt;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicU32, Ordering as AtomicOrdering};

/// Rows per vector block. Must be a multiple of 8 so the SIMD kernel can
/// process whole lanes.
pub const DEFAULT_BLOCK_ROWS: usize = 4096;

/// Magic number of the vector file header.
pub const VEC_MAGIC: u32 = 0x4d56_4331; // "MVC1"

/// Version of the vector file layout.
pub const VEC_VERSION: u32 = 1;

/// Size of the vector file header in bytes: magic, version, dimension and
/// block size.
pub const VEC_HEADER_SIZE: usize = 16;

/// The identifier of the next opened table file.
///
/// It distinguishes the blocks of different tables inside the shared cache.
static NEXT_FILE_ID: AtomicU32 = AtomicU32::new(1);

/// The budget of a cache that a table opens on its own.
///
/// A table that is opened outside the engine - in a test, for instance - gets
/// a cache of its own, with this budget.
const STANDALONE_POOL_MB: usize = 64;

/// The cache of the decoded vector blocks.
pub type VectorPool = Rc<RefCell<BufferPool<Vec<f32>>>>;

/// The cache of the decoded directory blocks.
pub type DirectoryPool = Rc<RefCell<BufferPool<Vec<RowMeta>>>>;

/// The cache of the decoded scalar column blocks.
pub type ScalarPool = Rc<RefCell<BufferPool<ScalarBlock>>>;

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

impl crate::storage::bufferpool::CacheEntry for Vec<RowMeta> {
    fn size(&self) -> usize {
        self.len() * ROW_META_SIZE
    }
}

/// The decoded scalar columns of a block.
///
/// A block of the scalar columns holds one entry per row, and every entry
/// holds one value per scalar column. The vector column is not part of it:
/// the vector lives in its own file, and keeping a second copy of it here
/// would defeat the purpose of the split.
pub type ScalarBlock = Vec<Vec<Option<ColValue>>>;

impl crate::storage::bufferpool::CacheEntry for ScalarBlock {
    fn size(&self) -> usize {
        // A rough estimate: every value is at most a text of a few dozen
        // bytes, and the overhead of the allocation is proportional to the
        // number of values.
        self.len() * 64
    }
}

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
    /// Identifier of the table file, used as the cache key.
    file_id: u32,
    /// The cache of the decoded vector blocks, shared with the other tables.
    vector_pool: VectorPool,
    /// The cache of the decoded directory blocks, shared with the other tables.
    directory_pool: DirectoryPool,
    /// The cache of the decoded scalar columns, shared with the other tables.
    scalar_pool: ScalarPool,
}

impl TableStore {
    pub fn open(dir: &Path, name: &str, dim: usize, block_rows: usize) -> io::Result<TableStore> {
        let vector_pool: VectorPool =
            Rc::new(RefCell::new(BufferPool::new(STANDALONE_POOL_MB)));
        let directory_pool: DirectoryPool =
            Rc::new(RefCell::new(BufferPool::new(STANDALONE_POOL_MB)));
        let scalar_pool: ScalarPool =
            Rc::new(RefCell::new(BufferPool::new(STANDALONE_POOL_MB)));
        TableStore::open_with_pools(
            dir,
            name,
            dim,
            block_rows,
            vector_pool,
            directory_pool,
            scalar_pool,
        )
    }

    /// Open a table whose blocks are cached in the given pools.
    ///
    /// Every table of a database shares one set of pools, so that the memory
    /// they use is bounded by one budget rather than by the number of tables.
    pub fn open_with_pools(
        dir: &Path,
        name: &str,
        dim: usize,
        block_rows: usize,
        vector_pool: VectorPool,
        directory_pool: DirectoryPool,
        scalar_pool: ScalarPool,
    ) -> io::Result<TableStore> {
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
            file_id: NEXT_FILE_ID.fetch_add(1, AtomicOrdering::Relaxed),
            vector_pool,
            directory_pool,
            scalar_pool,
        };
        store.reload_counts()?;
        // A fresh vector file gets its header right away, so that the file
        // is never read back as if it had no layout information.
        if store.vec.metadata()?.len() == 0 {
            store.reset_vector_file().map_err(|e| {
                std::io::Error::new(std::io::ErrorKind::Other, format!("cannot write vec header: {e}"))
            })?;
        }
        Ok(store)
    }

    /// Whether the vector file has to be rebuilt from the row images.
    ///
    /// The vector file is a derived copy: the authoritative data is the row
    /// image in `col.dat`, which holds the vector as well. A file whose
    /// header does not describe the current layout therefore holds garbage
    /// that can simply be thrown away and written again.
    pub fn vector_file_is_stale(&mut self) -> SqlResult<bool> {
        let mut head = [0u8; VEC_HEADER_SIZE];
        let n = read_at_up_to(&self.vec, &mut head, 0)
            .map_err(|e| SqlError::internal(format!("vec read failed: {e}")))?;
        if n < VEC_HEADER_SIZE {
            return Ok(true);
        }
        let magic = u32::from_le_bytes(head[0..4].try_into().unwrap());
        let version = u32::from_le_bytes(head[4..8].try_into().unwrap());
        let dim = u32::from_le_bytes(head[8..12].try_into().unwrap());
        let br = u32::from_le_bytes(head[12..16].try_into().unwrap());
        Ok(magic != VEC_MAGIC
            || version != VEC_VERSION
            || dim as usize != self.dim
            || br as usize != self.block_rows)
    }

    /// Drop the content of the vector file and write a fresh header.
    pub fn reset_vector_file(&mut self) -> SqlResult<()> {
        self.vec
            .set_len(0)
            .map_err(|e| SqlError::internal(format!("vec reset failed: {e}")))?;
        let mut head = Vec::with_capacity(VEC_HEADER_SIZE);
        head.extend_from_slice(&VEC_MAGIC.to_le_bytes());
        head.extend_from_slice(&VEC_VERSION.to_le_bytes());
        head.extend_from_slice(&(self.dim as u32).to_le_bytes());
        head.extend_from_slice(&(self.block_rows as u32).to_le_bytes());
        self.vec
            .write_at(&head, 0)
            .map_err(|e| SqlError::internal(format!("vec header write failed: {e}")))?;
        // Every cached vector block is stale now.
        self.vector_pool.borrow_mut().clear();
        Ok(())
    }

    /// Write the vector of a row into the slot the directory names.
    ///
    /// The directory entry knows the block and the slot, so the caller does
    /// not have to derive them from the row id.
    pub fn write_vector_at(&mut self, meta: &RowMeta, v: &[f32]) -> SqlResult<()> {
        self.write_vector(meta.vec_block, meta.vec_slot, v)
    }

    /// Read the vector of a row through its directory entry.
    pub fn read_vector_at(&mut self, meta: &RowMeta) -> SqlResult<Vec<f32>> {
        self.read_vector(meta.vec_block, meta.vec_slot)
    }

    /// Push the data of the table files into the kernel.
    ///
    /// The row directory and the row images are the durable copy of a
    /// committed row, so they are flushed before the process reports that it
    /// has shut down cleanly.
    pub fn sync(&mut self) -> SqlResult<()> {
        self.meta
            .sync_data()
            .and_then(|_| self.col.sync_data())
            .and_then(|_| self.vec.sync_data())
            .map_err(|e| SqlError::internal(format!("sync failed: {e}")))
    }

    /// Read the directory entries of a whole block with one call.
    ///
    /// The directory is a fixed size record array, so the entries of a block
    /// are contiguous. Reading them together turns `block_rows` reads into
    /// one, which is what makes a block scan cheap.
    ///
    /// The returned vector holds one entry per slot of the block; the slots
    /// beyond the last row are absent, so its length is the number of rows
    /// the block really holds.
    pub fn read_meta_block(&mut self, block: u32) -> io::Result<Rc<Vec<RowMeta>>> {
        let first = block as u64 * self.block_rows as u64;
        if first >= self.row_count {
            return Ok(Rc::new(Vec::new()));
        }
        let count = (self.row_count - first).min(self.block_rows as u64) as usize;
        let key = (self.file_id, block as u64);
        if let Some(hit) = self.directory_pool.borrow_mut().get(key) {
            return Ok(hit);
        }
        let mut buf = vec![0u8; count * ROW_META_SIZE];
        let n = read_at_up_to(&self.meta, &mut buf, first * ROW_META_SIZE as u64)?;
        let mut out = Vec::with_capacity(n / ROW_META_SIZE);
        for chunk in buf.chunks_exact(ROW_META_SIZE) {
            if chunk.len() < ROW_META_SIZE {
                break;
            }
            out.push(decode_meta(chunk));
        }
        // The block is cached, so that the next scan does not decode it
        // again. A write to the block drops it from the cache, so a block
        // that is still being filled is never served from a stale copy.
        Ok(self.directory_pool.borrow_mut().put(key, out))
    }

    /// Read the row images of a whole block with one call.
    ///
    /// The row images are appended, and the rows of a block were appended
    /// one after the other, so the images of a block are contiguous in
    /// `col.dat` as well. The returned buffer holds the images back to back,
    /// and the returned offsets hold the position of the image of every slot
    /// inside it.
    ///
    /// A row whose image was replaced by an update is the exception: its
    /// image was appended at the end of the file, so it does not sit between
    /// its neighbours. Such a row is read on its own.
    pub fn read_image_block(&mut self, metas: &[RowMeta]) -> io::Result<(Vec<u8>, Vec<u32>)> {
        let mut buf = Vec::new();
        let mut offsets = Vec::with_capacity(metas.len());
        // The images that follow each other in the file are read together.
        let mut i = 0usize;
        while i < metas.len() {
            let start = metas[i].col_offset;
            let mut end = start + metas[i].col_len as u64;
            let mut j = i + 1;
            while j < metas.len() && metas[j].col_offset == end {
                end += metas[j].col_len as u64;
                j += 1;
            }
            let base = buf.len() as u64;
            let mut chunk = vec![0u8; (end - start) as usize];
            let n = read_at_up_to(&self.col, &mut chunk, start)?;
            chunk.truncate(n);
            buf.extend_from_slice(&chunk);
            for m in &metas[i..j] {
                offsets.push((base + (m.col_offset - start)) as u32);
            }
            i = j;
        }
        Ok((buf, offsets))
    }

    /// The image of one slot of a block, out of the buffer of a block read.
    pub fn image_at<'a>(buf: &'a [u8], offsets: &[u32], slot: usize, len: u32) -> &'a [u8] {
        let start = offsets[slot] as usize;
        &buf[start..start + len as usize]
    }

    /// Read the scalar columns of a block and decode them.
    ///
    /// The row images of a block are read with one call and decoded once.
    /// The result is cached, so that the next query that filters on the
    /// block does not read and decode it again.
    ///
    /// The vector column is not decoded: it is stored in the vector file, and
    /// a scan that filters on the scalar columns must not copy it.
    pub fn read_scalar_block(
        &mut self,
        block: u32,
        metas: &[RowMeta],
    ) -> SqlResult<Rc<ScalarBlock>> {
        let key = (self.file_id, block as u64);
        if let Some(hit) = self.scalar_pool.borrow_mut().get(key) {
            return Ok(hit);
        }
        let (buf, offsets) = self.read_image_block(metas)?;
        let mut out = Vec::with_capacity(metas.len());
        for (slot, meta) in metas.iter().enumerate() {
            let image = Self::image_at(&buf, &offsets, slot, meta.col_len);
            out.push(crate::storage::format::decode_row_scalars(image)?);
        }
        // The block is cached, so that the next scan does not read and
        // decode it again. A write to the block drops it from the cache, so
        // a block that is still being filled is never served from a stale
        // copy.
        Ok(self.scalar_pool.borrow_mut().put(key, out))
    }

    /// The directory entry and the scalar columns of one row.
    ///
    /// Both come from the block caches, so a row of a block that is already
    /// cached costs no system call at all.
    ///
    /// The columns are handed out as a shared reference to the whole block
    /// together with the slot of the row, so that a caller that reads them
    /// allocates nothing at all.
    pub fn read_row_scalars(
        &mut self,
        row_id: u64,
    ) -> SqlResult<Option<(RowMeta, Rc<ScalarBlock>, usize)>> {
        let block = (row_id / self.block_rows as u64) as u32;
        let slot = (row_id % self.block_rows as u64) as usize;
        let metas = self.read_meta_block(block)?;
        let meta = match metas.get(slot) {
            Some(m) => *m,
            None => return Ok(None),
        };
        let scalars = self.read_scalar_block(block, &metas)?;
        if slot >= scalars.len() {
            return Ok(None);
        }
        Ok(Some((meta, scalars, slot)))
    }

    /// The vector of one row, taken from the cached block matrix.
    ///
    /// The matrix of a block is read once and kept, so a row of a block that
    /// is already cached costs a copy of `dim` floats and no system call.
    pub fn read_vector_cached(&mut self, meta: &RowMeta) -> SqlResult<Vec<f32>> {
        let matrix = self.block_matrix(meta.vec_block)?;
        let start = meta.vec_slot as usize * self.dim;
        let end = start + self.dim;
        Ok(matrix[start..end].to_vec())
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
        // The number of blocks has to follow the number of rows, because a
        // scan walks the blocks.
        self.blocks = self.row_count.div_ceil(self.block_rows as u64);
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
        Ok(Some(decode_meta(&buf)))
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
            .map_err(|e| SqlError::internal(format!("meta write failed: {e}")))?;
        // The directory block the entry belongs to changed, so the cached
        // copy of that block is stale.
        let block = row_id / self.block_rows as u64;
        self.directory_pool
            .borrow_mut()
            .invalidate((self.file_id, block));
        Ok(())
    }

    /// Append a row image to `col.dat` and return its offset.
    pub fn append_columns(&mut self, image: &[u8]) -> SqlResult<u64> {
        let offset = self.col.seek(SeekFrom::End(0))?;
        self.col
            .write_all(image)
            .map_err(|e| SqlError::internal(format!("col write failed: {e}")))?;
        // The image belongs to the block of the row that is written, and the
        // cached scalar columns of that block are now out of date.
        self.scalar_pool.borrow_mut().clear();
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
    /// The file starts with a header (see [`VEC_HEADER_SIZE`]), and the
    /// vectors of a block follow it in **row major** order: the vector of
    /// slot `s` of block `b` occupies `dim` consecutive floats, and the next
    /// slot follows it.
    ///
    /// ```text
    /// offset = VEC_HEADER_SIZE + ((b * block_rows) + s) * dim * 4
    /// ```
    ///
    /// Row major inside a block is deliberate. It makes a write of one vector
    /// a single contiguous write, and it makes a scan of one block a single
    /// contiguous read of `block_rows * dim * 4` bytes. The alternative -
    /// dimension major - would turn every vector write into `dim` scattered
    /// writes and every block scan into `dim` strided reads, which costs far
    /// more than the transpose it would save in the distance kernel.
    pub fn vector_offset(&self, block: u32, slot: u32) -> u64 {
        let br = self.block_rows as u64;
        let d = self.dim as u64;
        VEC_HEADER_SIZE as u64 + ((block as u64) * br + slot as u64) * d * 4
    }

    /// Write a vector into its slot.
    ///
    /// The block the vector belongs to is dropped from the cache, so that the
    /// next scan does not see the old content.
    pub fn write_vector(&mut self, block: u32, slot: u32, v: &[f32]) -> SqlResult<()> {
        let mut buf = Vec::with_capacity(v.len() * 4);
        for x in v {
            buf.extend_from_slice(&x.to_le_bytes());
        }
        let offset = self.vector_offset(block, slot);
        self.vec
            .write_at(&buf, offset)
            .map(|_| ())
            .map_err(|e| SqlError::internal(format!("vec write failed: {e}")))?;
        self.vector_pool.borrow_mut().invalidate((self.file_id, block as u64));
        Ok(())
    }

    /// Read one vector back.
    pub fn read_vector(&mut self, block: u32, slot: u32) -> SqlResult<Vec<f32>> {
        let mut buf = vec![0u8; self.dim * 4];
        let offset = self.vector_offset(block, slot);
        let n = read_at_up_to(&self.vec, &mut buf, offset)
            .map_err(|e| SqlError::internal(format!("vec read failed: {e}")))?;
        if n < buf.len() {
            // The slot was never written; the padding reads as zero.
            buf.truncate(n);
            buf.resize(self.dim * 4, 0);
        }
        Ok(buf.chunks_exact(4)
            .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
            .collect())
    }

    /// Read a contiguous run of vectors from one block.
    ///
    /// The vectors of one block are contiguous in the file, so the whole block
    /// is read with a single call. The returned buffer is a
    /// `block_rows x dim` matrix in row major order: the vector of slot `s`
    /// is `out[s * dim .. (s + 1) * dim]`.
    ///
    /// The matrix is cached, so that the next scan of the same block does
    /// not read and decode it again.
    pub fn read_block_matrix(&mut self, block: u32) -> SqlResult<Vec<f32>> {
        let br = self.block_rows;
        let d = self.dim;
        let total = br * d * 4;
        let offset = self.vector_offset(block, 0);
        let key = (self.file_id, block as u64);
        if let Some(hit) = self.vector_pool.borrow_mut().get(key) {
            return Ok((*hit).clone());
        }
        let mut buf = vec![0u8; total];
        let n = read_at_up_to(&self.vec, &mut buf, offset)
            .map_err(|e| SqlError::internal(format!("vec read failed: {e}")))?;
        // A partially filled block is padded with zeros.
        buf.truncate(n);
        buf.resize(total, 0);
        let out: Vec<f32> = buf
            .chunks_exact(4)
            .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
            .collect();
        self.vector_pool.borrow_mut().put(key, out.clone());
        Ok(out)
    }

    /// Read a block matrix without copying it.
    ///
    /// The cached matrix is handed out as a shared reference, so a scan that
    /// finds the block in the cache does not copy the whole matrix. This is
    /// the path a query takes; [`TableStore::read_block_matrix`] stays
    /// available for the callers that need their own copy.
    pub fn block_matrix(&mut self, block: u32) -> SqlResult<Rc<Vec<f32>>> {
        let key = (self.file_id, block as u64);
        if let Some(hit) = self.vector_pool.borrow_mut().get(key) {
            return Ok(hit);
        }
        let br = self.block_rows;
        let d = self.dim;
        let total = br * d * 4;
        let offset = self.vector_offset(block, 0);
        let mut buf = vec![0u8; total];
        let n = read_at_up_to(&self.vec, &mut buf, offset)
            .map_err(|e| SqlError::internal(format!("vec read failed: {e}")))?;
        buf.truncate(n);
        buf.resize(total, 0);
        let out: Vec<f32> = buf
            .chunks_exact(4)
            .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
            .collect();
        Ok(self.vector_pool.borrow_mut().put(key, out))
    }
}

/// Decode one directory entry.
fn decode_meta(buf: &[u8]) -> RowMeta {
    RowMeta {
        pk: i64::from_le_bytes(buf[0..8].try_into().unwrap()),
        col_offset: u64::from_le_bytes(buf[8..16].try_into().unwrap()),
        col_len: u32::from_le_bytes(buf[16..20].try_into().unwrap()),
        vec_block: u32::from_le_bytes(buf[20..24].try_into().unwrap()),
        vec_slot: u32::from_le_bytes(buf[24..28].try_into().unwrap()),
        version: u32::from_le_bytes(buf[28..32].try_into().unwrap()),
        alive: buf[32],
        _pad: [0; 11],
    }
}

/// Read up to `buf.len()` bytes at `offset`, tolerating a short file.
fn read_at_up_to(file: &File, buf: &mut [u8], offset: u64) -> io::Result<usize> {
    let mut total = 0usize;
    while total < buf.len() {
        match file.read_at(&mut buf[total..], offset + total as u64) {
            Ok(0) => break,
            Ok(n) => total += n,
            Err(ref e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A temporary directory that removes itself when the test ends.
    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> TempDir {
            let mut path = std::env::temp_dir();
            path.push(format!("mvdb-table-test-{tag}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).unwrap();
            TempDir(path)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn the_vector_file_is_row_major_inside_a_block() {
        // The layout is row major inside a block: the vector of slot `i`
        // occupies `dim` consecutive floats, and the next slot follows it.
        let dir = TempDir::new("layout");
        let mut store = TableStore::open(&dir.0, "t", 3, 8).unwrap();
        let vecs: Vec<Vec<f32>> = vec![
            vec![1.0, 2.0, 3.0],
            vec![4.0, 5.0, 6.0],
            vec![7.0, 8.0, 9.0],
        ];
        for (i, v) in vecs.iter().enumerate() {
            store.write_vector(0, i as u32, v).unwrap();
        }

        let raw = std::fs::read(dir.0.join("vec.dat")).unwrap();
        // The file starts with the header, the vectors follow it.
        assert_eq!(raw.len(), VEC_HEADER_SIZE + 9 * 4);
        let floats: Vec<f32> = raw[VEC_HEADER_SIZE..]
            .chunks_exact(4)
            .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
            .collect();
        assert_eq!(floats, vec![1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]);
    }

    #[test]
    fn the_block_matrix_matches_the_written_vectors() {
        let dir = TempDir::new("matrix");
        let mut store = TableStore::open(&dir.0, "t", 3, 8).unwrap();
        let vecs: Vec<Vec<f32>> = (0..5)
            .map(|i| vec![i as f32, i as f32 + 0.5, i as f32 + 0.25])
            .collect();
        for (i, v) in vecs.iter().enumerate() {
            store.write_vector(0, i as u32, v).unwrap();
        }

        let m = store.read_block_matrix(0).unwrap();
        assert_eq!(m.len(), 8 * 3);
        for (i, v) in vecs.iter().enumerate() {
            for k in 0..3 {
                assert_eq!(m[i * 3 + k], v[k], "slot {i} dimension {k}");
            }
        }
        // The slots that were not written are zero.
        for slot in 5..8 {
            for k in 0..3 {
                assert_eq!(m[slot * 3 + k], 0.0);
            }
        }
    }

    #[test]
    fn the_block_matrix_of_a_second_block_is_independent() {
        let dir = TempDir::new("blocks");
        let mut store = TableStore::open(&dir.0, "t", 2, 8).unwrap();
        store.write_vector(0, 0, &[1.0, 2.0]).unwrap();
        store.write_vector(1, 0, &[3.0, 4.0]).unwrap();

        let first = store.read_block_matrix(0).unwrap();
        let second = store.read_block_matrix(1).unwrap();
        assert_eq!(&first[..2], &[1.0, 2.0]);
        assert_eq!(&second[..2], &[3.0, 4.0]);
    }

    #[test]
    fn a_single_vector_is_read_back_unchanged() {
        let dir = TempDir::new("single");
        let mut store = TableStore::open(&dir.0, "t", 4, 8).unwrap();
        let v = vec![0.5, -1.25, 3.0, 8.5];
        store.write_vector(0, 3, &v).unwrap();
        assert_eq!(store.read_vector(0, 3).unwrap(), v);
        // A slot that was never written reads as zero.
        assert_eq!(store.read_vector(0, 0).unwrap(), vec![0.0; 4]);
    }

    #[test]
    fn a_fresh_file_is_not_stale_but_a_foreign_one_is() {
        let dir = TempDir::new("stale");
        let mut store = TableStore::open(&dir.0, "t", 3, 8).unwrap();
        assert!(!store.vector_file_is_stale().unwrap());

        // A file without a header is stale.
        std::fs::write(dir.0.join("vec.dat"), b"garbage").unwrap();
        assert!(store.vector_file_is_stale().unwrap());

        // A file with the wrong dimension is stale as well.
        store.reset_vector_file().unwrap();
        assert!(!store.vector_file_is_stale().unwrap());
        let mut other = TableStore::open(&dir.0, "t", 4, 8).unwrap();
        assert!(other.vector_file_is_stale().unwrap());
    }

    #[test]
    fn resetting_the_vector_file_drops_its_content() {
        let dir = TempDir::new("reset");
        let mut store = TableStore::open(&dir.0, "t", 2, 8).unwrap();
        store.write_vector(0, 0, &[1.0, 2.0]).unwrap();
        store.reset_vector_file().unwrap();
        assert_eq!(store.read_vector(0, 0).unwrap(), vec![0.0, 0.0]);
        assert_eq!(
            std::fs::metadata(dir.0.join("vec.dat")).unwrap().len(),
            VEC_HEADER_SIZE as u64
        );
    }

    #[test]
    fn the_block_count_follows_the_row_count() {
        let dir = TempDir::new("blocks-count");
        let mut store = TableStore::open(&dir.0, "t", 2, 8).unwrap();
        assert_eq!(store.blocks, 0);
        for i in 0..8 {
            let offset = store.append_columns(&[i as u8]).unwrap();
            store.append_row(i as i64, offset, 1, 0, i as u32).unwrap();
        }
        assert_eq!(store.row_count, 8);
        assert_eq!(store.blocks, 1);
        let offset = store.append_columns(&[9]).unwrap();
        store.append_row(9, offset, 1, 1, 0).unwrap();
        assert_eq!(store.blocks, 2);
    }

    #[test]
    fn the_directory_of_a_block_is_read_in_one_call() {
        let dir = TempDir::new("meta-block");
        let mut store = TableStore::open(&dir.0, "t", 2, 8).unwrap();
        for i in 0..10u64 {
            let offset = store.append_columns(&[i as u8]).unwrap();
            store.append_row(i as i64, offset, 1, 0, i as u32).unwrap();
        }
        let first = store.read_meta_block(0).unwrap();
        assert_eq!(first.len(), 8);
        assert_eq!(first[3].pk, 3);
        let second = store.read_meta_block(1).unwrap();
        assert_eq!(second.len(), 2);
        assert_eq!(second[0].pk, 8);
        assert_eq!(second[1].pk, 9);
        // A block beyond the last one is empty.
        assert!(store.read_meta_block(2).unwrap().is_empty());
    }

    #[test]
    fn a_cached_block_matrix_is_the_same_after_a_second_read() {
        let dir = TempDir::new("cached-matrix");
        let mut store = TableStore::open(&dir.0, "t", 3, 8).unwrap();
        for i in 0..8u64 {
            store.write_vector(0, i as u32, &[i as f32, 1.0, 2.0]).unwrap();
        }
        let first = store.read_block_matrix(0).unwrap();
        let second = store.read_block_matrix(0).unwrap();
        assert_eq!(first, second);
        // The shared form holds the same values.
        let shared = store.block_matrix(0).unwrap();
        assert_eq!(&*shared, &first[..]);
    }

    #[test]
    fn a_write_drops_the_cached_block() {
        let dir = TempDir::new("stale-cache");
        let mut store = TableStore::open(&dir.0, "t", 2, 8).unwrap();
        store.write_vector(0, 0, &[1.0, 2.0]).unwrap();
        assert_eq!(store.read_block_matrix(0).unwrap()[0], 1.0);
        // The write changes the file, so the cached copy has to be dropped.
        store.write_vector(0, 0, &[9.0, 9.0]).unwrap();
        assert_eq!(store.read_block_matrix(0).unwrap()[0], 9.0);
    }

    #[test]
    fn a_write_drops_the_cached_directory_block() {
        let dir = TempDir::new("stale-directory");
        let mut store = TableStore::open(&dir.0, "t", 2, 8).unwrap();
        for i in 0..8u64 {
            let offset = store.append_columns(&[i as u8]).unwrap();
            store.append_row(i as i64, offset, 1, 0, i as u32).unwrap();
        }
        assert_eq!(store.read_meta_block(0).unwrap()[2].pk, 2);
        // An update rewrites the directory entry of a row, so the cached
        // block of the directory has to be dropped.
        let mut meta = store.read_meta(2).unwrap().unwrap();
        meta.pk = 42;
        store.write_meta(2, &meta).unwrap();
        assert_eq!(store.read_meta_block(0).unwrap()[2].pk, 42);
    }
}

