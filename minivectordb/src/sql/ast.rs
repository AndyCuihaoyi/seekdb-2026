//! AST for the supported SQL subset.

#[derive(Debug, Clone, PartialEq)]
pub enum ColumnType {
    BigInt,
    /// VARCHAR(n): n is the maximum number of Unicode characters.
    VarChar(usize),
    /// VECTOR(n): n is the fixed dimension.
    Vector(usize),
}

impl ColumnType {
    pub fn type_name(&self) -> String {
        match self {
            ColumnType::BigInt => "BIGINT".to_string(),
            ColumnType::VarChar(n) => format!("VARCHAR({n})"),
            ColumnType::Vector(n) => format!("VECTOR({n})"),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct ColumnDef {
    pub name: String,
    pub ty: ColumnType,
    pub primary_key: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BoolOp {
    And,
    Or,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Expr {
    Column(String),
    Int(i64),
    /// Numeric literal kept as text; converted to float32 at evaluation time.
    Num(String),
    Str(String),
    /// `L2_DISTANCE(vector_column, vector_literal)`
    L2Distance(Box<Expr>, Box<Expr>),
    /// `left <op> right`
    Compare(Box<Expr>, CmpOp, Box<Expr>),
    /// Boolean composition of two predicates.
    BoolOp(Box<Expr>, BoolOp, Box<Expr>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CmpOp {
    Eq,
    Lt,
    LtEq,
    Gt,
    GtEq,
    Ne,
}

impl CmpOp {
    pub fn sql(&self) -> &'static str {
        match self {
            CmpOp::Eq => "=",
            CmpOp::Lt => "<",
            CmpOp::LtEq => "<=",
            CmpOp::Gt => ">",
            CmpOp::GtEq => ">=",
            CmpOp::Ne => "!=",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderTerm {
    Asc,
    Desc,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SelectItem {
    /// `None` means `*`.
    pub expr: Option<Expr>,
    pub alias: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Select {
    pub items: Vec<SelectItem>,
    pub table: String,
    pub filter: Option<Expr>,
    pub order_by: Vec<(Expr, OrderTerm)>,
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Insert {
    pub table: String,
    /// Each row is a list of value expressions.
    pub rows: Vec<Vec<Expr>>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Update {
    pub table: String,
    /// Column name to new value.
    pub assignments: Vec<(String, Expr)>,
    pub filter: Option<Expr>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Delete {
    pub table: String,
    pub filter: Option<Expr>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CreateTable {
    pub table: String,
    pub columns: Vec<ColumnDef>,
    /// Whether the statement carried `IF NOT EXISTS`.
    ///
    /// The flag makes the statement idempotent: creating a table that
    /// already exists is a success instead of an error.
    pub if_not_exists: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct CreateVectorIndex {
    pub index: String,
    pub table: String,
    pub column: String,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Statement {
    CreateTable(CreateTable),
    CreateVectorIndex(CreateVectorIndex),
    Insert(Insert),
    Select(Select),
    Update(Update),
    Delete(Delete),
    Begin,
    Commit,
    Rollback,
    /// Mode switch, e.g. `SET mode = ann` / `USE ANN`.
    SetMode { ann: bool },
}
