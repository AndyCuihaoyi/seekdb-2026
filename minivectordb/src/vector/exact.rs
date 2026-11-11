//! The exact TopK scan: block by block, with a bounded heap.
//!
//! The exact search has to measure every visible row of the table. Doing
//! that row by row means two system calls and one decoding per row, which
//! dominates the runtime of a query: a table of ten thousand rows and a
//! thousand queries cost twenty million system calls.
//!
//! The scan here works on the blocks of the table instead:
//!
//! 1. The directory entries of a block are read with one call, because they
//!    are a fixed size record array.
//! 2. The vectors of a block are read with one call, because they are
//!    stored row major inside the block.
//! 3. The row images of a block are read with one call, but only when the
//!    query carries a filter, because only then are the columns of the rows
//!    needed.
//!
//! The distance is measured over the vectors of the block matrix, which are
//! the very same values the row images hold, so the result is bit for bit
//! the one the row by row path produces.
//!
//! The answer is kept in a heap of at most `k` elements, so the cost of a
//! scan is `O(rows * log k)` instead of `O(rows log rows)`.

use crate::error::SqlResult;
use crate::storage::table::{ColValue, TableStore};
use crate::vector::dist::l2_sq_dispatch;

/// One candidate of the answer.
#[derive(Debug, Clone, Copy)]
pub struct Candidate {
    /// The primary key of the row.
    pub pk: i64,
    /// The distance to the query.
    pub dist: f32,
    /// The squared distance to the query.
    ///
    /// The square root is monotonic, so the squared distance orders the
    /// candidates exactly like the distance does. The scan keeps the squared
    /// distance and takes the square root only of the rows that reach the
    /// answer, which is `k` rows instead of one row per row of the table.
    pub dist_sq: f32,
    /// The identifier of the row.
    pub row_id: u64,
}

/// The ordering of the candidates.
///
/// The heap of the scan is a *max* heap, so that the worst candidate is at
/// its top and can be dropped when a better one arrives. A candidate is
/// worse when its distance is larger, and among equal distances when its
/// primary key is larger, because the answer breaks the ties by ascending
/// primary key.
impl Eq for Candidate {}

impl PartialEq for Candidate {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other) == std::cmp::Ordering::Equal
    }
}

impl Ord for Candidate {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        // The squared distance is compared, not the distance: the two order
        // the candidates the same way, and the square root is only taken
        // for the rows of the answer.
        self.dist_sq
            .partial_cmp(&other.dist_sq)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(self.pk.cmp(&other.pk))
    }
}

impl PartialOrd for Candidate {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

/// A bounded heap that keeps the best `k` candidates.
///
/// The heap holds at most `k` elements, so the memory it needs does not
/// depend on the size of the table.
pub struct TopK {
    k: usize,
    /// The candidates, worst first.
    heap: std::collections::BinaryHeap<Candidate>,
}

impl TopK {
    /// A heap that keeps the best `k` candidates.
    pub fn new(k: usize) -> TopK {
        TopK { k, heap: std::collections::BinaryHeap::with_capacity(k) }
    }

    /// Offer a candidate to the heap.
    ///
    /// The candidate is dropped when it is worse than every candidate the
    /// heap already holds.
    pub fn push(&mut self, candidate: Candidate) {
        if self.k == 0 {
            return;
        }
        if self.heap.len() < self.k {
            self.heap.push(candidate);
            return;
        }
        // The worst candidate is at the top of the heap.
        match self.heap.peek() {
            Some(worst) if candidate < *worst => {
                self.heap.pop();
                self.heap.push(candidate);
            }
            _ => {}
        }
    }

    /// The number of candidates the heap holds.
    pub fn len(&self) -> usize {
        self.heap.len()
    }

    /// Whether the heap is empty.
    pub fn is_empty(&self) -> bool {
        self.heap.is_empty()
    }

    /// The candidates, ordered by distance and then by primary key.
    ///
    /// The square root is taken here, for the rows of the answer only, so
    /// the scan never computes one for a row that does not reach the answer.
    pub fn into_sorted(self) -> Vec<Candidate> {
        let mut out: Vec<Candidate> = self.heap.into_iter().collect();
        out.sort_by(|a, b| {
            a.dist_sq
                .partial_cmp(&b.dist_sq)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.pk.cmp(&b.pk))
        });
        for candidate in out.iter_mut() {
            candidate.dist = candidate.dist_sq.sqrt();
        }
        out
    }
}

/// The filter of a scan.
///
/// The callback receives the row identifier, the primary key and the decoded
/// scalar columns of the row, and decides whether the row belongs to the
/// answer. The filter is applied *before* the answer is chosen, so a filtered
/// query is answered over the filtered set.
pub type Filter<'a> = &'a mut dyn FnMut(u64, i64, &[Option<ColValue>]) -> SqlResult<bool>;

/// A scan that only visits the rows of a set.
///
/// The rows are given by their identifiers. The scan visits them in the order
/// of the table, so the answer does not depend on the order in which the
/// rows are measured.
pub type Candidates<'a> = &'a [u64];

/// Scan a table block by block and return the best candidates.
///
/// The rows of the current transaction are not part of the scan: the caller
/// adds them separately, because they are not in the storage yet.
pub fn scan_exact_topk(
    store: &mut TableStore,
    query: &[f32],
    k: usize,
    mut filter: Option<Filter<'_>>,
) -> SqlResult<Vec<Candidate>> {
    scan_exact_topk_candidates(store, query, k, filter, None)
}

/// Scan a table block by block, visiting only the rows of a set.
///
/// When the set is given, the rows outside of it are never measured: the
/// distance of a row that the filter rejects is not computed at all. This is
/// what makes a filtered query cheaper than an unfiltered one, instead of
/// costing the same plus the filter.
///
/// The set is used as a *candidate* set, not as the answer: the rows it
/// names are still checked against the filter, so a set that is wrong or out
/// of date cannot change the answer of the query.
pub fn scan_exact_topk_candidates(
    store: &mut TableStore,
    query: &[f32],
    k: usize,
    mut filter: Option<Filter<'_>>,
    candidates: Option<Candidates<'_>>,
) -> SqlResult<Vec<Candidate>> {
    let mut top = TopK::new(k);
    let dim = store.dim;

    for block in 0..store.blocks {
        let metas = store.read_meta_block(block as u32)?;
        if metas.is_empty() {
            break;
        }
        // The vectors of the block, with one read. The matrix is handed out
        // as a shared reference, so a block that is already cached is not
        // copied.
        let matrix = store.block_matrix(block as u32)?;
        // The scalar columns of the block, with one read. They are only
        // needed when the query carries a filter, and they are decoded once
        // per block rather than once per row.
        let scalars = if filter.is_some() {
            Some(store.read_scalar_block(block as u32, &metas)?)
        } else {
            None
        };

        // The rows of the candidate set that fall into this block.
        //
        // The set is sorted, so the rows of the block are found with a
        // cursor that only moves forward: the whole scan costs one pass
        // over the set, not one search per row.
        let mut cursor = 0usize;
        let set = candidates.unwrap_or(&[]);
        let set_is_used = candidates.is_some();

        for (slot, meta) in metas.iter().enumerate() {
            if meta.alive == 0 {
                continue;
            }
            let row_id = block * store.block_rows as u64 + slot as u64;

            if set_is_used {
                // Skip the rows that are not candidates.
                while cursor < set.len() && set[cursor] < row_id {
                    cursor += 1;
                }
                if cursor >= set.len() || set[cursor] != row_id {
                    continue;
                }
                cursor += 1;
            }

            let vec = &matrix[slot * dim..(slot + 1) * dim];
            // The squared distance is what the scan compares, so the square
            // root is only taken for the rows that reach the answer.
            let dist_sq = l2_sq_dispatch(vec, query);

            if let Some(filter) = filter.as_mut() {
                let cols = scalars
                    .as_ref()
                    .expect("the columns were read")
                    .get(slot)
                    .expect("the block holds the row");
                if !filter(row_id, meta.pk, cols)? {
                    continue;
                }
            }
            top.push(Candidate { pk: meta.pk, dist: dist_sq.sqrt(), dist_sq, row_id });
        }
    }

    Ok(top.into_sorted())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_heap_keeps_the_best_candidates() {
        let mut top = TopK::new(3);
        for (pk, dist) in [(1, 5.0), (2, 1.0), (3, 3.0), (4, 2.0)] {
            top.push(Candidate { pk, dist, dist_sq: dist * dist, row_id: pk as u64 });
        }
        let out = top.into_sorted();
        assert_eq!(out.len(), 3);
        assert_eq!(out.iter().map(|c| c.pk).collect::<Vec<_>>(), vec![2, 4, 3]);
    }

    #[test]
    fn the_heap_breaks_the_ties_by_primary_key() {
        let mut top = TopK::new(2);
        for pk in [7, 3, 5] {
            top.push(Candidate { pk, dist: 1.0, dist_sq: 1.0, row_id: pk as u64 });
        }
        let out = top.into_sorted();
        assert_eq!(out.iter().map(|c| c.pk).collect::<Vec<_>>(), vec![3, 5]);
    }

    #[test]
    fn a_heap_of_zero_keeps_nothing() {
        let mut top = TopK::new(0);
        top.push(Candidate { pk: 1, dist: 0.0, dist_sq: 0.0, row_id: 1 });
        assert!(top.is_empty());
    }

    #[test]
    fn the_heap_reports_its_size() {
        let mut top = TopK::new(2);
        top.push(Candidate { pk: 1, dist: 0.0, dist_sq: 0.0, row_id: 1 });
        assert_eq!(top.len(), 1);
        top.push(Candidate { pk: 2, dist: 1.0, dist_sq: 1.0, row_id: 2 });
        assert_eq!(top.len(), 2);
        top.push(Candidate { pk: 3, dist: 2.0, dist_sq: 4.0, row_id: 3 });
        assert_eq!(top.len(), 2);
    }
}
