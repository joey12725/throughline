"""Lazy Snowpark-style DataFrame that pushes operations down to Throughline SQL."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .expr import Column, OrderExpr, ParamCollector, col, quote_ident, raw

if TYPE_CHECKING:
    from .session import Session

_JOIN_TYPES = {"inner": "INNER JOIN", "left": "LEFT JOIN", "right": "RIGHT JOIN", "full": "FULL JOIN"}
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_col(item: str | Column) -> Column:
    return item if isinstance(item, Column) else col(item)


def _normalize_order(item: str | Column | OrderExpr) -> OrderExpr:
    if isinstance(item, OrderExpr):
        return item
    if isinstance(item, str):
        if item.startswith("-"):
            return col(item[1:]).desc()
        return col(item).asc()
    return item.asc()


@dataclass(frozen=True)
class _JoinSpec:
    right: DataFrame
    on: str | tuple[str, str] | Column
    how: str
    right_alias: str


@dataclass(frozen=True)
class _QueryPlan:
    source_sql: str
    source_params: tuple[Any, ...] = ()
    source_alias: str | None = None
    selects: tuple[Column, ...] = ()
    filters: tuple[Column, ...] = ()
    group_by: tuple[Column, ...] = ()
    having: tuple[Column, ...] = ()
    joins: tuple[_JoinSpec, ...] = ()
    orders: tuple[OrderExpr, ...] = ()
    distinct: bool = False
    limit: int | None = None
    offset: int | None = None

    def needs_subquery_for_transform(self) -> bool:
        return bool(
            self.selects
            or self.group_by
            or self.distinct
            or self.limit is not None
            or self.offset is not None
        )

    def needs_subquery_for_filter(self) -> bool:
        return bool(
            any(c._alias is not None for c in self.selects)
            or self.group_by
            or self.distinct
            or self.limit is not None
            or self.offset is not None
        )


class DataFrame:
    """Immutable, lazy query over Throughline api.* views and functions.

    Every transformation (.select, .filter, .group_by, .join, .order_by, .limit)
    builds a query plan without executing anything. Call .collect(), .stream(),
    .first(), .count(), .to_pandas(), .to_polars(), or .show() to materialize.
    """

    def __init__(self, session: Session, plan: _QueryPlan):
        self._session = session
        self._plan = plan

    def _wrap(self, alias: str = "_q") -> DataFrame:
        sql, params = self.to_sql()
        return DataFrame(
            self._session,
            _QueryPlan(
                source_sql=f"({sql})",
                source_params=tuple(params),
                source_alias=alias,
            ),
        )

    def alias(self, name: str) -> DataFrame:
        if not _ALIAS_RE.fullmatch(name):
            raise ValueError(f"invalid relation alias: {name!r}")
        if self._plan.needs_subquery_for_transform() or self._plan.filters or self._plan.joins:
            return self._wrap(alias=name)
        return DataFrame(self._session, replace(self._plan, source_alias=name))

    # --- Transformations (lazy) ---

    def select(self, *columns: str | Column) -> DataFrame:
        if not columns:
            raise ValueError("select() requires at least one column")
        base = self._wrap() if self._plan.needs_subquery_for_transform() else self
        cols = tuple(_normalize_col(c) for c in columns)
        return DataFrame(self._session, replace(base._plan, selects=cols))

    def with_column(self, name: str, expr: Column) -> DataFrame:
        """Return a DataFrame with an added computed column alongside existing ones."""
        quote_ident(name)
        base = self._wrap() if self._plan.needs_subquery_for_transform() else self
        existing = base._plan.selects or (col("*"),)
        return DataFrame(
            self._session,
            replace(base._plan, selects=existing + (expr.alias(name),)),
        )

    def filter(self, condition: Column | str, *params: Any) -> DataFrame:
        cond = condition if isinstance(condition, Column) else raw(condition, *params)
        base = self._wrap() if self._plan.needs_subquery_for_filter() else self
        return DataFrame(
            self._session, replace(base._plan, filters=base._plan.filters + (cond,))
        )

    where = filter

    def group_by(self, *columns: str | Column) -> GroupedDataFrame:
        base = self._wrap() if self._plan.needs_subquery_for_transform() else self
        cols = tuple(_normalize_col(c) for c in columns)
        return GroupedDataFrame(base, cols)

    def join(
        self,
        other: DataFrame,
        on: str | tuple[str, str] | Column,
        *,
        how: str = "inner",
        lsuffix: str = "l",
        rsuffix: str = "r",
    ) -> DataFrame:
        how_norm = how.lower()
        if how_norm not in _JOIN_TYPES:
            raise ValueError(f"unsupported join type {how!r}; use inner, left, right, or full")
        left = self
        if left._plan.needs_subquery_for_transform():
            left = left._wrap(alias=lsuffix)
        elif left._plan.source_alias is None:
            left = DataFrame(self._session, replace(left._plan, source_alias=lsuffix))
        right_alias = other._plan.source_alias or rsuffix
        if right_alias == left._plan.source_alias:
            right_alias = f"{right_alias}_{len(left._plan.joins) + 1}"
        spec = _JoinSpec(right=other, on=on, how=how_norm, right_alias=right_alias)
        return DataFrame(
            self._session, replace(left._plan, joins=left._plan.joins + (spec,))
        )

    def order_by(self, *columns: str | Column | OrderExpr) -> DataFrame:
        if not columns:
            raise ValueError("order_by() requires at least one column")
        orders = tuple(_normalize_order(c) for c in columns)
        return DataFrame(self._session, replace(self._plan, orders=orders))

    sort = order_by

    def distinct(self) -> DataFrame:
        return DataFrame(self._session, replace(self._plan, distinct=True))

    def limit(self, n: int) -> DataFrame:
        if n < 0:
            raise ValueError("limit must be non-negative")
        return DataFrame(self._session, replace(self._plan, limit=int(n)))

    def offset(self, n: int) -> DataFrame:
        if n < 0:
            raise ValueError("offset must be non-negative")
        return DataFrame(self._session, replace(self._plan, offset=int(n)))

    # --- Compilation ---

    def _compile_into(self, collector: ParamCollector) -> str:
        p = self._plan

        # Re-bind any embedded source parameters ($1..$k in source_sql) to the
        # outer collector's current parameter indices.
        source_sql = p.source_sql
        if p.source_params:
            base_idx = len(collector.values)
            for val in p.source_params:
                collector.values.append(val)
            if base_idx > 0:
                source_sql = re.sub(
                    r"\$(\d+)",
                    lambda m: f"${int(m.group(1)) + base_idx}",
                    source_sql,
                )

        if p.selects:
            proj = ", ".join(c.compile_select(collector) for c in p.selects)
        else:
            proj = "*"

        distinct_kw = "DISTINCT " if p.distinct else ""
        from_clause = source_sql
        if p.source_alias:
            from_clause = f"{from_clause} AS {quote_ident(p.source_alias)}"

        join_parts = []
        left_alias = p.source_alias
        for spec in p.joins:
            right_df = spec.right
            if (
                right_df._plan.needs_subquery_for_transform()
                or right_df._plan.filters
                or right_df._plan.joins
            ):
                r_sql = f"({right_df._compile_into(collector)})"
            else:
                r_sql = right_df._plan.source_sql
                if right_df._plan.source_params:
                    r_base = len(collector.values)
                    collector.values.extend(right_df._plan.source_params)
                    if r_base > 0:
                        r_sql = re.sub(
                            r"\$(\d+)",
                            lambda m: f"${int(m.group(1)) + r_base}",
                            r_sql,
                        )
            r_alias = quote_ident(spec.right_alias)
            if isinstance(spec.on, Column):
                on_sql = spec.on.compile(collector)
            elif isinstance(spec.on, tuple):
                l_col, r_col = spec.on
                l_ref = f"{quote_ident(left_alias)}.{quote_ident(l_col)}" if left_alias else quote_ident(l_col)
                r_ref = f"{r_alias}.{quote_ident(r_col)}"
                on_sql = f"{l_ref} = {r_ref}"
            else:
                l_ref = f"{quote_ident(left_alias)}.{quote_ident(spec.on)}" if left_alias else quote_ident(spec.on)
                r_ref = f"{r_alias}.{quote_ident(spec.on)}"
                on_sql = f"{l_ref} = {r_ref}"
            join_parts.append(f"{_JOIN_TYPES[spec.how]} {r_sql} AS {r_alias} ON {on_sql}")

        parts = [f"SELECT {distinct_kw}{proj} FROM {from_clause}"]
        if join_parts:
            parts.extend(join_parts)
        if p.filters:
            where_sql = " AND ".join(f.compile(collector) for f in p.filters)
            parts.append(f"WHERE {where_sql}")
        if p.group_by:
            if p.selects and p.selects[: len(p.group_by)] == p.group_by:
                gb_sql = ", ".join(str(i) for i in range(1, len(p.group_by) + 1))
            else:
                gb_sql = ", ".join(g.compile(collector) for g in p.group_by)
            parts.append(f"GROUP BY {gb_sql}")
        if p.having:
            hv_sql = " AND ".join(h.compile(collector) for h in p.having)
            parts.append(f"HAVING {hv_sql}")
        if p.orders:
            ord_sql = ", ".join(o.compile(collector) for o in p.orders)
            parts.append(f"ORDER BY {ord_sql}")
        if p.limit is not None:
            parts.append(f"LIMIT {p.limit}")
        if p.offset is not None:
            parts.append(f"OFFSET {p.offset}")
        return " ".join(parts)

    def to_sql(self) -> tuple[str, list[Any]]:
        """Compile this lazy DataFrame into (sql_statement, bound_params)."""
        collector = ParamCollector()
        sql = self._compile_into(collector)
        return sql, collector.values

    def explain(self) -> str:
        """Return a human-readable string of the compiled SQL and parameters."""
        sql, params = self.to_sql()
        if params:
            return f"{sql}\n-- params: {params!r}"
        return sql

    # --- Materialization ---

    def collect(
        self,
        *,
        row_cap: int | None = None,
        deadline_ms: int | None = None,
        strict_exact: bool = False,
        stream: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Execute the query on Throughline and return rows as a list of dicts.

        Automatically uses POST /v1/sql/stream when stream=True or when no
        small LIMIT is set (or row_cap > 10,000), so large scripts never hit
        the 1,000-row / 4 MiB JSON cap or cell truncation.
        """
        sql, params = self.to_sql()
        use_stream = stream
        if use_stream is None:
            if row_cap is not None and row_cap > 10_000:
                use_stream = True
            elif self._plan.limit is None or self._plan.limit > 10_000:
                use_stream = True
            else:
                use_stream = False
        if use_stream:
            cap = row_cap or (self._plan.limit if self._plan.limit else 100_000)
            return list(
                self._session.stream_sql(
                    sql,
                    params or None,
                    row_cap=cap,
                    deadline_ms=deadline_ms,
                    strict_exact=strict_exact,
                )
            )
        cap = row_cap or max(self._plan.limit or 1000, 1)
        result = self._session.sql(
            sql,
            params or None,
            raw=True,
            row_cap=min(cap, 10_000),
            deadline_ms=deadline_ms,
            strict_exact=strict_exact,
        )
        return result.rows

    def stream(
        self,
        *,
        row_cap: int = 100_000,
        deadline_ms: int | None = None,
        strict_exact: bool = False,
    ) -> Iterator[dict[str, Any]]:
        """Lazily stream rows from POST /v1/sql/stream (NDJSON)."""
        sql, params = self.to_sql()
        return self._session.stream_sql(
            sql,
            params or None,
            row_cap=row_cap,
            deadline_ms=deadline_ms,
            strict_exact=strict_exact,
        )

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return self.stream()

    def first(self) -> dict[str, Any] | None:
        """Return the first matching row, or None."""
        rows = self.limit(1).collect(stream=False)
        return rows[0] if rows else None

    def one(self) -> dict[str, Any]:
        """Return the single matching row, or raise ValueError."""
        rows = self.limit(2).collect(stream=False)
        if len(rows) != 1:
            raise ValueError(f"expected exactly 1 row, got {len(rows)}")
        return rows[0]

    def count(self) -> int:
        """Push down COUNT(*) to PostgreSQL and return the integer count."""
        sql, params = self.to_sql()
        result = self._session.sql(
            f"SELECT count(*) AS n FROM ({sql}) AS _cnt",
            params or None,
            raw=True,
            row_cap=1,
        )
        return int(result.rows[0]["n"]) if result.rows else 0

    def to_dicts(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.collect(**kwargs)

    def to_pandas(self, **kwargs: Any):
        """Materialize directly into a pandas.DataFrame (requires pandas)."""
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "pandas is required for to_pandas(); install with `pip install throughline-client[pandas]`"
            ) from exc
        return pd.DataFrame(self.collect(**kwargs))

    def to_polars(self, **kwargs: Any):
        """Materialize directly into a polars.DataFrame (requires polars)."""
        try:
            import polars as pl
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "polars is required for to_polars(); install with `pip install throughline-client[polars]`"
            ) from exc
        return pl.DataFrame(self.collect(**kwargs))

    def to_csv(
        self,
        path: str | None = None,
        *,
        row_cap: int = 100_000,
        deadline_ms: int | None = None,
    ) -> str | None:
        """Stream RFC 4180 CSV directly from the server into a file or string."""
        sql, params = self.to_sql()
        chunks = self._session.stream_csv(
            sql, params or None, row_cap=row_cap, deadline_ms=deadline_ms
        )
        if path is not None:
            with open(path, "wb") as f:
                for chunk in chunks:
                    f.write(chunk)
            return None
        return b"".join(chunks).decode("utf-8")

    def show(self, n: int = 20, *, max_width: int = 48) -> str:
        """Print and return a compact Snowpark-style ASCII table preview."""
        rows = self.limit(n).collect(stream=False)
        if not rows:
            text = "(0 rows)"
            print(text)
            return text
        cols = list(rows[0].keys())

        def _fmt(v: Any) -> str:
            s = "" if v is None else str(v).replace("\n", " ")
            return s if len(s) <= max_width else s[: max_width - 1] + "…"

        formatted = [[_fmt(r.get(c)) for c in cols] for r in rows]
        widths = [
            max(len(c), *(len(row[i]) for row in formatted))
            for i, c in enumerate(cols)
        ]
        sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
        header = "| " + " | ".join(c.ljust(w) for c, w in zip(cols, widths)) + " |"
        lines = [sep, header, sep]
        for row in formatted:
            lines.append("| " + " | ".join(v.ljust(w) for v, w in zip(row, widths)) + " |")
        lines.append(sep)
        text = "\n".join(lines)
        print(text)
        return text


class GroupedDataFrame:
    """Result of DataFrame.group_by(...); call .agg(...) or .count()."""

    def __init__(self, df: DataFrame, group_cols: tuple[Column, ...]):
        self._df = df
        self._group_cols = group_cols

    def agg(self, *aggregations: Column) -> DataFrame:
        if not aggregations:
            raise ValueError("agg() requires at least one aggregate expression")
        selects = self._group_cols + tuple(aggregations)
        return DataFrame(
            self._df._session,
            replace(self._df._plan, selects=selects, group_by=self._group_cols),
        )

    def count(self, alias: str = "count") -> DataFrame:
        from .expr import F

        return self.agg(F.count("*").alias(alias))

    def having(self, condition: Column) -> GroupedDataFrame:
        updated = DataFrame(
            self._df._session,
            replace(self._df._plan, having=self._df._plan.having + (condition,)),
        )
        return GroupedDataFrame(updated, self._group_cols)
