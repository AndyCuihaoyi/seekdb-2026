//! The catalog file: the list of the tables of the database.
//!
//! The write-ahead log is folded into the data files at the end of a
//! recovery, so the log alone cannot describe the tables after a restart.
//! The catalog file is what makes the tables known again, together with the
//! identifiers the log records refer to.
//!
//! The format is deliberately simple and self describing:
//!
//! ```text
//! magic: "MVDBCTLG" | version u32 | count u32
//! per table: id u32 | name length u32 | name | schema length u32 | schema
//! ```
//!
//! All integers are little endian. A truncated file - the tail a crash can
//! leave behind - is read as far as it is complete, so a damaged catalog
//! never makes the database refuse to start.

use crate::catalog::TableDef;
use crate::error::{SqlError, SqlResult};
use std::path::Path;

/// The name of the catalog file inside the data directory.
pub const CATALOG_FILE: &str = "catalog.dat";

const MAGIC: &[u8; 8] = b"MVDBCTLG";
const VERSION: u32 = 1;

/// Write the catalog of the database.
pub fn write(path: &Path, tables: &[(u32, TableDef)]) -> SqlResult<()> {
    let mut buf = Vec::with_capacity(256);
    buf.extend_from_slice(MAGIC);
    buf.extend_from_slice(&VERSION.to_le_bytes());
    buf.extend_from_slice(&(tables.len() as u32).to_le_bytes());
    for (id, def) in tables {
        let schema = crate::engine::encode_schema(def);
        buf.extend_from_slice(&id.to_le_bytes());
        buf.extend_from_slice(&(def.name.len() as u32).to_le_bytes());
        buf.extend_from_slice(def.name.as_bytes());
        buf.extend_from_slice(&(schema.len() as u32).to_le_bytes());
        buf.extend_from_slice(&schema);
    }
    // The catalog is written to a temporary file and renamed, so that a
    // crash never leaves a half written catalog behind.
    let tmp = path.with_extension("dat.tmp");
    std::fs::write(&tmp, &buf)
        .map_err(|e| SqlError::internal(format!("cannot write catalog: {e}")))?;
    std::fs::rename(&tmp, path)
        .map_err(|e| SqlError::internal(format!("cannot rename catalog: {e}")))
}

/// Read the catalog of the database.
///
/// A missing file means that the database has no table yet, which is not an
/// error.
pub fn read(path: &Path) -> SqlResult<Vec<(u32, TableDef)>> {
    let buf = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(SqlError::internal(format!("cannot read catalog: {e}"))),
    };
    let mut out = Vec::new();
    let mut pos = 0usize;
    if buf.len() < 16 || &buf[0..8] != MAGIC {
        return Err(SqlError::internal("the catalog file is not recognised"));
    }
    let version = u32::from_le_bytes(buf[8..12].try_into().unwrap());
    if version != VERSION {
        return Err(SqlError::internal(format!(
            "the catalog file has the unsupported version {version}"
        )));
    }
    let count = u32::from_le_bytes(buf[12..16].try_into().unwrap()) as usize;
    pos = 16;
    for _ in 0..count {
        if pos + 4 > buf.len() {
            break;
        }
        let id = u32::from_le_bytes(buf[pos..pos + 4].try_into().unwrap());
        pos += 4;
        if pos + 4 > buf.len() {
            break;
        }
        let name_len = u32::from_le_bytes(buf[pos..pos + 4].try_into().unwrap()) as usize;
        pos += 4;
        if pos + name_len > buf.len() {
            break;
        }
        let _name = String::from_utf8_lossy(&buf[pos..pos + name_len]).to_string();
        pos += name_len;
        if pos + 4 > buf.len() {
            break;
        }
        let schema_len = u32::from_le_bytes(buf[pos..pos + 4].try_into().unwrap()) as usize;
        pos += 4;
        if pos + schema_len > buf.len() {
            break;
        }
        let schema = &buf[pos..pos + schema_len];
        pos += schema_len;
        let def = crate::engine::decode_schema(schema)?;
        out.push((id, def));
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sql::ast::{ColumnDef, ColumnType};

    fn sample() -> TableDef {
        TableDef {
            name: "t".to_string(),
            columns: vec![
                ColumnDef {
                    name: "id".to_string(),
                    ty: ColumnType::BigInt,
                    primary_key: true,
                },
                ColumnDef {
                    name: "v".to_string(),
                    ty: ColumnType::Vector(4),
                    primary_key: false,
                },
            ],
            vector_column: Some(1),
            pk_index: 0,
        }
    }

    #[test]
    fn round_trips_the_catalog() {
        let dir = std::env::temp_dir().join(format!("mvdb-catalog-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(CATALOG_FILE);
        write(&path, &[(7, sample())]).unwrap();
        let read_back = read(&path).unwrap();
        assert_eq!(read_back.len(), 1);
        assert_eq!(read_back[0].0, 7);
        assert_eq!(read_back[0].1.name, "t");
        assert_eq!(read_back[0].1.vector_dim(), Some(4));
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn a_missing_catalog_is_empty() {
        let dir = std::env::temp_dir().join(format!("mvdb-catalog-missing-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let read_back = read(&dir.join(CATALOG_FILE)).unwrap();
        assert!(read_back.is_empty());
        std::fs::remove_dir_all(&dir).ok();
    }
}
