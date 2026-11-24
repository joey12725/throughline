"""End-to-end tests for parameterized SQL, /v1/sql/stream, and throughline-client
against a real disposable PostgreSQL test database."""

from __future__ import annotations

import csv
import io
import json

import httpx
import pytest
from conftest import ingest, message

from throughline import access
from throughline.db import connect, create_token
from throughline_client import F, Session, ValidationError, col


def _asgi_session(app, token: str, *, asker: str | None = None) -> Session:
    """Connect throughline_client.Session directly to a FastAPI app via TestClient."""
    from fastapi.testclient import TestClient

    tc = TestClient(app)

    class _SyncTestClientTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.url.query:
                path += "?" + request.url.query.decode("ascii")
            resp = tc.request(
                request.method,
                path,
                content=request.read(),
                headers=dict(request.headers),
            )
            return httpx.Response(
                status_code=resp.status_code,
                headers=resp.headers,
                content=resp.content,
            )

    return Session(
        "http://testserver",
        token,
        asker=asker,
        retries=0,
        transport=_SyncTestClientTransport(),
    )


def _seed_sample(client):
    for name in ("synthetic.chat", "synthetic.secret"):
        r = client.post(
            "/v1/channels",
            json={
                "name": name,
                "plugin": "synthetic",
                "description": f"{name} channel",
                "fields": [{"name": "rating", "type": "integer", "description": "score"}],
            },
        )
        assert r.status_code == 200, r.text

    r = ingest(
        client,
        [
            message(
                1,
                external_id="m1",
                content="orchard purple kites in the park",
                happened_at="2024-03-01T10:00:00+00:00",
                fields={"rating": 90},
            ),
            message(
                2,
                external_id="m2",
                content="moon jam recipe with 50% sugar and $1 literal",
                happened_at="2024-03-02T11:00:00+00:00",
                fields={"rating": 75},
            ),
            message(
                3,
                external_id="m3",
                content="long text " + ("x" * 6000),
                happened_at="2024-03-03T12:00:00+00:00",
                fields={"rating": 95},
            ),
            message(
                4,
                external_id="s1",
                content="zqcanary hidden secret note",
                channel="synthetic.secret",
                happened_at="2024-03-04T13:00:00+00:00",
                fields={"rating": 100},
            ),
        ],
    )
    assert r.status_code == 200, r.text


def test_parameterized_sql_and_audit_privacy(client):
    _seed_sample(client)

    # Scalar params, repeated $1, literal '%', and literal '$1' inside string constant
    r = client.post(
        "/v1/sql",
        json={
            "statement": (
                "SELECT external_id, '$1' AS lit_dollar, content "
                "FROM api.messages "
                "WHERE channel = $1 AND ($1 = 'synthetic.chat') "
                "AND content LIKE '%50%' AND happened_at >= $2::timestamptz"
            ),
            "params": ["synthetic.chat", "2024-03-01T00:00:00+00:00"],
            "raw": True,
        },
    )
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert len(rows) == 1
    assert rows[0]["external_id"] == "m2"
    assert rows[0]["lit_dollar"] == "$1"

    # Array param with ANY($1)
    r2 = client.post(
        "/v1/sql",
        json={
            "statement": "SELECT external_id FROM api.messages WHERE external_id = ANY($1) ORDER BY external_id",
            "params": [["m1", "m3"]],
            "raw": True,
        },
    )
    assert r2.status_code == 200, r2.text
    assert [row["external_id"] for row in r2.json()["rows"]] == ["m1", "m3"]

    # Audit log keeps $1 template and param_count, never leaking parameter values
    with connect() as c:
        row = c.execute(
            "SELECT statement, detail FROM tl.audit_log WHERE statement LIKE '%ANY($1)%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert "$1" in row["statement"]
    assert "m3" not in row["statement"]
    assert row["detail"]["param_count"] == 1

    # Validation rejects missing/out-of-range parameters
    for payload in (
        {"statement": "SELECT * FROM api.messages WHERE channel = $1"},
        {"statement": "SELECT * FROM api.messages WHERE channel = $2", "params": ["a"]},
        {"statement": "SELECT * FROM api.messages WHERE channel = $0", "params": ["a"]},
    ):
        bad = client.post("/v1/sql", json=payload)
        assert bad.status_code == 400, (payload, bad.status_code, bad.text)


def test_sql_stream_ndjson_csv_raw_and_asker_gate(client):
    _seed_sample(client)

    # NDJSON stream returns full untruncated 6000-char content (unlike agent-shaped /v1/sql)
    r = client.post(
        "/v1/sql/stream",
        json={
            "statement": "SELECT external_id, content FROM api.messages WHERE channel = $1 ORDER BY external_id",
            "params": ["synthetic.chat"],
            "format": "ndjson",
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["x-throughline-format"] == "ndjson"
    items = [json.loads(line) for line in r.text.strip().splitlines()]
    assert [i["external_id"] for i in items] == ["m1", "m2", "m3"]
    assert len(items[2]["content"]) == len("long text ") + 6000

    # CSV stream includes header + valid rows
    rcsv = client.post(
        "/v1/sql/stream",
        json={
            "statement": "SELECT external_id, rating FROM api.ch_synthetic_chat WHERE rating >= $1 ORDER BY rating DESC",
            "params": [80],
            "format": "csv",
        },
    )
    assert rcsv.status_code == 200, rcsv.text
    parsed = list(csv.DictReader(io.StringIO(rcsv.text)))
    assert parsed == [
        {"external_id": "m3", "rating": "95"},
        {"external_id": "m1", "rating": "90"},
    ]

    # Validation errors fail with 400 JSON before streaming starts
    bad = client.post(
        "/v1/sql/stream",
        json={"statement": "DELETE FROM tl.messages"},
    )
    assert bad.status_code == 400

    # Restricted asker only sees granted channel over /v1/sql/stream (zero canary leaks)
    access.register_asker("guest", mode="restricted")
    access.grant("guest", channel="synthetic.chat")
    guest_tok = create_token("guest-script", kind="agent", askers=["guest"])["token"]
    rg = client.post(
        "/v1/sql/stream",
        json={"statement": "SELECT channel, external_id, content FROM api.messages ORDER BY external_id"},
        headers={"Authorization": f"Bearer {guest_tok}"},
    )
    assert rg.status_code == 200, rg.text
    assert "zqcanary" not in rg.text.lower()
    guest_rows = [json.loads(line) for line in rg.text.strip().splitlines()]
    assert {row["channel"] for row in guest_rows} == {"synthetic.chat"}


def test_python_snowpark_client_end_to_end(client):
    _seed_sample(client)
    owner_tok = client.headers["Authorization"].removeprefix("Bearer ")

    with _asgi_session(client.app, owner_tok) as s:
        # Typed channel view + lazy filter + sort + limit
        top = (
            s.channel("synthetic.chat")
            .select("external_id", "rating", "content")
            .filter((col("rating") >= 80) & col("content").is_not_null())
            .order_by(col("rating").desc())
            .limit(2)
            .collect()
        )
        assert [r["external_id"] for r in top] == ["m3", "m1"]
        assert len(top[0]["content"]) == len("long text ") + 6000

        # Server-side group_by + agg pushed down to Postgres
        grouped = (
            s.table("messages")
            .group_by("channel")
            .agg(F.count("*").alias("n"))
            .order_by(col("n").desc())
            .collect()
        )
        assert {r["channel"]: r["n"] for r in grouped} == {
            "synthetic.chat": 3,
            "synthetic.secret": 1,
        }

        # date_trunc in group_by (verifies positional GROUP BY 1 works in real Postgres!)
        by_day = (
            s.table("messages")
            .filter(col("channel") == "synthetic.chat")
            .group_by(F.date_trunc("day", "happened_at").alias("day"))
            .agg(F.count("*").alias("n"))
            .collect()
        )
        assert len(by_day) == 3

        # Join api.messages with api.threads
        joined = (
            s.table("messages")
            .filter(col("channel") == "synthetic.chat")
            .alias("m")
            .join(s.table("threads").alias("t"), on=("thread_id", "id"))
            .select("m.external_id", "t.title")
            .order_by("m.external_id")
            .collect()
        )
        assert [r["external_id"] for r in joined] == ["m1", "m2", "m3"]

        # Count, first, stream, show, CSV
        assert s.channel("synthetic.chat").count() == 3
        first = s.channel("synthetic.chat").order_by(col("rating").asc()).first()
        assert first is not None and first["external_id"] == "m2"
        streamed = list(s.channel("synthetic.chat").select("external_id").stream())
        assert len(streamed) == 3
        table_preview = s.channel("synthetic.chat").select("external_id", "rating").show(2)
        assert "external_id" in table_preview
        csv_out = s.channel("synthetic.chat").select("external_id", "rating").to_csv()
        assert "m1,90" in csv_out

        # Full MCP parity endpoints
        assert "episodes" in s.find("purple kites", k=5)
        tl_res = s.timeline(["orchard", "moon jam"], semantic=False)
        assert tl_res["total_episodes"] >= 1
        assert "channels" in s.orient(days=14)
        assert s.describe() is not None
        assert len(s.channels()) == 2
        assert "channels" in s.status()

        # Error mapping
        with pytest.raises(ValidationError):
            s.sql("SELECT * FROM tl.messages")

    # Restricted asker via throughline_client sees only granted channel
    access.register_asker("guest", mode="restricted")
    access.grant("guest", channel="synthetic.chat")
    guest_tok = create_token("guest-py", kind="agent", askers=["guest"])["token"]
    with _asgi_session(client.app, guest_tok) as gs:
        assert gs.table("messages").count() == 3
        rows = gs.table("messages").select("channel", "content").collect()
        assert all("zqcanary" not in r["content"] for r in rows)
