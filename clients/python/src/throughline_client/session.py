"""Session, Feed, and high-level retrieval methods (full MCP parity + Snowpark API)."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from .dataframe import DataFrame, _QueryPlan
from .errors import APIError, AuthError, ValidationError

_RELATION_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_LENS_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _sql_literal(value: Any) -> str:
    """Escape a scalar constant for semantic function arguments (e.g. api.recall)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "ARRAY[" + ", ".join(_sql_literal(v) for v in value) + "]"
    return "'" + str(value).replace("'", "''") + "'"


@dataclass
class SQLResult:
    """Response from POST /v1/sql."""

    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool
    execution: dict[str, Any] | None = None

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> dict[str, Any]:
        return self.rows[index]

    def to_pandas(self):
        try:
            import pandas as pd
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "pandas is required for to_pandas(); install with `pip install throughline-client[pandas]`"
            ) from exc
        return pd.DataFrame(self.rows)

    def to_polars(self):
        try:
            import polars as pl
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "polars is required for to_polars(); install with `pip install throughline-client[polars]`"
            ) from exc
        return pl.DataFrame(self.rows)


class Feed:
    """Named arrival cursor over Throughline messages (/v1/feeds/{name})."""

    def __init__(self, session: Session, name: str):
        self._session = session
        self.name = name

    def create(self, *, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": self.name}
        if scope is not None:
            body["scope"] = scope
        return self._session.request("POST", "feeds", json=body)

    def info(self) -> dict[str, Any]:
        return self._session.request("GET", f"feeds/{self.name}", retry=True)

    def news(self, *, limit: int | None = None, settle: bool | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if settle is not None:
            params["settle"] = str(settle).lower()
        return self._session.request("GET", f"feeds/{self.name}/news", params=params, retry=True)

    def pending(self, *, limit: int | None = None) -> dict[str, Any]:
        params = {"limit": limit} if limit is not None else {}
        return self._session.request("GET", f"feeds/{self.name}/pending", params=params, retry=True)

    def mark(
        self,
        *,
        ingested_at: str | None = None,
        message_id: int | None = None,
        expected_ingested_at: str | None = None,
        expected_message_id: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if ingested_at is not None:
            body["seen_to_ingested_at"] = ingested_at
        if message_id is not None:
            body["seen_to_message_id"] = message_id
        if expected_ingested_at is not None:
            body["expected_seen_to_ingested_at"] = expected_ingested_at
        if expected_message_id is not None:
            body["expected_seen_to_message_id"] = expected_message_id
        return self._session.request("POST", f"feeds/{self.name}/mark", json=body, retry=True)

    def drop(self) -> dict[str, Any]:
        return self._session.request("DELETE", f"feeds/{self.name}")


class Session:
    """Programmatic Throughline session (Snowpark-style DataFrames + full MCP parity)."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        asker: str | None = None,
        timeout: float = 60.0,
        retries: int = 3,
        client_name: str = "python-client",
        transport: httpx.BaseTransport | None = None,
    ):
        self.url = url.rstrip("/")
        self.asker = asker
        self.retries = retries
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Throughline-Client": client_name,
        }
        if asker:
            headers["X-Throughline-Asker"] = asker
        self._http = httpx.Client(
            base_url=self.url + "/v1/",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    @classmethod
    def from_env(
        cls,
        *,
        url: str | None = None,
        token: str | None = None,
        asker: str | None = None,
        **kwargs: Any,
    ) -> Session:
        """Create a Session from THROUGHLINE_URL, THROUGHLINE_TOKEN, and optional THROUGHLINE_ASKER."""
        resolved_url = url or os.getenv("THROUGHLINE_URL")
        resolved_token = token or os.getenv("THROUGHLINE_TOKEN")
        resolved_asker = asker if asker is not None else os.getenv("THROUGHLINE_ASKER") or None
        if not resolved_url:
            raise ValueError("THROUGHLINE_URL is not set (or pass url=...)")
        if not resolved_token:
            raise ValueError("THROUGHLINE_TOKEN is not set (or pass token=...)")
        return cls(resolved_url, resolved_token, asker=resolved_asker, **kwargs)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # --- Low-level HTTP ---

    def _raise_for_response(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = response.text or f"HTTP {response.status_code}"
        code = None
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("detail") or detail)
                code = payload.get("code")
        except ValueError:
            pass
        if response.status_code in (401, 403):
            raise AuthError(response.status_code, detail, code=code)
        if response.status_code in (400, 422):
            raise ValidationError(response.status_code, detail, code=code)
        raise APIError(response.status_code, detail, code=code)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        retry: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        clean_path = path.lstrip("/")
        if clean_path.startswith("v1/"):
            clean_path = clean_path[3:]
        for attempt in range(self.retries + 1):
            try:
                response = self._http.request(
                    method,
                    clean_path,
                    json=json,
                    params=params,
                    headers=headers,
                )
                if response.status_code in (429,) or response.status_code >= 500:
                    if retry and attempt < self.retries:
                        time.sleep(min(0.1 * (2**attempt), 4.0))
                        continue
                self._raise_for_response(response)
                return response.json()
            except httpx.TransportError:
                if not retry or attempt == self.retries:
                    raise
                time.sleep(min(0.1 * (2**attempt), 4.0))

    # --- Snowpark-style lazy DataFrame entry points ---

    def table(self, name: str) -> DataFrame:
        """Open a lazy DataFrame over an api.* view (e.g. 'messages', 'threads', 'people')."""
        clean = name[4:] if name.startswith("api.") else name
        if not _RELATION_RE.fullmatch(clean):
            raise ValueError(f"invalid api view name: {name!r}")
        return DataFrame(self, _QueryPlan(source_sql=f"api.{clean}"))

    def channel(self, name: str) -> DataFrame:
        """Open a lazy DataFrame over a channel's typed view (api.ch_<channel>)."""
        if not _CHANNEL_RE.fullmatch(name):
            raise ValueError(f"invalid channel name: {name!r}")
        view = "ch_" + name.replace(".", "_")
        return DataFrame(self, _QueryPlan(source_sql=f"api.{view}"))

    def lens(self, name: str) -> DataFrame:
        """Open a lazy DataFrame over a lens view (api.lens_<name>)."""
        if not _LENS_RE.fullmatch(name):
            raise ValueError(f"invalid lens name: {name!r}")
        return DataFrame(self, _QueryPlan(source_sql=f"api.lens_{name}"))

    def from_sql(self, statement: str, params: list[Any] | None = None) -> DataFrame:
        """Wrap an existing SELECT statement as a composable lazy DataFrame."""
        return DataFrame(
            self,
            _QueryPlan(
                source_sql=f"({statement.strip().rstrip(';')})",
                source_params=tuple(params or ()),
                source_alias="_q",
            ),
        )

    def recall_df(
        self,
        query: str,
        *,
        k: int = 20,
        channels: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        strategy: str | None = None,
    ) -> DataFrame:
        """Lazy DataFrame backed by semantic SQL api.recall(...), composable with .join/.filter."""
        args = [f"query => {_sql_literal(query)}", f"k => {int(k)}"]
        if channels is not None:
            args.append(f"channels => {_sql_literal(channels)}")
        if after is not None:
            args.append(f"after => {_sql_literal(after)}")
        if before is not None:
            args.append(f"before => {_sql_literal(before)}")
        if strategy is not None:
            args.append(f"strategy => {_sql_literal(strategy)}")
        return DataFrame(
            self,
            _QueryPlan(
                source_sql=f"api.recall({', '.join(args)})",
                source_alias="r",
            ),
        )

    # --- Parameterized & Streaming SQL ---

    def sql(
        self,
        statement: str,
        params: list[Any] | None = None,
        *,
        raw: bool = True,
        row_cap: int = 1000,
        deadline_ms: int | None = None,
        strict_exact: bool = False,
        dry_run: bool = False,
        trace: bool = False,
        budgets: dict[str, Any] | None = None,
        approval_id: str | None = None,
    ) -> SQLResult:
        """Execute one read-only SELECT over POST /v1/sql with optional $1..$N params."""
        body: dict[str, Any] = {
            "statement": statement,
            "raw": raw,
            "row_cap": row_cap,
            "strict_exact": strict_exact,
            "dry_run": dry_run,
            "trace": trace,
        }
        if params:
            body["params"] = list(params)
        if deadline_ms is not None:
            body["deadline_ms"] = deadline_ms
        if budgets is not None:
            body["budgets"] = budgets
        if approval_id is not None:
            body["approval_id"] = approval_id
        data = self.request("POST", "sql", json=body, retry=True)
        return SQLResult(
            rows=data["rows"],
            row_count=data["row_count"],
            truncated=data["truncated"],
            execution=data.get("execution"),
        )

    def stream_sql(
        self,
        statement: str,
        params: list[Any] | None = None,
        *,
        row_cap: int = 100_000,
        deadline_ms: int | None = None,
        strict_exact: bool = False,
        budgets: dict[str, Any] | None = None,
        approval_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Stream raw rows lazily as dicts via POST /v1/sql/stream (NDJSON)."""
        body: dict[str, Any] = {
            "statement": statement,
            "format": "ndjson",
            "row_cap": row_cap,
            "strict_exact": strict_exact,
        }
        if params:
            body["params"] = list(params)
        if deadline_ms is not None:
            body["deadline_ms"] = deadline_ms
        if budgets is not None:
            body["budgets"] = budgets
        if approval_id is not None:
            body["approval_id"] = approval_id
        with self._http.stream("POST", "sql/stream", json=body) as response:
            if response.status_code >= 400:
                response.read()
                self._raise_for_response(response)
            for line in response.iter_lines():
                if line:
                    yield json.loads(line)

    def stream_csv(
        self,
        statement: str,
        params: list[Any] | None = None,
        *,
        row_cap: int = 100_000,
        deadline_ms: int | None = None,
        strict_exact: bool = False,
    ) -> Iterator[bytes]:
        """Stream RFC 4180 CSV byte chunks via POST /v1/sql/stream."""
        body: dict[str, Any] = {
            "statement": statement,
            "format": "csv",
            "row_cap": row_cap,
            "strict_exact": strict_exact,
        }
        if params:
            body["params"] = list(params)
        if deadline_ms is not None:
            body["deadline_ms"] = deadline_ms
        with self._http.stream("POST", "sql/stream", json=body) as response:
            if response.status_code >= 400:
                response.read()
                self._raise_for_response(response)
            yield from response.iter_bytes()

    # --- High-level retrieval (100% parity with MCP tools) ---

    def find(
        self,
        query: str,
        *,
        k: int = 15,
        sort: str = "relevance",
        window: int = 4,
        channels: list[str] | None = None,
        persons: list[int] | None = None,
        after: str | None = None,
        before: str | None = None,
        max_chars: int | None = None,
        strategy: str | None = None,
        ranker: str | None = None,
        pages: int | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Hybrid keyword + meaning search returning compact episodes (POST /v1/find)."""
        body: dict[str, Any] = {"query": query, "k": k, "sort": sort, "window": window, **extra}
        for key, value in (
            ("channels", channels),
            ("persons", persons),
            ("after", after),
            ("before", before),
            ("max_chars", max_chars),
            ("strategy", strategy),
            ("ranker", ranker),
            ("pages", pages),
        ):
            if value is not None:
                body[key] = value
        return self.request("POST", "find", json=body, retry=True)

    def timeline(
        self,
        queries: str | list[str],
        *,
        semantic: bool = True,
        max_episodes: int = 150,
        order: str = "asc",
        sample: str = "head",
        text_chars: int | None = None,
        channels: list[str] | None = None,
        persons: list[int] | None = None,
        after: str | None = None,
        before: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """Exhaustive chronological episodes across synonyms (POST /v1/timeline)."""
        body: dict[str, Any] = {
            "semantic": semantic,
            "max_episodes": max_episodes,
            "order": order,
            "sample": sample,
            **extra,
        }
        if isinstance(queries, str):
            body["query"] = queries
        else:
            body["queries"] = list(queries)
        for key, value in (
            ("text_chars", text_chars),
            ("channels", channels),
            ("persons", persons),
            ("after", after),
            ("before", before),
        ):
            if value is not None:
                body[key] = value
        return self.request("POST", "timeline", json=body, retry=True)

    def orient(
        self,
        *,
        days: int = 14,
        people: list[str] | None = None,
        as_of: str | None = None,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Working-memory snapshot across channels, telemetry, calendar, threads (POST /v1/orient)."""
        body: dict[str, Any] = {"days": days}
        if people is not None:
            body["people"] = list(people)
        if as_of is not None:
            body["as_of"] = as_of
        if max_chars is not None:
            body["max_chars"] = max_chars
        return self.request("POST", "orient", json=body, retry=True)

    def recall(
        self,
        query: str,
        *,
        k: int = 20,
        scope: dict[str, Any] | None = None,
        strategy: str | None = None,
        ranker: str | None = None,
        view: str = "full",
        **extra: Any,
    ) -> dict[str, Any]:
        """Full programmatic record-contract retrieval (POST /v1/recall)."""
        body: dict[str, Any] = {"query": query, "k": k, "view": view, **extra}
        if scope is not None:
            body["scope"] = scope
        if strategy is not None:
            body["strategy"] = strategy
        if ranker is not None:
            body["ranker"] = ranker
        return self.request("POST", "recall", json=body, retry=True)

    def describe(self, subject: str | None = None) -> Any:
        """Archive schema: channels, typed fields, SQL functions, recipes (GET /v1/describe)."""
        params = {"subject": subject} if subject is not None else None
        return self.request("GET", "describe", params=params, retry=True)

    def context(self, message_id: int, *, before: int = 3, after: int = 3) -> dict[str, Any]:
        """Surrounding messages in a thread around message_id (GET /v1/context)."""
        return self.request(
            "GET",
            "context",
            params={"message_id": message_id, "before": before, "after": after},
            retry=True,
        )

    # --- People & temporal helpers ---

    def person(self, name: str, *, include_brief: bool = True, limit: int = 5) -> list[dict[str, Any]]:
        """Resolve a name to people (with briefs by default, matching MCP's `person` tool)."""
        if include_brief:
            res = self.sql(
                "SELECT p.id, b.* FROM api.person($1) p, LATERAL api.person_brief(p.id) b LIMIT $2",
                [name, limit],
                raw=True,
            )
            return res.rows
        return self.request("GET", "person", params={"q": name}, retry=True)["rows"]

    def person_brief(self, person_id: int) -> dict[str, Any]:
        return self.request("GET", f"people/{person_id}/brief", retry=True)

    def person_dms(
        self,
        person_id: int,
        *,
        after: str | None = None,
        before: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = {k: v for k, v in (("after", after), ("before", before), ("max_results", limit)) if v is not None}
        return self.request("GET", f"people/{person_id}/dms", params=params, retry=True)

    def person_timeline(
        self,
        person_id: int,
        *,
        after: str | None = None,
        before: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = {k: v for k, v in (("after", after), ("before", before), ("max_results", limit)) if v is not None}
        return self.request("GET", f"people/{person_id}/timeline", params=params, retry=True)

    def person_open_loops(self, person_id: int | None = None) -> dict[str, Any]:
        path = f"people/{person_id}/open-loops" if person_id is not None else "open-loops"
        return self.request("GET", path, retry=True)

    def person_last_contact(self, person_id: int) -> dict[str, Any]:
        return self.request("GET", f"people/{person_id}/last-contact", retry=True)

    def person_arc(self, person_id: int) -> dict[str, Any]:
        return self.request("GET", f"people/{person_id}/arc", retry=True)

    def person_circle(
        self,
        person_id: int,
        *,
        after: str | None = None,
        before: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = {k: v for k, v in (("after", after), ("before", before), ("max_results", limit)) if v is not None}
        return self.request("GET", f"people/{person_id}/circle", params=params, retry=True)

    def day(
        self,
        on_date: str,
        *,
        tz: str | None = None,
        channels: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"date": on_date}
        if tz is not None:
            params["tz"] = tz
        if channels is not None:
            params["channels"] = ",".join(channels)
        if limit is not None:
            params["max_results"] = limit
        return self.request("GET", "day", params=params, retry=True)

    def around(
        self,
        at: str,
        *,
        window_s: int | None = None,
        channels: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"at": at}
        if window_s is not None:
            params["window_s"] = window_s
        if channels is not None:
            params["channels"] = ",".join(channels)
        if limit is not None:
            params["max_results"] = limit
        return self.request("GET", "around", params=params, retry=True)

    # --- Catalog, pages, lenses, recipes, feeds, status ---

    def channels(self) -> list[dict[str, Any]]:
        return self.request("GET", "channels", retry=True)

    def status(self) -> dict[str, Any]:
        return self.request("GET", "status", retry=True)

    def pages(self, *, kind: str | None = None, q: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if kind is not None:
            params["kind"] = kind
        if q is not None:
            params["q"] = q
        return self.request("GET", "pages", params=params, retry=True)

    def page(self, slug: str) -> dict[str, Any]:
        return self.request("GET", f"pages/{slug.lstrip('/')}", retry=True)

    def lenses(self) -> list[dict[str, Any]]:
        return self.request("GET", "lenses", retry=True)["lenses"]

    def recipes(self) -> Any:
        return self.request("GET", "recipes", retry=True)

    def recipe(self, name: str) -> Any:
        return self.request("GET", f"recipes/{name}", retry=True)

    def feed(self, name: str) -> Feed:
        """Handle for a named arrival cursor (/v1/feeds/{name})."""
        return Feed(self, name)


def connect(
    url: str | None = None,
    token: str | None = None,
    *,
    asker: str | None = None,
    **kwargs: Any,
) -> Session:
    """Connect to Throughline using explicit credentials or THROUGHLINE_* env vars."""
    return Session.from_env(url=url, token=token, asker=asker, **kwargs)
