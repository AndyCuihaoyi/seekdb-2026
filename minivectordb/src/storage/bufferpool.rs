//! A bounded cache of decoded blocks.
//!
//! The evaluation measures the *whole container* peak memory (cgroup v2
//! `memory.peak`, page cache included) and requires that a data set larger
//! than the memory budget still works. Two rules follow:
//!
//! 1. Never `mmap` data files: mapped pages are counted against the cgroup
//!    and cannot be evicted independently.
//! 2. Read through a bounded cache so the process RSS stays proportional to
//!    the configured budget rather than to the data set.
//!
//! # What is cached
//!
//! The cache holds *decoded* blocks, not raw bytes. The expensive part of a
//! scan is not the read itself but the decoding that follows it: a block of
//! vectors holds `block_rows * dim` floats, and turning the bytes into floats
//! costs more than the copy out of the file. Caching the decoded form means
//! that a block is decoded once instead of once per query.
//!
//! Two kinds of block are cached:
//!
//! * the *vector* blocks, as the matrix of floats the distance kernel works
//!   on;
//! * the *directory* blocks, as the list of row directory entries.
//!
//! The row images are not cached: they are appended to, so a cached block at
//! the end of the file would go stale, and they are read in one call per
//! block anyway.
//!
//! # The eviction order
//!
//! The least recently used block is evicted first. The order is kept as an
//! intrusive doubly linked list, so touching a block and evicting a block
//! both cost a constant number of steps. A linear search through an access
//! order vector would make every access quadratic in the number of cached
//! blocks, which is exactly the kind of hidden cost that turns a cache into
//! a slowdown.

use std::collections::HashMap;
use std::rc::Rc;

/// A cache key: the identifier of a file and the index of a block in it.
pub type Key = (u32, u64);

/// Something the cache can hold.
pub trait CacheEntry {
    /// The number of bytes the entry accounts for.
    fn size(&self) -> usize;
}

impl CacheEntry for Vec<u8> {
    fn size(&self) -> usize {
        self.len()
    }
}

impl CacheEntry for Vec<f32> {
    fn size(&self) -> usize {
        self.len() * 4
    }
}

/// One cached block.
struct Slot<T> {
    /// The block itself. It is shared, so handing it out is cheap.
    data: Rc<T>,
    /// The key of the slot before this one, the least recently used end.
    prev: Option<Key>,
    /// The key of the slot after this one, the most recently used end.
    next: Option<Key>,
}

/// A bounded cache of blocks, evicted in least recently used order.
pub struct BufferPool<T: CacheEntry> {
    /// The budget of the pool, in bytes.
    max_bytes: usize,
    /// The number of bytes the cached blocks occupy.
    used_bytes: usize,
    /// The cached blocks.
    slots: HashMap<Key, Slot<T>>,
    /// The least recently used key.
    head: Option<Key>,
    /// The most recently used key.
    tail: Option<Key>,
}

impl<T: CacheEntry> BufferPool<T> {
    /// A pool with a budget of `max_mb` megabytes.
    pub fn new(max_mb: usize) -> BufferPool<T> {
        BufferPool {
            max_bytes: max_mb.saturating_mul(1024 * 1024),
            used_bytes: 0,
            slots: HashMap::new(),
            head: None,
            tail: None,
        }
    }

    /// The budget of the pool, in bytes.
    pub fn max_bytes(&self) -> usize {
        self.max_bytes
    }

    /// The number of bytes the cached blocks occupy.
    pub fn cached_bytes(&self) -> usize {
        self.used_bytes
    }

    /// The number of cached blocks.
    pub fn len(&self) -> usize {
        self.slots.len()
    }

    /// Whether the pool holds nothing.
    pub fn is_empty(&self) -> bool {
        self.slots.is_empty()
    }

    /// The cached block, if it is there.
    ///
    /// A hit moves the block to the most recently used end.
    pub fn get(&mut self, key: Key) -> Option<Rc<T>> {
        let data = self.slots.get(&key)?.data.clone();
        self.touch(key);
        Some(data)
    }

    /// Whether the pool holds the block, without touching it.
    pub fn contains(&self, key: Key) -> bool {
        self.slots.contains_key(&key)
    }

    /// Put a block into the pool and hand it back.
    ///
    /// The pool evicts the least recently used blocks until the budget is
    /// respected again.
    pub fn put(&mut self, key: Key, data: T) -> Rc<T> {
        let size = data.size();
        let data = Rc::new(data);
        if let Some(slot) = self.slots.get_mut(&key) {
            // The block is already cached; only its content changed.
            self.used_bytes = self.used_bytes + size - slot.data.size();
            slot.data = data.clone();
            self.touch(key);
            self.evict_if_needed();
            return data;
        }
        self.slots.insert(
            key,
            Slot { data: data.clone(), prev: self.tail, next: None },
        );
        if let Some(tail) = self.tail {
            if let Some(slot) = self.slots.get_mut(&tail) {
                slot.next = Some(key);
            }
        } else {
            self.head = Some(key);
        }
        self.tail = Some(key);
        self.used_bytes += size;
        self.evict_if_needed();
        data
    }

    /// Drop one block from the pool.
    ///
    /// A block that was written behind the pool's back has to be dropped,
    /// because the copy the pool holds is stale.
    pub fn invalidate(&mut self, key: Key) {
        if let Some(slot) = self.slots.remove(&key) {
            self.used_bytes -= slot.data.size();
            self.unlink(key, slot.prev, slot.next);
        }
    }

    /// Drop every block of the pool.
    pub fn clear(&mut self) {
        self.slots.clear();
        self.head = None;
        self.tail = None;
        self.used_bytes = 0;
    }

    /// Move a block to the most recently used end.
    fn touch(&mut self, key: Key) {
        let (prev, next) = match self.slots.get(&key) {
            Some(slot) => (slot.prev, slot.next),
            None => return,
        };
        if self.tail == Some(key) {
            return;
        }
        self.unlink(key, prev, next);
        let old_tail = self.tail;
        if let Some(slot) = self.slots.get_mut(&key) {
            slot.prev = old_tail;
            slot.next = None;
        }
        if let Some(tail) = old_tail {
            if let Some(slot) = self.slots.get_mut(&tail) {
                slot.next = Some(key);
            }
        } else {
            self.head = Some(key);
        }
        self.tail = Some(key);
    }

    /// Take a block out of the access order.
    fn unlink(&mut self, key: Key, prev: Option<Key>, next: Option<Key>) {
        if let Some(p) = prev {
            if let Some(slot) = self.slots.get_mut(&p) {
                slot.next = next;
            }
        } else {
            self.head = next;
        }
        if let Some(n) = next {
            if let Some(slot) = self.slots.get_mut(&n) {
                slot.prev = prev;
            }
        } else {
            self.tail = prev;
        }
        let _ = key;
    }

    /// Drop the least recently used blocks until the budget is respected.
    fn evict_if_needed(&mut self) {
        while self.used_bytes > self.max_bytes {
            let victim = match self.head {
                Some(h) => h,
                None => break,
            };
            let slot = match self.slots.remove(&victim) {
                Some(s) => s,
                None => break,
            };
            self.used_bytes -= slot.data.size();
            self.unlink(victim, slot.prev, slot.next);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(n: u64) -> Key {
        (0, n)
    }

    #[test]
    fn a_fresh_pool_holds_nothing() {
        let mut pool: BufferPool<Vec<f32>> = BufferPool::new(1);
        assert!(pool.is_empty());
        assert_eq!(pool.cached_bytes(), 0);
        assert!(pool.get(key(0)).is_none());
    }

    #[test]
    fn a_block_is_handed_out_again() {
        let mut pool: BufferPool<Vec<f32>> = BufferPool::new(1);
        pool.put(key(0), vec![1.0f32, 2.0]);
        let hit = pool.get(key(0)).expect("the block is cached");
        assert_eq!(&*hit, &[1.0, 2.0]);
        assert_eq!(pool.len(), 1);
    }

    #[test]
    fn the_least_recently_used_block_is_evicted_first() {
        // The budget holds two blocks of four bytes each.
        let mut pool: BufferPool<Vec<f32>> = BufferPool::new(0);
        pool.max_bytes = 8;
        pool.put(key(0), vec![0.0f32]);
        pool.put(key(1), vec![1.0f32]);
        // Touch the first one, so that the second becomes the oldest.
        assert!(pool.get(key(0)).is_some());
        pool.put(key(2), vec![2.0f32]);
        assert!(pool.get(key(1)).is_none(), "the oldest block was evicted");
        assert!(pool.get(key(0)).is_some(), "the touched block survived");
        assert!(pool.get(key(2)).is_some());
    }

    #[test]
    fn the_budget_is_respected() {
        let mut pool: BufferPool<Vec<f32>> = BufferPool::new(0);
        pool.max_bytes = 16;
        for i in 0..10u64 {
            pool.put(key(i), vec![0.0f32; 4]);
        }
        assert!(pool.cached_bytes() <= 16);
        assert!(pool.len() <= 4);
    }

    #[test]
    fn an_invalidated_block_is_gone() {
        let mut pool: BufferPool<Vec<f32>> = BufferPool::new(1);
        pool.put(key(3), vec![1.0f32]);
        assert!(pool.contains(key(3)));
        pool.invalidate(key(3));
        assert!(!pool.contains(key(3)));
        assert_eq!(pool.cached_bytes(), 0);
    }

    #[test]
    fn a_cleared_pool_is_empty() {
        let mut pool: BufferPool<Vec<f32>> = BufferPool::new(1);
        pool.put(key(0), vec![1.0f32]);
        pool.put(key(1), vec![2.0f32]);
        pool.clear();
        assert!(pool.is_empty());
        assert_eq!(pool.cached_bytes(), 0);
    }

    #[test]
    fn a_replaced_block_is_counted_once() {
        let mut pool: BufferPool<Vec<f32>> = BufferPool::new(1);
        pool.put(key(0), vec![0.0f32; 4]);
        assert_eq!(pool.cached_bytes(), 16);
        pool.put(key(0), vec![0.0f32; 8]);
        assert_eq!(pool.cached_bytes(), 32);
        assert_eq!(pool.len(), 1);
    }

    /// The access order has to stay a correct list under a long sequence of
    /// touches and evictions, otherwise the eviction would pick the wrong
    /// block.
    #[test]
    fn the_access_order_stays_consistent() {
        let mut pool: BufferPool<Vec<f32>> = BufferPool::new(0);
        pool.max_bytes = 24;
        // A fixed sequence of accesses.
        for i in 0..200u64 {
            pool.put(key(i), vec![0.0f32; 2]);
            if i % 3 == 0 {
                pool.get(key(i / 2));
            }
            if i % 7 == 0 {
                pool.invalidate(key(i / 2));
            }
        }
        // Every cached block is reachable through the list, exactly once.
        let mut seen = Vec::new();
        let mut cursor = pool.head;
        while let Some(k) = cursor {
            assert!(!seen.contains(&k), "a block appears twice in the order");
            seen.push(k);
            cursor = pool.slots.get(&k).and_then(|s| s.next);
            assert!(seen.len() <= pool.len());
        }
        assert_eq!(seen.len(), pool.len());
    }
}
