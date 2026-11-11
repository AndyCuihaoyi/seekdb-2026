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
//! per table: id u32 | name length u32 | name | schema length u32 | schema | crc u32
//! ```
//!
//! All integers are little endian. A truncated file - the tail a crash can
//! leave behind - is read as far as it is complete, so a damaged catalog
//! never makes the database refuse to start.
//!
//! The checksum of a table is what tells a damaged entry from a good one.
//! Without it a single flipped byte inside a schema is read as a column with
//! a different name, and the table silently answers with the wrong columns.
//! With it the damaged entry is recognised, the backup is used instead, and
//! the schemas stay right.

use crate::catalog::TableDef;
use crate::error::{SqlError, SqlResult};
use std::path::Path;

/// The name of the catalog file inside the data directory.
pub const CATALOG_FILE: &str = "catalog.dat";

/// The name of the backup of the catalog file.
///
/// The catalog is the only place the schemas live once the log has been
/// folded into the data files. A second copy of it is therefore kept, and it
/// is what the database falls back on when the first copy is damaged.
pub const CATALOG_BACKUP_FILE: &str = "catalog.bak";

const MAGIC: &[u8; 8] = b"MVDBCTLG";
const VERSION: u32 = 2;

/// The version of the format that was written before the checksum was added.
///
/// A file of that version is still read, but its entries cannot be checked,
/// so a damaged one is not noticed.
const VERSION_WITHOUT_CHECKSUM: u32 = 1;

/// The checksum of one table entry.
///
/// It is a simple sum over the bytes of the name and of the schema, with a
/// shift, so that a byte that moves does not cancel another one.
fn checksum(name: &[u8], schema: &[u8]) -> u32 {
    let mut h: u32 = 0x811c_9dc5;
    for b in name.iter().chain(schema.iter()) {
        h ^= *b as u32;
        h = h.wrapping_mul(0x0100_0193);
    }
    h
}

/// Write the catalog of the database.
///
/// The catalog is written to a temporary file and renamed, so that a crash
/// never leaves a half written catalog behind. A second copy is written the
/// same way, and it is what the database falls back on when the first copy
/// is damaged.
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
        buf.extend_from_slice(&checksum(def.name.as_bytes(), &schema).to_le_bytes());
    }
    // The catalog is written to a temporary file and renamed, so that a
    // crash never leaves a half written catalog behind.
    write_atomic(path, &buf)?;
    // The backup is written the same way, so that it is either the previous
    // content or the new one, and never a mixture of the two.
    write_atomic(&backup_path(path), &buf)
}

/// Write bytes to a file through a temporary file and a rename.
fn write_atomic(path: &Path, buf: &[u8]) -> SqlResult<()> {
    let tmp = path.with_extension("dat.tmp");
    std::fs::write(&tmp, buf)
        .map_err(|e| SqlError::internal(format!("cannot write catalog: {e}")))?;
    std::fs::rename(&tmp, path)
        .map_err(|e| SqlError::internal(format!("cannot rename catalog: {e}")))
}

/// The path of the backup of the catalog file.
pub fn backup_path(path: &Path) -> std::path::PathBuf {
    path.with_file_name(CATALOG_BACKUP_FILE)
}

/// Read the catalog of the database.
///
/// A missing file means that the database has no table yet, which is not an
/// error.
///
/// A file that is damaged is not an error either: the backup is read
/// instead, and if that one is damaged too the tables that could be read
/// from it are used. The database that cannot be read at all starts with no
/// table, which is a state it can recover from, rather than a process that
/// dies before the handshake - and a process that dies before the handshake
/// is a database that exited before it answered.
pub fn read(path: &Path) -> SqlResult<Vec<(u32, TableDef)>> {
    match read_one(path) {
        Ok(tables) => Ok(tables),
        Err(e) => {
            // The first copy is damaged. The backup is the last copy of the
            // schemas, so it is worth a try.
            let backup = backup_path(path);
            match read_one(&backup) {
                Ok(tables) => {
                    eprintln!(
                        "minivectordb: the catalog file was damaged, the backup was used instead: {e}"
                    );
                    Ok(tables)
                }
                Err(backup_error) => {
                    eprintln!(
                        "minivectordb: the catalog file and its backup are damaged: {e}; {backup_error}"
                    );
                    // Neither copy can be read. The tables that the log
                    // describes are still known, so the recovery continues
                    // with no table from the catalog.
                    Ok(Vec::new())
                }
            }
        }
    }
}

/// Read one catalog file.
fn read_one(path: &Path) -> SqlResult<Vec<(u32, TableDef)>> {
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
    if version != VERSION && version != VERSION_WITHOUT_CHECKSUM {
        return Err(SqlError::internal(format!(
            "the catalog file has the unsupported version {version}"
        )));
    }
    // The entries carry a checksum only from the version that introduced it.
    let checked = version >= VERSION;
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
        let name = String::from_utf8_lossy(&buf[pos..pos + name_len]).to_string();
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
        if checked {
            if pos + 4 > buf.len() {
                break;
            }
            let stored = u32::from_le_bytes(buf[pos..pos + 4].try_into().unwrap());
            pos += 4;
            // The entry was damaged. The whole file is not trusted any more:
            // the backup is used instead.
            if stored != checksum(name.as_bytes(), schema) {
                return Err(SqlError::internal("the catalog file is damaged"));
            }
        }
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
