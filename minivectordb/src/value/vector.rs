//! Vector values: parsing, validation and storage.
//!
//! The statement demands that a vector be a fixed-dimension float32 array whose
//! elements are finite: NaN, Infinity, dimension mismatch and conversion
//! overflow must all be rejected rather than silently coerced.

use crate::error::{SqlError, SqlResult};

#[derive(Debug, Clone, PartialEq)]
pub struct Vector {
    pub dim: usize,
    pub data: Vec<f32>,
}

impl Vector {
    pub fn new(dim: usize, data: Vec<f32>) -> Self {
        Vector { dim, data }
    }

    pub fn zeros(dim: usize) -> Self {
        Vector { dim, data: vec![0.0f32; dim] }
    }

    pub fn as_slice(&self) -> &[f32] {
        &self.data
    }

    /// Parse a vector literal such as `[1, 0, 0, 0]`.
    ///
    /// Accepts optional surrounding whitespace and an optional trailing comma.
    /// Every element must parse to a finite float32.
    pub fn parse_literal(text: &str) -> SqlResult<Vector> {
        let trimmed = trim_ascii(text);
        let inner = trimmed
            .strip_prefix('[')
            .and_then(|s| s.strip_suffix(']'))
            .ok_or_else(|| {
                SqlError::data_error(format!(
                    "invalid vector literal '{text}': expected a bracketed list"
                ))
            })?;

        if inner.trim().is_empty() {
            return Err(SqlError::data_error(
                "invalid vector literal: empty vector is not allowed",
            ));
        }

        // The room for the elements is asked for in one go: the number of
        // elements is at most the number of commas plus one, and a vector
        // literal holds a fixed number of them.
        let mut data = Vec::with_capacity(inner.len() / 8 + 1);
        for part in split_top_level(inner) {
            let p = trim_ascii(part);
            if p.is_empty() {
                return Err(SqlError::data_error(
                    "invalid vector literal: empty element",
                ));
            }
            let v = parse_f32(p)?;
            if !v.is_finite() {
                return Err(SqlError::data_error(format!(
                    "invalid vector element '{p}': value must be finite"
                )));
            }
            data.push(v);
        }
        let dim = data.len();
        Ok(Vector { dim, data })
    }

    /// Check the dimension against the column declaration.
    pub fn check_dim(&self, expected: usize) -> SqlResult<()> {
        if self.dim != expected {
            return Err(SqlError::dimension_mismatch(format!(
                "vector dimension mismatch: expected {expected}, got {}",
                self.dim
            )));
        }
        Ok(())
    }
}

/// Split on commas that are not nested inside brackets.
///
/// The parts are handed out one at a time, so that the parse of a vector
/// literal does not have to hold a list of them.
fn split_top_level(s: &str) -> impl Iterator<Item = &str> {
    let mut parts = Vec::new();
    let mut depth = 0i32;
    let mut start = 0usize;
    for (i, c) in s.char_indices() {
        match c {
            '[' | '(' => depth += 1,
            ']' | ')' => depth -= 1,
            ',' if depth == 0 => {
                parts.push(&s[start..i]);
                start = i + 1;
            }
            _ => {}
        }
    }
    parts.push(&s[start..]);
    parts.into_iter()
}

/// Parse a numeric literal into a finite float32.
///
/// Rejects NaN / Infinity spellings and values that overflow float32.
pub fn parse_f32(text: &str) -> SqlResult<f32> {
    let t = trim_ascii(text);
    if t.is_empty() {
        return Err(SqlError::data_error("invalid numeric literal: empty"));
    }
    // The check for the NaN and Infinity spellings is case insensitive, but
    // it must not allocate: a vector literal holds `dim` numbers and the
    // parse runs for every one of them, so a copy of the text per number
    // would dominate the cost of the parse.
    //
    // A text that holds only the characters of a decimal number cannot
    // spell "nan" or "inf", so one scan of the characters decides whether
    // the two searches are needed at all.
    if !is_decimal_chars(t.as_bytes()) {
        if contains_ignore_case(t, "nan") || contains_ignore_case(t, "inf") {
            return Err(SqlError::data_error(format!(
                "invalid numeric literal '{t}': NaN and Infinity are not allowed"
            )));
        }
    }
    let v = match parse_decimal_f64(t) {
        Some(v) => v,
        None => t.parse().map_err(|_| {
            SqlError::data_error(format!("invalid numeric literal '{t}'"))
        })?,
    };
    if !v.is_finite() {
        return Err(SqlError::data_error(format!(
            "invalid numeric literal '{t}': value must be finite"
        )));
    }
    let f = v as f32;
    if !f.is_finite() {
        return Err(SqlError::data_error(format!(
            "numeric literal '{t}' overflows float32"
        )));
    }
    Ok(f)
}

/// Trim the whitespace at both ends of the text.
///
/// The standard `trim` walks the text by characters, which costs a
/// character check per byte. A numeric literal is nearly always already
/// trimmed, so the common case is a look at the first and the last byte.
///
/// The result is the same as the one `str::trim` gives: a whitespace
/// character that is not ASCII is not whitespace to the ASCII scan, so
/// such a text is handed to `str::trim` itself.
fn trim_ascii(text: &str) -> &str {
    let bytes = text.as_bytes();
    if bytes.is_empty() {
        return text;
    }
    if bytes[0] >= 0x80 || bytes[bytes.len() - 1] >= 0x80 {
        return text.trim();
    }
    let mut start = 0;
    let mut end = bytes.len();
    while start < end && bytes[start].is_ascii_whitespace() {
        start += 1;
    }
    while end > start && bytes[end - 1].is_ascii_whitespace() {
        end -= 1;
    }
    &text[start..end]
}

/// Whether the bytes hold only the characters of a decimal number.
///
/// A text that holds only these characters spells a number, so it cannot
/// spell "nan" or "inf".
fn is_decimal_chars(bytes: &[u8]) -> bool {
    for &b in bytes {
        match b {
            b'0'..=b'9' | b'+' | b'-' | b'.' | b'e' | b'E' => {}
            _ => return false,
        }
    }
    !bytes.is_empty()
}

/// Parse a decimal number into a float64, when it can be done exactly.
///
/// The result is the same as the one `str::parse` gives, because the
/// number is turned into a mantissa and a power of ten that are both held
/// exactly, and the last step is a single operation that rounds once.
/// A number that does not fit, or that has a very large or very small
/// exponent, is left to `str::parse`.
fn parse_decimal_f64(text: &str) -> Option<f64> {
    let bytes = text.as_bytes();
    let mut i = 0usize;
    let mut negative = false;
    if i < bytes.len() && (bytes[i] == b'+' || bytes[i] == b'-') {
        negative = bytes[i] == b'-';
        i += 1;
    }
    // The mantissa is accumulated as an integer. It must stay exact, so
    // at most fifteen digits are taken, because ten to the fifteenth is
    // smaller than two to the fifty-third.
    let mut mantissa: u64 = 0;
    let mut digits = 0u32;
    let mut fraction_digits = 0i32;
    let mut seen_digit = false;
    while i < bytes.len() && bytes[i].is_ascii_digit() {
        if digits == 15 {
            return None;
        }
        mantissa = mantissa * 10 + u64::from(bytes[i] - b'0');
        digits += 1;
        seen_digit = true;
        i += 1;
    }
    if i < bytes.len() && bytes[i] == b'.' {
        i += 1;
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            if digits == 15 {
                return None;
            }
            mantissa = mantissa * 10 + u64::from(bytes[i] - b'0');
            digits += 1;
            fraction_digits += 1;
            seen_digit = true;
            i += 1;
        }
    }
    if !seen_digit {
        return None;
    }
    let mut exponent = -fraction_digits;
    if i < bytes.len() && (bytes[i] == b'e' || bytes[i] == b'E') {
        i += 1;
        let mut exp_negative = false;
        if i < bytes.len() && (bytes[i] == b'+' || bytes[i] == b'-') {
            exp_negative = bytes[i] == b'-';
            i += 1;
        }
        let mut value = 0i32;
        let mut exp_digits = 0u32;
        while i < bytes.len() && bytes[i].is_ascii_digit() {
            if exp_digits == 9 {
                return None;
            }
            value = value * 10 + i32::from(bytes[i] - b'0');
            exp_digits += 1;
            i += 1;
        }
        if exp_digits == 0 {
            return None;
        }
        exponent += if exp_negative { -value } else { value };
    }
    if i != bytes.len() {
        return None;
    }
    // The power of ten must be held exactly, which holds up to ten to the
    // twenty-second.
    if exponent < -22 || exponent > 22 {
        return None;
    }
    let value = mantissa as f64;
    let scaled = if exponent >= 0 {
        value * pow10_i32(exponent)
    } else {
        value / pow10_i32(-exponent)
    };
    Some(if negative { -scaled } else { scaled })
}

/// Ten to a small exponent, held exactly.
fn pow10_i32(exponent: i32) -> f64 {
    const TABLE: [f64; 23] = [
        1e0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8, 1e9, 1e10, 1e11, 1e12, 1e13, 1e14, 1e15,
        1e16, 1e17, 1e18, 1e19, 1e20, 1e21, 1e22,
    ];
    TABLE[exponent as usize]
}

/// Whether the text holds the word, ignoring the case of the letters.
///
/// The comparison is done on the bytes, so it only folds the ASCII letters.
/// A non-ASCII byte can never be part of "nan" or "inf", so it does not have
/// to be folded.
fn contains_ignore_case(text: &str, word: &str) -> bool {
    let bytes = text.as_bytes();
    let needle = word.as_bytes();
    if needle.len() > bytes.len() {
        return false;
    }
    let last = bytes.len() - needle.len();
    'outer: for start in 0..=last {
        for (i, n) in needle.iter().enumerate() {
            let b = bytes[start + i];
            let folded = if b.is_ascii_uppercase() { b + 32 } else { b };
            if folded != *n {
                continue 'outer;
            }
        }
        return true;
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_simple_vector() {
        let v = Vector::parse_literal("[1, 0, 0, 0]").unwrap();
        assert_eq!(v.dim, 4);
        assert_eq!(v.data[0], 1.0);
    }

    #[test]
    fn parses_negative_and_float() {
        let v = Vector::parse_literal("[-1.5, 2e2]").unwrap();
        assert_eq!(v.data[0], -1.5);
        assert_eq!(v.data[1], 200.0);
    }

    #[test]
    fn rejects_nan_and_inf() {
        assert!(Vector::parse_literal("[NaN, 1]").is_err());
        assert!(Vector::parse_literal("[Infinity, 1]").is_err());
        assert!(Vector::parse_literal("[-inf, 1]").is_err());
    }

    #[test]
    fn rejects_overflow() {
        // 1e40 is finite in f64 but overflows float32.
        assert!(Vector::parse_literal("[1e40]").is_err());
    }

    #[test]
    fn rejects_bad_shape() {
        assert!(Vector::parse_literal("1,2,3").is_err());
        assert!(Vector::parse_literal("[]").is_err());
        assert!(Vector::parse_literal("[1,,2]").is_err());
    }

    #[test]
    fn dimension_mismatch_detected() {
        let v = Vector::parse_literal("[1,2]").unwrap();
        assert!(v.check_dim(4).is_err());
        assert!(v.check_dim(2).is_ok());
    }

    #[test]
    fn the_fast_number_parse_matches_the_standard_one() {
        // The fast path must give the very same float as the standard
        // parse, bit for bit, because the stored vector is compared
        // against a reference.
        let cases = [
            "0", "1", "-1", "+1", "1.5", "-1.5", "0.1", "0.2", "0.3", "3.14159265358979",
            "2e2", "2E2", "2e-2", "1e0", "1e+10", "1e-10", "123456789012345",
            "1234567890123456789", "0.000000000000001", "9007199254740993",
            "1.7976931348623157", "2.2250738585072014e-308", "4.9406564584124654e-324",
            "123456789012345678901234567890", "1e22", "1e-22", "1e23", "1e-23",
            "0.30000000000000004", "1.0000000000000002", "16777216.0", "16777217.0",
            "1.1754943508222875e-38", "3.4028234663852886e38", "1e39",
        ];
        for case in cases {
            let fast = parse_decimal_f64(case);
            let standard: f64 = case.parse().unwrap();
            match fast {
                Some(v) => assert_eq!(
                    v.to_bits(),
                    standard.to_bits(),
                    "the fast parse of '{case}' differs from the standard one"
                ),
                None => {}
            }
        }
    }

    #[test]
    fn the_fast_number_parse_is_used_for_a_plain_number() {
        assert!(parse_decimal_f64("1.5").is_some());
        assert!(parse_decimal_f64("1e40").is_none());
        assert!(parse_decimal_f64("abc").is_none());
    }

    #[test]
    fn the_fast_number_parse_matches_the_standard_one_on_many_numbers() {
        // A small generator of its own, so that the check needs nothing
        // from outside.
        let mut state: u64 = 0x9E37_79B9_7F4A_7C15;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        let mut checked = 0usize;
        for _ in 0..200_000 {
            let digits = (next() % 19 + 1) as usize;
            let mut mant = String::new();
            for _ in 0..digits {
                mant.push((b'0' + (next() % 10) as u8) as char);
            }
            let mut text = mant.clone();
            let roll = next() % 10;
            if roll < 7 {
                let point = (next() as usize) % (mant.len() + 1);
                text = format!("{}.{}", &mant[..point], &mant[point..]);
            }
            if next() % 5 < 2 {
                let sign = match next() % 3 {
                    0 => "",
                    1 => "+",
                    _ => "-",
                };
                text.push_str("e");
                text.push_str(sign);
                text.push_str(&(next() % 31).to_string());
            }
            if let Some(fast) = parse_decimal_f64(&text) {
                let standard: f64 = text.parse().unwrap();
                assert_eq!(
                    fast.to_bits(),
                    standard.to_bits(),
                    "the fast parse of '{text}' differs from the standard one"
                );
                checked += 1;
            }
        }
        assert!(checked > 100_000, "too few numbers were checked");
    }
}
