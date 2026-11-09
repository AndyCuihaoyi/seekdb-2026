//! Bounded buffer pool.
//!
//! The evaluation measures the *whole container* peak memory (cgroup v2
//! `memory.peak`, page cache included) and requires that a data set larger
//! than the memory budget still works. Two rules follow:
//!
//! 1. Never `mmap` data files: mapped pages are counted against the cgroup
//!    and cannot be evicted independently.
//! 2. Read through a bounded cache so the process RSS stays proportional to
//!    the configured budget rather than to the data set.

use std::collections::HashMap;
use std::fs::File;
use std::io;
use std::os::unix::fs::FileExt;

/// A cache key: (file id, block index).
type Key = (u32, u64);

/// Number of bytes in one cached block.
pub const BLOCK_SIZE: usize = 64 * 1024;

struct Entry {
    data: Vec<u8>,
    dirty: bool,
}

/// Bounded LRU cache of fixed size blocks.
pub struct BufferPool {
    max_bytes: usize,
    used_bytes: usize,
    map: HashMap<Key, Entry>,
    /// Access order, least recently used first.
    order: Vec<Key>,
}

impl BufferPool {
    pub fn new(max_mb: usize) -> Self {
        BufferPool {
            max_bytes: max_mb.saturating_mul(1024 * 1024).max(BLOCK_SIZE),
            used_bytes: 0,
            map: HashMap::new(),
            order: Vec::new(),
        }
    }

    pub fn max_bytes(&self) -> usize {
        self.max_bytes
    }

    fn touch(&mut self, key: Key) {
        if let Some(pos) = self.order.iter().position(|k| *k == key) {
            let k = self.order.remove(pos);
            self.order.push(k);
        }
    }

    fn evict_if_needed(&mut self) {
        while self.used_bytes > self.max_bytes {
            if self.order.is_empty() {
                break;
            }
            let victim = self.order.remove(0);
            if let Some(entry) = self.map.remove(&victim) {
                self.used_bytes -= entry.data.len();
            }
        }
    }

    /// Read a block, loading it from the file when it is not cached.
    pub fn read_block(&mut self, file_id: u32, file: &File, block: u64) -> io::Result<&[u8]> {
        let key = (file_id, block);
        if !self.map.contains_key(&key) {
            let offset = block * BLOCK_SIZE as u64;
            let mut buf = vec![0u8; BLOCK_SIZE];
            let n = file.read_at(&mut buf, offset)?;
            if n < BLOCK_SIZE {
                buf.truncate(n);
            }
            self.used_bytes += buf.len();
            self.map.insert(key, Entry { data: buf, dirty: false });
            self.order.push(key);
            self.evict_if_needed();
        }
        self.touch(key);
        Ok(&self.map.get(&key).unwrap().data)
    }

    /// Read an arbitrary byte range, assembling it from cached blocks.
    pub fn read_range(
        &mut self,
        file_id: u32,
        file: &File,
        offset: u64,
        len: usize,
    ) -> io::Result<Vec<u8>> {
        let mut out = Vec::with_capacity(len);
        let mut remaining = len;
        let mut pos = offset;
        while remaining > 0 {
            let block = pos / BLOCK_SIZE as u64;
            let within = (pos % BLOCK_SIZE as u64) as usize;
            let chunk = self.read_block(file_id, file, block)?;
            let avail = chunk.len().saturating_sub(within);
            if avail == 0 {
                break;
            }
            let take = avail.min(remaining);
            out.extend_from_slice(&chunk[within..within + take]);
            remaining -= take;
            pos += take as u64;
        }
        Ok(out)
    }

    /// Invalidate a cached block after it was written behind the pool's back.
    pub fn invalidate(&mut self, file_id: u32, block: u64) {
        let key = (file_id, block);
        if let Some(entry) = self.map.remove(&key) {
            self.used_bytes -= entry.data.len();
        }
        self.order.retain(|k| *k != key);
    }

    /// Drop every cached block of a file (used when a file is rewritten).
    pub fn invalidate_file(&mut self, file_id: u32) {
        let keys: Vec<Key> = self.map.keys().copied().filter(|(f, _)| *f == file_id).collect();
        for k in keys {
            if let Some(entry) = self.map.remove(&k) {
                self.used_bytes -= entry.data.len();
            }
        }
        self.order.retain(|(f, _)| *f != file_id);
    }

    pub fn cached_bytes(&self) -> usize {
        self.used_bytes
    }
}
