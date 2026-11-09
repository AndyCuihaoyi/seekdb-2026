//! Row image encoding.
//!
//! A row image is a compact byte string holding the non vector column values.
//! It is what the WAL stores and what `col.dat` holds.
//!
//! Layout per value:
//!
//! * `BIGINT`   : tag `0x01` + 8 bytes little endian
//! * `VARCHAR`  : tag `0x02` + 4 bytes length + UTF-8 bytes
//! * `VECTOR`   : tag `0x03` + 4 bytes dimension + `dim * 4` bytes of f32
//!
//! The vector is kept in the image so that the WAL entry is self contained
//! and recovery can rebuild the vector file without depending on the order in
//! which data files were flushed.

use crate::error::{SqlError, SqlResult};
use crate::storage::table::ColValue;
use crate::value::vector::Vector;

const TAG_BIGINT: u8 = 0x01;
const TAG_TEXT: u8 = 0x02;
const TAG_VECTOR: u8 = 0x03;

/// Encode one value.
pub fn encode_value(out: &mut Vec<u8>, v: &ColValue) {
    match v {
        ColValue::BigInt(i) => {
            out.push(TAG_BIGINT);
            out.extend_from_slice(&i.to_le_bytes());
        }
        ColValue::Text(s) => {
            out.push(TAG_TEXT);
            out.extend_from_slice(&(s.len() as u32).to_le_bytes());
            out.extend_from_slice(s.as_bytes());
        }
        ColValue::Vector(v) => {
            out.push(TAG_VECTOR);
            out.extend_from_slice(&(v.dim as u32).to_le_bytes());
            for x in v.as_slice() {
                out.extend_from_slice(&x.to_le_bytes());
            }
        }
    }
}

/// Decode one value, returning it and the number of bytes consumed.
pub fn decode_value(buf: &[u8]) -> SqlResult<(ColValue, usize)> {
    if buf.is_empty() {
        return Err(SqlError::internal("truncated row image"));
    }
    match buf[0] {
        TAG_BIGINT => {
            if buf.len() < 9 {
                return Err(SqlError::internal("truncated BIGINT value"));
            }
            let i = i64::from_le_bytes(buf[1..9].try_into().unwrap());
            Ok((ColValue::BigInt(i), 9))
        }
        TAG_TEXT => {
            if buf.len() < 5 {
                return Err(SqlError::internal("truncated VARCHAR value"));
            }
            let len = u32::from_le_bytes(buf[1..5].try_into().unwrap()) as usize;
            if buf.len() < 5 + len {
                return Err(SqlError::internal("truncated VARCHAR payload"));
            }
            let s = std::str::from_utf8(&buf[5..5 + len])
                .map_err(|_| SqlError::internal("invalid UTF-8 in stored VARCHAR"))?
                .to_string();
            Ok((ColValue::Text(s), 5 + len))
        }
        TAG_VECTOR => {
            if buf.len() < 5 {
                return Err(SqlError::internal("truncated VECTOR value"));
            }
            let dim = u32::from_le_bytes(buf[1..5].try_into().unwrap()) as usize;
            let need = 5 + dim * 4;
            if buf.len() < need {
                return Err(SqlError::internal("truncated VECTOR payload"));
            }
            let data: Vec<f32> = buf[5..need]
                .chunks_exact(4)
                .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
                .collect();
            Ok((ColValue::Vector(Vector::new(dim, data)), need))
        }
        tag => Err(SqlError::internal(format!("unknown value tag {tag}"))),
    }
}

/// The length of the value at the start of `buf`.
///
/// The length is computed from the header of the value alone, so a value
/// can be skipped without materialising it. That matters for the vector
/// column, which is `dim` floats long: a scan that only needs the scalar
/// columns of a row must not copy the vector of every row.
fn value_span(buf: &[u8]) -> SqlResult<usize> {
    if buf.is_empty() {
        return Err(SqlError::internal("truncated row image"));
    }
    match buf[0] {
        TAG_BIGINT => Ok(9),
        TAG_TEXT => {
            if buf.len() < 5 {
                return Err(SqlError::internal("truncated VARCHAR value"));
            }
            let len = u32::from_le_bytes(buf[1..5].try_into().unwrap()) as usize;
            Ok(5 + len)
        }
        TAG_VECTOR => {
            if buf.len() < 5 {
                return Err(SqlError::internal("truncated VECTOR value"));
            }
            let dim = u32::from_le_bytes(buf[1..5].try_into().unwrap()) as usize;
            Ok(5 + dim * 4)
        }
        tag => Err(SqlError::internal(format!("unknown value tag {tag}"))),
    }
}

/// The bytes of the value at `index` of a row image.
///
/// The value is not copied; the returned slice points into the image.
pub fn value_slice(buf: &[u8], index: usize) -> SqlResult<&[u8]> {
    let mut pos = 0usize;
    for _ in 0..index {
        let span = value_span(buf.get(pos..).unwrap_or_default())?;
        pos += span;
    }
    let span = value_span(buf.get(pos..).unwrap_or_default())?;
    Ok(&buf[pos..pos + span])
}

/// Decode the value at `index` of a row image.
///
/// Only the requested column is materialised; the other columns are skipped
/// with arithmetic alone.
pub fn decode_value_at(buf: &[u8], index: usize) -> SqlResult<ColValue> {
    let slice = value_slice(buf, index)?;
    Ok(decode_value(slice)?.0)
}

/// The raw bytes of the vector at `index` of a row image.
///
/// The bytes are the little endian `f32` values of the vector, without a
/// copy, so that the distance kernel can be fed from them directly.
pub fn vector_bytes(buf: &[u8], index: usize) -> SqlResult<&[u8]> {
    let slice = value_slice(buf, index)?;
    if slice.is_empty() || slice[0] != TAG_VECTOR {
        return Err(SqlError::internal("column does not hold a vector"));
    }
    if slice.len() < 5 {
        return Err(SqlError::internal("truncated VECTOR value"));
    }
    let dim = u32::from_le_bytes(slice[1..5].try_into().unwrap()) as usize;
    Ok(&slice[5..5 + dim * 4])
}

/// Encode a whole row image.
pub fn encode_row(cols: &[ColValue]) -> Vec<u8> {
    let mut out = Vec::new();
    for c in cols {
        encode_value(&mut out, c);
    }
    out
}

/// Decode a whole row image.
pub fn decode_row(buf: &[u8]) -> SqlResult<Vec<ColValue>> {
    let mut out = Vec::new();
    let mut pos = 0usize;
    while pos < buf.len() {
        let (v, used) = decode_value(&buf[pos..])?;
        out.push(v);
        pos += used;
    }
    Ok(out)
}

/// Decode a whole row image, leaving the vector column undecoded.
///
/// The vector column is by far the largest part of an image - `dim` floats
/// against a few bytes for the other columns - and it is stored a second
/// time in the vector file. A scan that only needs the scalar columns must
/// therefore not copy it: this function decodes every value except the
/// vector, which becomes a `None` at its position. The positions of the
/// other values are unchanged, so a column is still addressed by its index.
///
/// The vector bytes themselves are left in the image, so a caller that does
/// need the vector can still reach them with [`vector_bytes`].
pub fn decode_row_scalars(buf: &[u8]) -> SqlResult<Vec<Option<ColValue>>> {
    let mut out = Vec::new();
    let mut pos = 0usize;
    while pos < buf.len() {
        if buf[pos] == TAG_VECTOR {
            // The vector is not decoded, but its length is still needed to
            // step over it.
            let span = value_span(&buf[pos..])?;
            out.push(None);
            pos += span;
            continue;
        }
        let (v, used) = decode_value(&buf[pos..])?;
        out.push(Some(v));
        pos += used;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_row() {
        let cols = vec![
            ColValue::BigInt(42),
            ColValue::Text("旅行攻略".to_string()),
            ColValue::Vector(Vector::parse_literal("[1,2,3]").unwrap()),
        ];
        let bytes = encode_row(&cols);
        let back = decode_row(&bytes).unwrap();
        assert_eq!(back, cols);
    }

    #[test]
    fn roundtrip_empty() {
        assert_eq!(decode_row(&encode_row(&[])).unwrap(), vec![]);
    }

    #[test]
    fn detects_truncation() {
        let bytes = encode_row(&[ColValue::Text("abc".into())]);
        assert!(decode_row(&bytes[..3]).is_err());
    }
}
