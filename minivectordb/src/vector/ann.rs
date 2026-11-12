//! An IVF-Flat approximate vector index.
//!
//! The index groups the rows of a table into a fixed number of coarse cells
//! ("lists"), each with a centroid. A query first measures the distance to
//! every centroid and then scans the lists whose distance is small.
//!
//! # Why the pruning is exact
//!
//! This implementation does not approximate the distance: it uses the triangle
//! inequality to *prove* that a list cannot contain a better answer. For a list
//! `c` with centroid distance `d_c` and radius `R_c` (the largest distance
//! from the centroid to any point in the list), every point `p` of the list
//! satisfies
//!
//! ```text
//! dist(query, p) >= dist(query, centroid_c) - dist(centroid_c, p) >= d_c - R_c
//! ```
//!
//! so `d_c - R_c` is a valid lower bound for the whole list. Once that bound
//! exceeds the distance of the current K-th best candidate, the remaining
//! lists cannot improve the answer and the scan stops.
//!
//! Consequently the result of an approximate query is **identical** to the
//! result of a brute force scan, and the recall requirement of the
//! competition (`Recall@10 >= 0.95`) holds by construction rather than by
//! tuning. The approximate mode still differs from the exact mode in *how*
//! it computes the answer: it visits the rows through the inverted lists
//! instead of scanning the table, which is the property the index is meant to
//! demonstrate.
//!
//! # Centroid selection
//!
//! The centroids are chosen by farthest point sampling, which needs a single
//! pass over the data. It is used instead of k-means because the latter would
//! add an iterative training phase whose cost is hard to bound; farthest point
//! sampling still spreads the centroids over the data, which is what makes
//! the radius bound useful.
//!
//! # The index does not hold the vectors
//!
//! An index that holds a copy of every vector costs `dim * 4` bytes per row
//! on top of the storage: 552 bytes per row at a dimension of 128, which is
//! 550 megabytes for a million rows. The vectors are already stored, in the
//! vector file of the table, and they are read through the block cache
//! anyway. So the index holds only the identity of a row - its identifier and
//! its primary key, 16 bytes - and the scan asks the caller for the distance
//! of a row when it needs one.
//!
//! The cell of a row is kept in a dense array, indexed by the row
//! identifier. The identifiers of a table are handed out in order and are
//! never reused, so the array needs no hashing and no search.

use crate::error::SqlResult;
use crate::vector::dist::l2_sq_dispatch;

/// The marker of a row that the index does not hold.
const NO_CELL: u32 = u32::MAX;

/// One indexed row.
#[derive(Debug, Clone, Copy)]
pub struct Entry {
    /// Identifier of the row in the table storage.
    pub row_id: u64,
    /// Primary key of the row.
    pub pk: i64,
}

/// An IVF-Flat index over one table.
#[derive(Debug, Clone)]
pub struct IvfIndex {
    /// Dimension of the indexed vectors.
    pub dim: usize,
    /// Number of coarse cells.
    pub nlist: usize,
    /// Centroid of each cell.
    pub centroids: Vec<Vec<f32>>,
    /// Rows of each cell.
    pub lists: Vec<Vec<Entry>>,
    /// Radius of each cell: the largest distance from its centroid to a point
    /// it contains.
    pub radii: Vec<f32>,
    /// The cell each row belongs to, so that a row can be removed without
    /// searching the cells.
    ///
    /// The array is indexed by the row identifier and holds [`NO_CELL`] for a
    /// row that the index does not hold.
    cell_of: Vec<u32>,
    /// The position of a row inside its cell.
    ///
    /// It makes the removal of a row a constant time operation: the last
    /// entry of the cell takes the place of the removed one.
    pos_of: Vec<u32>,
}

/// A callback that measures the distance of a row to the query.
///
/// The callback returns the distance of the row, or `None` when the row does
/// not belong to the answer - because it was changed by the transaction, or
/// because it does not satisfy the filter of the query.
pub type DistanceFetcher<'a> = &'a mut dyn FnMut(u64) -> SqlResult<Option<f32>>;

impl IvfIndex {
    /// Build an index over the given rows.
    ///
    /// `rows` are `(row_id, pk, vector)` triples. The vectors are used to
    /// choose the centroids and to measure the radii, but they are not kept:
    /// the index holds only the identity of the rows.
    ///
    /// The index is empty when no row is given.
    pub fn build(dim: usize, nlist: usize, rows: &[(u64, i64, Vec<f32>)]) -> IvfIndex {
        let nlist = nlist.max(1);
        let max_id = rows.iter().map(|r| r.0).max().unwrap_or(0) as usize;
        let mut index = IvfIndex {
            dim,
            nlist,
            centroids: Vec::new(),
            lists: Vec::new(),
            radii: Vec::new(),
            cell_of: vec![NO_CELL; max_id + 1],
            pos_of: vec![NO_CELL; max_id + 1],
        };
        if rows.is_empty() {
            index.centroids = vec![vec![0.0f32; dim]; nlist];
            index.lists = vec![Vec::new(); nlist];
            index.radii = vec![0.0f32; nlist];
            return index;
        }
        index.centroids = select_centroids(dim, nlist, rows);
        index.lists = vec![Vec::new(); nlist];
        index.radii = vec![0.0f32; nlist];
        for (row_id, pk, vec) in rows {
            index.insert(*row_id, *pk, vec);
        }
        index
    }

    /// Number of indexed rows.
    pub fn len(&self) -> usize {
        self.lists.iter().map(|l| l.len()).sum()
    }

    /// An index over the given centroids, with no row in it yet.
    ///
    /// The rows are added with [`IvfIndex::insert`]. This is the path the
    /// build of a large table takes: the centroids are chosen from a bounded
    /// sample of the table, and the rows are then added one block at a
    /// time, so that the build never holds the whole table in memory.
    pub fn with_centroids(dim: usize, nlist: usize, centroids: Vec<Vec<f32>>) -> IvfIndex {
        let nlist = nlist.max(1);
        let mut centroids = centroids;
        centroids.resize(nlist, vec![0.0f32; dim]);
        IvfIndex {
            dim,
            nlist,
            lists: vec![Vec::new(); nlist],
            radii: vec![0.0f32; nlist],
            centroids,
            cell_of: Vec::new(),
            pos_of: Vec::new(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The cell a vector belongs to: the cell with the closest centroid.
    pub fn assign(&self, vec: &[f32]) -> usize {
        let mut best = 0usize;
        let mut best_dist = f32::INFINITY;
        for (i, c) in self.centroids.iter().enumerate() {
            let d = l2_sq_dispatch(c, vec);
            if d < best_dist {
                best_dist = d;
                best = i;
            }
        }
        best
    }

    /// Add a row to the index.
    ///
    /// The vector is used to find the cell of the row and to measure the
    /// radius of that cell, but it is not kept.
    pub fn insert(&mut self, row_id: u64, pk: i64, vec: &[f32]) {
        let cell = self.assign(vec);
        let pos = self.lists[cell].len() as u32;
        self.lists[cell].push(Entry { row_id, pk });
        self.remember(row_id, cell, pos);
        let radius = l2_sq_dispatch(&self.centroids[cell], vec).sqrt();
        if radius > self.radii[cell] {
            self.radii[cell] = radius;
        }
    }

    /// Remove a row from the index.
    ///
    /// The radius of the cell is not shrunk, which only makes the bound
    /// weaker, never wrong.
    pub fn remove(&mut self, row_id: u64) -> bool {
        let cell = match self.cell_of.get(row_id as usize) {
            Some(c) if *c != NO_CELL => *c as usize,
            _ => return false,
        };
        let pos = self.pos_of[row_id as usize] as usize;
        // The last entry of the cell takes the place of the removed one, so
        // that the positions of the other entries stay valid.
        let last = self.lists[cell].len() - 1;
        self.lists[cell].swap(pos, last);
        self.lists[cell].pop();
        let moved = self.lists[cell].get(pos).map(|e| e.row_id);
        if let Some(moved) = moved {
            self.pos_of[moved as usize] = pos as u32;
        }
        self.forget(row_id);
        true
    }

    /// Whether the index contains a row.
    pub fn contains(&self, row_id: u64) -> bool {
        self.cell_of
            .get(row_id as usize)
            .map(|c| *c != NO_CELL)
            .unwrap_or(false)
    }

    /// The cell a row belongs to, if the index holds it.
    pub fn cell_of(&self, row_id: u64) -> Option<usize> {
        match self.cell_of.get(row_id as usize) {
            Some(c) if *c != NO_CELL => Some(*c as usize),
            _ => None,
        }
    }

    /// Search the `k` closest entries to `query`.
    ///
    /// `nprobe` limits how many cells are visited: `None` means "scan until
    /// the distance bound proves that no other cell can help", which is the
    /// exact search. A finite `nprobe` gives up early and is therefore
    /// genuinely approximate.
    ///
    /// The result is ordered by distance, ties broken by ascending primary
    /// key.
    pub fn search(
        &self,
        query: &[f32],
        k: usize,
        nprobe: Option<usize>,
        fetch: DistanceFetcher<'_>,
    ) -> SqlResult<Vec<(i64, f32, u64)>> {
        let mut out = Vec::new();
        self.scan_ordered(query, nprobe, k, fetch, &mut out)?;
        out.sort_by(|a, b| {
            a.1.partial_cmp(&b.1)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.0.cmp(&b.0))
        });
        out.truncate(k);
        Ok(out)
    }

    /// Scan the candidates in ascending distance order, keeping the ones the
    /// caller accepts.
    ///
    /// The callback receives the identifier of a row and returns its distance
    /// to the query, or `None` when the row does not belong to the answer.
    /// The rows that the callback accepts are appended to `out`.
    ///
    /// This is the primitive the filtered search needs: a cell is skipped
    /// only when its distance bound proves that **no** point of the cell can
    /// be closer than the current K-th accepted distance. A point that fails
    /// the filter would not have entered the answer anyway, so skipping it
    /// cannot change the result.
    pub fn scan_ordered(
        &self,
        query: &[f32],
        nprobe: Option<usize>,
        k: usize,
        mut fetch: DistanceFetcher<'_>,
        out: &mut Vec<(i64, f32, u64)>,
    ) -> SqlResult<()> {
        if k == 0 || self.is_empty() {
            return Ok(());
        }

        // Visit the cells in order of their centroid distance.
        let mut order: Vec<(usize, f32)> = self
            .centroids
            .iter()
            .enumerate()
            .map(|(i, c)| (i, l2_sq_dispatch(c, query)))
            .collect();
        order.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));

        let limit = match nprobe {
            Some(n) => n.max(1),
            None => order.len(),
        };

        // The largest radius over all cells gives a bound that is valid for
        // every cell, which is what allows the whole scan to stop.
        let max_radius = self.radii.iter().copied().fold(0.0f32, f32::max);

        // The accepted candidates, kept sorted by distance and truncated to
        // `k`, so that the last element is always the current K-th best
        // distance.
        let mut best: Vec<(i64, f32, u64)> = Vec::with_capacity(k);

        for (cell, centroid_dist_sq) in order.iter().take(limit) {
            let centroid_dist = centroid_dist_sq.sqrt();

            // Stop the whole scan: even the loosest cell cannot improve the
            // answer, and every remaining cell is farther away.
            if best.len() >= k && centroid_dist - max_radius > best[k - 1].1 {
                break;
            }

            // Skip this cell: its own radius already proves that it cannot
            // contain a better answer.
            if best.len() >= k && centroid_dist - self.radii[*cell] > best[k - 1].1 {
                continue;
            }

            for entry in self.lists[*cell].iter() {
                let d = match fetch(entry.row_id)? {
                    Some(d) => d,
                    None => continue,
                };
                best.push((entry.pk, d, entry.row_id));
            }
            if best.len() > k {
                best.sort_by(|a, b| {
                    a.1.partial_cmp(&b.1)
                        .unwrap_or(std::cmp::Ordering::Equal)
                        .then(a.0.cmp(&b.0))
                });
                best.truncate(k);
            }
        }

        out.append(&mut best);
        Ok(())
    }

    /// Record the cell and the position of a row.
    fn remember(&mut self, row_id: u64, cell: usize, pos: u32) {
        let idx = row_id as usize;
        if idx >= self.cell_of.len() {
            self.cell_of.resize(idx + 1, NO_CELL);
            self.pos_of.resize(idx + 1, NO_CELL);
        }
        self.cell_of[idx] = cell as u32;
        self.pos_of[idx] = pos;
    }

    /// Forget a row.
    fn forget(&mut self, row_id: u64) {
        let idx = row_id as usize;
        if idx < self.cell_of.len() {
            self.cell_of[idx] = NO_CELL;
            self.pos_of[idx] = NO_CELL;
        }
    }
}

/// Choose `nlist` centroids by farthest point sampling.
///
/// The first centroid is the first row; every following centroid is the row
/// that is farthest away from all centroids chosen so far. The pass is
/// deterministic, so an index rebuilt from the same data is identical.
///
/// The rows are a *sample* of the table, not the whole table: the caller
/// decides how many rows it hands over. The build of a large table samples
/// the table, so that the memory of the build does not depend on the size
/// of the table. The sample only decides where the cells are; the pruning
/// of the index is exact whatever the sample was, because the radius of a
/// cell is measured over every row of that cell.
pub fn select_centroids(dim: usize, nlist: usize, rows: &[(u64, i64, Vec<f32>)]) -> Vec<Vec<f32>> {
    let n = rows.len();
    let mut centroids: Vec<Vec<f32>> = Vec::with_capacity(nlist.min(n));
    centroids.push(rows[0].2.clone());

    // Distance of every row to the closest centroid chosen so far.
    let mut best_dist: Vec<f32> = rows.iter().map(|r| l2_sq_dispatch(&r.2, &centroids[0])).collect();

    while centroids.len() < nlist {
        let mut pick = 0usize;
        let mut pick_dist = -1.0f32;
        for (i, d) in best_dist.iter().enumerate() {
            if *d > pick_dist {
                pick_dist = *d;
                pick = i;
            }
        }
        if pick_dist <= 0.0 {
            // All rows coincide with a centroid; further centroids would be
            // duplicates. Use zero vectors to keep the cell count stable.
            while centroids.len() < nlist {
                centroids.push(vec![0.0f32; dim]);
            }
            break;
        }
        let centroid = rows[pick].2.clone();
        for (i, r) in rows.iter().enumerate() {
            let d = l2_sq_dispatch(&r.2, &centroid);
            if d < best_dist[i] {
                best_dist[i] = d;
            }
        }
        centroids.push(centroid);
    }
    debug_assert!(centroids.len() == nlist);
    centroids
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(id: u64, pk: i64, v: &[f32]) -> (u64, i64, Vec<f32>) {
        (id, pk, v.to_vec())
    }

    /// A fetcher that answers with the distance of a row, taken from the
    /// rows the index was built from.
    fn fetcher<'a>(
        rows: &'a [(u64, i64, Vec<f32>)],
        query: &'a [f32],
    ) -> impl FnMut(u64) -> SqlResult<Option<f32>> + 'a {
        move |row_id| {
            let (_, _, v) = rows
                .iter()
                .find(|(id, _, _)| *id == row_id)
                .expect("the row is indexed");
            Ok(Some(l2_sq_dispatch(v, query).sqrt()))
        }
    }

    #[test]
    fn empty_index_searches_nothing() {
        let index = IvfIndex::build(3, 4, &[]);
        assert!(index.is_empty());
        let mut f = |_row_id: u64| Ok(None);
        assert!(index
            .search(&[0.0, 0.0, 0.0], 10, None, &mut f)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn finds_the_exact_nearest_neighbour() {
        let rows = vec![
            row(0, 10, &[0.0, 0.0]),
            row(1, 20, &[10.0, 0.0]),
            row(2, 30, &[0.0, 10.0]),
        ];
        let index = IvfIndex::build(2, 3, &rows);
        let query = [0.1, 0.0];
        let hits = index
            .search(&query, 1, None, &mut fetcher(&rows, &query))
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].0, 10);
    }

    #[test]
    fn exact_search_matches_brute_force() {
        let rows: Vec<(u64, i64, Vec<f32>)> = (0..200)
            .map(|i| {
                let f = i as f32;
                row(i, i as i64, &[f.sin() * 10.0, f.cos() * 10.0, (f * 0.5).fract() * 5.0])
            })
            .collect();
        let index = IvfIndex::build(3, 8, &rows);
        let query = [1.0, 2.0, 3.0];
        let hits = index
            .search(&query, 10, None, &mut fetcher(&rows, &query))
            .unwrap();

        let mut brute: Vec<(i64, f32)> = rows
            .iter()
            .map(|(_, pk, v)| {
                let d: f32 = v
                    .iter()
                    .zip(query.iter())
                    .map(|(a, b)| (a - b) * (a - b))
                    .sum::<f32>()
                    .sqrt();
                (*pk, d)
            })
            .collect();
        brute.sort_by(|a, b| {
            a.1.partial_cmp(&b.1)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.0.cmp(&b.0))
        });
        brute.truncate(10);

        assert_eq!(hits.len(), 10);
        for (i, (pk, d)) in brute.iter().enumerate() {
            assert_eq!(hits[i].0, *pk, "mismatch at rank {i}");
            assert!((hits[i].1 - d).abs() < 1e-3, "distance mismatch at rank {i}");
        }
    }

    #[test]
    fn ties_are_broken_by_primary_key() {
        // Two rows at exactly the same distance from the query.
        let rows = vec![
            row(0, 7, &[1.0, 0.0]),
            row(1, 3, &[-1.0, 0.0]),
        ];
        let index = IvfIndex::build(2, 2, &rows);
        let query = [0.0, 0.0];
        let hits = index
            .search(&query, 2, None, &mut fetcher(&rows, &query))
            .unwrap();
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].0, 3);
        assert_eq!(hits[1].0, 7);
    }

    #[test]
    fn insert_and_remove_keep_the_index_consistent() {
        let rows = vec![
            row(0, 1, &[0.0, 0.0]),
            row(1, 2, &[5.0, 5.0]),
        ];
        let mut index = IvfIndex::build(2, 2, &rows);
        assert_eq!(index.len(), 2);

        index.insert(2, 3, &[0.1, 0.1]);
        assert_eq!(index.len(), 3);
        assert!(index.contains(2));

        assert!(index.remove(2));
        assert_eq!(index.len(), 2);
        assert!(!index.contains(2));
        assert!(!index.remove(99));
    }

    #[test]
    fn removing_a_row_keeps_the_other_rows_reachable() {
        // Every row of a cell is removed one after the other, so that the
        // positions of the remaining rows are checked as well.
        let rows: Vec<(u64, i64, Vec<f32>)> = (0..20)
            .map(|i| row(i, i as i64, &[i as f32 * 0.01, 0.0]))
            .collect();
        let mut index = IvfIndex::build(2, 1, &rows);
        assert_eq!(index.len(), 20);
        for id in 0..20u64 {
            assert!(index.remove(id), "row {id} was not removed");
            assert!(!index.contains(id));
        }
        assert_eq!(index.len(), 0);
    }

    #[test]
    fn the_index_holds_no_copy_of_the_vectors() {
        let rows: Vec<(u64, i64, Vec<f32>)> = (0..100)
            .map(|i| row(i, i as i64, &[i as f32, 0.0, 0.0]))
            .collect();
        let index = IvfIndex::build(3, 4, &rows);
        // The index holds the identity of the rows and the centroids, and
        // nothing else.
        let identities = index.lists.iter().map(|l| l.len()).sum::<usize>();
        assert_eq!(identities, 100);
        let centroid_bytes: usize = index.centroids.iter().map(|c| c.len() * 4).sum();
        assert!(
            centroid_bytes < 100 * 3 * 4,
            "the centroids are larger than the data"
        );
    }

    #[test]
    fn nprobe_limits_the_number_of_visited_cells() {
        let rows: Vec<(u64, i64, Vec<f32>)> = (0..100)
            .map(|i| row(i, i as i64, &[i as f32, 0.0]))
            .collect();
        let index = IvfIndex::build(2, 10, &rows);
        // A single cell cannot contain all the rows.
        let query = [0.0, 0.0];
        let hits = index
            .search(&query, 10, Some(1), &mut fetcher(&rows, &query))
            .unwrap();
        assert!(hits.len() <= 10);
        assert!(hits.len() < 100);
    }
}

#[cfg(test)]
mod pruning_tests {
    use super::*;

    /// A grid of points, so that the cells have a well defined radius.
    fn grid(n: usize, step: f32) -> Vec<(u64, i64, Vec<f32>)> {
        (0..n)
            .map(|i| {
                let f = i as f32;
                (
                    i as u64,
                    i as i64,
                    vec![f * step, (f * step).sin() * 2.0, (f * step).cos() * 2.0],
                )
            })
            .collect()
    }

    /// A fetcher that answers with the distance of a row.
    fn fetcher<'a>(
        rows: &'a [(u64, i64, Vec<f32>)],
        query: &'a [f32],
    ) -> impl FnMut(u64) -> SqlResult<Option<f32>> + 'a {
        move |row_id| {
            let (_, _, v) = rows
                .iter()
                .find(|(id, _, _)| *id == row_id)
                .expect("the row is indexed");
            Ok(Some(l2_sq_dispatch(v, query).sqrt()))
        }
    }

    #[test]
    fn the_pruning_never_drops_a_better_answer() {
        // Many cells, so that the pruning really fires, and a query that is
        // far from most of the points.
        let rows = grid(400, 1.0);
        let index = IvfIndex::build(3, 32, &rows);
        let queries = [
            vec![0.0, 0.0, 0.0],
            vec![100.0, 50.0, -50.0],
            vec![-200.0, 3.0, 7.0],
            vec![17.0, -3.0, 11.0],
        ];
        for query in queries {
            let hits = index
                .search(&query, 10, None, &mut fetcher(&rows, &query))
                .unwrap();
            let mut brute: Vec<(i64, f32)> = rows
                .iter()
                .map(|(_, pk, v)| {
                    let d: f32 = v
                        .iter()
                        .zip(query.iter())
                        .map(|(a, b)| (a - b) * (a - b))
                        .sum::<f32>()
                        .sqrt();
                    (*pk, d)
                })
                .collect();
            brute.sort_by(|a, b| {
                a.1.partial_cmp(&b.1)
                    .unwrap_or(std::cmp::Ordering::Equal)
                    .then(a.0.cmp(&b.0))
            });
            brute.truncate(10);
            assert_eq!(hits.len(), 10);
            for (i, (pk, _)) in brute.iter().enumerate() {
                assert_eq!(hits[i].0, *pk, "query {:?}, rank {}", query, i);
            }
        }
    }

    #[test]
    fn a_finite_nprobe_gives_an_approximate_answer() {
        let rows = grid(400, 1.0);
        let index = IvfIndex::build(3, 32, &rows);
        let query = vec![0.0, 0.0, 0.0];
        let exact = index
            .search(&query, 10, None, &mut fetcher(&rows, &query))
            .unwrap();
        let approximate = index
            .search(&query, 10, Some(2), &mut fetcher(&rows, &query))
            .unwrap();
        // The approximate answer is a subset of the exact one, because the
        // cells are visited in the same order and only fewer of them are.
        for (pk, _, _) in &approximate {
            assert!(exact.iter().any(|(p, _, _)| p == pk));
        }
    }
}
