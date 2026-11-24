use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::error::{Error, Result};
use crate::expr::{col, count, is_ident, quote_ident, Expr, OrderExpr, ParamCollector};
use crate::session::{Row, RowStream, Session, StreamOptions};

/// SQL join kind for [`DataFrame::join`].
#[derive(Clone, Copy, Debug)]
pub enum JoinType {
    Inner,
    Left,
    Right,
    Full,
}

impl JoinType {
    fn as_sql(self) -> &'static str {
        match self {
            JoinType::Inner => "INNER JOIN",
            JoinType::Left => "LEFT JOIN",
            JoinType::Right => "RIGHT JOIN",
            JoinType::Full => "FULL JOIN",
        }
    }
}

#[derive(Clone)]
struct JoinSpec {
    right: DataFrame,
    left_col: String,
    right_col: String,
    how: JoinType,
    right_alias: String,
}

/// Immutable, lazy pushdown query over Throughline `api.*` views and functions.
#[derive(Clone)]
pub struct DataFrame {
    session: Session,
    source_sql: String,
    source_params: Vec<Value>,
    source_alias: Option<String>,
    selects: Vec<Expr>,
    filters: Vec<Expr>,
    group_by: Vec<Expr>,
    having: Vec<Expr>,
    joins: Vec<JoinSpec>,
    orders: Vec<OrderExpr>,
    distinct: bool,
    limit: Option<usize>,
    offset: Option<usize>,
}

/// Result of [`DataFrame::group_by`]; call [`GroupedDataFrame::agg`] or [`GroupedDataFrame::count`].
pub struct GroupedDataFrame {
    df: DataFrame,
    group_cols: Vec<Expr>,
}

fn rebind_params(sql_text: &str, params: &[Value], collector: &mut ParamCollector) -> String {
    if params.is_empty() {
        return sql_text.to_string();
    }
    let base = collector.values.len();
    collector.values.extend(params.iter().cloned());
    if base == 0 {
        return sql_text.to_string();
    }
    // Shift $N references by `base` without pulling in a regex crate.
    let bytes = sql_text.as_bytes();
    let mut out = String::with_capacity(sql_text.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'$' {
            let start = i + 1;
            let mut j = start;
            while j < bytes.len() && bytes[j].is_ascii_digit() {
                j += 1;
            }
            if j > start {
                if let Ok(n) = sql_text[start..j].parse::<usize>() {
                    out.push('$');
                    out.push_str(&(n + base).to_string());
                    i = j;
                    continue;
                }
            }
        }
        let ch = sql_text[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

impl DataFrame {
    pub(crate) fn new(
        session: Session,
        source_sql: String,
        source_params: Vec<Value>,
        source_alias: Option<String>,
    ) -> Self {
        Self {
            session,
            source_sql,
            source_params,
            source_alias,
            selects: Vec::new(),
            filters: Vec::new(),
            group_by: Vec::new(),
            having: Vec::new(),
            joins: Vec::new(),
            orders: Vec::new(),
            distinct: false,
            limit: None,
            offset: None,
        }
    }

    fn needs_subquery(&self) -> bool {
        !self.selects.is_empty()
            || !self.group_by.is_empty()
            || self.distinct
            || self.limit.is_some()
            || self.offset.is_some()
    }

    fn needs_subquery_for_filter(&self) -> bool {
        self.selects.iter().any(|c| c.has_alias())
            || !self.group_by.is_empty()
            || self.distinct
            || self.limit.is_some()
            || self.offset.is_some()
    }

    fn wrap(&self, alias: &str) -> Result<Self> {
        let (sql, params) = self.to_sql()?;
        Ok(DataFrame::new(
            self.session.clone(),
            format!("({sql})"),
            params,
            Some(alias.to_string()),
        ))
    }

    pub fn alias(&self, name: &str) -> Result<Self> {
        if !is_ident(name) {
            return Err(Error::Query(format!("invalid alias: {name:?}")));
        }
        if self.needs_subquery() || !self.filters.is_empty() || !self.joins.is_empty() {
            return self.wrap(name);
        }
        let mut next = self.clone();
        next.source_alias = Some(name.to_string());
        Ok(next)
    }

    pub fn select(&self, columns: &[&str]) -> Self {
        let exprs: Vec<Expr> = columns.iter().map(|c| col(c)).collect();
        self.select_expr(exprs)
    }

    pub fn select_expr(&self, exprs: Vec<Expr>) -> Self {
        let mut base = if self.needs_subquery() {
            self.wrap("_q").unwrap_or_else(|_| self.clone())
        } else {
            self.clone()
        };
        base.selects = exprs;
        base
    }

    pub fn with_column(&self, name: &str, expr: Expr) -> Self {
        let mut base = if self.needs_subquery() {
            self.wrap("_q").unwrap_or_else(|_| self.clone())
        } else {
            self.clone()
        };
        if base.selects.is_empty() {
            base.selects.push(col("*"));
        }
        base.selects.push(expr.alias(name));
        base
    }

    pub fn filter(&self, condition: Expr) -> Self {
        let mut base = if self.needs_subquery_for_filter() {
            self.wrap("_q").unwrap_or_else(|_| self.clone())
        } else {
            self.clone()
        };
        base.filters.push(condition);
        base
    }

    pub fn group_by(&self, columns: &[&str]) -> GroupedDataFrame {
        let exprs: Vec<Expr> = columns.iter().map(|c| col(c)).collect();
        self.group_by_expr(exprs)
    }

    pub fn group_by_expr(&self, exprs: Vec<Expr>) -> GroupedDataFrame {
        let base = if self.needs_subquery() {
            self.wrap("_q").unwrap_or_else(|_| self.clone())
        } else {
            self.clone()
        };
        GroupedDataFrame {
            df: base,
            group_cols: exprs,
        }
    }

    pub fn join(&self, other: &DataFrame, left_col: &str, right_col: &str, how: JoinType) -> Self {
        let mut left = if self.needs_subquery() {
            self.wrap("l").unwrap_or_else(|_| self.clone())
        } else {
            let mut c = self.clone();
            if c.source_alias.is_none() {
                c.source_alias = Some("l".to_string());
            }
            c
        };
        let mut r_alias = other
            .source_alias
            .clone()
            .unwrap_or_else(|| "r".to_string());
        if left.source_alias.as_deref() == Some(r_alias.as_str()) {
            r_alias = format!("{r_alias}_{}", left.joins.len() + 1);
        }
        left.joins.push(JoinSpec {
            right: other.clone(),
            left_col: left_col.to_string(),
            right_col: right_col.to_string(),
            how,
            right_alias: r_alias,
        });
        left
    }

    pub fn order_by(&self, orders: Vec<OrderExpr>) -> Self {
        let mut next = self.clone();
        next.orders = orders;
        next
    }

    pub fn distinct(&self) -> Self {
        let mut next = self.clone();
        next.distinct = true;
        next
    }

    pub fn limit(&self, n: usize) -> Self {
        let mut next = self.clone();
        next.limit = Some(n);
        next
    }

    pub fn offset(&self, n: usize) -> Self {
        let mut next = self.clone();
        next.offset = Some(n);
        next
    }

    fn compile_into(&self, collector: &mut ParamCollector) -> Result<String> {
        let source_sql = rebind_params(&self.source_sql, &self.source_params, collector);

        let proj = if self.selects.is_empty() {
            "*".to_string()
        } else {
            let mut cols = Vec::with_capacity(self.selects.len());
            for c in &self.selects {
                cols.push(c.compile_select(collector)?);
            }
            cols.join(", ")
        };

        let distinct_kw = if self.distinct { "DISTINCT " } else { "" };
        let from_clause = match &self.source_alias {
            Some(alias) => format!("{source_sql} AS {}", quote_ident(alias)?),
            None => source_sql,
        };

        let mut parts = vec![format!("SELECT {distinct_kw}{proj} FROM {from_clause}")];

        for j in &self.joins {
            let r_sql = if j.right.needs_subquery()
                || !j.right.filters.is_empty()
                || !j.right.joins.is_empty()
            {
                format!("({})", j.right.compile_into(collector)?)
            } else {
                rebind_params(&j.right.source_sql, &j.right.source_params, collector)
            };
            let l_alias = quote_ident(self.source_alias.as_deref().unwrap_or("l"))?;
            let r_alias = quote_ident(&j.right_alias)?;
            let lc = quote_ident(&j.left_col)?;
            let rc = quote_ident(&j.right_col)?;
            parts.push(format!(
                "{} {r_sql} AS {r_alias} ON {l_alias}.{lc} = {r_alias}.{rc}",
                j.how.as_sql()
            ));
        }

        if !self.filters.is_empty() {
            let mut conds = Vec::with_capacity(self.filters.len());
            for f in &self.filters {
                conds.push(f.compile(collector)?);
            }
            parts.push(format!("WHERE {}", conds.join(" AND ")));
        }

        if !self.group_by.is_empty() {
            if self.selects.len() >= self.group_by.len() {
                let positions: Vec<String> =
                    (1..=self.group_by.len()).map(|i| i.to_string()).collect();
                parts.push(format!("GROUP BY {}", positions.join(", ")));
            } else {
                let mut gbs = Vec::with_capacity(self.group_by.len());
                for g in &self.group_by {
                    gbs.push(g.compile(collector)?);
                }
                parts.push(format!("GROUP BY {}", gbs.join(", ")));
            }
        }

        if !self.having.is_empty() {
            let mut hvs = Vec::with_capacity(self.having.len());
            for h in &self.having {
                hvs.push(h.compile(collector)?);
            }
            parts.push(format!("HAVING {}", hvs.join(" AND ")));
        }

        if !self.orders.is_empty() {
            let mut ords = Vec::with_capacity(self.orders.len());
            for o in &self.orders {
                ords.push(o.compile(collector)?);
            }
            parts.push(format!("ORDER BY {}", ords.join(", ")));
        }

        if let Some(limit) = self.limit {
            parts.push(format!("LIMIT {limit}"));
        }
        if let Some(offset) = self.offset {
            parts.push(format!("OFFSET {offset}"));
        }

        Ok(parts.join(" "))
    }

    /// Compile this lazy DataFrame into `(statement, params)`.
    pub fn to_sql(&self) -> Result<(String, Vec<Value>)> {
        let mut collector = ParamCollector::default();
        let sql = self.compile_into(&mut collector)?;
        Ok((sql, collector.values))
    }

    /// Execute and return untyped JSON rows (automatically streams when unbounded or >10k).
    pub fn collect(&self) -> Result<Vec<Row>> {
        self.collect_as::<Row>()
    }

    /// Execute and deserialize every row directly into `T`.
    pub fn collect_as<T: DeserializeOwned>(&self) -> Result<Vec<T>> {
        let (sql, params) = self.to_sql()?;
        let use_stream = match self.limit {
            None => true,
            Some(n) => n > 10_000,
        };
        if use_stream {
            let opts = StreamOptions {
                row_cap: self.limit.unwrap_or(100_000),
                ..StreamOptions::default()
            };
            self.session.stream_as::<T>(&sql, &params, Some(&opts))?.collect()
        } else {
            self.session.query_as::<T>(&sql, &params)
        }
    }

    /// Lazily stream rows as `Row` via `POST /v1/sql/stream` (NDJSON).
    pub fn stream(&self, opts: Option<&StreamOptions>) -> Result<RowStream<Row>> {
        self.stream_as::<Row>(opts)
    }

    /// Lazily stream typed structs `T` via `POST /v1/sql/stream` (NDJSON).
    pub fn stream_as<T: DeserializeOwned>(
        &self,
        opts: Option<&StreamOptions>,
    ) -> Result<RowStream<T>> {
        let (sql, params) = self.to_sql()?;
        self.session.stream_as::<T>(&sql, &params, opts)
    }

    /// Push down `LIMIT 1` and return the first row, if any.
    pub fn first(&self) -> Result<Option<Row>> {
        let mut rows = self.limit(1).collect()?;
        Ok(if rows.is_empty() {
            None
        } else {
            Some(rows.remove(0))
        })
    }

    /// Push down `SELECT count(*) AS n` to PostgreSQL.
    pub fn count(&self) -> Result<i64> {
        #[derive(serde::Deserialize)]
        struct CountRow {
            n: i64,
        }
        let (sql, params) = self.to_sql()?;
        let rows: Vec<CountRow> = self
            .session
            .query_as(&format!("SELECT count(*) AS n FROM ({sql}) AS _cnt"), &params)?;
        Ok(rows.first().map(|r| r.n).unwrap_or(0))
    }

    /// Stream RFC 4180 CSV text via `POST /v1/sql/stream`.
    pub fn to_csv(&self, opts: Option<&StreamOptions>) -> Result<String> {
        let (sql, params) = self.to_sql()?;
        let mut reader = self.session.stream_csv(&sql, &params, opts)?;
        let mut out = String::new();
        reader.read_to_string(&mut out)?;
        Ok(out)
    }
}

impl GroupedDataFrame {
    pub fn agg(mut self, aggregations: Vec<Expr>) -> DataFrame {
        let mut selects = self.group_cols.clone();
        selects.extend(aggregations);
        self.df.group_by = self.group_cols;
        self.df.selects = selects;
        self.df
    }

    pub fn count(self) -> DataFrame {
        self.agg(vec![count("*").alias("count")])
    }

    pub fn having(mut self, condition: Expr) -> Self {
        self.df.having.push(condition);
        self
    }
}
