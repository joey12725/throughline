"""Result sets: asker-scoped working sets joined live to the gated record
views, sessions, transforms, inspect views and expiry (migration 0039).
Synthetic data only."""

import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import ingest, message
from fastapi.testclient import TestClient
from psycopg import sql
from typer.testing import CliRunner

from throughline import access, resultsets
from throughline.api import create_app
from throughline.budget import estimate_tokens
from throughline.db import connect, create_token
from throughline.query import reader_transaction
from throughline.resultsets import ResultSetNotFound, ResultSetStale
from throughline.scope import Scope

NOTES = "synthetic.notes"
PAGES = "synthetic.pages"
ASKER = "fake-asker"
ZERO = "rs_" + "0" * 32
EXTRAS = [
    "span_first",
    "span_last",
    "score",
    "role",
    "depends_on",
    "groups",
    "discovery",
    "meta",
    "set_completeness",
]
RESULT_TABLES = ("sessions", "result_sets", "result_set_members", "result_set_groups")


# --- helpers ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_askers(database):
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


def owner_token():
    with connect() as c:
        return c.execute(
            "SELECT * FROM tl.tokens WHERE kind='owner' AND revoked_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()


def owner_scope():
    return Scope.from_token(dict(owner_token(), asker=None))


def asker_scope(asker=ASKER):
    return Scope.from_token(dict(owner_token(), asker=asker))


def add_channel(client, name):
    r = client.post("/v1/channels", json=dict(name=name, plugin="synthetic"))
    assert r.status_code == 200, r.text


def ids(channel="synthetic.chat"):
    """{external_id: message row id} of current messages in a channel."""
    with connect() as c:
        rows = c.execute(
            "SELECT m.id,m.external_id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.superseded_by IS NULL ORDER BY m.id",
            (channel,),
        ).fetchall()
    return {r["external_id"]: r["id"] for r in rows}


def seed(client, n=5, channel="synthetic.chat", start=0, **kwargs):
    if channel != "synthetic.chat":
        kwargs.setdefault("fields", {})
    messages = [
        message(i, channel=channel, external_id=f"{channel}-{i}", **kwargs)
        for i in range(start, start + n)
    ]
    r = ingest(client, messages)
    assert r.status_code == 200 and r.json()["rejected"] == [], r.text
    found = ids(channel)
    return [found[f"{channel}-{i}"] for i in range(start, start + n)]


def restricted(*channels):
    access.register_asker(ASKER, "restricted")
    for channel in channels:
        access.grant(ASKER, channel=channel)


@contextmanager
def reader(scope):
    with reader_transaction(scope) as c:
        yield c


def result_set_rows(scope, handle):
    with reader(scope) as c:
        return c.execute(
            "SELECT * FROM api.result_set(%s) ORDER BY ordinal", (handle,)
        ).fetchall()


def contract_columns():
    with connect() as c:
        cursor = c.execute("SELECT * FROM api.record_versions LIMIT 0")
        return [d.name for d in cursor.description]


def make(scope, refs, completeness="exhaustive", **kwargs):
    return resultsets.create(scope, refs, completeness=completeness, **kwargs)


def refs(message_ids, **extra):
    return [dict(record_id=f"m:{i}", **extra) for i in message_ids]


def table_counts():
    with connect() as c:
        names = [
            r["tablename"]
            for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='tl' ORDER BY 1"
            )
        ]
        return {
            name: c.execute(
                sql.SQL("SELECT count(*) n FROM {}").format(sql.Identifier("tl", name))
            ).fetchone()["n"]
            for name in names
        }


# Stand-in record views for prefixes another package registers (links,
# entities) and a page view whose rows need every source visible. They are
# built only when the prefix is missing and removed afterwards.
STAND_INS = {
    "l": (
        "rs_fake_link_records",
        "EXISTS(SELECT 1 FROM api.links k WHERE k.id=v.ref_id)",
        "SELECT 'l:'||l.id AS record_id,'link'::text AS kind,l.id AS ref_id,NULL::bigint AS message_id,NULL::int AS version,true AS is_current,NULL::text AS channel,NULL::text AS external_id,NULL::bigint AS thread_id,NULL::int AS thread_no,l.type AS content,l.created_at AS record_time,NULL::timestamptz AS ended_at,l.created_at AS ingested_at,NULL::timestamptz AS subject_from,NULL::timestamptz AS subject_to,NULL::text AS subject_from_precision,NULL::text AS subject_to_precision,NULL::bigint AS person_id,NULL::text AS author_handle,NULL::float8 AS lat,NULL::float8 AS lon,'not_applicable'::text AS loc_reason,false AS is_outside,NULL::text[] AS heading_path,NULL::int AS char_start,NULL::int AS char_end,NULL::jsonb AS claim,l.method IN ('rule','model') AS generated,false AS unsupported,jsonb_build_object('method',l.method) AS provenance,coalesce(l.evidence->'message_ids','[]'::jsonb) AS support,NULL::jsonb AS expansion FROM tl.links l",
    ),
    "e": (
        "rs_fake_entity_records",
        "false",
        "SELECT 'e:'||e.id AS record_id,'entity'::text AS kind,e.id AS ref_id,NULL::bigint AS message_id,NULL::int AS version,true AS is_current,NULL::text AS channel,NULL::text AS external_id,NULL::bigint AS thread_id,NULL::int AS thread_no,coalesce(e.name,e.kind) AS content,NULL::timestamptz AS record_time,NULL::timestamptz AS ended_at,NULL::timestamptz AS ingested_at,NULL::timestamptz AS subject_from,NULL::timestamptz AS subject_to,NULL::text AS subject_from_precision,NULL::text AS subject_to_precision,NULL::bigint AS person_id,NULL::text AS author_handle,NULL::float8 AS lat,NULL::float8 AS lon,'not_applicable'::text AS loc_reason,false AS is_outside,NULL::text[] AS heading_path,NULL::int AS char_start,NULL::int AS char_end,NULL::jsonb AS claim,true AS generated,true AS unsupported,'{}'::jsonb AS provenance,'[]'::jsonb AS support,NULL::jsonb AS expansion FROM tl.entities e",
    ),
    # A page record (prefix x, never registered by a package) visible only
    # when its page message and every 'fake_source' it cites are visible.
    "x": (
        "rs_fake_page_records",
        "api.message_visible(v.ref_id) AND api.sources_visible(ARRAY(SELECT l.to_id FROM tl.links l WHERE l.type='fake_source' AND l.from_kind='message' AND l.from_id=v.ref_id AND l.to_kind='message'))",
        "SELECT 'x:'||m.id AS record_id,'page_revision'::text AS kind,m.id AS ref_id,m.id AS message_id,m.version,m.superseded_by IS NULL AS is_current,c.name AS channel,m.external_id,m.thread_id,m.thread_no,m.content,m.happened_at AS record_time,m.ended_at,m.ingested_at,m.subject_from,m.subject_to,m.subject_from_precision,m.subject_to_precision,m.person_id,m.author_handle,m.lat,m.lon,m.loc_reason,false AS is_outside,NULL::text[] AS heading_path,NULL::int AS char_start,NULL::int AS char_end,NULL::jsonb AS claim,true AS generated,false AS unsupported,'{}'::jsonb AS provenance,'[]'::jsonb AS support,NULL::jsonb AS expansion FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name='synthetic.pages'",
    ),
}


@contextmanager
def stand_ins():
    made = []
    try:
        with connect() as c:
            present = {
                r["prefix"]
                for r in c.execute(
                    "SELECT prefix FROM tl.record_sources WHERE to_regclass(relation) IS NOT NULL"
                )
            }
            for prefix, (view, predicate, definition) in STAND_INS.items():
                if prefix in present:
                    continue
                c.execute("DELETE FROM tl.record_sources WHERE prefix=%s", (prefix,))
                c.execute(
                    "SELECT tl.install_gate(%s,%s,%s)", (view, predicate, definition)
                )
                c.execute(
                    "INSERT INTO tl.record_sources VALUES (%s,%s)",
                    (prefix, "api." + view),
                )
                made.append((prefix, view))
        yield
    finally:
        with connect() as c:
            for prefix, view in made:
                c.execute("DELETE FROM tl.record_sources WHERE prefix=%s", (prefix,))
                c.execute(
                    sql.SQL("DROP VIEW IF EXISTS {}, {}, {}").format(
                        sql.Identifier("api", view),
                        sql.Identifier("tl", view + "_gated"),
                        sql.Identifier("tl", view + "_all"),
                    )
                )
                c.execute("DELETE FROM tl.gates WHERE view_name=%s", (view,))


# --- QRY-34 -----------------------------------------------------------------


def test_qry_34_roundtrip_and_sql_composition(client, channel):
    stored = seed(client, 3)
    r = client.post(
        "/v1/sets",
        json=dict(
            records=[
                dict(record_id=f"m:{stored[0]}", score=0.9),
                dict(record_id=f"m:{stored[2]}", score=0.4, role="context"),
            ]
        ),
    )
    assert r.status_code == 200, r.text
    info = r.json()
    handle = info["handle"]
    assert re.fullmatch(r"rs_[0-9a-f]{32}", handle)
    assert info["member_count"] == 2 and info["completeness"] == "exhaustive"

    statement = f"SELECT r.ordinal,r.record_id,r.version,r.score,r.role,m.content,m.external_id FROM api.result_set('{handle}') r JOIN api.messages m ON m.id=r.message_id ORDER BY r.ordinal"
    r = client.post("/v1/sql", json={"statement": statement})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    assert [row["record_id"] for row in rows] == [f"m:{stored[0]}", f"m:{stored[2]}"]
    assert [row["content"] for row in rows] == [
        "Synthetic orchard message 0",
        "Synthetic orchard message 2",
    ]
    assert [row["role"] for row in rows] == ["hit", "context"]
    assert rows[0]["score"] == pytest.approx(0.9)
    # Aggregates and the info function compose like any other relation.
    r = client.post(
        "/v1/sql",
        json={
            "statement": f"SELECT count(*) AS n, max(score) AS top FROM api.result_set('{handle}')"
        },
    )
    assert r.json()["rows"][0]["n"] == 2
    r = client.post(
        "/v1/sql",
        json={
            "statement": f"SELECT completeness, member_count FROM api.result_set_info('{handle}')"
        },
    )
    assert r.json()["rows"] == [{"completeness": "exhaustive", "member_count": 2}]

    r = client.get(f"/v1/sets/{handle}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(contract_columns()) <= set(body["records"][0])
    assert body["records"][0]["content"] == "Synthetic orchard message 0"
    assert body["info"]["member_count"] == 2
    assert body["execution"]["completeness"] == "exhaustive"
    assert client.get(f"/v1/sets/{handle}/info").json()["handle"] == handle

    # A caller-supplied id that does not exist is 404, like a hidden one.
    r = client.post("/v1/sets", json=dict(records=[dict(record_id="m:999999")]))
    assert r.status_code == 404
    r = client.post("/v1/sets", json=dict(records=[dict(record_id="q:1")]))
    assert r.status_code == 404
    r = client.post("/v1/sets", json=dict(records=[dict(record_id="bad")]))
    assert r.status_code == 400
    r = client.post(
        "/v1/sets",
        json=dict(records=[dict(record_id=f"m:{stored[0]}", depends_on="m:999999")]),
    )
    assert r.status_code == 404

    assert client.delete(f"/v1/sets/{handle}").status_code == 200
    assert client.get(f"/v1/sets/{handle}").status_code == 404


def test_qry_34_pinned_version_live_text(client, channel):
    first = seed(client, 1)[0]
    scope = owner_scope()
    handle = make(scope, refs([first]))
    r = ingest(
        client,
        [message(0, external_id="synthetic.chat-0", content="Edited orchard text")],
    )
    assert r.status_code == 200, r.text
    current = ids()["synthetic.chat-0"]
    assert current != first
    later = make(scope, refs([current]))

    [pinned] = resultsets.read(scope, handle)["records"]
    assert (pinned["record_id"], pinned["version"], pinned["is_current"]) == (
        f"m:{first}",
        1,
        False,
    )
    assert pinned["content"] == "Synthetic orchard message 0"
    [edited] = resultsets.read(scope, later)["records"]
    assert (edited["version"], edited["is_current"]) == (2, True)
    assert edited["content"] == "Edited orchard text"
    # The text is read live: the set itself holds none.
    with connect() as c:
        stored = json.dumps(
            c.execute(
                "SELECT jsonb_agg(to_jsonb(x)) j FROM tl.result_set_members x"
            ).fetchone()["j"]
        )
    assert "orchard" not in stored.lower()
    # A version given with the id must be the one the record has.
    with connect() as c:
        assert (
            c.execute(
                "SELECT version FROM tl.result_set_members WHERE handle=%s", (handle,)
            ).fetchone()["version"]
            == 1
        )
    r = client.post(
        "/v1/sets", json=dict(records=[dict(record_id=f"m:{first}", version=2)])
    )
    assert r.status_code == 404


def test_qry_34_session_history_and_unresolved(client, channel):
    stored = seed(client, 4)
    r = client.post("/v1/sessions", json=dict(note="synthetic exploration"))
    assert r.status_code == 200, r.text
    session_id = r.json()["id"]
    r = client.post(
        "/v1/sets",
        json=dict(
            records=[dict(record_id=f"m:{i}") for i in stored], session=session_id
        ),
    )
    assert r.status_code == 200, r.text
    base = r.json()["handle"]
    assert r.json()["session_id"] == session_id
    r = client.post(
        "/v1/sets/transform", json=dict(op="head", handles=[base], params=dict(n=2))
    )
    assert r.status_code == 200, r.text
    head = r.json()["handle"]
    r = client.post(
        "/v1/sets/transform",
        json=dict(op="order", handles=[head], params=dict(by="record_id")),
    )
    ordered = r.json()["handle"]

    r = client.get(f"/v1/sessions/{session_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["note"] == "synthetic exploration"
    assert [s["handle"] for s in body["sets"]] == [base, head, ordered]
    assert [s["parents"] for s in body["sets"]] == [[], [base], [head]]
    assert [s["origin"]["op"] for s in body["sets"]] == ["create", "head", "order"]
    assert body["unresolved"] == []

    scope = owner_scope()
    updated = resultsets.set_unresolved(
        scope, session_id, ["who attended the synthetic meeting", {"need": "dates"}]
    )
    assert updated["unresolved"] == [
        "who attended the synthetic meeting",
        {"need": "dates"},
    ]
    with pytest.raises(ValueError):
        resultsets.set_unresolved(scope, session_id, ["x" * 501])
    history = resultsets.inspect(scope, ordered, "history")
    assert [i["handle"] for i in history["items"]] == [base, head, ordered]
    assert history["unresolved"][1] == {"need": "dates"}
    # Foreign or malformed session ids are the same 404.
    assert client.get("/v1/sessions/not-a-uuid").status_code == 404
    other = create_token("synthetic other owner")["token"]
    with TestClient(create_app()) as second:
        second.headers["Authorization"] = "Bearer " + other
        assert second.get(f"/v1/sessions/{session_id}").status_code == 404
        r = second.post(
            "/v1/sets",
            json=dict(records=[dict(record_id=f"m:{stored[0]}")], session=session_id),
        )
        assert r.status_code == 404


def test_no_content_stored(client, channel):
    stored = seed(client, 3, content="Zebra-unique synthetic phrase")
    scope = owner_scope()
    session_id = resultsets.open_session(scope, "synthetic")["id"]
    handle = make(
        scope,
        refs(stored, score=1.0, discovery=[dict(leg="lexical", rank=1)]),
        session=session_id,
    )
    resultsets.transform(scope, "group", [handle], by="thread")
    with connect() as c:
        for table in RESULT_TABLES:
            columns = {
                r["column_name"]
                for r in c.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='tl' AND table_name=%s",
                    (table,),
                )
            }
            assert not columns & {"content", "text", "excerpt", "claim"}, table
            dumped = c.execute(
                sql.SQL(
                    "SELECT coalesce(jsonb_agg(to_jsonb(t)),'[]')::text j FROM {} t"
                ).format(sql.Identifier("tl", table))
            ).fetchone()["j"]
            assert "zebra" not in dumped.lower(), table
            assert "orchard" not in dumped.lower(), table
    # Operator output is short: a long value is rejected, not stored.
    with pytest.raises(ValueError):
        make(scope, [dict(record_id=f"m:{stored[0]}", meta={"note": "x" * 501})])
    with pytest.raises(ValueError):
        make(scope, [dict(record_id=f"m:{stored[0]}", meta={"x" * 501: 1})])
    assert make(scope, [dict(record_id=f"m:{stored[0]}", meta={"note": "x" * 500})])
    r = client.post(
        "/v1/sets",
        json=dict(records=[dict(record_id=f"m:{stored[0]}", meta={"v": ["y" * 600]})]),
    )
    assert r.status_code == 400
    # The database refuses it too, whatever writes it.
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect("app") as c:
            c.execute(
                "INSERT INTO tl.result_set_members(handle,ordinal,prefix,ref_id,record_id,message_id,version,meta) VALUES (%s,99,'m',%s,%s,%s,1,%s)",
                (
                    handle,
                    stored[0],
                    f"m:{stored[0]}",
                    stored[0],
                    json.dumps({"deep": {"text": "z" * 501}}),
                ),
            )


# --- ACL-10 ------------------------------------------------------------------


def test_acl_10_other_asker_or_token_reads_nothing_and_404(client, channel):
    stored = seed(client, 3)
    r = client.post(
        "/v1/sets", json=dict(records=[dict(record_id=f"m:{i}") for i in stored])
    )
    handle = r.json()["handle"]
    unknown = "rs_" + "f" * 32

    other = create_token("synthetic other owner")
    agent = create_token("synthetic agent", kind="agent")
    for header in (
        {"Authorization": "Bearer " + other["token"]},
        {"Authorization": "Bearer " + agent["token"]},
        # Same token, but asking for someone else.
        {
            "Authorization": client.headers["Authorization"],
            "X-Throughline-Asker": ASKER,
        },
    ):
        with TestClient(create_app()) as caller:
            caller.headers.update(header)
            for path in (f"/v1/sets/{handle}", f"/v1/sets/{unknown}"):
                r = caller.get(path)
                assert (
                    r.status_code == 404
                    and r.json()["detail"] == "result set not found"
                )
            assert caller.get(f"/v1/sets/{handle}/info").status_code == 404
            assert caller.post(f"/v1/sets/{handle}/inspect", json={}).status_code == 404
            assert caller.delete(f"/v1/sets/{handle}").status_code == 404
            r = caller.post(
                "/v1/sets/transform", json=dict(op="head", handles=[handle])
            )
            assert r.status_code == 404
            r = caller.post(
                "/v1/sql",
                json={"statement": f"SELECT * FROM api.result_set('{handle}')"},
            )
            assert r.status_code == 200 and r.json()["rows"] == []
            r = caller.post(
                "/v1/sql",
                json={"statement": f"SELECT * FROM api.result_set_info('{handle}')"},
            )
            assert r.json()["rows"] == []
    for scope in (
        Scope.from_token(dict(id=other["id"], kind="owner", asker=None)),
        Scope.from_token(dict(id=agent["id"], kind="agent", askers=[], asker=None)),
        asker_scope(),
        Scope.local(),
    ):
        with pytest.raises(ResultSetNotFound):
            resultsets.members(scope, handle)
        assert result_set_rows(scope, handle) == []
    # The owner still reads its own set.
    assert len(resultsets.members(owner_scope(), handle)) == 3
    # A set written for an asker belongs to that asker only.
    restricted("synthetic.chat")
    mine = make(asker_scope(), refs(stored))
    assert len(resultsets.members(asker_scope(), mine)) == 3
    with pytest.raises(ResultSetNotFound):
        resultsets.members(owner_scope(), mine)


def test_acl_10_policy_change_invalidates(client, channel):
    stored = seed(client, 2)
    scope = owner_scope()
    session_id = resultsets.open_session(scope)["id"]
    handle = make(scope, refs(stored), session=session_id)
    assert len(result_set_rows(scope, handle)) == 2
    access.register_asker("fake-other", "restricted")
    with pytest.raises(ResultSetStale):
        resultsets.read(scope, handle)
    with pytest.raises(ResultSetStale):
        resultsets.session(scope, session_id)
    with pytest.raises(ResultSetStale):
        resultsets.transform(scope, "head", [handle], n=1)
    assert result_set_rows(scope, handle) == []
    with reader(scope) as c:
        assert (
            c.execute("SELECT * FROM api.result_set_info(%s)", (handle,)).fetchall()
            == []
        )
    r = client.get(f"/v1/sets/{handle}")
    assert r.status_code == 409
    assert client.get(f"/v1/sets/{handle}/info").status_code == 409
    assert client.get(f"/v1/sessions/{session_id}").status_code == 409
    # A stale set can still be dropped by its owner.
    assert client.delete(f"/v1/sets/{handle}").status_code == 200
    assert client.get(f"/v1/sets/{handle}").status_code == 404
    # New sets work under the new policy.
    assert len(resultsets.members(scope, make(scope, refs(stored)))) == 2


def test_acl_10_revoked_token_loses_sets(client, channel):
    stored = seed(client, 2)
    agent = create_token("synthetic agent", kind="agent")
    with TestClient(create_app()) as caller:
        caller.headers["Authorization"] = "Bearer " + agent["token"]
        r = caller.post(
            "/v1/sets", json=dict(records=[dict(record_id=f"m:{i}") for i in stored])
        )
        assert r.status_code == 200, r.text
        handle = r.json()["handle"]
        assert caller.get(f"/v1/sets/{handle}").status_code == 200
        scope = Scope.from_token(
            dict(id=agent["id"], kind="agent", askers=[], asker=None)
        )
        assert len(resultsets.members(scope, handle)) == 2
        with connect() as c:
            c.execute(
                "UPDATE tl.tokens SET revoked_at=now() WHERE id=%s", (agent["id"],)
            )
        assert caller.get(f"/v1/sets/{handle}").status_code == 401
    with pytest.raises(ResultSetNotFound):
        resultsets.members(scope, handle)
    assert result_set_rows(scope, handle) == []
    # Even a policy version equal to the set's would not revive it: the token
    # check is separate from the version check.
    with connect() as c:
        c.execute(
            "UPDATE tl.result_sets SET policy_version=api.policy_version() WHERE handle=%s",
            (handle,),
        )
    assert result_set_rows(scope, handle) == []
    # Deleting the token deletes its sets.
    with connect() as c:
        c.execute("DELETE FROM tl.audit_log WHERE token_id=%s", (agent["id"],))
        c.execute("DELETE FROM tl.tokens WHERE id=%s", (agent["id"],))
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.result_set_members WHERE handle=%s",
                (handle,),
            ).fetchone()["n"]
            == 0
        )


# --- ACL-9 -------------------------------------------------------------------


def test_acl_9_hidden_members_disappear_including_derived(client, channel):
    add_channel(client, NOTES)
    add_channel(client, PAGES)
    visible = seed(client, 2)
    hidden = seed(client, 1, channel=NOTES)
    page = seed(client, 1, channel=PAGES)[0]
    restricted("synthetic.chat", PAGES)
    with connect() as c:
        entity = c.execute(
            "INSERT INTO tl.entities(kind,name) VALUES ('work','Synthetic work') RETURNING id"
        ).fetchone()["id"]
        link = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,evidence) VALUES ('fake_about','message',%s,'message',%s,%s) RETURNING id",
            (visible[0], visible[1], json.dumps({"message_ids": [hidden[0]]})),
        ).fetchone()["id"]
        clean = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) VALUES ('fake_about','message',%s,'message',%s) RETURNING id",
            (visible[0], visible[1]),
        ).fetchone()["id"]
        c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) VALUES ('fake_source','message',%s,'message',%s)",
            (page, visible[0]),
        )
    # Versions are given, as recall gives them: a message member without one
    # is pinned to the version the scope sees, and a hidden one has none.
    everything = refs(visible + hidden, version=1) + [
        dict(record_id=f"e:{entity}"),
        dict(record_id=f"l:{link}"),
        dict(record_id=f"l:{clean}"),
        dict(record_id=f"x:{page}"),
    ]
    with stand_ins():
        owner = owner_scope()
        full = make(owner, everything)
        assert len(resultsets.members(owner, full)) == 7

        scope = asker_scope()
        handle = make(scope, everything)
        expected = [f"m:{visible[0]}", f"m:{visible[1]}", f"l:{clean}", f"x:{page}"]
        assert [m["record_id"] for m in resultsets.members(scope, handle)] == expected
        assert [
            r["record_id"] for r in resultsets.read(scope, handle)["records"]
        ] == expected
        info = resultsets.info(scope, handle)
        assert info["member_count"] == 4 and info["counts"]["returned"] == 4
        summary = resultsets.inspect(scope, handle, "summary")
        assert summary["member_count"] == 4
        assert sum(i["n"] for i in summary["items"]) == 4
        with reader(scope) as c:
            assert (
                c.execute(
                    "SELECT count(*) n FROM api.result_set(%s)", (handle,)
                ).fetchone()["n"]
                == 4
            )
            assert (
                c.execute(
                    "SELECT member_count FROM api.result_set_info(%s)", (handle,)
                ).fetchone()["member_count"]
                == 4
            )
        # Transforms see only what the asker sees.
        copy = resultsets.transform(scope, "union", [handle])
        with connect() as c:
            assert (
                c.execute(
                    "SELECT count(*) n FROM tl.result_set_members WHERE handle=%s",
                    (copy,),
                ).fetchone()["n"]
                == 4
            )

        # A data change, not a policy change: the page now also cites a hidden
        # source, so it is hidden from the asker from this read on.
        version = access.policy_version()
        with connect() as c:
            c.execute(
                "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) VALUES ('fake_source','message',%s,'message',%s)",
                (page, hidden[0]),
            )
        assert access.policy_version() == version
        assert [m["record_id"] for m in resultsets.members(scope, handle)] == expected[
            :3
        ]
        assert resultsets.info(scope, handle)["member_count"] == 3
        assert len(resultsets.members(owner, full)) == 7

        # Over HTTP a restricted asker cannot name a hidden record at all.
        with TestClient(create_app()) as caller:
            caller.headers.update(
                {
                    "Authorization": client.headers["Authorization"],
                    "X-Throughline-Asker": ASKER,
                }
            )
            for record_id in (
                f"m:{hidden[0]}",
                f"e:{entity}",
                f"l:{link}",
                f"x:{page}",
            ):
                r = caller.post(
                    "/v1/sets", json=dict(records=[dict(record_id=record_id)])
                )
                assert r.status_code == 404, record_id
            r = caller.post(
                "/v1/sets", json=dict(records=[dict(record_id=f"m:{visible[0]}")])
            )
            assert r.status_code == 200, r.text


def test_result_set_columns_equal_record_contract(client):
    contract = contract_columns()
    with connect() as c:
        cursor = c.execute(f"SELECT * FROM api.result_set('{ZERO}')")
        columns = [d.name for d in cursor.description]
        registered = c.execute(
            "SELECT prefix,relation FROM tl.record_sources"
        ).fetchall()
        result_type = c.execute(
            "SELECT pg_get_function_result('api.result_set(text)'::regprocedure) r"
        ).fetchone()["r"]
        for row in registered:
            cursor = c.execute(
                sql.SQL("SELECT * FROM {} LIMIT 0").format(
                    sql.Identifier(*row["relation"].split("."))
                )
            )
            assert [d.name for d in cursor.description][: len(contract)] == contract
    assert columns == ["ordinal"] + contract + EXTRAS
    assert contract == list(resultsets.CONTRACT)
    assert "content text" in result_type and "set_completeness text" in result_type


# --- QRY-35 transforms -----------------------------------------------------------


def test_qry_35_transforms_and_origin_chain(client, channel):
    add_channel(client, NOTES)
    stored = seed(client, 6)
    notes = seed(client, 2, channel=NOTES, start=10)
    scope = owner_scope()
    a = make(
        scope,
        [
            dict(
                record_id=f"m:{i}",
                score=s,
                meta={"label": "keep" if s > 0.3 else "drop"},
            )
            for i, s in zip(stored[:4], (0.9, 0.2, 0.5, 0.7))
        ]
        + refs(notes, score=0.1, role="context"),
    )
    b = make(scope, refs(stored[2:6], score=0.6))

    def members(handle):
        return [m["record_id"] for m in resultsets.members(scope, handle)]

    union = resultsets.transform(scope, "union", [a, b])
    assert members(union) == [f"m:{i}" for i in stored[:4] + notes + stored[4:6]]
    inter = resultsets.transform(scope, "intersect", [a, b])
    assert members(inter) == [f"m:{i}" for i in stored[2:4]]
    sub = resultsets.transform(scope, "subtract", [a, b])
    assert members(sub) == [f"m:{i}" for i in stored[:2] + notes]

    def filtered(**params):
        return members(resultsets.transform(scope, "filter", [a], **params))

    assert filtered(min_score=0.5) == [
        f"m:{stored[0]}",
        f"m:{stored[2]}",
        f"m:{stored[3]}",
    ]
    assert filtered(channels=[NOTES]) == [f"m:{i}" for i in notes]
    assert filtered(role="context") == [f"m:{i}" for i in notes]
    assert filtered(meta={"label": "drop"}) == [f"m:{stored[1]}"]
    assert filtered(kinds=["life_event"]) == members(a)
    assert filtered(kinds=["entity"]) == []
    t0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert filtered(
        after=(t0 + timedelta(seconds=1)).isoformat(),
        before=(t0 + timedelta(seconds=3)).isoformat(),
    ) == [
        f"m:{stored[1]}",
        f"m:{stored[2]}",
    ]
    with pytest.raises(ValueError):
        resultsets.transform(scope, "filter", [a], colour="blue")
    with pytest.raises(ValueError):
        resultsets.transform(scope, "explode", [a])

    ordered = resultsets.transform(scope, "order", [union], by="score")
    scores = [m["score"] for m in resultsets.members(scope, ordered)]
    assert scores == sorted(scores, reverse=True)
    head = resultsets.transform(scope, "head", [ordered], n=2)
    assert members(head) == [f"m:{stored[0]}", f"m:{stored[3]}"]
    first = resultsets.transform(scope, "sample", [union], n=3, seed=7)
    second = resultsets.transform(scope, "sample", [union], n=3, seed=7)
    assert members(first) == members(second) and len(members(first)) == 3
    assert set(members(first)) <= set(members(union))
    doubled = make(scope, refs(stored[:2]) + refs(stored[:1]))
    assert members(resultsets.transform(scope, "dedup", [doubled])) == [
        f"m:{stored[0]}",
        f"m:{stored[1]}",
    ]
    spans = make(
        scope,
        [
            dict(record_id=f"m:{stored[0]}", span=dict(first_no=1, last_no=3)),
            dict(record_id=f"m:{stored[2]}", span=dict(first_no=3, last_no=4)),
            dict(record_id=f"m:{stored[4]}", span=dict(first_no=5, last_no=6)),
        ],
    )
    assert members(resultsets.transform(scope, "dedup", [spans], by="span")) == [
        f"m:{stored[0]}",
        f"m:{stored[4]}",
    ]
    # Every message is its own support: nothing is a support duplicate.
    assert (
        len(members(resultsets.transform(scope, "dedup", [union], by="support"))) == 8
    )

    # The origin chain walks back to the created sets.
    chain = resultsets.inspect(scope, head, "origin")["items"]
    assert [(i["handle"], i["op"]) for i in chain] == [
        (head, "head"),
        (ordered, "order"),
        (union, "union"),
        (a, "create"),
        (b, "create"),
    ]
    assert chain[0]["params"] == {"n": 2} and chain[2]["parents"] == [a, b]
    # Handles are inputs to SQL, including composition of two sets.
    with reader(scope) as c:
        rows = c.execute(
            "SELECT x.record_id FROM api.result_set(%s) x JOIN api.result_set(%s) y USING (record_id,version) ORDER BY x.ordinal",
            (a, b),
        ).fetchall()
    assert [r["record_id"] for r in rows] == members(inter)
    # Over HTTP.
    r = client.post(
        "/v1/sets/transform",
        json=dict(op="filter", handles=[a], params=dict(min_score=0.8)),
    )
    assert r.status_code == 200, r.text
    assert r.json()["member_count"] == 1
    assert r.json()["origin"] == {
        "op": "filter",
        "params": {"min_score": 0.8},
        "parents": [a],
    }
    r = client.post(
        "/v1/sets/transform", json=dict(op="filter", handles=[a], params=dict(bad=1))
    )
    assert r.status_code == 400


# --- LIB-9 / QRY-30 grouping ------------------------------------------------------


def test_lib_9_member_in_several_groups_with_uncertainty(client, channel):
    stored = seed(client, 3)
    scope = owner_scope()
    base = make(scope, refs(stored))
    grouped = resultsets.transform(
        scope,
        "group",
        [base],
        by="explicit",
        assignments={
            f"m:{stored[0]}": ["fake-a", {"grp": "fake-b", "certain": False}],
            f"m:{stored[1]}": ["fake-a"],
        },
    )
    grouped = resultsets.transform(scope, "group", [grouped], by="person")
    found = {
        m["record_id"]: {g["grp"]: g["certain"] for g in m["groups"]}
        for m in resultsets.members(scope, grouped)
    }
    # Two explicit groups (one tentative) plus the person group.
    first = found[f"m:{stored[0]}"]
    assert {k: v for k, v in first.items() if k.startswith("fake-")} == {
        "fake-a": True,
        "fake-b": False,
    }
    assert len(first) == 3
    assert [k for k in found[f"m:{stored[2]}"]] == [
        next(k for k in first if k.startswith("person:"))
    ]
    with connect() as c:
        rows = c.execute(
            "SELECT ordinal,grp,certain FROM tl.result_set_groups WHERE handle=%s ORDER BY ordinal,grp",
            (grouped,),
        ).fetchall()
    assert [(r["ordinal"], r["grp"]) for r in rows][:2] == [
        (0, "fake-a"),
        (0, "fake-b"),
    ]
    manifest = resultsets.inspect(scope, grouped, "groups")
    entries = {i["grp"]: i for i in manifest["items"]}
    assert entries["fake-a"]["members"] == 2 and entries["fake-a"]["certain"] == 2
    assert entries["fake-b"]["uncertain"] == 1
    assert manifest["multi_group_members"] >= 2 and manifest["ungrouped"] == 0
    listed = resultsets.inspect(scope, grouped, "groups", {"grp": "fake-b"})
    assert listed["items"] == [
        {"ordinal": 0, "record_id": f"m:{stored[0]}", "certain": False}
    ]
    # Grouping is reversible: the parent is unchanged and ungroup removes it.
    assert all(m["groups"] == [] for m in resultsets.members(scope, base))
    plain = resultsets.transform(scope, "ungroup", [grouped], prefix="fake-")
    assert all(
        not g["grp"].startswith("fake-")
        for m in resultsets.members(scope, plain)
        for g in m["groups"]
    )
    none = resultsets.transform(scope, "ungroup", [grouped])
    assert all(m["groups"] == [] for m in resultsets.members(scope, none))
    by_day = resultsets.transform(scope, "group", [base], by="day")
    assert {
        g["grp"]: g["certain"]
        for m in resultsets.members(scope, by_day)
        for g in m["groups"]
    } == {"day:2020-01-01": True}


def test_qry_30_group_manifest_and_completeness_propagation(client, channel):
    stored = seed(client, 4)
    stored += seed(client, 2, start=10, thread={"external_id": "fake-thread-2"})
    scope = owner_scope()
    exhaustive = make(scope, refs(stored))
    by_thread = resultsets.transform(scope, "group", [exhaustive], by="thread")
    manifest = resultsets.inspect(scope, by_thread, "groups")
    assert manifest["completeness"] == "exhaustive"
    assert sorted(i["members"] for i in manifest["items"]) == [2, 4]
    assert all(i["channels"] == ["synthetic.chat"] for i in manifest["items"])
    assert all(i["first_time"] <= i["last_time"] for i in manifest["items"])
    by_conversation = resultsets.transform(
        scope, "group", [exhaustive], by="conversation"
    )
    groups = [
        g for m in resultsets.members(scope, by_conversation) for g in m["groups"]
    ]
    assert len(groups) == 6
    assert all(re.fullmatch(r"(conversation|thread):\d+", g["grp"]) for g in groups)
    assert all(g["certain"] == g["grp"].startswith("conversation:") for g in groups)
    by_month = resultsets.transform(scope, "group", [exhaustive], by="month")
    assert {
        g["grp"] for m in resultsets.members(scope, by_month) for g in m["groups"]
    } == {"month:2020-01"}

    def completeness(handle):
        return resultsets.info(scope, handle)["completeness"]

    head = resultsets.transform(scope, "head", [exhaustive], n=2)
    assert completeness(head) == "truncated"
    assert (
        completeness(resultsets.transform(scope, "head", [exhaustive], n=50))
        == "exhaustive"
    )
    assert (
        completeness(
            resultsets.transform(scope, "filter", [exhaustive], min_score=None)
        )
        == "exhaustive"
    )
    assert (
        completeness(resultsets.transform(scope, "union", [head, exhaustive]))
        == "truncated"
    )
    sample = resultsets.transform(scope, "sample", [exhaustive], n=2)
    assert completeness(sample) == "ranked_sample"
    assert (
        completeness(resultsets.transform(scope, "union", [sample, head]))
        == "ranked_sample"
    )
    assert (
        completeness(resultsets.transform(scope, "order", [sample])) == "ranked_sample"
    )
    assert (
        completeness(resultsets.transform(scope, "head", [sample], n=1))
        == "ranked_sample"
    )
    ranked = make(scope, refs(stored[:2]), completeness="ranked_sample")
    assert (
        completeness(resultsets.transform(scope, "intersect", [exhaustive, ranked]))
        == "ranked_sample"
    )
    assert (
        completeness(resultsets.transform(scope, "group", [ranked], by="day"))
        == "ranked_sample"
    )
    # SQL sees the completeness on every row.
    with reader(scope) as c:
        assert {
            r["set_completeness"]
            for r in c.execute(
                "SELECT set_completeness FROM api.result_set(%s)", (sample,)
            )
        } == {"ranked_sample"}


def test_lib_9_counts_separate(client, channel):
    stored = seed(client, 3)
    scope = owner_scope()
    handle = make(
        scope,
        refs(stored),
        completeness="ranked_sample",
        counts=dict(
            retrieved=120,
            mechanically_processed=60,
            semantically_evaluated=12,
            inspected_by_controller=4,
        ),
    )
    counts = resultsets.info(scope, handle)["counts"]
    assert counts == dict(
        retrieved=120,
        mechanically_processed=60,
        semantically_evaluated=12,
        inspected_by_controller=4,
        returned=3,
    )
    head = resultsets.transform(scope, "head", [handle], n=1)
    counts = resultsets.info(scope, head)["counts"]
    assert counts == dict(
        retrieved=3,
        mechanically_processed=3,
        semantically_evaluated=0,
        inspected_by_controller=0,
        returned=1,
    )
    legs = make(scope, refs(stored), counts=dict(retrieved=9, per_leg={"lexical": 9}))
    assert resultsets.info(scope, legs)["counts"]["per_leg"] == {"lexical": 9}
    plain = make(scope, refs(stored[:2]))
    assert resultsets.info(scope, plain)["counts"]["retrieved"] == 2
    with pytest.raises(ValueError):
        make(scope, refs(stored), counts={"retrieved": "many"})
    with pytest.raises(ValueError):
        make(scope, refs(stored), completeness="complete")


# --- LIB-5 inspect --------------------------------------------------------------


def test_lib_5_inspect_views_bounded(client, channel):
    stored = seed(client, 60, content="Synthetic orchard text " + "long " * 80)
    scope = owner_scope()
    handle = make(
        scope,
        [
            dict(
                record_id=f"m:{i}",
                score=n / 60,
                depends_on=f"m:{stored[0]}" if n == 5 else None,
            )
            for n, i in enumerate(stored[1:])
        ],
    )
    handle = resultsets.transform(scope, "group", [handle], by="day")
    for view in resultsets.VIEWS:
        for budget in (120, 1000):
            result = resultsets.inspect(scope, handle, view, token_budget=budget)
            assert estimate_tokens(result) <= budget, (view, budget)
            assert result["view"] == view and result["member_count"] == 59
    sample = resultsets.inspect(scope, handle, "sample", {"n": 50}, token_budget=400)
    assert sample["truncated"] and 0 < len(sample["items"]) < 50
    assert all(len(i["excerpt"]) <= resultsets.EXCERPT_CHARS for i in sample["items"])
    assert sample["items"][0]["excerpt_truncated"]
    full = resultsets.inspect(scope, handle, "sample", {"n": 3}, token_budget=1000)
    assert len(full["items"]) == 3 and not full["truncated"]
    summary = resultsets.inspect(scope, handle, "summary")
    assert summary["kinds"] == {"life_event": 59}
    assert summary["items"] == [{"channel": "synthetic.chat", "n": 59}]
    by_time = resultsets.inspect(scope, handle, "by_time", {"granularity": "day"})
    assert by_time["items"] == [{"bucket": "2020-01-01", "n": 59}]
    assert resultsets.inspect(scope, handle, "by_channel")["items"][0]["n"] == 59
    assert (
        sum(i["n"] for i in resultsets.inspect(scope, handle, "by_person")["items"])
        == 59
    )
    missing = resultsets.inspect(scope, handle, "missing_link")
    assert missing["items"] == [
        {
            "record_id": f"m:{stored[0]}",
            "reasons": ["depends_on"],
            "referenced_by": [f"m:{stored[6]}"],
        }
    ]
    with pytest.raises(ValueError):
        resultsets.inspect(scope, handle, "everything")
    # Over HTTP (the owner scope above is this client's token).
    r = client.post(
        f"/v1/sets/{handle}/inspect",
        json={"view": "sample", "selector": {"n": 40}, "token_budget": 200},
    )
    assert r.status_code == 200, r.text
    assert estimate_tokens(r.json()) <= 200 and r.json()["truncated"]
    r = client.post(f"/v1/sets/{handle}/inspect", json={"view": "nope"})
    assert r.status_code == 400
    # The token's return limit caps the requested budget.
    with connect() as c:
        c.execute(
            "UPDATE tl.tokens SET max_return_tokens=150 WHERE id=%s", (scope.token_id,)
        )
    r = client.post(
        f"/v1/sets/{handle}/inspect",
        json={"view": "sample", "selector": {"n": 40}, "token_budget": 100000},
    )
    assert estimate_tokens(r.json()) <= 150


# --- lifetime -------------------------------------------------------------------


def test_expiry(client, channel, monkeypatch):
    stored = seed(client, 2)
    scope = owner_scope()
    handle = make(scope, refs(stored))
    with connect() as c:
        row = c.execute(
            "SELECT expires_at-created_at AS ttl FROM tl.result_sets WHERE handle=%s",
            (handle,),
        ).fetchone()
    assert abs(row["ttl"].total_seconds() - 86400) < 60
    monkeypatch.setenv("THROUGHLINE_RESULT_SET_TTL_S", "120")
    short = make(scope, refs(stored))
    capped = make(scope, refs(stored), ttl_s=10**9)
    with connect() as c:
        ttl = {
            r["handle"]: float(r["ttl"])
            for r in c.execute(
                "SELECT handle,extract(epoch FROM expires_at-created_at) AS ttl FROM tl.result_sets"
            ).fetchall()
        }
    assert abs(ttl[short] - 120) < 60
    assert abs(ttl[capped] - 7 * 86400) < 60
    with pytest.raises(ValueError):
        make(scope, refs(stored), ttl_s=0)
    # A set never outlives its session.
    session_id = resultsets.open_session(scope, ttl_s=30)["id"]
    inner = make(scope, refs(stored), session=session_id, ttl_s=3600)
    with connect() as c:
        row = c.execute(
            "SELECT s.expires_at<=x.expires_at ok FROM tl.result_sets s JOIN tl.sessions x ON x.id=s.session_id WHERE s.handle=%s",
            (inner,),
        ).fetchone()
    assert row["ok"]

    with connect() as c:
        c.execute(
            "UPDATE tl.result_sets SET expires_at=now()-interval '1 second' WHERE handle=%s",
            (handle,),
        )
        c.execute(
            "UPDATE tl.sessions SET expires_at=now()-interval '1 second' WHERE id=%s",
            (session_id,),
        )
    with pytest.raises(ResultSetNotFound):
        resultsets.read(scope, handle)
    with pytest.raises(ResultSetNotFound):
        resultsets.session(scope, session_id)
    assert result_set_rows(scope, handle) == []
    assert result_set_rows(scope, short) != []
    removed = resultsets.expire()
    assert removed == {"sets": 1, "sessions": 1}
    with connect() as c:
        left = {r["handle"] for r in c.execute("SELECT handle FROM tl.result_sets")}
    assert handle not in left and inner not in left and short in left


def test_purge_cascades_members(client, channel):
    import throughline.cli as cli

    add_channel(client, NOTES)
    chat = seed(client, 2)
    notes = seed(client, 2, channel=NOTES)
    scope = owner_scope()
    handle = make(scope, refs(chat + notes))
    grouped = resultsets.transform(scope, "group", [handle], by="channel")
    result = CliRunner().invoke(cli.app, ["purge", NOTES, "--confirm"])
    assert result.exit_code == 0, result.output
    with connect() as c:
        left = c.execute(
            "SELECT handle,count(*) n FROM tl.result_set_members GROUP BY handle"
        ).fetchall()
        groups = c.execute(
            "SELECT count(*) n FROM tl.result_set_groups WHERE handle=%s", (grouped,)
        ).fetchone()["n"]
    assert {r["handle"]: r["n"] for r in left} == {handle: 2, grouped: 2}
    assert groups == 2
    with connect() as c:
        kept = c.execute(
            "SELECT record_id FROM tl.result_set_members WHERE handle=%s ORDER BY ordinal",
            (handle,),
        ).fetchall()
    assert [r["record_id"] for r in kept] == [f"m:{i}" for i in chat]
    # Purging deletes the channel's threads and with them any thread grants,
    # which moves the access policy: earlier sets are stale afterwards.
    with pytest.raises(ResultSetStale):
        resultsets.members(scope, handle)
    fresh = make(scope, refs(chat))
    assert len(resultsets.members(scope, fresh)) == 2
    with pytest.raises(ResultSetNotFound):
        make(scope, refs(notes))


def test_handle_format(client, channel):
    stored = seed(client, 1)
    scope = owner_scope()
    handles = {make(scope, refs(stored)) for _ in range(5)}
    assert len(handles) == 5
    assert all(re.fullmatch(r"rs_[0-9a-f]{32}", h) for h in handles)
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect("app") as c:
            c.execute(
                "INSERT INTO tl.result_sets(handle,policy_version,completeness,expires_at) VALUES ('rs_XYZ',1,'exhaustive',now()+interval '1 hour')"
            )
    for bad in (
        "rs_123",
        "RS_" + "0" * 32,
        "rs_" + "g" * 32,
        "",
        "rs_" + "0" * 32 + "x",
    ):
        with pytest.raises(ResultSetNotFound):
            resultsets.members(scope, bad)
        assert result_set_rows(scope, bad) == []
    assert client.get("/v1/sets/rs_123").status_code == 404
    assert client.get(f"/v1/sets/{ZERO}").status_code == 404


def test_arc_6_only_result_tables_written(client, channel):
    stored = seed(client, 4)
    scope = owner_scope()
    before = table_counts()
    session_id = resultsets.open_session(scope, "synthetic")["id"]
    handle = make(scope, refs(stored, groups=["fake"]), session=session_id)
    resultsets.read(scope, handle)
    grouped = resultsets.transform(scope, "group", [handle], by="conversation")
    for view in resultsets.VIEWS:
        resultsets.inspect(scope, grouped, view)
    resultsets.set_unresolved(scope, session_id, ["synthetic need"])
    resultsets.drop(scope, grouped)
    after = table_counts()
    changed = {name for name in after if after[name] != before.get(name)}
    assert changed <= set(RESULT_TABLES), changed
    assert changed >= {
        "sessions",
        "result_sets",
        "result_set_members",
        "result_set_groups",
    }
    # The runtime roles: the app role writes these tables, the reader none.
    with connect() as c:
        for table in RESULT_TABLES:
            privileges = c.execute(
                "SELECT has_table_privilege('throughline_app',%s,'INSERT') app, has_table_privilege('throughline_reader',%s,'SELECT') reader",
                ("tl." + table, "tl." + table),
            ).fetchone()
            assert privileges == {"app": True, "reader": False}, table
    with pytest.raises(psycopg.Error):
        with reader(scope) as c:
            c.execute("SELECT * FROM tl.result_sets")


def test_registry_examples_execute(client):
    with connect() as c:
        registry = {
            r["name"]: r
            for r in c.execute(
                "SELECT * FROM tl.api_functions WHERE name IN ('result_set','result_set_info')"
            )
        }
    assert set(registry) == {"result_set", "result_set_info"}
    for name, row in registry.items():
        assert row["signature"].startswith(name + "(handle text) RETURNS TABLE("), row
        assert (row["kind"], row["exactness"], row["cost_class"]) == (
            "sql",
            "exact",
            "free",
        )
        assert "ranked_sample" in row["description"]
        assert f"api.{name}(" in row["example"]
        with reader(Scope.local()) as conn:
            assert conn.execute(row["example"]).fetchall() == []
    r = client.get("/v1/describe", params={"subject": "result_sets"})
    assert r.status_code == 200, r.text
    assert {"storage", "ownership", "lifetime", "completeness"} <= set(r.json())
    example = r.json()["example"]
    r = client.post("/v1/sql", json={"statement": example})
    assert r.status_code == 200 and r.json()["rows"] == []
    assert {"result_set", "result_set_info"} <= {
        f["name"]
        for f in client.get("/v1/describe", params={"subject": "functions"}).json()
    }


def test_cli_set_and_session_commands(client, channel, monkeypatch):
    import throughline.cli as cli

    stored = seed(client, 3)
    r = client.post("/v1/sessions", json={})
    session_id = r.json()["id"]
    r = client.post(
        "/v1/sets",
        json=dict(
            records=[dict(record_id=f"m:{i}") for i in stored], session=session_id
        ),
    )
    handle = r.json()["handle"]

    def request(method, path, data=None, params=None):
        response = client.request(method, "/v1/" + path, json=data, params=params)
        assert not response.is_error, response.text
        return response.json()

    monkeypatch.setattr(cli, "request", request)
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(cli.app, ["lib", *args])
        assert result.exit_code == 0, result.output
        return result.output

    assert json.loads(run("set", "info", handle, "--json"))["member_count"] == 3
    shown = json.loads(run("set", "show", handle, "--limit", "2", "--json"))
    assert len(shown["records"]) == 2 and shown["next_offset"] == 2
    inspected = json.loads(
        run(
            "set", "inspect", handle, "--view", "by_time", "--selector", "day", "--json"
        )
    )
    assert inspected["items"] == [{"bucket": "2020-01-01", "n": 3}]
    made = json.loads(
        run("set", "transform", "head", handle, "--params", '{"n": 1}', "--json")
    )
    assert made["member_count"] == 1
    session = json.loads(run("session", "show", session_id, "--json"))
    assert [s["handle"] for s in session["sets"]] == [handle, made["handle"]]
    assert json.loads(run("set", "drop", made["handle"], "--json"))["dropped"]


def test_read_returns_full_records_through_shared_hydrator(client, channel):
    # Optional feature: the shared hydrator (retrieval.hydrate) builds records.
    hydrate = pytest.importorskip("throughline.retrieval.hydrate")
    stored = seed(client, 2)
    scope = owner_scope()
    handle = make(scope, refs(stored, score=0.5))
    records = resultsets.read(scope, handle)["records"]
    expected = hydrate.records_for_members(scope, resultsets.members(scope, handle))
    assert [r["record_id"] for r in records] == [f"m:{i}" for i in stored]
    assert set(contract_columns()) <= set(records[0])
    assert {"span", "score", "discovery", "role", "depends_on", "groups"} <= set(
        records[0]
    )
    assert len(expected) == 2


# --- checker additions ------------------------------------------------------------


def test_read_keeps_duplicate_members_in_ordinal_order(client, channel):
    # The same record twice (at two ordinals) reads back twice, each with its
    # own ordinal and score, whichever builder makes the records.
    stored = seed(client, 2)
    scope = owner_scope()
    handle = make(
        scope,
        [
            dict(record_id=f"m:{stored[0]}", score=0.9),
            dict(record_id=f"m:{stored[1]}", score=0.5),
            dict(record_id=f"m:{stored[0]}", score=0.1),
        ],
    )
    records = resultsets.read(scope, handle)["records"]
    assert [(r["ordinal"], r["record_id"]) for r in records] == [
        (0, f"m:{stored[0]}"),
        (1, f"m:{stored[1]}"),
        (2, f"m:{stored[0]}"),
    ]
    assert [r["score"] for r in records] == pytest.approx([0.9, 0.5, 0.1])


def test_origin_is_short_operator_output(client, channel):
    stored = seed(client, 1)
    scope = owner_scope()
    with pytest.raises(ValueError):
        make(scope, refs(stored), origin={"op": "create", "note": "x" * 501})
    r = client.post(
        "/v1/sets",
        json=dict(
            records=[dict(record_id=f"m:{stored[0]}")],
            origin={"op": "recall", "query": "y" * 600},
        ),
    )
    assert r.status_code == 400
    handle = make(scope, refs(stored))
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect("app") as c:
            c.execute(
                "UPDATE tl.result_sets SET origin=%s WHERE handle=%s",
                (json.dumps({"op": "create", "text": "z" * 501}), handle),
            )


def test_transform_bad_parameter_types_are_400(client, channel):
    stored = seed(client, 3)
    r = client.post(
        "/v1/sets", json=dict(records=[dict(record_id=f"m:{i}") for i in stored])
    )
    handle = r.json()["handle"]
    for op, params in (
        ("sample", {"n": 2, "seed": {"x": 1}}),
        ("head", {"n": [1]}),
        ("head", {"n": True}),
        ("filter", {"min_score": "high"}),
        ("filter", {"kinds": 5}),
        ("filter", {"channels": "synthetic.chat"}),
        ("group", {"by": "explicit", "assignments": {f"m:{stored[0]}": 5}}),
        ("head", {"scope": 1}),
        ("head", {"handles": [handle]}),
    ):
        r = client.post(
            "/v1/sets/transform", json=dict(op=op, handles=[handle], params=params)
        )
        assert r.status_code == 400, (op, params, r.text)
    r = client.post(
        "/v1/sets/transform",
        json=dict(op="sample", handles=[handle], params={"n": 2, "seed": "fixed"}),
    )
    assert r.status_code == 200 and r.json()["member_count"] == 2


def test_session_counts_are_visible_members(client, channel):
    add_channel(client, NOTES)
    visible = seed(client, 2)
    hidden = seed(client, 1, channel=NOTES)
    restricted("synthetic.chat")
    scope = asker_scope()
    session_id = resultsets.open_session(scope)["id"]
    handle = make(scope, refs(visible + hidden, version=1), session=session_id)
    [listed] = resultsets.session(scope, session_id)["sets"]
    assert listed["handle"] == handle
    assert listed["counts"]["returned"] == 2
    assert listed["counts"] == resultsets.info(scope, handle)["counts"]


def test_group_by_subject_time_marks_spread_as_uncertain(client, channel):
    subject = dict(
        start="2019-06-01T00:00:00+00:00",
        end="2019-08-31T00:00:00+00:00",
        start_precision="day",
        end_precision="month",
    )
    exact = dict(
        start="2019-03-10T00:00:00+00:00",
        end="2019-03-10T00:00:00+00:00",
        start_precision="day",
        end_precision="day",
    )
    r = ingest(
        client,
        [
            message(0, external_id="synthetic.chat-0", subject=subject),
            message(1, external_id="synthetic.chat-1", subject=exact),
            message(2, external_id="synthetic.chat-2"),
        ],
    )
    assert r.status_code == 200 and r.json()["rejected"] == [], r.text
    found = ids()
    stored = [found[f"synthetic.chat-{i}"] for i in range(3)]
    scope = owner_scope()
    base = make(scope, refs(stored))
    grouped = resultsets.transform(scope, "group", [base], by="month", basis="subject")
    groups = {
        m["record_id"]: {g["grp"]: g["certain"] for g in m["groups"]}
        for m in resultsets.members(scope, grouped)
    }
    # A three-month subject range is in each month, tentatively.
    assert groups[f"m:{stored[0]}"] == {
        "month:2019-06": False,
        "month:2019-07": False,
        "month:2019-08": False,
    }
    # A day-precise subject date is certainly in its month.
    assert groups[f"m:{stored[1]}"] == {"month:2019-03": True}
    # Without subject time the record time is only a guess at the subject.
    assert groups[f"m:{stored[2]}"] == {"month:2020-01": False}
    manifest = resultsets.inspect(scope, grouped, "groups")
    assert {i["grp"]: i["uncertain"] for i in manifest["items"]}["month:2019-07"] == 1
    with pytest.raises(ValueError):
        resultsets.transform(scope, "group", [base], by="month", basis="mood")
