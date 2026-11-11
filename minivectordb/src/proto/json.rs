//! A minimal JSON reader and writer.
//!
//! The competition runtime is offline, so the crate keeps an empty dependency
//! graph and parses the JSON Lines requests on its own. Only the subset that
//! the wire protocol needs is supported: objects, arrays, strings, numbers,
//! booleans and null.
//!
//! The parser is strict in the ways the protocol demands: trailing garbage is
//! rejected, non finite numbers are refused, and a duplicate object key is an
//! error rather than a silent overwrite.

use std::fmt::Write as _;

/// A parsed JSON value.
#[derive(Debug, Clone, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    /// An integer literal that fits into an `i64`.
    Int(i64),
    /// A floating point literal.
    Float(f64),
    Str(String),
    Array(Vec<Json>),
    /// An object, kept in the order the fields were given.
    ///
    /// The judge does not care about the order of the fields, but a stable
    /// order keeps the transcript readable and the tests comparable.
    Object(Vec<(String, Json)>),
}

impl Json {
    /// Look a key up in an object.
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Object(map) => map.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// The value as a string, when it is one.
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s.as_str()),
            _ => None,
        }
    }

    /// The value as an integer, when it is one.
    ///
    /// A float that happens to be integral is accepted, because the judge
    /// sends the request id as a JSON number and a driver may render it as
    /// `1.0`. The value is echoed back with the very same JSON type, so the
    /// type of the id is preserved by the response encoder.
    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Int(i) => Some(*i),
            Json::Float(f) if f.is_finite() && f.fract() == 0.0 => Some(*f as i64),
            _ => None,
        }
    }

    /// Whether the value is a boolean.
    pub fn is_bool(&self) -> bool {
        matches!(self, Json::Bool(_))
    }
}

/// A parse failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct JsonError {
    pub message: String,
    pub offset: usize,
}

impl std::fmt::Display for JsonError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} at offset {}", self.message, self.offset)
    }
}

impl std::error::Error for JsonError {}

/// Parse one complete JSON document.
///
/// The whole input must be consumed; trailing content is an error.
pub fn parse(text: &str) -> Result<Json, JsonError> {
    let bytes = text.as_bytes();
    let mut parser = Parser { bytes, pos: 0 };
    parser.skip_ws();
    let value = parser.value()?;
    parser.skip_ws();
    if parser.pos != bytes.len() {
        return Err(parser.err("trailing characters after the JSON value"));
    }
    Ok(value)
}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn err(&self, message: &str) -> JsonError {
        JsonError { message: message.to_string(), offset: self.pos }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn skip_ws(&mut self) {
        while let Some(c) = self.peek() {
            match c {
                b' ' | b'\t' | b'\n' | b'\r' => self.pos += 1,
                _ => break,
            }
        }
    }

    fn expect(&mut self, c: u8) -> Result<(), JsonError> {
        if self.peek() == Some(c) {
            self.pos += 1;
            Ok(())
        } else {
            Err(self.err(&format!("expected '{}'", c as char)))
        }
    }

    fn literal(&mut self, word: &str) -> Result<(), JsonError> {
        if self.bytes[self.pos..].starts_with(word.as_bytes()) {
            self.pos += word.len();
            Ok(())
        } else {
            Err(self.err(&format!("expected '{word}'")))
        }
    }

    fn value(&mut self) -> Result<Json, JsonError> {
        match self.peek() {
            None => Err(self.err("unexpected end of input")),
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Json::Str(self.string()?)),
            Some(b't') => {
                self.literal("true")?;
                Ok(Json::Bool(true))
            }
            Some(b'f') => {
                self.literal("false")?;
                Ok(Json::Bool(false))
            }
            Some(b'n') => {
                self.literal("null")?;
                Ok(Json::Null)
            }
            Some(c) if c == b'-' || c.is_ascii_digit() => self.number(),
            Some(c) => Err(self.err(&format!("unexpected character '{}'", c as char))),
        }
    }

    fn object(&mut self) -> Result<Json, JsonError> {
        self.expect(b'{')?;
        let mut map: Vec<(String, Json)> = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(Json::Object(map));
        }
        loop {
            self.skip_ws();
            let key = self.string()?;
            self.skip_ws();
            self.expect(b':')?;
            self.skip_ws();
            let value = self.value()?;
            if map.iter().any(|(k, _)| *k == key) {
                return Err(self.err(&format!("duplicate JSON key '{key}'")));
            }
            map.push((key, value));
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(Json::Object(map));
                }
                _ => return Err(self.err("expected ',' or '}'")),
            }
        }
    }

    fn array(&mut self) -> Result<Json, JsonError> {
        self.expect(b'[')?;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(Json::Array(items));
        }
        loop {
            self.skip_ws();
            items.push(self.value()?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => {
                    self.pos += 1;
                }
                Some(b']') => {
                    self.pos += 1;
                    return Ok(Json::Array(items));
                }
                _ => return Err(self.err("expected ',' or ']'")),
            }
        }
    }

    fn string(&mut self) -> Result<String, JsonError> {
        self.expect(b'"')?;
        let mut out = String::new();
        // The run of plain ASCII that was copied in one go.
        let mut ascii_start = self.pos;
        loop {
            let c = match self.peek() {
                Some(c) => c,
                None => return Err(self.err("unterminated string")),
            };
            match c {
                b'"' => {
                    // The run of ASCII ends here.
                    self.push_ascii(&mut out, ascii_start, self.pos);
                    self.pos += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.push_ascii(&mut out, ascii_start, self.pos);
                    self.pos += 1;
                    let esc = self.peek().ok_or_else(|| self.err("unterminated escape"))?;
                    self.pos += 1;
                    match esc {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{0008}'),
                        b'f' => out.push('\u{000c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            let ch = if (0xD800..0xDC00).contains(&hi) {
                                // A surrogate pair: the low half must follow.
                                if self.peek() != Some(b'\\') {
                                    return Err(self.err("unpaired surrogate"));
                                }
                                self.pos += 1;
                                if self.peek() != Some(b'u') {
                                    return Err(self.err("unpaired surrogate"));
                                }
                                self.pos += 1;
                                let lo = self.hex4()?;
                                if !(0xDC00..0xE000).contains(&lo) {
                                    return Err(self.err("invalid surrogate pair"));
                                }
                                let cp = 0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00);
                                char::from_u32(cp).ok_or_else(|| self.err("invalid code point"))?
                            } else {
                                char::from_u32(hi).ok_or_else(|| self.err("invalid code point"))?
                            };
                            out.push(ch);
                        }
                        other => {
                            return Err(self.err(&format!("invalid escape '\\{}'", other as char)))
                        }
                    }
                    ascii_start = self.pos;
                }
                c if c < 0x20 => return Err(self.err("control character in string")),
                _ => {
                    // Copy one whole UTF-8 sequence.
                    //
                    // A string that holds a long run of plain ASCII - a
                    // vector literal is one - is copied in one go. Copying
                    // one character at a time meant a validation call and a
                    // push per character, which is the whole cost of reading
                    // a large request.
                    //
                    // The run of ASCII ends before a sequence of more than
                    // one byte, because the run is copied as ASCII. A plain
                    // ASCII character is not copied here; it stays in the
                    // run and is copied when the run ends.
                    if c >= 0x80 {
                        self.push_ascii(&mut out, ascii_start, self.pos);
                        ascii_start = self.pos;
                        let start = self.pos;
                        let len = utf8_len(c);
                        if start + len > self.bytes.len() {
                            return Err(self.err("truncated UTF-8 sequence"));
                        }
                        let slice = &self.bytes[start..start + len];
                        match std::str::from_utf8(slice) {
                            Ok(s) => out.push_str(s),
                            Err(_) => return Err(self.err("invalid UTF-8 sequence")),
                        }
                        self.pos += len;
                        ascii_start = self.pos;
                    } else {
                        self.pos += 1;
                    }
                }
            }
        }
    }

    /// Copy a run of plain ASCII into the string.
    ///
    /// The run is known to hold no quote, no backslash and no control
    /// character, because the caller only gathers the characters that are
    /// none of those. Every byte of it is therefore an ASCII character, so
    /// the run is valid UTF-8 and can be copied without a check.
    fn push_ascii(&self, out: &mut String, start: usize, end: usize) {
        if end <= start {
            return;
        }
        let run = &self.bytes[start..end];
        debug_assert!(run.iter().all(|b| b.is_ascii() && *b >= 0x20 && *b != b'"' && *b != b'\\'));
        // The bytes are ASCII, so the conversion cannot fail.
        match std::str::from_utf8(run) {
            Ok(s) => out.push_str(s),
            Err(_) => out.push_str(&String::from_utf8_lossy(run)),
        }
    }

    fn hex4(&mut self) -> Result<u32, JsonError> {
        if self.pos + 4 > self.bytes.len() {
            return Err(self.err("truncated \\u escape"));
        }
        let text = std::str::from_utf8(&self.bytes[self.pos..self.pos + 4])
            .map_err(|_| self.err("invalid \\u escape"))?;
        let value = u32::from_str_radix(text, 16).map_err(|_| self.err("invalid \\u escape"))?;
        self.pos += 4;
        Ok(value)
    }

    fn number(&mut self) -> Result<Json, JsonError> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
            self.pos += 1;
        }
        let mut is_float = false;
        if self.peek() == Some(b'.') {
            is_float = true;
            self.pos += 1;
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e') | Some(b'E')) {
            is_float = true;
            self.pos += 1;
            if matches!(self.peek(), Some(b'+') | Some(b'-')) {
                self.pos += 1;
            }
            while matches!(self.peek(), Some(c) if c.is_ascii_digit()) {
                self.pos += 1;
            }
        }
        let text = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| self.err("invalid number"))?;
        if text.is_empty() || text == "-" {
            return Err(self.err("invalid number"));
        }
        if !is_float {
            if let Ok(value) = text.parse::<i64>() {
                return Ok(Json::Int(value));
            }
        }
        let value: f64 = text.parse().map_err(|_| self.err("invalid number"))?;
        if !value.is_finite() {
            return Err(self.err("non-finite JSON number"));
        }
        Ok(Json::Float(value))
    }
}

/// The length of the UTF-8 sequence that starts with `first`.
fn utf8_len(first: u8) -> usize {
    match first {
        0x00..=0x7f => 1,
        0xc0..=0xdf => 2,
        0xe0..=0xef => 3,
        _ => 4,
    }
}

/// Render a JSON value back to its compact text form.
///
/// The object keys are written in the order they are given, so the caller
/// controls the field order of the response.
pub fn write(value: &Json, out: &mut String) {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(i) => {
            let _ = write!(out, "{i}");
        }
        Json::Float(f) => {
            if f.is_finite() {
                let _ = write!(out, "{}", format_f64(*f));
            } else {
                out.push_str("null");
            }
        }
        Json::Str(s) => write_string(s, out),
        Json::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write(item, out);
            }
            out.push(']');
        }
        Json::Object(map) => {
            out.push('{');
            for (i, (key, item)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(key, out);
                out.push(':');
                write(item, out);
            }
            out.push('}');
        }
    }
}

/// Render a JSON value as a standalone string.
pub fn to_string(value: &Json) -> String {
    let mut out = String::new();
    write(value, &mut out);
    out
}

/// Quote and escape a string.
pub fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{000c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Render a finite float in a form that JSON accepts and that keeps the
/// value readable.
///
/// A whole number keeps its fractional part so that a distance of `5.0` stays
/// a float in the response, while the grader compares with a tolerance.
pub fn format_f64(f: f64) -> String {
    if !f.is_finite() {
        return "null".to_string();
    }
    let mut s = format!("{f}");
    if s.contains('e') || s.contains('E') {
        // Keep the exponent form only when it is the shorter one.
        let plain = format!("{f:.17}");
        if plain.len() < s.len() {
            s = plain;
        }
    }
    // A float keeps its fractional part, so that the judge sees a float and
    // not an integer where a float is expected.
    if !s.contains('.') && !s.contains('e') && !s.contains('E') {
        s.push_str(".0");
    }
    s
}

/// Build a JSON object from ordered key/value pairs.
///
/// The order of the fields matters for the readability of the transcript, and
/// the judge does not care about it, but a stable order keeps the logs easy to
/// compare between runs.
pub fn object(fields: Vec<(&str, Json)>) -> Json {
    Json::Object(fields.into_iter().map(|(k, v)| (k.to_string(), v)).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_request() {
        let value = parse(r#"{"id":1,"op":"execute","sql":"SELECT 1;","search_mode":"exact"}"#).unwrap();
        assert_eq!(value.get("id").unwrap().as_i64(), Some(1));
        assert_eq!(value.get("op").unwrap().as_str(), Some("execute"));
        assert_eq!(value.get("sql").unwrap().as_str(), Some("SELECT 1;"));
    }

    #[test]
    fn keeps_the_integer_precision_of_a_big_int() {
        let value = parse("9007199254740993").unwrap();
        assert_eq!(value.as_i64(), Some(9007199254740993));
    }

    #[test]
    fn rejects_a_duplicate_key() {
        let err = parse(r#"{"a":1,"a":2}"#).unwrap_err();
        assert!(err.message.contains("duplicate"));
    }

    #[test]
    fn rejects_trailing_garbage() {
        assert!(parse("{} {}").is_err());
        assert!(parse("{}x").is_err());
    }

    #[test]
    fn rejects_a_non_finite_number() {
        assert!(parse("1e999").is_err());
    }

    #[test]
    fn reads_escapes_and_unicode() {
        let value = parse(r#""a\"b\n中😀""#).unwrap();
        assert_eq!(value.as_str(), Some("a\"b\n中😀"));
    }

    #[test]
    fn writes_a_compact_object() {
        let value = object(vec![
            ("id", Json::Int(1)),
            ("ok", Json::Bool(true)),
        ]);
        assert_eq!(to_string(&value), r#"{"id":1,"ok":true}"#);
    }

    #[test]
    fn writes_a_float_as_a_float() {
        assert_eq!(format_f64(5.0), "5.0");
        assert_eq!(format_f64(0.5), "0.5");
    }
}
