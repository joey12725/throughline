use std::sync::Arc;

use serde_json::Value;

use crate::error::{Error, Result};

type CompilerFn = Arc<dyn Fn(&mut ParamCollector) -> Result<String> + Send + Sync>;

pub(crate) fn is_ident(s: &str) -> bool {
    let mut chars = s.chars();
    match chars.next() {
        Some(c) if c.is_ascii_alphabetic() || c == '_' => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_alphanumeric() || c == '_')
}

pub(crate) fn quote_ident(name: &str) -> Result<String> {
    if name == "*" {
        return Ok("*".to_string());
    }
    let parts: Vec<&str> = name.split('.').collect();
    if parts.len() > 2 {
        return Err(Error::Query(format!("invalid column identifier: {name:?}")));
    }
    let mut out = Vec::with_capacity(parts.len());
    for (i, part) in parts.iter().enumerate() {
        if *part == "*" && i + 1 == parts.len() {
            out.push("*".to_string());
            continue;
        }
        if !is_ident(part) {
            return Err(Error::Query(format!("invalid SQL identifier: {part:?}")));
        }
        out.push(format!("\"{part}\""));
    }
    Ok(out.join("."))
}

fn validate_pg_type(pg_type: &str) -> Result<String> {
    let cleaned = pg_type
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_ascii_lowercase();
    let stripped = cleaned.strip_suffix("[]").unwrap_or(&cleaned);
    let valid = !stripped.is_empty()
        && !stripped.starts_with("reg")
        && stripped
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_lowercase())
        && stripped
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == ' ');
    if !valid {
        return Err(Error::Query(format!("unsupported cast type: {pg_type:?}")));
    }
    Ok(cleaned)
}

fn escape_like(s: &str) -> String {
    s.replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

/// Collects positional `$1..$N` parameter values during SQL compilation.
#[derive(Default)]
pub struct ParamCollector {
    pub values: Vec<Value>,
}

impl ParamCollector {
    pub fn add(&mut self, value: Value) -> String {
        self.values.push(value);
        format!("${}", self.values.len())
    }
}

/// Composable Snowpark-style SQL expression.
#[derive(Clone)]
pub struct Expr {
    compiler: CompilerFn,
    alias: Option<String>,
}

/// Ordering expression for `ORDER BY`.
#[derive(Clone)]
pub struct OrderExpr {
    expr: Expr,
    desc: bool,
    nulls_last: Option<bool>,
}

impl Expr {
    fn from_fn<F>(f: F) -> Self
    where
        F: Fn(&mut ParamCollector) -> Result<String> + Send + Sync + 'static,
    {
        Self {
            compiler: Arc::new(f),
            alias: None,
        }
    }

    pub(crate) fn compile(&self, params: &mut ParamCollector) -> Result<String> {
        (self.compiler)(params)
    }

    pub(crate) fn compile_select(&self, params: &mut ParamCollector) -> Result<String> {
        let sql = self.compile(params)?;
        if let Some(ref alias) = self.alias {
            let quoted = quote_ident(alias)?;
            Ok(format!("{sql} AS {quoted}"))
        } else {
            Ok(sql)
        }
    }

    pub fn alias(mut self, name: &str) -> Self {
        self.alias = Some(name.to_string());
        self
    }

    pub(crate) fn has_alias(&self) -> bool {
        self.alias.is_some()
    }

    pub fn asc(self) -> OrderExpr {
        OrderExpr {
            expr: self,
            desc: false,
            nulls_last: None,
        }
    }

    pub fn desc(self) -> OrderExpr {
        OrderExpr {
            expr: self,
            desc: true,
            nulls_last: None,
        }
    }

    fn bin(self, op: &'static str, rhs: Expr) -> Expr {
        Expr::from_fn(move |p| {
            let l = self.compile(p)?;
            let r = rhs.compile(p)?;
            Ok(format!("({l} {op} {r})"))
        })
    }

    pub fn eq<V: Into<Expr>>(self, rhs: V) -> Expr {
        self.bin("=", rhs.into())
    }

    pub fn neq<V: Into<Expr>>(self, rhs: V) -> Expr {
        self.bin("<>", rhs.into())
    }

    pub fn lt<V: Into<Expr>>(self, rhs: V) -> Expr {
        self.bin("<", rhs.into())
    }

    pub fn lte<V: Into<Expr>>(self, rhs: V) -> Expr {
        self.bin("<=", rhs.into())
    }

    pub fn gt<V: Into<Expr>>(self, rhs: V) -> Expr {
        self.bin(">", rhs.into())
    }

    pub fn gte<V: Into<Expr>>(self, rhs: V) -> Expr {
        self.bin(">=", rhs.into())
    }

    pub fn and(self, rhs: Expr) -> Expr {
        self.bin("AND", rhs)
    }

    pub fn or(self, rhs: Expr) -> Expr {
        self.bin("OR", rhs)
    }

    pub fn is_null(self) -> Expr {
        Expr::from_fn(move |p| Ok(format!("({} IS NULL)", self.compile(p)?)))
    }

    pub fn is_not_null(self) -> Expr {
        Expr::from_fn(move |p| Ok(format!("({} IS NOT NULL)", self.compile(p)?)))
    }

    pub fn is_in<I, V>(self, values: I) -> Expr
    where
        I: IntoIterator<Item = V>,
        V: Into<Value>,
    {
        let items: Vec<Value> = values.into_iter().map(Into::into).collect();
        if items.is_empty() {
            return Expr::from_fn(|_| Ok("FALSE".to_string()));
        }
        Expr::from_fn(move |p| {
            let lhs = self.compile(p)?;
            let placeholders: Vec<String> = items.iter().cloned().map(|v| p.add(v)).collect();
            Ok(format!("({lhs} IN ({}))", placeholders.join(", ")))
        })
    }

    pub fn between<A: Into<Expr>, B: Into<Expr>>(self, low: A, high: B) -> Expr {
        let lo = low.into();
        let hi = high.into();
        Expr::from_fn(move |p| {
            let lhs = self.compile(p)?;
            let l = lo.compile(p)?;
            let h = hi.compile(p)?;
            Ok(format!("({lhs} BETWEEN {l} AND {h})"))
        })
    }

    pub fn like(self, pattern: &str) -> Expr {
        self.bin("LIKE", lit(pattern))
    }

    pub fn ilike(self, pattern: &str) -> Expr {
        self.bin("ILIKE", lit(pattern))
    }

    pub fn contains(self, sub: &str) -> Expr {
        self.ilike(&format!("%{}%", escape_like(sub)))
    }

    pub fn starts_with(self, prefix: &str) -> Expr {
        self.ilike(&format!("{}%", escape_like(prefix)))
    }

    pub fn ends_with(self, suffix: &str) -> Expr {
        self.ilike(&format!("%{}", escape_like(suffix)))
    }

    /// Extract a JSONB object field as text (`col ->> $1`).
    pub fn json(self, key: &str) -> Expr {
        self.bin("->>", lit(key))
    }

    /// Extract a nested JSONB field (`col -> $1`).
    pub fn json_obj(self, key: &str) -> Expr {
        self.bin("->", lit(key))
    }

    pub fn cast(self, pg_type: &str) -> Expr {
        let target = pg_type.to_string();
        Expr::from_fn(move |p| {
            let safe = validate_pg_type(&target)?;
            let inner = self.compile(p)?;
            Ok(format!("CAST({inner} AS {safe})"))
        })
    }
}

impl OrderExpr {
    pub fn nulls_last(mut self) -> Self {
        self.nulls_last = Some(true);
        self
    }

    pub fn nulls_first(mut self) -> Self {
        self.nulls_last = Some(false);
        self
    }

    pub(crate) fn compile(&self, params: &mut ParamCollector) -> Result<String> {
        let mut sql = self.expr.compile(params)?;
        sql.push_str(if self.desc { " DESC" } else { " ASC" });
        match self.nulls_last {
            Some(true) => sql.push_str(" NULLS LAST"),
            Some(false) => sql.push_str(" NULLS FIRST"),
            None => {}
        }
        Ok(sql)
    }
}

impl std::ops::Not for Expr {
    type Output = Expr;

    fn not(self) -> Self::Output {
        Expr::from_fn(move |p| Ok(format!("(NOT {})", self.compile(p)?)))
    }
}

impl From<&str> for Expr {
    fn from(v: &str) -> Self {
        lit(v)
    }
}

impl From<String> for Expr {
    fn from(v: String) -> Self {
        lit(v)
    }
}

impl From<i64> for Expr {
    fn from(v: i64) -> Self {
        lit(v)
    }
}

impl From<i32> for Expr {
    fn from(v: i32) -> Self {
        lit(v)
    }
}

impl From<f64> for Expr {
    fn from(v: f64) -> Self {
        lit(v)
    }
}

impl From<bool> for Expr {
    fn from(v: bool) -> Self {
        lit(v)
    }
}

impl From<Value> for Expr {
    fn from(v: Value) -> Self {
        lit(v)
    }
}

/// Reference a column by name (`col("happened_at")` or `col("m.channel")`).
pub fn col(name: &str) -> Expr {
    let owned = name.to_string();
    Expr::from_fn(move |_| quote_ident(&owned))
}

/// Bind a literal value as a `$N` positional parameter.
pub fn lit<V: Into<Value>>(value: V) -> Expr {
    let val = value.into();
    if val.is_null() {
        return Expr::from_fn(|_| Ok("NULL".to_string()));
    }
    Expr::from_fn(move |p| Ok(p.add(val.clone())))
}

/// Raw SQL expression fragment with optional `?` placeholders bound in order.
pub fn raw<I, V>(sql_expr: &str, params: I) -> Expr
where
    I: IntoIterator<Item = V>,
    V: Into<Value>,
{
    let text = sql_expr.to_string();
    let values: Vec<Value> = params.into_iter().map(Into::into).collect();
    Expr::from_fn(move |p| {
        if values.is_empty() {
            return Ok(format!("({text})"));
        }
        let parts: Vec<&str> = text.split('?').collect();
        if parts.len() - 1 != values.len() {
            return Err(Error::Query(format!(
                "raw() expected {} params for '?' placeholders, got {}",
                parts.len() - 1,
                values.len()
            )));
        }
        let mut out = String::from("(");
        out.push_str(parts[0]);
        for (val, tail) in values.iter().zip(parts[1..].iter()) {
            out.push_str(&p.add(val.clone()));
            out.push_str(tail);
        }
        out.push(')');
        Ok(out)
    })
}

fn fn_call(name: &'static str, args: Vec<Expr>) -> Expr {
    Expr::from_fn(move |p| {
        let mut compiled = Vec::with_capacity(args.len());
        for a in &args {
            compiled.push(a.compile(p)?);
        }
        Ok(format!("{name}({})", compiled.join(", ")))
    })
}

pub fn count(column: &str) -> Expr {
    if column.is_empty() || column == "*" {
        return Expr::from_fn(|_| Ok("count(*)".to_string()));
    }
    fn_call("count", vec![col(column)])
}

pub fn count_distinct(column: &str) -> Expr {
    let c = col(column);
    Expr::from_fn(move |p| Ok(format!("count(DISTINCT {})", c.compile(p)?)))
}

pub fn sum(column: &str) -> Expr {
    fn_call("sum", vec![col(column)])
}

pub fn avg(column: &str) -> Expr {
    fn_call("avg", vec![col(column)])
}

pub fn min(column: &str) -> Expr {
    fn_call("min", vec![col(column)])
}

pub fn max(column: &str) -> Expr {
    fn_call("max", vec![col(column)])
}

pub fn lower(column: &str) -> Expr {
    fn_call("lower", vec![col(column)])
}

pub fn upper(column: &str) -> Expr {
    fn_call("upper", vec![col(column)])
}

pub fn date_trunc(unit: &str, column: &str) -> Expr {
    fn_call("date_trunc", vec![lit(unit), col(column)])
}

pub fn coalesce(exprs: Vec<Expr>) -> Expr {
    fn_call("coalesce", exprs)
}

pub fn string_agg(column: &str, delimiter: &str) -> Expr {
    fn_call("string_agg", vec![col(column), lit(delimiter)])
}

pub fn array_agg(column: &str) -> Expr {
    fn_call("array_agg", vec![col(column)])
}

pub fn now() -> Expr {
    fn_call("now", vec![])
}
