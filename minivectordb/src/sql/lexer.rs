//! SQL lexer for the supported subset.
//!
//! Produces a flat token stream; the parser turns it into an AST. Numbers are
//! lexed as raw text so that the value layer can apply the strict float32
//! finiteness rules the statement demands (reject NaN / Infinity / overflow).

use crate::error::{SqlError, SqlResult};

#[derive(Debug, Clone, PartialEq)]
pub enum Token {
    /// Bare word or keyword.
    Word(String),
    /// Integer literal.
    Int(i64),
    /// Numeric literal kept as text (may be a float, or `1e400` which must be
    /// rejected as an overflow).
    Num(String),
    /// Single quoted string literal (decoded).
    Str(String),
    /// Backtick quoted identifier (decoded).
    Ident(String),
    /// Punctuation / operator.
    Punct(String),
    Eof,
}

impl Token {
    pub fn is_punct(&self, p: &str) -> bool {
        matches!(self, Token::Punct(x) if x == p)
    }

    pub fn is_keyword(&self, kw: &str) -> bool {
        matches!(self, Token::Word(w) if w.eq_ignore_ascii_case(kw))
    }

    pub fn word(&self) -> Option<&str> {
        match self {
            Token::Word(w) => Some(w.as_str()),
            _ => None,
        }
    }
}

pub struct Lexer<'a> {
    src: &'a [u8],
    pos: usize,
}

impl<'a> Lexer<'a> {
    pub fn new(src: &'a str) -> Self {
        Lexer { src: src.as_bytes(), pos: 0 }
    }

    pub fn tokenize(mut self) -> SqlResult<Vec<Token>> {
        let mut out = Vec::new();
        loop {
            self.skip_ws();
            if self.pos >= self.src.len() {
                out.push(Token::Eof);
                return Ok(out);
            }
            let tok = self.next_token()?;
            out.push(tok);
        }
    }

    fn skip_ws(&mut self) {
        while self.pos < self.src.len() && (self.src[self.pos] as char).is_ascii_whitespace() {
            self.pos += 1;
        }
    }

    fn next_token(&mut self) -> SqlResult<Token> {
        let c = self.src[self.pos];
        match c {
            b'\'' => self.lex_string(b'\''),
            b'"' => self.lex_string(b'"'),
            b'`' => {
                let s = self.lex_quoted(b'`')?;
                Ok(Token::Ident(s))
            }
            b'0'..=b'9' => self.lex_number(),
            b'.' if self.pos + 1 < self.src.len() && self.src[self.pos + 1].is_ascii_digit() => {
                self.lex_number()
            }
            b'-' | b'+' => {
                // A sign is only a token when it prefixes a number; otherwise it
                // is a punctuation character (not part of our grammar).
                if self.pos + 1 < self.src.len() && self.src[self.pos + 1].is_ascii_digit() {
                    self.lex_number()
                } else {
                    self.pos += 1;
                    Ok(Token::Punct((c as char).to_string()))
                }
            }
            // Two character comparison operators must be checked before the
            // single character forms.
            b'<' | b'>' if self.pos + 1 < self.src.len() && self.src[self.pos + 1] == b'=' => {
                self.pos += 2;
                Ok(Token::Punct(format!("{}=", c as char)))
            }
            b'<' if self.src.get(self.pos + 1) == Some(&b'>') => {
                self.pos += 2;
                Ok(Token::Punct("!=".to_string()))
            }
            b'!' if self.src.get(self.pos + 1) == Some(&b'=') => {
                self.pos += 2;
                Ok(Token::Punct("!=".to_string()))
            }
            b'(' | b')' | b',' | b';' | b'=' | b'<' | b'>' | b'*' | b'.' => {
                self.pos += 1;
                Ok(Token::Punct((c as char).to_string()))
            }
            _ if is_ident_start(c) => {
                let start = self.pos;
                while self.pos < self.src.len() && is_ident_continue(self.src[self.pos]) {
                    self.pos += 1;
                }
                let word = std::str::from_utf8(&self.src[start..self.pos])
                    .map_err(|_| SqlError::syntax("invalid UTF-8 in identifier"))?;
                Ok(Token::Word(word.to_string()))
            }
            _ => Err(SqlError::syntax(format!(
                "unexpected character '{}'",
                c as char
            ))),
        }
    }

    fn lex_number(&mut self) -> SqlResult<Token> {
        let start = self.pos;
        if self.src[self.pos] == b'-' || self.src[self.pos] == b'+' {
            self.pos += 1;
        }
        let mut is_float = false;
        while self.pos < self.src.len() {
            let c = self.src[self.pos];
            if c.is_ascii_digit() {
                self.pos += 1;
            } else if c == b'.' {
                is_float = true;
                self.pos += 1;
            } else if c == b'e' || c == b'E' {
                is_float = true;
                self.pos += 1;
                if self.pos < self.src.len()
                    && (self.src[self.pos] == b'-' || self.src[self.pos] == b'+')
                {
                    self.pos += 1;
                }
            } else {
                break;
            }
        }
        let text = std::str::from_utf8(&self.src[start..self.pos])
            .map_err(|_| SqlError::syntax("invalid UTF-8 in numeric literal"))?
            .to_string();
        if !is_float {
            if let Ok(v) = text.parse::<i64>() {
                return Ok(Token::Int(v));
            }
            // Integer overflow: keep as a float so the value layer can reject it
            // with a data conversion error instead of a parse error.
        }
        Ok(Token::Num(text))
    }

    fn lex_string(&mut self, quote: u8) -> SqlResult<Token> {
        self.pos += 1; // opening quote
        let mut out = String::new();
        while self.pos < self.src.len() {
            let c = self.src[self.pos];
            if c == b'\\' && self.pos + 1 < self.src.len() {
                let e = self.src[self.pos + 1];
                out.push(match e {
                    b'n' => '\n',
                    b't' => '\t',
                    b'r' => '\r',
                    b'0' => '\0',
                    other => other as char,
                });
                self.pos += 2;
                continue;
            }
            if c == quote {
                if self.src.get(self.pos + 1) == Some(&quote) {
                    out.push(quote as char);
                    self.pos += 2;
                    continue;
                }
                self.pos += 1;
                return Ok(Token::Str(out));
            }
            // Copy raw bytes: multi-byte UTF-8 passes through untouched.
            let ch_len = utf8_len(c);
            let end = (self.pos + ch_len).min(self.src.len());
            let s = std::str::from_utf8(&self.src[self.pos..end])
                .map_err(|_| SqlError::syntax("invalid UTF-8 in string literal"))?;
            out.push_str(s);
            self.pos = end;
        }
        Err(SqlError::syntax("unterminated string literal"))
    }

    fn lex_quoted(&mut self, quote: u8) -> SqlResult<String> {
        self.pos += 1;
        let start = self.pos;
        while self.pos < self.src.len() {
            if self.src[self.pos] == quote {
                let s = std::str::from_utf8(&self.src[start..self.pos])
                    .map_err(|_| SqlError::syntax("invalid UTF-8 in quoted identifier"))?
                    .to_string();
                self.pos += 1;
                return Ok(s);
            }
            self.pos += 1;
        }
        Err(SqlError::syntax("unterminated quoted identifier"))
    }
}

fn is_ident_start(c: u8) -> bool {
    c.is_ascii_alphabetic() || c == b'_' || c >= 0x80
}

fn is_ident_continue(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'_' || c == b'$' || c >= 0x80
}

fn utf8_len(first: u8) -> usize {
    if first < 0x80 {
        1
    } else if first >> 5 == 0b110 {
        2
    } else if first >> 4 == 0b1110 {
        3
    } else if first >> 3 == 0b11110 {
        4
    } else {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn toks(s: &str) -> Vec<Token> {
        Lexer::new(s).tokenize().unwrap()
    }

    #[test]
    fn lexes_keywords_and_punct() {
        let t = toks("SELECT id FROM t WHERE id >= 3");
        assert_eq!(t[0], Token::Word("SELECT".into()));
        assert!(t[6].is_punct(">="));
        assert_eq!(t[7], Token::Int(3));
    }

    #[test]
    fn lexes_two_char_operators() {
        assert!(toks("a <= b")[1].is_punct("<="));
        assert!(toks("a >= b")[1].is_punct(">="));
        assert!(toks("a != b")[1].is_punct("!="));
        assert!(toks("a <> b")[1].is_punct("!="));
    }

    #[test]
    fn lexes_strings_with_escapes() {
        let t = toks("'a\\'b'");
        assert_eq!(t[0], Token::Str("a'b".into()));
    }

    #[test]
    fn lexes_multibyte_strings() {
        let t = toks("'旅行攻略'");
        assert_eq!(t[0], Token::Str("旅行攻略".into()));
    }

    #[test]
    fn lexes_big_int_as_num() {
        let t = toks("99999999999999999999");
        assert!(matches!(t[0], Token::Num(_)));
    }
}
