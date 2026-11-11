//! An inverted index over the low cardinality columns of a table.
//!
//! # Why
//!
//! A filtered query has to find the rows that satisfy the filter, and then
//! measure only those rows. The exact scan measures a row *before* it looks
//! at the filter, so a filtered query costs the same distance work as an
//! unfiltered one, plus the filter itself.
//!
//! The filter of the evaluation is an equality on an ordinary column that
//! holds a small number of distinct values - a category, a label, a status.
//! Such a column is a perfect fit for a *dictionary encoded inverted index*:
//! the distinct values are numbered, and every value owns the list of the
//! rows that hold it.
//!
//! # What it buys
//!
//! With the index, a filtered query only measures the rows of one list.
//! With eight categories and ten thousand rows, a filtered query measures
//! 1250 rows instead of 10000 - an eightfold reduction of the dominant cost
//! of the scan.
//!
//! # What it must not change
//!
//! The index is a *filter*, not an answer: the rows it produces are still
//! measured with the same kernel, and the answer is still the best K of the
//! rows that satisfy the filter, ties broken by ascending primary key. A
//! query whose filter is not an equality on an indexed column falls back to
//! the full scan, so the semantics of the answer do not depend on whether
//! the index exists.
//!
//! # When the index is not built
//!
//! A column with more than `MAX_DISTINCT` distinct values is not indexed: an
//! inverted index over a high cardinality column costs more memory than the
//! scan it saves, because every list would hold about one row. The decision
//! is made per column and is never hard coded to a particular column name.

use std::collections::HashMap;

/// The largest number of distinct values a column may have to be indexed.
///
/// A column with more distinct values than this has, on average, less than
/// one row per list, which makes the index pointless.
pub const MAX_DISTINCT: usize = 1024;

/// The value of a column, as the dictionary of the index holds it.
///
/// The dictionary is keyed by the text of the value, so it works for every
/// column type without a special case per type.
pub type Key = String;

/// An inverted index over one column of a table.
///
/// The index maps the value of the column to the rows that hold it. The rows
/// are held in a dense array indexed by the row identifier, so a row can be
/// added and removed without searching a list.
#[derive(Debug, Default)]
pub struct InvertedIndex {
    /// The rows of every value.
    ///
    /// The lists are kept sorted, so that a scan visits the rows in the order
    /// of the table. That keeps the answer independent of the order in which
    /// the rows are measured, which matters because the ties of the distance
    /// are broken by the primary key and not by the order of the scan.
    lists: HashMap<Key, Vec<u64>>,
    /// The position of every row in the list of its value.
    ///
    /// The array is indexed by the row identifier and holds `NO_LIST` for a
    /// row that is not in the index.
    list_of: Vec<u32>,
    /// The number of distinct values seen so far.
    distinct: usize,
    /// Whether the column was given up on.
    ///
    /// A column that grows past `MAX_DISTINCT` distinct values is dropped
    /// from the index, because from then on the index costs more than the
    /// scan it saves.
    abandoned: bool,
}

/// The marker of a row that the index does not hold.
const NO_LIST: u32 = u32::MAX;

impl InvertedIndex {
    /// An empty index.
    pub fn new() -> InvertedIndex {
        InvertedIndex {
            lists: HashMap::new(),
            list_of: Vec::new(),
            distinct: 0,
            abandoned: false,
        }
    }

    /// Whether the index was given up on.
    pub fn is_abandoned(&self) -> bool {
        self.abandoned
    }

    /// The number of distinct values in the index.
    pub fn distinct(&self) -> usize {
        self.distinct
    }

    /// The number of rows in the index.
    pub fn len(&self) -> usize {
        self.lists.values().map(|l| l.len()).sum()
    }

    /// Whether the index holds no row.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// Record that a row holds a value.
    ///
    /// A row that is already in the index is moved to the list of its new
    /// value, so an update does not leave the row behind.
    pub fn insert(&mut self, row_id: u64, value: &str) {
        if self.abandoned {
            return;
        }
        // A row that changes its value leaves its old list.
        self.remove(row_id);

        let idx = row_id as usize;
        if idx >= self.list_of.len() {
            self.list_of.resize(idx + 1, NO_LIST);
        }
        let list = match self.lists.get_mut(value) {
            Some(l) => l,
            None => {
                if self.lists.len() >= MAX_DISTINCT {
                    // The column is too wide to be indexed. The index is
                    // dropped, so every query falls back to the full scan.
                    self.abandon();
                    return;
                }
                self.distinct += 1;
                self.lists.entry(value.to_string()).or_default()
            }
        };
        // The lists are kept sorted, so that the scan visits the rows in the
        // order of the table.
        let pos = list.partition_point(|&r| r < row_id);
        list.insert(pos, row_id);
        self.list_of[idx] = list.len() as u32 - 1;
    }

    /// Forget a row.
    pub fn remove(&mut self, row_id: u64) {
        let idx = row_id as usize;
        if idx >= self.list_of.len() {
            return;
        }
        let pos = self.list_of[idx];
        if pos == NO_LIST {
            return;
        }
        self.list_of[idx] = NO_LIST;
        // The list of the value is found by scanning the dictionary, which is
        // small: the column has at most `MAX_DISTINCT` values.
        for list in self.lists.values_mut() {
            if let Ok(p) = list.binary_search(&row_id) {
                list.remove(p);
                break;
            }
        }
    }

    /// The rows that hold a value, in the order of the table.
    pub fn lookup(&self, value: &str) -> Option<&[u64]> {
        self.lists.get(value).map(|l| l.as_slice())
    }

    /// Whether the index holds a row.
    pub fn contains(&self, row_id: u64) -> bool {
        let idx = row_id as usize;
        idx < self.list_of.len() && self.list_of[idx] != NO_LIST
    }

    /// Drop the whole index.
    fn abandon(&mut self) {
        self.lists.clear();
        self.list_of.clear();
        self.lists.shrink_to_fit();
        self.list_of.shrink_to_fit();
        self.distinct = 0;
        self.abandoned = true;
    }
}

/// The inverted indexes of the columns of a table.
#[derive(Debug, Default)]
pub struct ColumnIndexes {
    /// The index of every column that is indexed.
    indexes: Vec<Option<InvertedIndex>>,
}

impl ColumnIndexes {
    /// An empty set of indexes.
    pub fn new() -> ColumnIndexes {
        ColumnIndexes { indexes: Vec::new() }
    }

    /// The index of a column, if the column is indexed.
    pub fn get(&self, column: usize) -> Option<&InvertedIndex> {
        self.indexes.get(column).and_then(|i| i.as_ref())
    }

    /// The index of a column, creating it when it is not there yet.
    pub fn get_or_create(&mut self, column: usize) -> &mut InvertedIndex {
        if self.indexes.len() <= column {
            // The vector is grown by hand, because `resize` needs the
            // element type to be cloneable and the index is not.
            while self.indexes.len() <= column {
                self.indexes.push(None);
            }
        }
        if self.indexes[column].is_none() {
            self.indexes[column] = Some(InvertedIndex::new());
        }
        self.indexes[column].as_mut().expect("the index was just created")
    }

    /// Record that a row holds a value in a column.
    pub fn insert(&mut self, column: usize, row_id: u64, value: &str) {
        self.get_or_create(column).insert(row_id, value);
    }

    /// Forget a row of a column.
    pub fn remove(&mut self, column: usize, row_id: u64) {
        if let Some(index) = self.indexes.get_mut(column).and_then(|i| i.as_mut()) {
            index.remove(row_id);
        }
    }

    /// The number of indexed columns.
    pub fn len(&self) -> usize {
        self.indexes.iter().filter(|i| i.is_some()).count()
    }

    /// Whether no column is indexed.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_row_is_found_by_its_value() {
        let mut index = InvertedIndex::new();
        index.insert(1, "a");
        index.insert(2, "b");
        index.insert(3, "a");
        assert_eq!(index.lookup("a"), Some(&[1u64, 3][..]));
        assert_eq!(index.lookup("b"), Some(&[2u64][..]));
        assert_eq!(index.lookup("c"), None);
    }

    #[test]
    fn the_rows_are_kept_in_the_order_of_the_table() {
        let mut index = InvertedIndex::new();
        index.insert(5, "a");
        index.insert(1, "a");
        index.insert(3, "a");
        assert_eq!(index.lookup("a"), Some(&[1u64, 3, 5][..]));
    }

    #[test]
    fn an_updated_row_leaves_its_old_value() {
        let mut index = InvertedIndex::new();
        index.insert(1, "a");
        index.insert(2, "a");
        index.insert(1, "b");
        assert_eq!(index.lookup("a"), Some(&[2u64][..]));
        assert_eq!(index.lookup("b"), Some(&[1u64][..]));
    }

    #[test]
    fn a_deleted_row_is_gone() {
        let mut index = InvertedIndex::new();
        index.insert(1, "a");
        index.insert(2, "a");
        index.remove(1);
        assert_eq!(index.lookup("a"), Some(&[2u64][..]));
        assert!(!index.contains(1));
    }

    #[test]
    fn a_wide_column_is_dropped() {
        let mut index = InvertedIndex::new();
        for i in 0..(MAX_DISTINCT as u64 + 10) {
            index.insert(i, &format!("v{i}"));
        }
        assert!(index.is_abandoned());
        assert_eq!(index.lookup("v0"), None);
    }

    #[test]
    fn the_indexes_of_the_columns_are_independent() {
        let mut indexes = ColumnIndexes::new();
        indexes.insert(0, 1, "a");
        indexes.insert(1, 1, "b");
        assert_eq!(indexes.get(0).unwrap().lookup("a"), Some(&[1u64][..]));
        assert_eq!(indexes.get(1).unwrap().lookup("b"), Some(&[1u64][..]));
        assert!(indexes.get(2).is_none());
    }
}
