"""Unit and mock-HTTP tests for throughline-client (no Postgres required)."""

from __future__ import annotations

import json

import httpx
import pytest

import throughline_client as tl
from throughline_client import AuthError, F, Session, ValidationError, col, lit, raw


def _mock_session(handler, *, asker: str | None = None, retries: int = 1) -> Session:
    return Session(
        "http://throughline.test",
        "secret-token",
        asker=asker,
        retries=retries,
        transport=httpx.MockTransport(handler),
    )


def test_dataframe_compiles_parameterized_sql():
    s = _mock_session(lambda req: httpx.Response(200, json={}))
    df = (
        s.channel("health.oura.sleep")
        .select("happened_at", "score")
        .filter((col("happened_at") >= "2026-01-01") & (col("score") > 80))
        .filter(col("note").contains("deep_50%"))
        .order_by(col("happened_at").desc())
        .limit(25)
    )
    sql, params = df.to_sql()
    assert sql == (
        'SELECT "happened_at", "score" FROM api.ch_health_oura_sleep '
        'WHERE (("happened_at" >= $1) AND ("score" > $2)) '
        'AND ("note" ILIKE $3) ORDER BY "happened_at" DESC LIMIT 25'
    )
    assert params == ["2026-01-01", 80, r"%deep\_50\%%"]


def test_with_column_then_filter_wraps_subquery():
    s = _mock_session(lambda req: httpx.Response(200, json={}))
    df = (
        s.table("messages")
        .with_column("kind", col("fields").json("kind"))
        .filter(col("kind") == "note")
        .limit(10)
    )
    sql, params = df.to_sql()
    assert 'FROM (SELECT *, ("fields" ->> $1) AS "kind" FROM api.messages) AS "_q"' in sql
    assert 'WHERE ("kind" = $2)' in sql
    assert params == ["kind", "note"]


def test_group_by_and_join_renumber_params():
    s = _mock_session(lambda req: httpx.Response(200, json={}))
    agg = (
        s.table("messages")
        .filter(col("channel").isin(["chat.sms", "email.gmail"]))
        .group_by(F.date_trunc("day", "happened_at").alias("day"), "channel")
        .agg(F.count("*").alias("n"))
    )
    sql, params = agg.to_sql()
    assert "GROUP BY 1, 2" in sql
    assert params == ["day", "chat.sms", "email.gmail"]

    left = s.table("messages").filter(col("channel") == "chat.sms").alias("m")
    right = s.table("threads").filter(col("title").contains("trip")).alias("t")
    joined = left.join(right, on=("thread_id", "id"), how="inner").select("m.id", "t.title")
    jsql, jparams = joined.to_sql()
    assert "INNER JOIN" in jsql
    assert "$1" in jsql and "$2" in jsql
    assert jparams == ["chat.sms", "%trip%"]


def test_identifier_and_type_validation_blocks_injection():
    s = _mock_session(lambda req: httpx.Response(200, json={}))
    with pytest.raises(ValueError):
        s.table("messages; DROP TABLE tl.messages")
    with pytest.raises(ValueError):
        s.channel("bad..channel")
    with pytest.raises(ValueError):
        col("id; SELECT 1")
    with pytest.raises(ValueError):
        col("id").cast("regclass")


def test_from_env(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("THROUGHLINE_TOKEN", "tok-123")
    monkeypatch.setenv("THROUGHLINE_ASKER", "guest")
    with tl.connect() as session:
        assert session.url == "http://127.0.0.1:8000"
        assert session.asker == "guest"


def test_http_streaming_errors_retries_and_mcp_parity():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.headers["X-Throughline-Asker"] == "guest"
        assert request.headers["X-Throughline-Client"] == "python-client"
        calls.append((request.method, request.url.path))

        if request.url.path == "/v1/sql":
            payload = json.loads(request.content)
            assert payload["raw"] is True
            if payload["statement"] == "BAD":
                return httpx.Response(400, json={"detail": "invalid SQL syntax"})
            return httpx.Response(
                200,
                json={
                    "rows": [{"id": 1, "channel": "synthetic.chat", "content": "hello"}],
                    "row_count": 1,
                    "truncated": False,
                },
            )
        if request.url.path == "/v1/sql/stream":
            payload = json.loads(request.content)
            if payload.get("format") == "csv":
                return httpx.Response(
                    200,
                    content=b"id,channel,content\n1,synthetic.chat,hello\n2,synthetic.chat,world\n",
                    headers={"Content-Type": "text/csv; charset=utf-8"},
                )
            return httpx.Response(
                200,
                content=(
                    b'{"id":1,"channel":"synthetic.chat","content":"hello"}\n'
                    b'{"id":2,"channel":"synthetic.chat","content":"world"}\n'
                ),
                headers={"Content-Type": "application/x-ndjson"},
            )
        if request.url.path == "/v1/find":
            return httpx.Response(200, json={"episodes": [{"id": 1}]})
        if request.url.path == "/v1/timeline":
            return httpx.Response(200, json={"total_episodes": 2})
        if request.url.path == "/v1/orient":
            return httpx.Response(200, json={"channels": {"synthetic.chat": "10"}})
        if request.url.path == "/v1/describe":
            return httpx.Response(200, json={"channels": ["synthetic.chat"]})
        if request.url.path == "/v1/status":
            return httpx.Response(403, json={"detail": "restricted"})
        return httpx.Response(404, json={"detail": "not found"})

    with _mock_session(handler, asker="guest") as s:
        # Bounded collect -> /v1/sql
        rows = s.table("messages").select("id", "channel", "content").limit(5).collect()
        assert len(rows) == 1 and rows[0]["content"] == "hello"

        # Unbounded collect -> /v1/sql/stream NDJSON
        all_rows = s.table("messages").select("id", "channel", "content").collect()
        assert len(all_rows) == 2 and all_rows[1]["content"] == "world"

        # CSV export
        csv_text = s.table("messages").to_csv()
        assert "2,synthetic.chat,world" in csv_text

        # MCP parity
        assert s.find("orchard", k=5)["episodes"] == [{"id": 1}]
        assert s.timeline(["orchard"])["total_episodes"] == 2
        assert "synthetic.chat" in s.orient(days=7)["channels"]
        assert s.describe()["channels"] == ["synthetic.chat"]
        assert len(s.person("Zibble")) == 1

        # Typed errors
        with pytest.raises(ValidationError):
            s.sql("BAD")
        with pytest.raises(AuthError):
            s.status()

        # Raw expression helper & lit
        expr_df = s.table("messages").filter(raw("length(content) > ?", 10) & (col("id") != lit(0)))
        esql, eparams = expr_df.to_sql()
        assert "length(content) > $1" in esql and eparams == [10, 0]
