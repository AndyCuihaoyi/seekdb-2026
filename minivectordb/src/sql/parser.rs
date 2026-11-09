//! Recursive descent parser for the supported SQL subset.
//!
//! Scope is deliberately narrow. Anything outside the documented subset must
//! fail with an explicit error rather than being silently reinterpreted.
//!
//! Boolean structure is modelled with a dedicated node so that `AND` / `OR`
//! are not confused with the `L2_DISTANCE` call node.

use crate::error::{SqlError, SqlResult};
use crate::sql::ast::*;
use crate::sql::lexer::{Lexer, Token};

pub fn parse(stmt: &str) -> SqlResult<Statement> {
    let tokens = Lexer::new(stmt).tokenize()?;
    let mut p = Parser { toks: tokens, pos: 0 };
    let s = p.parse_statement()?;
    p.expect_eof()?;
    Ok(s)
}

struct Parser {
    toks: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn peek(&self) -> &Token {
        self.toks.get(self.pos).unwrap_or(&Token::Eof)
    }

    fn next(&mut self) -> Token {
        let t = self.toks.get(self.pos).cloned().unwrap_or(Token::Eof);
        self.pos += 1;
        t
    }

    fn expect_eof(&self) -> SqlResult<()> {
        if matches!(self.peek(), Token::Eof) {
            Ok(())
        } else {
            Err(SqlError::syntax("unexpected trailing input"))
        }
    }

    fn eat_punct(&mut self, p: &str) -> bool {
        if self.peek().is_punct(p) {
            self.pos += 1;
            true
        } else {
            false
        }
    }

    fn expect_punct(&mut self, p: &str) -> SqlResult<()> {
        if self.eat_punct(p) {
            Ok(())
        } else {
            Err(SqlError::syntax(format!(
                "expected '{}' but found {}",
                p,
                describe(self.peek())
            )))
        }
    }

    fn eat_keyword(&mut self, kw: &str) -> bool {
        if self.peek().is_keyword(kw) {
            self.pos += 1;
            true
        } else {
            false
        }
    }

    fn expect_keyword(&mut self, kw: &str) -> SqlResult<()> {
        if self.eat_keyword(kw) {
            Ok(())
        } else {
            Err(SqlError::syntax(format!(
                "expected keyword {} but found {}",
                kw,
                describe(self.peek())
            )))
        }
    }

    fn parse_ident(&mut self) -> SqlResult<String> {
        match self.next() {
            Token::Word(w) => Ok(w),
            Token::Ident(w) => Ok(w),
            other => Err(SqlError::syntax(format!(
                "expected identifier but found {}",
                describe(&other)
            ))),
        }
    }

    fn parse_statement(&mut self) -> SqlResult<Statement> {
        if self.peek().is_punct("(") {
            return Err(SqlError::not_supported(
                "subqueries and parenthesised expressions are not supported",
            ));
        }
        let kw = match self.peek().word() {
            Some(w) => w.to_ascii_uppercase(),
            None => {
                return Err(SqlError::syntax(format!(
                    "unexpected statement starting with {}",
                    describe(self.peek())
                )))
            }
        };
        match kw.as_str() {
            "SELECT" => Ok(Statement::Select(self.parse_select()?)),
            "INSERT" => Ok(Statement::Insert(self.parse_insert()?)),
            "UPDATE" => Ok(Statement::Update(self.parse_update()?)),
            "DELETE" => Ok(Statement::Delete(self.parse_delete()?)),
            "CREATE" => self.parse_create(),
            "BEGIN" | "START" => {
                self.pos += 1;
                self.eat_keyword("TRANSACTION");
                Ok(Statement::Begin)
            }
            "COMMIT" => {
                self.pos += 1;
                Ok(Statement::Commit)
            }
            "ROLLBACK" => {
                self.pos += 1;
                Ok(Statement::Rollback)
            }
            "SET" => self.parse_set(),
            "USE" => {
                self.pos += 1;
                let w = self.parse_ident()?;
                match w.to_ascii_uppercase().as_str() {
                    "ANN" | "APPROXIMATE" => Ok(Statement::SetMode { ann: true }),
                    "EXACT" => Ok(Statement::SetMode { ann: false }),
                    _ => Err(SqlError::not_supported(format!(
                        "unsupported USE target '{w}'"
                    ))),
                }
            }
            other => Err(SqlError::not_supported(format!(
                "statement '{}' is not supported by MiniVectorDB",
                other
            ))),
        }
    }

    fn parse_set(&mut self) -> SqlResult<Statement> {
        self.expect_keyword("SET")?;
        let mut name = self.parse_ident()?;
        if name.eq_ignore_ascii_case("session") || name.eq_ignore_ascii_case("global") {
            name = self.parse_ident()?;
        }
        self.expect_punct("=")?;
        let text = match self.next() {
            Token::Word(w) => w,
            Token::Str(s) => s,
            Token::Int(i) => i.to_string(),
            Token::Num(n) => n,
            other => {
                return Err(SqlError::syntax(format!(
                    "unsupported SET value {}",
                    describe(&other)
                )))
            }
        };
        let key = name.to_ascii_lowercase();
        let normalized = text.to_ascii_lowercase();
        match key.as_str() {
            "mode" | "search_mode" | "vector_search_mode" | "ann_mode" | "use_ann" => {
                match normalized.as_str() {
                    "ann" | "approximate" | "1" | "on" | "true" => {
                        Ok(Statement::SetMode { ann: true })
                    }
                    "exact" | "0" | "off" | "false" => Ok(Statement::SetMode { ann: false }),
                    other => Err(SqlError::syntax(format!(
                        "unknown search mode '{other}'"
                    ))),
                }
            }
            other => Err(SqlError::not_supported(format!(
                "SET {other} is not supported by MiniVectorDB"
            ))),
        }
    }

    fn parse_create(&mut self) -> SqlResult<Statement> {
        self.expect_keyword("CREATE")?;
        if self.peek().is_keyword("VECTOR") {
            self.pos += 1;
            self.expect_keyword("INDEX")?;
            let index = self.parse_ident()?;
            self.expect_keyword("ON")?;
            let table = self.parse_ident()?;
            self.expect_punct("(")?;
            let column = self.parse_ident()?;
            self.expect_punct(")")?;
            return Ok(Statement::CreateVectorIndex(CreateVectorIndex {
                index,
                table,
                column,
            }));
        }
        if self.peek().is_keyword("INDEX") || self.peek().is_keyword("UNIQUE") {
            return Err(SqlError::not_supported(
                "only CREATE VECTOR INDEX is supported by MiniVectorDB",
            ));
        }
        self.eat_keyword("TABLE");
        if self.peek().is_keyword("IF") {
            return Err(SqlError::not_supported(
                "CREATE TABLE IF NOT EXISTS is not supported by MiniVectorDB",
            ));
        }
        let table = self.parse_ident()?;
        self.expect_punct("(")?;
        let mut columns = Vec::new();
        loop {
            let name = self.parse_ident()?;
            let ty = self.parse_column_type()?;
            let mut primary_key = false;
            loop {
                if self.peek().is_keyword("PRIMARY") {
                    self.pos += 1;
                    self.expect_keyword("KEY")?;
                    primary_key = true;
                    continue;
                }
                if self.eat_keyword("NOT") {
                    self.expect_keyword("NULL")?;
                    continue;
                }
                if self.eat_keyword("NULL") {
                    continue;
                }
                break;
            }
            columns.push(ColumnDef { name, ty, primary_key });
            if self.eat_punct(",") {
                continue;
            }
            break;
        }
        self.expect_punct(")")?;
        Ok(Statement::CreateTable(CreateTable { table, columns }))
    }

    fn parse_column_type(&mut self) -> SqlResult<ColumnType> {
        let w = match self.peek().word() {
            Some(w) => w.to_ascii_uppercase(),
            None => {
                return Err(SqlError::syntax(format!(
                    "expected column type but found {}",
                    describe(self.peek())
                )))
            }
        };
        self.pos += 1;
        match w.as_str() {
            "BIGINT" | "INT" | "INTEGER" => Ok(ColumnType::BigInt),
            "VARCHAR" => Ok(ColumnType::VarChar(self.parse_optional_len()?.unwrap_or(255))),
            "VECTOR" => {
                let n = self.parse_optional_len()?.ok_or_else(|| {
                    SqlError::syntax("VECTOR requires an explicit dimension, e.g. VECTOR(4)")
                })?;
                Ok(ColumnType::Vector(n))
            }
            "TEXT" | "CHAR" => Ok(ColumnType::VarChar(self.parse_optional_len()?.unwrap_or(255))),
            other => Err(SqlError::not_supported(format!(
                "column type '{other}' is not supported by MiniVectorDB"
            ))),
        }
    }

    fn parse_optional_len(&mut self) -> SqlResult<Option<usize>> {
        if !self.eat_punct("(") {
            return Ok(None);
        }
        let n = match self.next() {
            Token::Int(i) if i >= 0 => i as usize,
            other => {
                return Err(SqlError::syntax(format!(
                    "expected a non-negative length but found {}",
                    describe(&other)
                )))
            }
        };
        self.expect_punct(")")?;
        Ok(Some(n))
    }

    fn parse_insert(&mut self) -> SqlResult<Insert> {
        self.expect_keyword("INSERT")?;
        self.expect_keyword("INTO")?;
        let table = self.parse_ident()?;
        if self.eat_punct("(") {
            // The optional column list is accepted but the engine uses the
            // table's declared column order.
            let mut depth = 1usize;
            loop {
                match self.next() {
                    Token::Eof => return Err(SqlError::syntax("unterminated column list")),
                    Token::Punct(p) if p == "(" => depth += 1,
                    Token::Punct(p) if p == ")" => {
                        depth -= 1;
                        if depth == 0 {
                            break;
                        }
                    }
                    _ => {}
                }
            }
        }
        self.expect_keyword("VALUES")?;
        let mut rows = Vec::new();
        loop {
            self.expect_punct("(")?;
            let mut vals = Vec::new();
            loop {
                vals.push(self.parse_expr()?);
                if self.eat_punct(",") {
                    continue;
                }
                break;
            }
            self.expect_punct(")")?;
            rows.push(vals);
            if self.eat_punct(",") {
                continue;
            }
            break;
        }
        Ok(Insert { table, rows })
    }

    fn parse_update(&mut self) -> SqlResult<Update> {
        self.expect_keyword("UPDATE")?;
        let table = self.parse_ident()?;
        self.expect_keyword("SET")?;
        let mut assignments = Vec::new();
        loop {
            let col = self.parse_ident()?;
            self.expect_punct("=")?;
            let val = self.parse_expr()?;
            assignments.push((col, val));
            if self.eat_punct(",") {
                continue;
            }
            break;
        }
        let filter = if self.eat_keyword("WHERE") {
            Some(self.parse_expr()?)
        } else {
            None
        };
        Ok(Update { table, assignments, filter })
    }

    fn parse_delete(&mut self) -> SqlResult<Delete> {
        self.expect_keyword("DELETE")?;
        self.expect_keyword("FROM")?;
        let table = self.parse_ident()?;
        let filter = if self.eat_keyword("WHERE") {
            Some(self.parse_expr()?)
        } else {
            None
        };
        Ok(Delete { table, filter })
    }

    fn parse_select(&mut self) -> SqlResult<Select> {
        self.expect_keyword("SELECT")?;
        let mut items = Vec::new();
        if self.eat_punct("*") {
            items.push(SelectItem { expr: None, alias: None });
        } else {
            loop {
                let expr = self.parse_expr()?;
                let alias = self.parse_optional_alias()?;
                items.push(SelectItem { expr: Some(expr), alias });
                if self.eat_punct(",") {
                    continue;
                }
                break;
            }
        }
        self.expect_keyword("FROM")?;
        let table = self.parse_ident()?;
        let filter = if self.eat_keyword("WHERE") {
            Some(self.parse_expr()?)
        } else {
            None
        };
        let mut order_by = Vec::new();
        if self.eat_keyword("ORDER") {
            self.expect_keyword("BY")?;
            loop {
                let e = self.parse_expr()?;
                let term = if self.eat_keyword("DESC") {
                    OrderTerm::Desc
                } else {
                    self.eat_keyword("ASC");
                    OrderTerm::Asc
                };
                order_by.push((e, term));
                if self.eat_punct(",") {
                    continue;
                }
                break;
            }
        }
        let limit = if self.eat_keyword("LIMIT") {
            match self.next() {
                Token::Int(i) if i >= 0 => Some(i as usize),
                other => {
                    return Err(SqlError::syntax(format!(
                        "LIMIT expects a non-negative integer but found {}",
                        describe(&other)
                    )))
                }
            }
        } else {
            None
        };
        Ok(Select { items, table, filter, order_by, limit })
    }

    fn parse_optional_alias(&mut self) -> SqlResult<Option<String>> {
        if self.eat_keyword("AS") {
            return Ok(Some(self.parse_ident()?));
        }
        if let Token::Word(w) = self.peek() {
            let upper = w.to_ascii_uppercase();
            const CLAUSES: [&str; 9] = [
                "FROM", "WHERE", "ORDER", "LIMIT", "GROUP", "HAVING", "JOIN", "ON", "AS",
            ];
            if !CLAUSES.contains(&upper.as_str()) {
                let name = w.clone();
                self.pos += 1;
                return Ok(Some(name));
            }
        }
        Ok(None)
    }

    fn parse_expr(&mut self) -> SqlResult<Expr> {
        let mut left = self.parse_and()?;
        while self.peek().is_keyword("OR") {
            self.pos += 1;
            let right = self.parse_and()?;
            left = Expr::BoolOp(Box::new(left), BoolOp::Or, Box::new(right));
        }
        Ok(left)
    }

    fn parse_and(&mut self) -> SqlResult<Expr> {
        let mut left = self.parse_predicate()?;
        while self.peek().is_keyword("AND") {
            self.pos += 1;
            let right = self.parse_predicate()?;
            left = Expr::BoolOp(Box::new(left), BoolOp::And, Box::new(right));
        }
        Ok(left)
    }

    fn parse_predicate(&mut self) -> SqlResult<Expr> {
        let left = self.parse_primary()?;
        let op = match self.peek() {
            Token::Punct(p) => match p.as_str() {
                "=" => Some(CmpOp::Eq),
                "<" => Some(CmpOp::Lt),
                "<=" => Some(CmpOp::LtEq),
                ">" => Some(CmpOp::Gt),
                ">=" => Some(CmpOp::GtEq),
                "!=" => Some(CmpOp::Ne),
                _ => None,
            },
            _ => None,
        };
        match op {
            Some(op) => {
                self.pos += 1;
                let right = self.parse_primary()?;
                Ok(Expr::Compare(Box::new(left), op, Box::new(right)))
            }
            None => Ok(left),
        }
    }

    fn parse_primary(&mut self) -> SqlResult<Expr> {
        match self.peek().clone() {
            Token::Int(i) => {
                self.pos += 1;
                Ok(Expr::Int(i))
            }
            Token::Num(n) => {
                self.pos += 1;
                Ok(Expr::Num(n))
            }
            Token::Str(s) => {
                self.pos += 1;
                Ok(Expr::Str(s))
            }
            Token::Ident(name) => {
                self.pos += 1;
                Ok(Expr::Column(name))
            }
            Token::Word(w) => {
                let upper = w.to_ascii_uppercase();
                if upper == "L2_DISTANCE" || upper == "EUCLIDEAN_DISTANCE" {
                    self.pos += 1;
                    self.expect_punct("(")?;
                    let a = self.parse_primary()?;
                    self.expect_punct(",")?;
                    let b = self.parse_primary()?;
                    self.expect_punct(")")?;
                    return Ok(Expr::L2Distance(Box::new(a), Box::new(b)));
                }
                if upper == "NULL" {
                    return Err(SqlError::not_supported(
                        "NULL values are not supported by MiniVectorDB",
                    ));
                }
                self.pos += 1;
                Ok(Expr::Column(w))
            }
            Token::Punct(p) if p == "(" => Err(SqlError::not_supported(
                "parenthesised expressions are not supported by MiniVectorDB",
            )),
            other => Err(SqlError::syntax(format!(
                "unexpected token {} in expression",
                describe(&other)
            ))),
        }
    }
}

fn describe(t: &Token) -> String {
    match t {
        Token::Word(w) => format!("'{}'", w),
        Token::Int(i) => format!("'{}'", i),
        Token::Num(n) => format!("'{}'", n),
        Token::Str(s) => format!("string '{}'", s),
        Token::Ident(s) => format!("identifier '{}'", s),
        Token::Punct(p) => format!("'{}'", p),
        Token::Eof => "end of statement".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_create_table() {
        let s = parse(
            "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(32), embedding VECTOR(4))",
        )
        .unwrap();
        match s {
            Statement::CreateTable(ct) => {
                assert_eq!(ct.table, "docs");
                assert_eq!(ct.columns.len(), 3);
                assert!(ct.columns[0].primary_key);
                assert_eq!(ct.columns[2].ty, ColumnType::Vector(4));
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn rejects_null() {
        assert!(parse("SELECT NULL FROM t").is_err());
    }

    #[test]
    fn rejects_alter() {
        assert!(parse("ALTER TABLE t ADD COLUMN c INT").is_err());
    }

    #[test]
    fn parses_insert_multiple_rows() {
        let s = parse(
            "INSERT INTO docs VALUES (1, 'a', '[1,0,0,0]'), (2, 'b', '[0,1,0,0]')",
        )
        .unwrap();
        match s {
            Statement::Insert(i) => assert_eq!(i.rows.len(), 2),
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn parses_select_with_filter_order_limit() {
        let s = parse(
            "SELECT id, L2_DISTANCE(embedding, '[0.9,0.1,0,0]') AS distance FROM docs \
             WHERE category = 'database' ORDER BY distance LIMIT 2",
        )
        .unwrap();
        match s {
            Statement::Select(sel) => {
                assert_eq!(sel.items.len(), 2);
                assert_eq!(sel.items[1].alias.as_deref(), Some("distance"));
                assert!(sel.filter.is_some());
                assert_eq!(sel.order_by.len(), 1);
                assert_eq!(sel.limit, Some(2));
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn parses_mode_switch() {
        assert_eq!(parse("SET mode = ann").unwrap(), Statement::SetMode { ann: true });
        assert_eq!(parse("USE EXACT").unwrap(), Statement::SetMode { ann: false });
    }

    #[test]
    fn parses_update_and_delete() {
        assert!(matches!(
            parse("UPDATE docs SET embedding = '[1,0,0,0]' WHERE id = 1").unwrap(),
            Statement::Update(_)
        ));
        assert!(matches!(
            parse("DELETE FROM docs WHERE id = 2").unwrap(),
            Statement::Delete(_)
        ));
    }
}
