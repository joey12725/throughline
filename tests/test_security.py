import psycopg
import pytest
from conftest import ingest, message

from throughline.db import connect, create_token

ESCAPES = [
    "SELECT ('SELECT * FROM tl.conversations').ts_stat",
    "SELECT (1).pg_advisory_lock",
    "SELECT m.content.ts_stat FROM api.messages m",
    "SELEC wrong",
    "SELECT 1; SELECT 2",
    "SET ROLE postgres",
    "RESET ROLE",
    "SET SESSION AUTHORIZATION postgres",
    "COPY api.messages TO STDOUT",
    "DO $$ BEGIN NULL; END $$",
    "SELECT set_config('throughline.purge','on',false)",
    "SELECT pg_catalog.set_config('throughline.purge','on',false)",
    "SELECT * FROM tl.messages",
    "SELECT * FROM pg_authid",
    "SELECT * FROM pg_catalog.pg_authid",
    "CREATE TEMP TABLE fake(x int)",
    "SELECT 1 INTO TEMP fake",
    "DELETE FROM api.messages",
    "UPDATE api.messages SET content='modified'",
    "WITH changed AS (DELETE FROM api.messages RETURNING *) SELECT * FROM changed",
    "SELECT tl.refresh_channel('synthetic.chat')",
    "SELECT public.fake_security_definer()",
    "SELECT 'tl.messages'::regclass",
    "SELECT * FROM api.messages FOR UPDATE",
    # alias.name on a FROM function that returns a scalar is name(alias).
    "SELECT s.current_setting FROM lower('server_version') s",
    "SELECT s.pg_sleep FROM generate_series(25,25) s",
    "SELECT s.pg_get_userbyid FROM generate_series(10,20000) s",
    "SELECT s.pg_get_functiondef FROM generate_series(1,99999) s",
    "SELECT (s.ts_stat).* FROM lower('SELECT 1') s",
    "SELECT x.pg_sleep FROM coalesce(5) x",
    "SELECT generate_series.pg_sleep FROM generate_series(25,25)",
    "SELECT c.jsonb_pretty FROM api.catalog() c",
    # A WITH name does not make the catalog relation of that name readable.
    "WITH pg_roles AS (SELECT * FROM pg_roles) SELECT * FROM pg_roles",
    "SELECT * FROM (WITH pg_roles AS (SELECT 1) SELECT 1) x, pg_roles",
]


@pytest.mark.parametrize("statement", ESCAPES)
def test_qry_1_sql_escapes(client, statement):
    r = client.post("/v1/sql", json={"statement": statement})
    assert r.status_code == 400, (statement, r.text)


def test_qry_1_role_and_cap_acl_7_audit(client, channel):
    ingest(client, [message(i) for i in range(5)])
    statement = "SELECT * FROM api.messages ORDER BY id"
    r = client.post(
        "/v1/sql",
        json={"statement": statement, "row_cap": 2},
        headers={"X-Throughline-Asker": "synthetic-owner"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["truncated"] and r.json()["row_count"] == 2
    with connect() as c:
        row = c.execute(
            "SELECT * FROM tl.audit_log WHERE statement=%s", (statement,)
        ).fetchone()
        assert (
            row["statement"] == statement
            and row["row_count"] == 2
            and row["asker"] == "synthetic-owner"
        )
    with connect("reader") as c:
        assert (
            c.execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
            == "on"
        )
        assert (
            c.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "30s"
        )
        with pytest.raises(psycopg.Error):
            c.execute("SELECT * FROM tl.messages")


@pytest.mark.parametrize("role", ["app", "reader"])
@pytest.mark.parametrize(
    "operation",
    [
        "UPDATE tl.messages SET content='tampered'",
        "UPDATE tl.messages SET happened_at=now()",
        "UPDATE tl.raw_payloads SET payload='{}'",
        "DELETE FROM tl.messages",
        "DELETE FROM tl.raw_payloads",
        "UPDATE tl.location_track SET lat=42",
        "DELETE FROM tl.location_track",
    ],
)
def test_arc_5_direct_role_protection(client, channel, role, operation):
    ingest(client, [message()])
    client.post(
        "/v1/ingest/track",
        json={
            "points": [
                {"at": "2020-01-01T00:00:00Z", "lat": 1, "lon": 2, "source": "fake"}
            ]
        },
    )
    with pytest.raises(psycopg.Error):
        with connect(role) as c:
            if role == "app":
                c.execute("SET LOCAL throughline.purge='on'")
            c.execute(operation)


MATRIX = [
    ("GET", "/v1/channels", None, {"owner": 200, "agent": 200, "plugin": 200}),
    (
        "POST",
        "/v1/channels",
        {"name": "fake.channel", "plugin": "synthetic"},
        {"owner": 200, "agent": 403, "plugin": 200},
    ),
    (
        "POST",
        "/v1/ingest/messages",
        {"plugin": "synthetic", "messages": []},
        {"owner": 200, "agent": 403, "plugin": 200},
    ),
    (
        "POST",
        "/v1/ingest/track",
        {"points": []},
        {"owner": 200, "agent": 403, "plugin": 200},
    ),
    (
        "POST",
        "/v1/sql",
        {"statement": "SELECT 1"},
        {"owner": 200, "agent": 200, "plugin": 403},
    ),
    ("GET", "/v1/catalog", None, {"owner": 200, "agent": 200, "plugin": 403}),
    ("GET", "/v1/status", None, {"owner": 200, "agent": 200, "plugin": 403}),
    (
        "POST",
        "/v1/identity/people",
        {"handle": "fake-person"},
        {"owner": 200, "agent": 200, "plugin": 403},
    ),
    (
        "PUT",
        "/v1/plugins/synthetic/cursor/key",
        {"cursor": 1},
        {"owner": 200, "agent": 403, "plugin": 200},
    ),
    (
        "POST",
        "/v1/plugins/synthetic/heartbeat",
        {},
        {"owner": 200, "agent": 403, "plugin": 200},
    ),
    (
        "POST",
        "/v1/plugins/synthetic/manifest",
        {},
        {"owner": 200, "agent": 403, "plugin": 200},
    ),
    ("POST", "/v1/purge", {}, {"owner": 404, "agent": 404, "plugin": 404}),
    ("POST", "/v1/tokens", {}, {"owner": 404, "agent": 404, "plugin": 404}),
    ("DELETE", "/v1/messages/1", None, {"owner": 404, "agent": 404, "plugin": 404}),
    (
        "PATCH",
        "/v1/messages/1",
        {"content": "tampered"},
        {"owner": 404, "agent": 404, "plugin": 404},
    ),
]


MATRIX.extend(
    [
        ("GET", "/v1/openapi.json", None, {"owner": 200, "agent": 200, "plugin": 200}),
        (
            "GET",
            "/v1/context?message_id=999",
            None,
            {"owner": 200, "agent": 200, "plugin": 403},
        ),
        (
            "GET",
            "/v1/search?q=synthetic",
            None,
            {"owner": 200, "agent": 200, "plugin": 403},
        ),
        (
            "GET",
            "/v1/person?q=synthetic",
            None,
            {"owner": 200, "agent": 200, "plugin": 403},
        ),
        (
            "GET",
            "/v1/identity/people",
            None,
            {"owner": 200, "agent": 200, "plugin": 403},
        ),
        (
            "GET",
            "/v1/identity/unclaimed",
            None,
            {"owner": 200, "agent": 200, "plugin": 403},
        ),
        (
            "GET",
            "/v1/registry/schemes",
            None,
            {"owner": 200, "agent": 200, "plugin": 403},
        ),
        (
            "POST",
            "/v1/registry/fact-keys",
            {"key": "synthetic"},
            {"owner": 200, "agent": 200, "plugin": 403},
        ),
        (
            "POST",
            "/v1/entities",
            {"kind": "synthetic", "name": "Synthetic entity"},
            {"owner": 200, "agent": 200, "plugin": 200},
        ),
        (
            "POST",
            "/v1/links",
            {
                "type": "synthetic",
                "from": {"kind": "page", "id": 1},
                "to": {"kind": "table_row", "id": 2},
            },
            # A plugin asserts links about its own messages and entities only.
            {"owner": 200, "agent": 200, "plugin": 403},
        ),
        (
            "PATCH",
            "/v1/links/999",
            {"status": "removed"},
            # Changing a link that does not exist is reported, not ignored.
            {"owner": 404, "agent": 404, "plugin": 404},
        ),
        (
            "POST",
            "/v1/track/detect-visits",
            {},
            {"owner": 200, "agent": 200, "plugin": 403},
        ),
        (
            "POST",
            "/v1/reparse",
            {"channel": "synthetic"},
            {"owner": 400, "agent": 403, "plugin": 403},
        ),
    ]
)


@pytest.mark.parametrize("kind", ["owner", "agent", "plugin"])
@pytest.mark.parametrize("method,path,body,expected", MATRIX)
def test_acl_6_permission_matrix(client, kind, method, path, body, expected):
    token = create_token(
        "synthetic-" + kind, kind, "synthetic" if kind == "plugin" else None
    )["token"]
    r = client.request(
        method, path, json=body, headers={"Authorization": "Bearer " + token}
    )
    assert r.status_code == expected[kind], r.text


def test_acl_6_token_revocation_scopes_askers(client):
    token = create_token("synthetic-plugin", "plugin", "synthetic")["token"]
    headers = {"Authorization": "Bearer " + token}
    assert (
        client.post(
            "/v1/channels",
            json={"name": "fake.other", "plugin": "other"},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.put("/v1/plugins/other/cursor/k", json={}, headers=headers).status_code
        == 403
    )
    agent = create_token("synthetic-agent", "agent", askers=["fake-allowed"])["token"]
    assert (
        client.post(
            "/v1/sql",
            json={"statement": "SELECT 1"},
            headers={
                "Authorization": "Bearer " + agent,
                "X-Throughline-Asker": "fake-denied",
            },
        ).status_code
        == 403
    )
    with connect() as c:
        c.execute("UPDATE tl.tokens SET revoked_at=now() WHERE name='synthetic-agent'")
    assert (
        client.get(
            "/v1/channels", headers={"Authorization": "Bearer " + agent}
        ).status_code
        == 401
    )
    assert client.get("/v1/channels", headers={"Authorization": ""}).status_code == 401


def test_arc_5_owner_trigger_still_immutable_without_purge(client, channel):
    ingest(client, [message()])
    with pytest.raises(psycopg.Error):
        with connect() as c:
            c.execute("UPDATE tl.messages SET content='tampered'")


def test_qry_1_timeout_cancels_runaway_query(client):
    import time

    start = time.perf_counter()
    r = client.post(
        "/v1/sql",
        json={"statement": "SELECT count(*) FROM generate_series(1,1000000000)"},
    )
    elapsed = time.perf_counter() - start
    assert r.status_code == 400 and r.json()["code"] == "57014", r.text
    assert 29 <= elapsed < 40
