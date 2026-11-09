//! Lifecycle management of the vector indexes.
//!
//! The competition allows a vector index to be rebuilt from the persisted
//! records ("the vector index may be rebuilt from the persisted records"), so
//! the index itself is not written to disk. What is persisted is the *fact*
//! that a table has a vector index, in a small marker file next to the table
//! data. When the database starts, every marked table gets its index rebuilt
//! before the server reports readiness.
//!
//! Keeping the index in memory only has two consequences worth naming:
//!
//! * the rebuild cost is paid at every start, which is charged to the
//!   recovery time and the resource limits;
//! * the index holds a copy of every vector, so it consumes memory
//!   proportional to the data set. The exact search path does not use it,
//!   so a deployment that never switches to approximate mode never pays for
//!   it either - and an index that was never created costs nothing.

use crate::error::{SqlError, SqlResult};
use crate::storage::table::{ColValue, TableStore};
use crate::vector::ann::IvfIndex;
use std::path::{Path, PathBuf};

/// Marker file that records the existence of a vector index.
const INDEX_MARKER: &str = "index.dat";

/// Read the rows of a table together with their vectors.
///
/// The rows are read in storage order, so the index built from them is
/// deterministic.
pub fn collect_indexable_rows(
    store: &mut TableStore,
    pk_index: usize,
    vector_index: usize,
) -> SqlResult<Vec<(u64, i64, Vec<f32>)>> {
    let mut out = Vec::with_capacity(store.row_count as usize);
    for row_id in 0..store.row_count {
        let meta = match store.read_meta(row_id)? {
            Some(m) => m,
            None => continue,
        };
        if meta.alive == 0 {
            continue;
        }
        let image = store.read_columns(meta.col_offset, meta.col_len)?;
        let cols = crate::storage::format::decode_row(&image)?;
        let pk = match cols.get(pk_index) {
            Some(ColValue::BigInt(i)) => *i,
            _ => meta.pk,
        };
        let vec = match cols.get(vector_index) {
            Some(ColValue::Vector(v)) => v.as_slice().to_vec(),
            _ => continue,
        };
        out.push((row_id, pk, vec));
    }
    Ok(out)
}

/// Build an index over a table.
pub fn build_index(
    dim: usize,
    nlist: usize,
    store: &mut TableStore,
    pk_index: usize,
    vector_index: usize,
) -> SqlResult<IvfIndex> {
    let rows = collect_indexable_rows(store, pk_index, vector_index)?;
    Ok(IvfIndex::build(dim, nlist, &rows))
}

/// Path of the marker file of a table.
pub fn marker_path(table_dir: &Path) -> PathBuf {
    table_dir.join(INDEX_MARKER)
}

/// Whether a table directory records the existence of a vector index.
pub fn has_marker(table_dir: &Path) -> bool {
    marker_path(table_dir).exists()
}

/// Record the existence of a vector index for a table.
pub fn write_marker(table_dir: &Path) -> SqlResult<()> {
    std::fs::create_dir_all(table_dir)
        .map_err(|e| SqlError::internal(format!("cannot create table dir: {e}")))?;
    std::fs::write(marker_path(table_dir), b"vector-index v1\n")
        .map_err(|e| SqlError::internal(format!("cannot write index marker: {e}")))
}

/// Remove the marker of a table.
pub fn remove_marker(table_dir: &Path) -> SqlResult<()> {
    let path = marker_path(table_dir);
    if path.exists() {
        std::fs::remove_file(&path)
            .map_err(|e| SqlError::internal(format!("cannot remove index marker: {e}")))?;
    }
    Ok(())
}

/// The vector of a row, if the row has one.
pub fn vector_of(cols: &[ColValue], vector_index: Option<usize>) -> Option<Vec<f32>> {
    let idx = vector_index?;
    match cols.get(idx) {
        Some(ColValue::Vector(v)) => Some(v.as_slice().to_vec()),
        _ => None,
    }
}
