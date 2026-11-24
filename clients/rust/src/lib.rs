//! Snowpark-style programmatic Rust client for Throughline.
//!
//! Build lazy, SQL-injection-safe queries with [`Session::table`], [`Session::channel`],
//! [`Session::lens`], and [`col`]/[`lit`] expressions that push down to a single validated
//! PostgreSQL `SELECT` over `api.*` views, or call high-level retrieval methods with full
//! MCP parity ([`Session::find`], [`Session::timeline`], [`Session::orient`],
//! [`Session::person`], [`Session::describe`], [`Session::sql`], [`Session::stream_sql`]).

pub mod dataframe;
pub mod error;
pub mod expr;
pub mod session;

pub use dataframe::{DataFrame, GroupedDataFrame, JoinType};
pub use error::{Error, Result};
pub use expr::{
    array_agg, avg, coalesce, col, count, count_distinct, date_trunc, lit, lower, max, min, now,
    raw, string_agg, sum, upper, Expr, OrderExpr,
};
pub use session::{
    Feed, FindRequest, OrientRequest, RecallRequest, Row, RowStream, Session, SessionBuilder,
    SqlOptions, SqlResult, StreamOptions, TimelineRequest,
};
