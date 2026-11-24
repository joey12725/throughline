"""Composable SQL expression builder (Snowpark-style col / lit / F).

Expressions compile to PostgreSQL SQL with positional $1, $2, ... parameters,
keeping every user value out of SQL text while staying strictly within
Throughline's read-only AST allow-list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_ ]*(\[\])?$")


def quote_ident(name: str) -> str:
    """Validate and quote a SQL identifier (supports table.column and *)."""
    if name == "*":
        return "*"
    parts = name.split(".")
    if len(parts) > 2:
        raise ValueError(f"invalid column identifier: {name!r}")
    out = []
    for i, part in enumerate(parts):
        if part == "*" and i == len(parts) - 1:
            out.append("*")
            continue
        if not _IDENT_RE.fullmatch(part):
            raise ValueError(f"invalid SQL identifier: {part!r}")
        out.append(f'"{part}"')
    return ".".join(out)


def validate_pg_type(pg_type: str) -> str:
    cleaned = " ".join(pg_type.strip().lower().split())
    if not _TYPE_RE.fullmatch(cleaned) or cleaned.startswith("reg"):
        raise ValueError(f"unsupported cast type: {pg_type!r}")
    return cleaned


@dataclass
class ParamCollector:
    """Collects bound values and assigns 1-based PostgreSQL $N placeholders."""

    values: list[Any] = field(default_factory=list)

    def add(self, value: Any) -> str:
        self.values.append(value)
        return f"${len(self.values)}"


class Expr:
    """Base SQL expression node."""

    def compile(self, params: ParamCollector) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


def _as_expr(value: Any) -> Column:
    if isinstance(value, Column):
        return value
    return lit(value)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Column(Expr):
    """A composable column or scalar expression, modeled after Snowpark Column."""

    def __init__(
        self,
        compiler,
        *,
        alias_name: str | None = None,
    ):
        self._compiler = compiler
        self._alias = alias_name

    def compile(self, params: ParamCollector) -> str:
        return self._compiler(params)

    def compile_select(self, params: ParamCollector) -> str:
        sql = self.compile(params)
        if self._alias is not None:
            return f"{sql} AS {quote_ident(self._alias)}"
        return sql

    # --- Aliasing & ordering ---

    def alias(self, name: str) -> Column:
        quote_ident(name)
        return Column(self._compiler, alias_name=name)

    as_ = alias

    def asc(self, *, nulls_last: bool | None = None) -> OrderExpr:
        return OrderExpr(self, desc=False, nulls_last=nulls_last)

    def desc(self, *, nulls_last: bool | None = None) -> OrderExpr:
        return OrderExpr(self, desc=True, nulls_last=nulls_last)

    def asc_nulls_last(self) -> OrderExpr:
        return self.asc(nulls_last=True)

    def desc_nulls_last(self) -> OrderExpr:
        return self.desc(nulls_last=True)

    # --- Comparisons ---

    def _bin(self, op: str, other: Any) -> Column:
        rhs = _as_expr(other)
        return Column(lambda p: f"({self.compile(p)} {op} {rhs.compile(p)})")

    def __eq__(self, other: Any) -> Column:  # type: ignore[override]
        if other is None:
            return self.is_null()
        return self._bin("=", other)

    def __ne__(self, other: Any) -> Column:  # type: ignore[override]
        if other is None:
            return self.is_not_null()
        return self._bin("<>", other)

    def __lt__(self, other: Any) -> Column:
        return self._bin("<", other)

    def __le__(self, other: Any) -> Column:
        return self._bin("<=", other)

    def __gt__(self, other: Any) -> Column:
        return self._bin(">", other)

    def __ge__(self, other: Any) -> Column:
        return self._bin(">=", other)

    # --- Logical ---

    def __and__(self, other: Column) -> Column:
        rhs = _as_expr(other)
        return Column(lambda p: f"({self.compile(p)} AND {rhs.compile(p)})")

    def __or__(self, other: Column) -> Column:
        rhs = _as_expr(other)
        return Column(lambda p: f"({self.compile(p)} OR {rhs.compile(p)})")

    def __invert__(self) -> Column:
        return Column(lambda p: f"(NOT {self.compile(p)})")

    # --- Arithmetic ---

    def __add__(self, other: Any) -> Column:
        return self._bin("+", other)

    def __sub__(self, other: Any) -> Column:
        return self._bin("-", other)

    def __mul__(self, other: Any) -> Column:
        return self._bin("*", other)

    def __truediv__(self, other: Any) -> Column:
        return self._bin("/", other)

    # --- Predicates ---

    def is_null(self) -> Column:
        return Column(lambda p: f"({self.compile(p)} IS NULL)")

    def is_not_null(self) -> Column:
        return Column(lambda p: f"({self.compile(p)} IS NOT NULL)")

    def isin(self, values: list[Any] | tuple[Any, ...] | set[Any]) -> Column:
        items = list(values)
        if not items:
            return Column(lambda _p: "FALSE")

        def _compile(p: ParamCollector) -> str:
            placeholders = ", ".join(p.add(v) for v in items)
            return f"({self.compile(p)} IN ({placeholders}))"

        return Column(_compile)

    def not_in(self, values: list[Any] | tuple[Any, ...] | set[Any]) -> Column:
        items = list(values)
        if not items:
            return Column(lambda _p: "TRUE")

        def _compile(p: ParamCollector) -> str:
            placeholders = ", ".join(p.add(v) for v in items)
            return f"({self.compile(p)} NOT IN ({placeholders}))"

        return Column(_compile)

    def between(self, lower: Any, upper: Any) -> Column:
        lo = _as_expr(lower)
        hi = _as_expr(upper)
        return Column(
            lambda p: f"({self.compile(p)} BETWEEN {lo.compile(p)} AND {hi.compile(p)})"
        )

    def like(self, pattern: str) -> Column:
        pat = lit(pattern)
        return Column(lambda p: f"({self.compile(p)} LIKE {pat.compile(p)})")

    def ilike(self, pattern: str) -> Column:
        pat = lit(pattern)
        return Column(lambda p: f"({self.compile(p)} ILIKE {pat.compile(p)})")

    def contains(self, substring: str, *, case_sensitive: bool = False) -> Column:
        pat = f"%{_escape_like(substring)}%"
        return self.like(pat) if case_sensitive else self.ilike(pat)

    def startswith(self, prefix: str, *, case_sensitive: bool = False) -> Column:
        pat = f"{_escape_like(prefix)}%"
        return self.like(pat) if case_sensitive else self.ilike(pat)

    def endswith(self, suffix: str, *, case_sensitive: bool = False) -> Column:
        pat = f"%{_escape_like(suffix)}"
        return self.like(pat) if case_sensitive else self.ilike(pat)

    # --- Casts & JSONB field access ---

    def cast(self, pg_type: str) -> Column:
        safe_type = validate_pg_type(pg_type)
        return Column(lambda p: f"CAST({self.compile(p)} AS {safe_type})")

    def json(self, key: str) -> Column:
        """Extract a JSONB object field as text: (col ->> $1)."""
        key_expr = lit(key)
        return Column(lambda p: f"({self.compile(p)} ->> {key_expr.compile(p)})")

    def json_obj(self, key: str) -> Column:
        """Extract a nested JSONB value: (col -> $1)."""
        key_expr = lit(key)
        return Column(lambda p: f"({self.compile(p)} -> {key_expr.compile(p)})")


@dataclass(frozen=True)
class OrderExpr:
    expr: Column
    desc: bool = False
    nulls_last: bool | None = None

    def compile(self, params: ParamCollector) -> str:
        sql = self.expr.compile(params)
        sql += " DESC" if self.desc else " ASC"
        if self.nulls_last is True:
            sql += " NULLS LAST"
        elif self.nulls_last is False:
            sql += " NULLS FIRST"
        return sql


def col(name: str) -> Column:
    """Reference a column by name (e.g. col('channel'), col('m.happened_at'))."""
    quoted = quote_ident(name)
    return Column(lambda _p: quoted)


def lit(value: Any) -> Column:
    """Bind a Python scalar or list value as a PostgreSQL $N parameter."""
    if value is None:
        return Column(lambda _p: "NULL")
    return Column(lambda p: p.add(value))


def raw(sql_expr: str, *params: Any) -> Column:
    """Escape hatch for a raw SQL expression fragment.

    If positional params are provided, occurrences of '?' in sql_expr are bound
    in order as $N parameters.
    """
    if not params:
        return Column(lambda _p: f"({sql_expr})")

    def _compile(p: ParamCollector) -> str:
        parts = sql_expr.split("?")
        if len(parts) - 1 != len(params):
            raise ValueError(
                f"raw() expected {len(parts) - 1} params for '?' placeholders, got {len(params)}"
            )
        out = [parts[0]]
        for val, tail in zip(params, parts[1:]):
            out.append(p.add(val))
            out.append(tail)
        return "(" + "".join(out) + ")"

    return Column(_compile)


def _col_or_expr(value: str | Column) -> Column:
    return value if isinstance(value, Column) else col(value)


def _fn(name: str, *args: str | Column) -> Column:
    exprs = [_col_or_expr(a) for a in args]
    return Column(lambda p: f"{name}({', '.join(e.compile(p) for e in exprs)})")


class F:
    """Snowpark-style SQL function helpers (all within Throughline's allow-list)."""

    col = staticmethod(col)
    lit = staticmethod(lit)
    raw = staticmethod(raw)

    @staticmethod
    def count(column: str | Column = "*") -> Column:
        if column == "*":
            return Column(lambda _p: "count(*)")
        c = _col_or_expr(column)
        return Column(lambda p: f"count({c.compile(p)})")

    @staticmethod
    def count_distinct(column: str | Column) -> Column:
        c = _col_or_expr(column)
        return Column(lambda p: f"count(DISTINCT {c.compile(p)})")

    @staticmethod
    def sum(column: str | Column) -> Column:
        return _fn("sum", column)

    @staticmethod
    def avg(column: str | Column) -> Column:
        return _fn("avg", column)

    @staticmethod
    def min(column: str | Column) -> Column:
        return _fn("min", column)

    @staticmethod
    def max(column: str | Column) -> Column:
        return _fn("max", column)

    @staticmethod
    def date_trunc(unit: str, column: str | Column) -> Column:
        u = lit(unit)
        c = _col_or_expr(column)
        return Column(lambda p: f"date_trunc({u.compile(p)}, {c.compile(p)})")

    @staticmethod
    def extract(field_name: str, column: str | Column) -> Column:
        if not _IDENT_RE.fullmatch(field_name):
            raise ValueError(f"invalid extract field: {field_name!r}")
        c = _col_or_expr(column)
        return Column(lambda p: f"extract({field_name} FROM {c.compile(p)})")

    @staticmethod
    def lower(column: str | Column) -> Column:
        return _fn("lower", column)

    @staticmethod
    def upper(column: str | Column) -> Column:
        return _fn("upper", column)

    @staticmethod
    def length(column: str | Column) -> Column:
        return _fn("length", column)

    @staticmethod
    def coalesce(*columns: str | Column) -> Column:
        if not columns:
            raise ValueError("coalesce requires at least one argument")
        exprs = [c if isinstance(c, Column) else col(c) for c in columns]
        return Column(lambda p: f"coalesce({', '.join(e.compile(p) for e in exprs)})")

    @staticmethod
    def left(column: str | Column, n: int) -> Column:
        c = _col_or_expr(column)
        n_expr = lit(int(n))
        return Column(lambda p: f"left({c.compile(p)}, {n_expr.compile(p)})")

    @staticmethod
    def substr(column: str | Column, start: int, length: int | None = None) -> Column:
        c = _col_or_expr(column)
        s = lit(int(start))
        if length is None:
            return Column(lambda p: f"substr({c.compile(p)}, {s.compile(p)})")
        ln = lit(int(length))
        return Column(
            lambda p: f"substr({c.compile(p)}, {s.compile(p)}, {ln.compile(p)})"
        )

    @staticmethod
    def array_agg(column: str | Column) -> Column:
        return _fn("array_agg", column)

    @staticmethod
    def string_agg(column: str | Column, delimiter: str = ", ") -> Column:
        c = _col_or_expr(column)
        d = lit(delimiter)
        return Column(lambda p: f"string_agg({c.compile(p)}, {d.compile(p)})")

    @staticmethod
    def now() -> Column:
        return Column(lambda _p: "now()")
