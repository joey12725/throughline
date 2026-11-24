"""Asker scope enforced in the database (migration 0033).

Every test works at database level: it opens the reader role itself and places
the settings the service places in a reader transaction.
"""

import importlib.util
import json
import re
import statistics
import time
from contextlib import contextmanager

import psycopg
import pytest
import typer
from conftest import ingest, message
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from typer.testing import CliRunner

from throughline import access
from throughline.cli_commands.access import register
from throughline.db import connect, create_token
from throughline.query import validate_sql

OTHER = "synthetic.notes"
# Columns of api.message_versions before the gate, in order.
MESSAGE_COLUMNS = [
    "id",
    "channel_id",
    "external_id",
    "version",
    "superseded_by",
    "thread_id",
    "thread_no",
    "happened_at",
    "happened_offset_min",
    "ended_at",
    "ingested_at",
    "author_handle",
    "author_identifier_id",
    "person_id",
    "content",
    "images",
    "fields",
    "lat",
    "lon",
    "loc_accuracy_m",
    "loc_method",
    "loc_reason",
    "access_label",
    "payload_hash",
    "tsv",
    "channel",
    "is_reference",
    "is_outside",
    "thread_external_id",
    "thread_title",
    "person_handle",
    "display_name",
]
FALSE_GATES = [
    "people",
    "person_names",
    "identifiers",
    "facts",
    "facts_history",
    "relations",
    "relations_history",
    "not_same",
    "entities",
    "location_track",
    "plugins",
]


@contextmanager
def reader(mode=None, asker=None, unknown=None):
    """A reader transaction with the settings the service would place."""
    with connect("reader") as c:
        for name, value in (
            ("scope_mode", mode),
            ("asker", asker),
            ("unknown_asker", unknown),
        ):
            if value is not None:
                c.execute(
                    sql.SQL("SET LOCAL throughline.{}={}").format(
                        sql.SQL(name), sql.Literal(value)
                    )
                )
        yield c


def as_asker(name, unknown="deny"):
    return reader("asker", name, unknown)


def rows(c, statement, params=None):
    return c.execute(statement, params).fetchall()


def count(c, relation):
    return c.execute(
        sql.SQL("SELECT count(*) AS n FROM api.{}").format(sql.Identifier(relation))
    ).fetchone()["n"]


def ids(c, statement, params=None):
    return sorted(r["id"] for r in rows(c, statement, params))


# Views documented as exceptions to the gate rule: they hold no personal data
# (see tests/test_v11_security_matrix.py UNGATED and migration 0045).
UNGATED = {"recipes"}


def api_views():
    with connect() as c:
        return [
            r["relname"]
            for r in rows(
                c,
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='api' AND c.relkind='v' ORDER BY 1",
            )
        ]


def exists(name):
    with connect() as c:
        return c.execute(
            "SELECT to_regclass(%s) IS NOT NULL AS ok", (name,)
        ).fetchone()["ok"]


@pytest.fixture(autouse=True)
def no_askers(database):
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


@pytest.fixture()
def world(client, channel):
    """Two channels; synthetic.chat has an open and a secret thread.

    chat/open   messages 0-4   "Synthetic orchard message i", message 0 located
    chat/secret messages 5-9   "zebra orchard harvest i", thread title "zebra plans"
    notes/n1    messages 100-104 "Synthetic orchard note i"
    """
    r = client.post("/v1/channels", json=dict(name=OTHER, plugin="synthetic"))
    assert r.status_code == 200, r.text
    batch = [
        message(
            i,
            thread={"external_id": "open", "title": "open talk"},
            **({"location": dict(lat=10.0, lon=20.0)} if i == 0 else {}),
        )
        for i in range(5)
    ]
    batch += [
        message(
            i,
            thread={"external_id": "secret", "title": "zebra plans"},
            content=f"zebra orchard harvest {i}",
            **({"location": dict(lat=10.0, lon=20.0)} if i == 5 else {}),
        )
        for i in range(5, 10)
    ]
    batch += [
        message(
            i,
            channel=OTHER,
            thread={"external_id": "n1"},
            content=f"Synthetic orchard note {i}",
            fields={},
        )
        for i in range(100, 105)
    ]
    r = ingest(client, batch)
    assert r.status_code == 200 and r.json()["inserted"] == 15, r.text
    with connect() as c:
        for t in rows(c, "SELECT id FROM tl.threads"):
            c.execute("SELECT tl.refresh_thread(%s)", (t["id"],))
        person = c.execute(
            "INSERT INTO tl.people(handle,display_name) VALUES('synthetic-person','Synthetic Person') RETURNING id"
        ).fetchone()["id"]
        c.execute("UPDATE tl.messages SET person_id=%s", (person,))
        c.execute(
            "INSERT INTO tl.person_names(person_id,name) VALUES(%s,'Synthetic')",
            (person,),
        )
        c.execute(
            "INSERT INTO tl.plugins(name) VALUES('synthetic') ON CONFLICT DO NOTHING"
        )
        track = c.execute(
            "INSERT INTO tl.location_track(at,lat,lon,source) VALUES('2020-01-01Z',10,20,'synthetic') RETURNING id"
        ).fetchone()["id"]
        entity = c.execute(
            "INSERT INTO tl.entities(kind,name,external_ref) VALUES('topic','Synthetic topic','synthetic-topic') RETURNING id"
        ).fetchone()["id"]
        message_ids = {
            r["external_id"]: r["id"]
            for r in rows(c, "SELECT id,external_id FROM tl.messages")
        }
        c.execute(
            "INSERT INTO tl.message_locations(message_id,track_id) VALUES(%s,%s),(%s,%s)",
            (message_ids["fake-1"], track, message_ids["fake-6"], track),
        )
        threads = {
            r["external_id"]: r["id"]
            for r in rows(c, "SELECT id,external_id FROM tl.threads")
        }
        channels = {
            r["name"]: r["id"] for r in rows(c, "SELECT id,name FROM tl.channels")
        }
    return dict(
        client=client,
        m=message_ids,
        threads=threads,
        channels=channels,
        person=person,
        entity=entity,
        open=sorted(message_ids[f"fake-{i}"] for i in range(5)),
        secret=sorted(message_ids[f"fake-{i}"] for i in range(5, 10)),
        notes=sorted(message_ids[f"fake-{i}"] for i in range(100, 105)),
    )


def link(c, from_kind, from_id, to_kind, to_id, evidence=None, kind="synthetic"):
    return c.execute(
        "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,evidence) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
        (kind, from_kind, from_id, to_kind, to_id, Jsonb(evidence or {})),
    ).fetchone()["id"]


def bulk(c, channel_id, thread, n, content_sql, start=0):
    """Insert n current messages into one new thread; returns the thread id."""
    tid = c.execute(
        "INSERT INTO tl.threads(channel_id,external_id) VALUES(%s,%s) RETURNING id",
        (channel_id, thread),
    ).fetchone()["id"]
    c.execute(
        sql.SQL(
            "INSERT INTO tl.messages(channel_id,external_id,version,thread_id,thread_no,happened_at,content,payload_hash) SELECT %s,%s||'-'||i,1,%s,i,'2021-01-01Z'::timestamptz+i*interval '1 second',{},'synthetic-hash' FROM generate_series(%s::int,%s::int) i"
        ).format(sql.SQL(content_sql)),
        (channel_id, thread, tid, start + 1, start + n),
    )
    return tid


# --- ACL-1, ACL-2 -----------------------------------------------------------


def test_acl_1_restricted_without_grants_sees_nothing(world):
    with connect() as c:
        link(c, "message", world["open"][0], "message", world["open"][1])
        link(c, "message", world["open"][0], "entity", world["entity"])
    access.register_asker("nobody")
    views = api_views()
    populated = {
        "messages",
        "message_versions",
        "threads",
        "channels",
        "conversations",
        "links",
        "people",
        "person_names",
        "identifiers",
        "entities",
        "location_track",
        "message_locations",
        "plugins",
        "ch_synthetic_chat",
        "ch_synthetic_notes",
    } | ({"records", "record_versions"} & set(views))
    assert populated <= set(views)
    with reader() as c:
        for view in populated:
            assert count(c, view) > 0, view
    functions = [
        ("SELECT * FROM api.search('orchard',50)", ()),
        ("SELECT * FROM api.search_all('orchard',50)", ()),
        ("SELECT * FROM api.search_filtered('orchard',50)", ()),
        ("SELECT * FROM api.context(%s,5,5)", (world["open"][2],)),
        ("SELECT * FROM api.near(10,20,1000)", ()),
        ("SELECT * FROM api.overlapping('2019-01-01Z','2022-01-01Z')", ()),
        ("SELECT * FROM api.person_messages(%s)", (world["person"],)),
        ("SELECT * FROM api.follow('message',%s,NULL,3)", (world["open"][0],)),
    ]
    with reader() as c:
        for statement, params in functions:
            assert rows(c, statement, params), statement
    scopes = [
        dict(mode="asker", asker="nobody", unknown="deny"),
        dict(mode="asker", asker="nobody", unknown="unrestricted"),
        dict(mode="asker", asker="never-registered", unknown="deny"),
        dict(mode="deny"),
        dict(mode="deny", asker="nobody"),
        dict(mode="not-a-mode"),
    ]
    for scope in scopes:
        with reader(**scope) as c:
            for view in views:
                if view in UNGATED:
                    continue
                assert count(c, view) == 0, (scope, view)
            for statement, params in functions:
                assert rows(c, statement, params) == [], (scope, statement)
            assert (
                c.execute(
                    "SELECT api.current_message(%s) AS id", (world["open"][0],)
                ).fetchone()["id"]
                is None
            )
            catalog = c.execute("SELECT api.catalog() AS c").fetchone()["c"]
            assert catalog["channels"] == [] and catalog["link_types"] == []
            text = json.dumps(catalog)
            assert "zebra" not in text


def test_acl_1_every_reading_function_is_scoped(world):
    """Functions beyond the core list: meaning search, fused search, during,
    near_place, records_asof, person, describe of a channel. Each runs as its
    owner (SECURITY DEFINER), so only the gate inside the views it reads can
    narrow it; the gate reads settings, not the role."""
    with connect() as c:
        c.execute(
            "INSERT INTO tl.embedding_models(model,dimensions,active) VALUES('synthetic-gate',3,true) ON CONFLICT (model) DO UPDATE SET active=true"
        )
        c.execute(
            "INSERT INTO tl.embeddings(conversation_id,model,text_hash,embedding) SELECT id,'synthetic-gate',text_hash,'[1,1,1]'::vector FROM tl.conversations WHERE text_hash IS NOT NULL ON CONFLICT DO NOTHING"
        )
        c.execute(
            "INSERT INTO tl.entities(kind,name,external_ref,metadata) VALUES('place','synthetic place','synthetic-place','{\"lat\":10,\"lon\":20}')"
        )
    has_records = exists("api.records")
    calls = [
        ("SELECT id FROM api.search_meaning('[1,1,1]',50)", (), True),
        ("SELECT id FROM api.search_fused('orchard','[1,1,1]',50)", (), True),
        ("SELECT id FROM api.during(%s)", (world["secret"][0],), False),
        ("SELECT id FROM api.during(%s)", (world["notes"][0],), True),
        ("SELECT id FROM api.near_place('synthetic place',1000)", (), False),
        ("SELECT id FROM api.person('Synthetic Person')", (), False),
        ("SELECT id FROM api.visits", (), True),
        ("SELECT id FROM api.search_results", (), True),
    ]
    if has_records:
        calls.append(("SELECT message_id AS id FROM api.records_asof(now())", (), True))
    with reader() as c:
        for statement, params, _ in calls:
            if "visits" in statement or "search_results" in statement:
                continue  # empty for everyone in this world
            assert rows(c, statement, params), statement
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)
    access.register_asker("nobody")
    for asker in ("nobody", "guest"):
        with as_asker(asker) as c:
            for statement, params, may_see_notes in calls:
                found = set(ids(c, statement, params))
                allowed = (
                    set(world["notes"]) if asker == "guest" and may_see_notes else set()
                )
                assert found <= allowed, (asker, statement, found)
                assert not found & set(world["open"] + world["secret"])
            described = c.execute(
                "SELECT api.describe('channel:synthetic.chat') AS d"
            ).fetchone()["d"]
            assert described is None, asker
    with as_asker("guest") as c:
        # The visible part is still found, not only nothing.
        assert set(ids(c, "SELECT id FROM api.search_meaning('[1,1,1]',50)")) <= set(
            world["notes"]
        )
        assert ids(c, "SELECT id FROM api.search_fused('orchard','[1,1,1]',50)")
        assert set(
            ids(c, "SELECT id FROM api.during(%s)", (world["notes"][0],))
        ) <= set(world["notes"])
        if has_records:
            assert (
                sorted(
                    r["id"]
                    for r in rows(
                        c, "SELECT message_id AS id FROM api.records_asof(now())"
                    )
                )
                == world["notes"]
            )


def test_acl_2_counts_exclude_hidden(world):
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)
    with connect() as c:
        link(c, "message", world["notes"][0], "message", world["notes"][1], kind="near")
        link(c, "message", world["open"][0], "message", world["open"][1], kind="far")
    with as_asker("guest") as c:
        assert count(c, "messages") == 5
        assert count(c, "message_versions") == 5
        assert count(c, "ch_synthetic_notes") == 5
        assert count(c, "ch_synthetic_chat") == 0
        assert count(c, "threads") == 1 and count(c, "channels") == 1
        assert count(c, "links") == 1
        grouped = rows(
            c, "SELECT channel,count(*) AS n FROM api.messages GROUP BY channel"
        )
        assert grouped == [dict(channel=OTHER, n=5)]
        assert (
            c.execute(
                "SELECT count(*) AS n FROM api.search_all('orchard',NULL)"
            ).fetchone()["n"]
            == 5
        )
        totals = c.execute(
            "SELECT count(*) AS n,min(id) AS low,max(id) AS high FROM api.messages"
        ).fetchone()
        assert (totals["low"], totals["high"]) == (
            world["notes"][0],
            world["notes"][-1],
        )
        catalog = c.execute("SELECT api.catalog() AS c").fetchone()["c"]
        assert [ch["name"] for ch in catalog["channels"]] == [OTHER]
        assert catalog["link_types"] == ["near"]
    with reader() as c:
        assert count(c, "messages") == 15 and count(c, "links") == 2


# --- ACL-8 ------------------------------------------------------------------


def test_acl_8_channel_and_thread_grants(world):
    access.register_asker("by-channel")
    access.grant("by-channel", channel="synthetic.chat")
    access.register_asker("by-thread")
    access.grant("by-thread", thread_id=world["threads"]["open"])
    chat = sorted(world["open"] + world["secret"])
    with as_asker("by-channel") as c:
        assert ids(c, "SELECT id FROM api.messages") == chat
        assert ids(c, "SELECT id FROM api.threads") == sorted(
            [world["threads"]["open"], world["threads"]["secret"]]
        )
        assert [r["name"] for r in rows(c, "SELECT name FROM api.channels")] == [
            "synthetic.chat"
        ]
        assert {
            r["thread_id"] for r in rows(c, "SELECT thread_id FROM api.conversations")
        } == {
            world["threads"]["open"],
            world["threads"]["secret"],
        }
        assert sorted(
            r["message_id"]
            for r in rows(c, "SELECT message_id FROM api.message_locations")
        ) == sorted([world["m"]["fake-1"], world["m"]["fake-6"]])
        assert ids(c, "SELECT id FROM api.near(10,20,1000)") == sorted(
            [world["m"]["fake-0"], world["m"]["fake-5"]]
        )
    with as_asker("by-thread") as c:
        assert ids(c, "SELECT id FROM api.messages") == world["open"]
        assert ids(c, "SELECT id FROM api.message_versions") == world["open"]
        assert ids(c, "SELECT id FROM api.ch_synthetic_chat") == world["open"]
        assert ids(c, "SELECT id FROM api.threads") == [world["threads"]["open"]]
        # The channel of a granted thread is listed; its other threads are not.
        assert [r["name"] for r in rows(c, "SELECT name FROM api.channels")] == [
            "synthetic.chat"
        ]
        assert {
            r["thread_id"] for r in rows(c, "SELECT thread_id FROM api.conversations")
        } == {world["threads"]["open"]}
        assert [
            r["message_id"]
            for r in rows(c, "SELECT message_id FROM api.message_locations")
        ] == [world["m"]["fake-1"]]
        assert ids(c, "SELECT id FROM api.search('orchard',50)") == world["open"]
        assert ids(c, "SELECT id FROM api.search('zebra',50)") == []
        assert (
            ids(c, "SELECT id FROM api.context(%s,50,50)", (world["open"][0],))
            == world["open"]
        )
        assert (
            rows(c, "SELECT * FROM api.context(%s,50,50)", (world["secret"][0],)) == []
        )
        assert (
            ids(c, "SELECT id FROM api.overlapping('2019-01-01Z','2022-01-01Z')")
            == world["open"]
        )
        assert (
            ids(c, "SELECT id FROM api.person_messages(%s)", (world["person"],))
            == world["open"]
        )
        current = c.execute(
            "SELECT api.current_message(%s) AS seen,api.current_message(%s) AS hidden",
            (world["open"][0], world["secret"][0]),
        ).fetchone()
        assert current == dict(seen=world["open"][0], hidden=None)
    access.revoke("by-thread", thread_id=world["threads"]["open"])
    with as_asker("by-thread") as c:
        assert count(c, "messages") == 0 and count(c, "channels") == 0


PROBES = {
    # Each probe is false on a row without the secret and fails (or matches)
    # on a row with it, so it reports on hidden rows if it ever runs on one.
    "divide": "1/(CASE WHEN {col} LIKE '%zebra%' THEN 0 ELSE 1 END)=0",
    "cast": "(CASE WHEN {col} LIKE '%zebra%' THEN 'x' ELSE '0' END)::int=1",
    "like": "{col} LIKE '%zebra%'",
}
PROBE_TARGETS = [
    ("api.messages", "content"),
    ("api.message_versions", "content"),
    ("api.ch_synthetic_chat", "content"),
    ("api.threads", "title"),
    ("api.links", "evidence::text"),
    ("api.records", "content"),
    ("api.record_versions", "content"),
]


def test_acl_8_error_oracle_closed(world):
    with connect() as c:
        link(
            c,
            "message",
            world["secret"][0],
            "message",
            world["secret"][1],
            evidence={"note": "zebra"},
        )
        link(c, "message", world["notes"][0], "message", world["notes"][1])
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)
    statements = {
        (relation, name): f"SELECT * FROM {relation} WHERE " + probe.format(col=column)
        for relation, column in PROBE_TARGETS
        if exists(relation)
        for name, probe in PROBES.items()
    }
    # Views without text columns: the probe keys on a hidden row's id.
    hidden = world["secret"][0]
    for relation, column, value in (
        ("api.message_locations", "message_id", world["m"]["fake-6"]),
        ("api.channels", "id", world["channels"]["synthetic.chat"]),
        ("api.threads", "id", world["threads"]["secret"]),
        ("api.conversations", "thread_id", world["threads"]["secret"]),
        ("api.messages", "id", hidden),
    ):
        statements[(relation, "divide-id")] = (
            f"SELECT * FROM {relation} WHERE 1/(CASE WHEN {column}={value} THEN 0 ELSE 1 END)=0"
        )
    # The probes are real: an unrestricted reader fails or finds the secret.
    for (relation, name), statement in statements.items():
        if name == "like":
            with reader() as c:
                assert rows(c, statement), statement
        else:
            with pytest.raises(psycopg.DataError):
                with reader() as c:
                    rows(c, statement)
    for scope in (
        dict(mode="asker", asker="guest", unknown="deny"),
        dict(mode="asker", asker="unregistered", unknown="deny"),
        dict(mode="deny"),
    ):
        for statement in statements.values():
            with reader(**scope) as c:
                assert rows(c, validate_sql(statement)) == [], (scope, statement)
    # The same statements through the service's own reader path.
    if importlib.util.find_spec("throughline.scope") is None:
        return
    from throughline import query
    from throughline.scope import Scope

    if not hasattr(query, "query_ex"):
        return
    scope = Scope(token_id=None, token_kind="agent", asker="guest", mode="asker")
    for statement in statements.values():
        result = query.query_ex(statement, scope=scope)
        assert result["rows"] == [] and result["row_count"] == 0, statement
    visible = query.query_ex("SELECT id FROM api.messages ORDER BY id", scope=scope)
    assert [r["id"] for r in visible["rows"]] == world["notes"]


def plan_nodes(plan, ancestors=()):
    yield plan, ancestors
    for child in plan.get("Plans", []):
        yield from plan_nodes(child, ancestors + (plan,))


def quals(node):
    return " ".join(
        str(node.get(key, ""))
        for key in (
            "Filter",
            "Index Cond",
            "Recheck Cond",
            "Join Filter",
            "Hash Cond",
            "Merge Cond",
            "One-Time Filter",
        )
    )


def test_acl_8_explain_gate_below_caller_quals_for_restricted(world):
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)
    statement = "SELECT id FROM api.messages WHERE " + PROBES["divide"].format(
        col="content"
    )
    with as_asker("guest") as c:
        plan = c.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement).fetchone()[
            "QUERY PLAN"
        ][0]["Plan"]
    nodes = list(plan_nodes(plan))
    gates = [node for node, _ in nodes if "row_visible" in quals(node)]
    assert gates, plan
    probes = [(node, up) for node, up in nodes if "~~" in quals(node)]
    assert probes, plan
    for gate in gates:
        # Nothing of the caller's predicate is at or below a gate.
        for node, _ in plan_nodes(gate):
            assert "~~" not in quals(node), node
    executed = [node for node, _ in probes if node.get("Actual Loops", 0) > 0]
    assert executed, plan
    for node in executed:
        # Where the caller's predicate did run, its input came through a gate.
        below = [n for n, _ in plan_nodes(node) if n is not node]
        assert any(n in gates for n in below), node
    # The base table was only read behind the gate.
    for node, up in nodes:
        if any(n.get("Subplan Name", "").startswith("CTE") for n in up + (node,)):
            continue  # thread numbering of dirty threads: ids only, no output
        if node.get("Relation Name") == "messages" and node.get("Actual Loops", 0) > 0:
            assert "row_visible" in quals(node), node
    # An unrestricted asker runs the other branch and never calls the gate.
    with reader("owner") as c:
        plan = c.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM api.messages WHERE content LIKE '%zebra%'"
        ).fetchone()["QUERY PLAN"][0]["Plan"]
    for node, _ in plan_nodes(plan):
        if "row_visible" in quals(node):
            assert node.get("Actual Loops", 0) == 0, node


def test_keyword_search_uses_gin_index_for_unrestricted(world):
    with connect() as c:
        bulk(
            c,
            world["channels"]["synthetic.chat"],
            "bulk",
            5000,
            "CASE WHEN i%%500=0 THEN 'rare quince '||i ELSE 'common filler text '||i END",
        )
        c.execute("ANALYZE tl.messages")
        source = c.execute(
            "SELECT prosrc FROM pg_proc WHERE oid='api.search(text,int)'::regprocedure"
        ).fetchone()["prosrc"]
    predicate = "tsv @@ websearch_to_tsquery('english','quince')"
    direct = "SELECT id FROM api.messages WHERE " + predicate
    ungated = (
        "SELECT id FROM tl.message_versions_all WHERE superseded_by IS NULL AND "
        + predicate
    )

    def plan(c, statement, forced=True):
        # With sequential and plain B-tree scans disabled the GIN index
        # (Bitmap Index Scan) is chosen whenever the predicate can reach the
        # table at all, whatever the table size.
        with c.transaction():
            if forced:
                c.execute("SET LOCAL enable_seqscan=off")
                c.execute("SET LOCAL enable_indexscan=off")
            return "\n".join(r["QUERY PLAN"] for r in rows(c, "EXPLAIN " + statement))

    for scope in (dict(), dict(mode="owner")):
        with reader(**scope) as c:
            assert "messages_search" in plan(c, direct), plan(c, direct)
            assert len(rows(c, direct)) == 10
            assert len(rows(c, "SELECT id FROM api.search('quince',50)")) == 10
    # The body of api.search with its arguments written in, planned as the
    # function plans it (a function call itself is opaque to EXPLAIN).
    body = re.sub(r"\bmax_results\b", "10", re.sub(r"\bquery\b", "'quince'", source))
    assert body != source
    with connect() as c:
        assert "messages_search" in plan(c, body), plan(c, body)
        # Left to itself the planner makes the same choice for the gated view
        # as for its ungated definition.
        assert ("messages_search" in plan(c, direct, False)) == (
            "messages_search" in plan(c, ungated, False)
        )
    # A restricted asker gets the same answer for what it may see, without the
    # index being applied to rows it may not see.
    access.register_asker("guest")
    access.grant("guest", channel="synthetic.chat")
    with as_asker("guest") as c:
        assert len(rows(c, direct)) == 10


def test_acl_4_link_needs_all_ends_and_visible_evidence_ids(world):
    v1, v2 = world["notes"][0], world["notes"][1]
    hidden = world["secret"][0]
    with connect() as c:
        entity = world["entity"]
        expect = {
            "both ends visible": (link(c, "message", v1, "message", v2), True),
            "one end hidden": (link(c, "message", v1, "message", hidden), False),
            "hidden to visible": (link(c, "message", hidden, "message", v2), False),
            "message to entity": (link(c, "message", v1, "entity", entity), True),
            "entity to message": (link(c, "entity", entity, "message", v2), True),
            "hidden message to entity": (
                link(c, "message", hidden, "entity", entity),
                False,
            ),
            "no message end": (
                link(c, "entity", entity, "person", world["person"]),
                False,
            ),
            "missing message": (link(c, "message", v1, "message", 987654321), False),
            "evidence visible": (
                link(c, "message", v1, "message", v2, {"message_ids": [v1, v2]}),
                True,
            ),
            "evidence hidden": (
                link(c, "message", v1, "message", v2, {"message_ids": [v2, hidden]}),
                False,
            ),
            "evidence empty list": (
                link(c, "message", v1, "message", v2, {"message_ids": []}),
                True,
            ),
            "evidence other keys": (
                link(c, "message", v1, "message", v2, {"note": "synthetic"}),
                True,
            ),
            "evidence not ids": (
                link(c, "message", v1, "message", v2, {"message_ids": ["x"]}),
                False,
            ),
            "evidence fractional": (
                link(c, "message", v1, "message", v2, {"message_ids": [1.5]}),
                False,
            ),
        }
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)
    with as_asker("guest") as c:
        seen = set(ids(c, "SELECT id FROM api.links"))
        for name, (link_id, visible) in expect.items():
            assert (link_id in seen) == visible, name
        reached = {
            (r["kind"], r["id"])
            for r in rows(
                c, "SELECT kind,id FROM api.follow('message',%s,NULL,3)", (v1,)
            )
        }
        assert ("message", hidden) not in reached
        assert ("message", v2) in reached and ("entity", world["entity"]) in reached
        assert rows(c, "SELECT * FROM api.follow('message',%s,NULL,3)", (hidden,)) == []
    with reader("owner") as c:
        assert set(ids(c, "SELECT id FROM api.links")) == {
            i for i, _ in expect.values()
        }


def test_acl_8_scope_precedes_ranking(world):
    with connect() as c:
        chat = world["channels"]["synthetic.chat"]
        # 990 hidden hits rank above the 10 visible ones.
        bulk(c, chat, "loud", 990, "'quince quince quince quince '||i")
        quiet = bulk(c, chat, "quiet", 10, "'one quince among other words '||i", 2000)
        visible = ids(c, "SELECT id FROM tl.messages WHERE thread_id=%s", (quiet,))
    access.register_asker("guest")
    access.grant("guest", thread_id=quiet)
    with reader() as c:
        top = ids(c, "SELECT id FROM api.search('quince',10)")
        assert len(top) == 10 and not set(top) & set(visible)
        assert (
            c.execute("SELECT count(*) AS n FROM api.search('quince',NULL)").fetchone()[
                "n"
            ]
            == 1000
        )
    with as_asker("guest") as c:
        assert ids(c, "SELECT id FROM api.search('quince',10)") == visible
        assert ids(c, "SELECT id FROM api.search_all('quince',10)") == visible
        assert ids(c, "SELECT id FROM api.search_filtered('quince',10)") == visible
        assert len(rows(c, "SELECT id FROM api.search('quince',3)")) == 3
        assert (
            c.execute("SELECT count(*) AS n FROM api.search('quince',NULL)").fetchone()[
                "n"
            ]
            == 10
        )


def test_scope_modes(world):
    access.register_asker("boss", "owner")
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)

    def seen(**scope):
        with reader(**scope) as c:
            return (
                count(c, "messages"),
                count(c, "people"),
                c.execute("SELECT api.asker_unrestricted() AS u").fetchone()["u"],
            )

    with connect() as c:
        people = c.execute("SELECT count(*) AS n FROM tl.people").fetchone()["n"]
    assert people > 0
    everything, nothing = (15, people, True), (0, 0, False)
    # No settings: a direct connection, as existing callers and tests use it.
    assert seen() == everything
    assert seen(mode="") == everything
    assert seen(mode="owner") == everything
    assert seen(mode="owner", asker="guest") == everything
    assert seen(mode="deny") == nothing
    assert seen(mode="deny", asker="boss") == nothing
    assert seen(mode="unknown-mode", asker="boss") == nothing
    assert seen(mode="asker", asker="boss", unknown="deny") == everything
    assert seen(mode="asker", asker="guest", unknown="unrestricted") == (5, 0, False)
    assert seen(mode="asker", asker="stranger", unknown="unrestricted") == everything
    assert seen(mode="asker", asker="stranger") == everything
    assert seen(mode="asker", asker="stranger", unknown="deny") == nothing
    assert seen(mode="asker", asker="", unknown="deny") == nothing
    assert seen(mode="asker", unknown="deny") == nothing
    # Mode changes take effect at once.
    access.register_asker("guest", "owner")
    assert seen(mode="asker", asker="guest", unknown="deny") == everything
    access.register_asker("boss", "restricted")
    assert seen(mode="asker", asker="boss", unknown="deny") == nothing
    access.remove_asker("guest")
    assert seen(mode="asker", asker="guest", unknown="deny") == nothing
    # The application role sets nothing and reads everything, which is why
    # every app-role route refuses a restricted scope before it reads.
    with connect("app") as c:
        assert count(c, "messages") == 15


def test_sources_visible_empty_array_only_for_unrestricted(world):
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)
    v, hidden = world["notes"][0], world["secret"][0]
    cases = [
        ("'{}'::bigint[]", (), False),
        ("NULL::bigint[]", (), False),
        ("%s::bigint[]", ([v],), True),
        ("%s::bigint[]", ([v, world["notes"][1]],), True),
        ("%s::bigint[]", ([v, hidden],), False),
        ("%s::bigint[]", ([hidden],), False),
        ("%s::bigint[]", ([987654321],), False),
        ("ARRAY[%s,NULL]::bigint[]", (v,), False),
    ]
    for scope in (dict(), dict(mode="owner"), dict(mode="asker", asker="stranger")):
        with reader(**scope) as c:
            for expression, params, _ in cases:
                assert c.execute(
                    f"SELECT api.sources_visible({expression}) AS ok", params
                ).fetchone()["ok"], (scope, expression)
    with as_asker("guest") as c:
        for expression, params, expected in cases:
            assert (
                c.execute(
                    f"SELECT api.sources_visible({expression}) AS ok", params
                ).fetchone()["ok"]
                is expected
            ), (expression, params)
        visible = c.execute(
            "SELECT api.message_visible(%s) AS a,api.message_visible(%s) AS b,api.message_visible(NULL) AS c",
            (v, hidden),
        ).fetchone()
        assert visible == dict(a=True, b=False, c=False)
    with reader("deny") as c:
        assert not c.execute(
            "SELECT api.sources_visible(%s::bigint[]) AS ok", ([v],)
        ).fetchone()["ok"]


# --- ACL-10 -----------------------------------------------------------------


def test_acl_10_policy_version_bumps(world):
    def bumped(action):
        before = access.policy_version()
        action()
        return access.policy_version() - before

    assert bumped(lambda: access.register_asker("guest")) == 1
    assert bumped(lambda: access.register_asker("guest")) == 0
    assert bumped(lambda: access.register_asker("guest", "owner")) == 1
    assert bumped(lambda: access.register_asker("guest", "restricted")) == 1
    assert bumped(lambda: access.grant("guest", channel=OTHER)) == 1
    assert bumped(lambda: access.grant("guest", channel=OTHER)) == 0
    assert (
        bumped(lambda: access.grant("guest", thread_id=world["threads"]["open"])) == 1
    )
    assert bumped(lambda: access.revoke("guest", channel=OTHER)) == 1
    with pytest.raises(ValueError):
        access.revoke("guest", channel=OTHER)
    assert bumped(access.list_askers) == 0
    token = create_token("synthetic agent", kind="agent", askers=["guest"])

    def owner_sql(statement):
        def run():
            with connect() as c:
                c.execute(statement, (token["id"],))

        return run

    assert (
        bumped(owner_sql("UPDATE tl.tokens SET askers='{guest,other}' WHERE id=%s"))
        == 1
    )
    assert bumped(owner_sql("UPDATE tl.tokens SET name='renamed' WHERE id=%s")) == 0
    assert bumped(owner_sql("UPDATE tl.tokens SET revoked_at=now() WHERE id=%s")) == 1
    assert bumped(lambda: access.remove_asker("guest")) >= 1
    # Readable by both runtime roles, and the same number.
    version = access.policy_version()
    for role in ("app", "reader"):
        with connect(role) as c:
            assert (
                c.execute("SELECT api.policy_version() AS v").fetchone()["v"] == version
            )
    with as_asker("nobody") as c:
        assert (
            c.execute("SELECT api.describe_access() AS d").fetchone()["d"][
                "policy_version"
            ]
            == version
        )


def test_access_module_validation(world):
    with pytest.raises(ValueError):
        access.register_asker("guest", "admin")
    with pytest.raises(ValueError):
        access.register_asker(" ")
    with pytest.raises(ValueError):
        access.grant("missing", channel=OTHER)
    access.register_asker("guest")
    for kwargs in (
        dict(),
        dict(channel=OTHER, thread_id=world["threads"]["open"]),
        dict(channel="synthetic.absent"),
        dict(thread_id=987654321),
    ):
        with pytest.raises(ValueError):
            access.grant("guest", **kwargs)
    with pytest.raises(ValueError):
        access.remove_asker("missing")
    access.grant("guest", channel=OTHER)
    access.grant("guest", thread_id=world["threads"]["open"])
    assert access.list_askers() == [
        dict(
            name="guest",
            mode="restricted",
            channels=[OTHER],
            threads=[world["threads"]["open"]],
        )
    ]
    with connect() as c:
        for values in ("('guest',NULL,NULL)", "('guest',1,1)"):
            with pytest.raises(psycopg.errors.CheckViolation):
                with c.transaction():
                    c.execute(
                        "INSERT INTO tl.asker_grants(asker,channel_id,thread_id) VALUES "
                        + values
                    )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with c.transaction():
                c.execute(
                    "INSERT INTO tl.asker_grants(asker,channel_id) VALUES('guest',%s)",
                    (world["channels"][OTHER],),
                )
    assert access.remove_asker("guest") == dict(name="guest", removed=True)
    with connect() as c:
        assert (
            c.execute("SELECT count(*) AS n FROM tl.asker_grants").fetchone()["n"] == 0
        )


# --- view shape ---------------------------------------------------------------


def columns(c, schema, relation):
    return [
        (r["attname"], r["type"])
        for r in rows(
            c,
            "SELECT a.attname,format_type(a.atttypid,a.atttypmod) AS type FROM pg_attribute a WHERE a.attrelid=to_regclass(%s) AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",
            (f"{schema}.{relation}",),
        )
    ]


def test_views_keep_columns_and_dependents_bound(world):
    client = world["client"]
    with connect() as c:
        gates = {
            r["view_name"]: r["predicate"] for r in rows(c, "SELECT * FROM tl.gates")
        }
        expected = set(FALSE_GATES) | {
            "message_versions",
            "threads",
            "channels",
            "conversations",
            "message_locations",
            "links",
        }
        assert expected <= set(gates)
        assert all(gates[name] == "false" for name in FALSE_GATES)
        for name in gates:
            shape = columns(c, "api", name)
            assert shape and shape == columns(c, "tl", name + "_all"), name
            assert shape == columns(c, "tl", name + "_gated"), name
            options = c.execute(
                "SELECT reloptions FROM pg_class WHERE oid=to_regclass(%s)",
                (f"tl.{name}_gated",),
            ).fetchone()["reloptions"]
            assert options == ["security_barrier=true"], name
        # The columns the gate found are the columns that are there now.
        versions = [name for name, _ in columns(c, "api", "message_versions")]
        assert versions[: len(MESSAGE_COLUMNS)] == MESSAGE_COLUMNS
        assert [name for name, _ in columns(c, "api", "messages")][
            : len(MESSAGE_COLUMNS)
        ] == MESSAGE_COLUMNS
        results = [name for name, _ in columns(c, "api", "search_results")]
        assert results[len(MESSAGE_COLUMNS) :][:4] == [
            "score",
            "conversation_id",
            "first_thread_no",
            "last_thread_no",
        ]
        assert [name for name, _ in columns(c, "api", "threads")] == [
            name for name, _ in columns(c, "tl", "threads")
        ]
        assert [name for name, _ in columns(c, "api", "links")] == [
            name for name, _ in columns(c, "tl", "links")
        ]
        # Dependents still read the replaced view, not a stale copy.
        dependents = {
            r["name"]
            for r in rows(
                c,
                "SELECT DISTINCT v.relname AS name FROM pg_depend d JOIN pg_rewrite w ON w.oid=d.objid JOIN pg_class v ON v.oid=w.ev_class WHERE d.refobjid='api.message_versions'::regclass AND v.oid<>'api.message_versions'::regclass",
            )
        }
        assert "messages" in dependents
        # Runtime roles reach the gated view and nothing behind it.
        for role in ("throughline_app", "throughline_reader"):
            for name in gates:
                assert c.execute(
                    "SELECT has_table_privilege(%s,%s,'SELECT') AS ok",
                    (role, f"api.{name}"),
                ).fetchone()["ok"], (role, name)
        assert not c.execute(
            "SELECT has_schema_privilege('throughline_reader','tl','USAGE') AS ok"
        ).fetchone()["ok"]
    with reader() as c:
        assert count(c, "messages") == 15
        assert count(c, "message_versions") == 15
        assert count(c, "ch_synthetic_chat") == 10
        row = c.execute(
            "SELECT rating,channel,thread_title FROM api.ch_synthetic_chat WHERE external_id='fake-7'"
        ).fetchone()
        assert row == dict(
            rating=7, channel="synthetic.chat", thread_title="zebra plans"
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with c.transaction():
                c.execute("SELECT * FROM tl.message_versions_all")
    # A new version and a re-registered channel go through the same views.
    r = ingest(
        client,
        [
            message(
                3, thread={"external_id": "open"}, content="Synthetic orchard revised"
            )
        ],
    )
    assert r.status_code == 200 and r.json()["new_versions"] == 1, r.text
    r = client.post(
        "/v1/channels",
        json=dict(
            name="synthetic.chat",
            plugin="synthetic",
            fields=[
                dict(name="rating", type="integer", description="Synthetic score"),
                dict(name="mood", type="text", description="Synthetic mood"),
            ],
        ),
    )
    assert r.status_code == 200, r.text
    access.register_asker("guest")
    access.grant("guest", thread_id=world["threads"]["open"])
    for scope, total in ((dict(), 16), (dict(mode="asker", asker="guest"), 6)):
        with reader(**scope) as c:
            assert count(c, "message_versions") == total
            assert count(c, "messages") == total - 1
            assert (
                c.execute(
                    "SELECT count(*) AS n FROM api.ch_synthetic_chat WHERE mood IS NULL AND content='Synthetic orchard revised'"
                ).fetchone()["n"]
                == 1
            )


def test_install_gate_reinstall_appends_columns_and_keeps_predicate(database):
    predicate = "api.asker_sees(v.channel_id, NULL)"
    with psycopg.connect(database, row_factory=dict_row) as c:
        try:
            cid = c.execute(
                "INSERT INTO tl.channels(name,plugin) VALUES('synthetic.probe','synthetic') RETURNING id"
            ).fetchone()["id"]
            c.execute(
                "CREATE TABLE tl.gate_probe(id int PRIMARY KEY,channel_id bigint,body text,extra text)"
            )
            c.execute(
                "INSERT INTO tl.gate_probe VALUES(1,%s,'seen','x'),(2,NULL,'hidden','y')",
                (cid,),
            )
            c.execute("INSERT INTO tl.askers(name) VALUES('guest')")
            c.execute(
                "INSERT INTO tl.asker_grants(asker,channel_id) VALUES('guest',%s)",
                (cid,),
            )
            c.execute(
                "CREATE VIEW api.gate_probe AS SELECT id,channel_id,body FROM tl.gate_probe"
            )
            c.execute("CREATE VIEW api.gate_probe_user AS SELECT * FROM api.gate_probe")

            def seen(asker=None):
                # SET LOCAL lasts for the whole transaction, so every call
                # states all three settings ('' is a direct connection).
                c.execute(
                    sql.SQL("SET LOCAL throughline.scope_mode={}").format(
                        sql.Literal("asker" if asker else "")
                    )
                )
                c.execute(
                    sql.SQL("SET LOCAL throughline.asker={}").format(
                        sql.Literal(asker or "")
                    )
                )
                c.execute("SET LOCAL throughline.unknown_asker='deny'")
                return [
                    tuple(r.values())
                    for r in rows(c, "SELECT * FROM api.gate_probe ORDER BY id")
                ]

            def failing(*args):
                with pytest.raises(psycopg.Error) as error:
                    with c.transaction():
                        c.execute("SELECT tl.install_gate(%s,%s,%s)", args)
                return str(error.value)

            assert "no stored gate predicate" in failing("gate_probe", None, None)
            assert "invalid gate view name" in failing("Gate Probe", "false", None)
            assert "does not exist" in failing("gate_absent", "false", None)
            c.execute("SELECT tl.install_gate('gate_probe',%s)", (predicate,))
            assert seen() == [(1, cid, "seen"), (2, None, "hidden")]
            assert seen("guest") == [(1, cid, "seen")]
            assert seen("stranger") == []
            # Reinstall with a definition that appends a column: the stored
            # predicate is kept and dependents stay bound to their columns.
            c.execute(
                "SELECT tl.install_gate('gate_probe',NULL,'SELECT id,channel_id,body,extra FROM tl.gate_probe;')"
            )
            assert columns(c, "api", "gate_probe")[-1] == ("extra", "text")
            assert columns(c, "tl", "gate_probe_gated")[-1] == ("extra", "text")
            assert len(columns(c, "api", "gate_probe_user")) == 3
            assert seen() == [(1, cid, "seen", "x"), (2, None, "hidden", "y")]
            assert seen("guest") == [(1, cid, "seen", "x")]
            assert (
                c.execute(
                    "SELECT predicate FROM tl.gates WHERE view_name='gate_probe'"
                ).fetchone()["predicate"]
                == predicate
            )
            assert (
                "asker_sees"
                in c.execute(
                    "SELECT pg_get_viewdef('tl.gate_probe_gated'::regclass) AS d"
                ).fetchone()["d"]
            )
            # A plain reinstall changes nothing; columns may only be appended.
            c.execute("SELECT tl.install_gate('gate_probe')")
            assert len(columns(c, "api", "gate_probe")) == 4
            assert seen("guest") == [(1, cid, "seen", "x")]
            failing("gate_probe", None, "SELECT id,channel_id FROM tl.gate_probe")
            failing(
                "gate_probe", None, "SELECT id,body,channel_id,extra FROM tl.gate_probe"
            )
            assert seen("guest") == [(1, cid, "seen", "x")]
            # A new predicate replaces the stored one.
            c.execute("SELECT tl.install_gate('gate_probe','false')")
            assert seen("guest") == [] and len(seen()) == 2
            # A gated api view is never taken as its own definition.
            c.execute("CREATE VIEW api.gate_other AS SELECT id FROM tl.gate_probe")
            c.execute("INSERT INTO tl.gates VALUES('gate_other','false')")
            assert "already gated" in failing("gate_other", None, None)
        finally:
            c.rollback()
    assert not exists("api.gate_probe") and not exists("tl.gate_probe_all")


# --- describe ---------------------------------------------------------------------


def test_describe_access_non_leaking(world):
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)
    access.grant("guest", thread_id=world["threads"]["open"])
    access.register_asker("rival")
    access.grant("rival", channel="synthetic.chat")
    access.grant("rival", thread_id=world["threads"]["secret"])
    access.register_asker("boss", "owner")

    def describe(**scope):
        with reader(**scope) as c:
            return c.execute("SELECT api.describe_access() AS d").fetchone()["d"]

    mine = describe(mode="asker", asker="guest", unknown="deny")
    assert mine["scope_mode"] == "asker" and mine["asker"] == "guest"
    assert mine["asker_mode"] == "restricted" and mine["unrestricted"] is False
    assert mine["granted_channels"] == [OTHER] and mine["granted_threads"] == 1
    text = json.dumps(mine)
    assert "rival" not in text and "boss" not in text
    assert "synthetic.chat" not in text and "zebra" not in text
    rival = describe(mode="asker", asker="rival", unknown="deny")
    assert rival["granted_channels"] == ["synthetic.chat"]
    assert rival["granted_threads"] == 1 and OTHER not in json.dumps(rival)
    boss = describe(mode="asker", asker="boss", unknown="deny")
    assert boss["asker_mode"] == "owner" and boss["unrestricted"] is True
    assert boss["granted_channels"] == [] and "guest" not in json.dumps(boss)
    stranger = describe(mode="asker", asker="stranger", unknown="deny")
    assert stranger["asker_mode"] is None and stranger["unrestricted"] is False
    assert stranger["granted_channels"] == [] and stranger["granted_threads"] == 0
    assert describe(mode="asker", asker="stranger", unknown="unrestricted")[
        "unrestricted"
    ]
    denied = describe(mode="deny", asker="guest")
    assert denied["unrestricted"] is False and denied["granted_channels"] == []
    assert denied["asker"] is None
    direct = describe()
    assert direct["scope_mode"] is None and direct["unrestricted"] is True
    assert set(mine) == set(direct)
    # api.describe (migration 0032) serves it as the subject 'access'.
    with connect() as c:
        has_describe = c.execute(
            "SELECT to_regprocedure('api.describe(text)') IS NOT NULL AS ok"
        ).fetchone()["ok"]
    if has_describe:
        with as_asker("guest") as c:
            served = c.execute("SELECT api.describe('access') AS d").fetchone()["d"]
            assert served == mine
        with as_asker("rival") as c:
            seen = c.execute(
                "SELECT api.describe('channel:synthetic.chat') AS granted,api.describe(%s) AS hidden,api.describe('channel:synthetic.absent') AS absent",
                ("channel:" + OTHER,),
            ).fetchone()
            assert seen["granted"]["name"] == "synthetic.chat"
            assert seen["hidden"] is None and seen["absent"] is None


# --- ACL-6 ----------------------------------------------------------------------


def test_acl_6_runtime_roles_cannot_write_policies(world):
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)
    writes = [
        "INSERT INTO tl.askers(name,mode) VALUES('intruder','owner')",
        "UPDATE tl.askers SET mode='owner'",
        "DELETE FROM tl.askers",
        "TRUNCATE tl.askers CASCADE",
        "INSERT INTO tl.asker_grants(asker,channel_id) SELECT 'guest',id FROM tl.channels",
        "UPDATE tl.asker_grants SET asker='guest'",
        "DELETE FROM tl.asker_grants",
        "UPDATE tl.policy_state SET version=0",
        "DELETE FROM tl.policy_state",
        "INSERT INTO tl.gates VALUES('messages','true')",
        "UPDATE tl.gates SET predicate='true'",
        "DELETE FROM tl.gates",
        "SELECT tl.install_gate('threads','true')",
        "SELECT tl.bump_policy()",
        "CREATE OR REPLACE VIEW api.threads AS SELECT * FROM tl.threads",
        "CREATE OR REPLACE FUNCTION api.asker_unrestricted() RETURNS boolean LANGUAGE sql AS 'SELECT true'",
        "DROP VIEW tl.threads_gated CASCADE",
    ]
    for role in ("app", "reader"):
        for statement in writes:
            with pytest.raises(psycopg.Error) as error:
                with connect(role) as c:
                    c.execute(statement)
            assert isinstance(
                error.value,
                (
                    psycopg.errors.InsufficientPrivilege,
                    psycopg.errors.ReadOnlySqlTransaction,
                    psycopg.errors.UndefinedFunction,
                ),
            ), (role, statement, error.value)
    # The reader cannot read the policy tables either; the app role can.
    for table in ("tl.askers", "tl.asker_grants", "tl.policy_state", "tl.gates"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect("reader") as c:
                c.execute(f"SELECT * FROM {table}")
        with connect("app") as c:
            c.execute(f"SELECT * FROM {table}")
    # Gate functions are callable, but not through validated SQL.
    for statement in (
        "SELECT api.asker_unrestricted()",
        "SELECT api.message_visible(1)",
        "SELECT api.describe_access()",
        "SELECT api.policy_version()",
        "SELECT * FROM tl.askers",
        "SELECT set_config('throughline.scope_mode','owner',true)",
    ):
        with pytest.raises(ValueError):
            validate_sql(statement)
    # A restricted asker cannot widen its own scope from inside a statement.
    with as_asker("guest") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with c.transaction():
                c.execute("SELECT set_config('throughline.scope_mode','owner',true)")
        assert count(c, "messages") == 5
    with connect() as c:
        state = c.execute(
            "SELECT (SELECT count(*) FROM tl.askers) AS askers,(SELECT count(*) FROM tl.asker_grants) AS grants,(SELECT predicate FROM tl.gates WHERE view_name='threads') AS predicate"
        ).fetchone()
    assert state["askers"] == 1 and state["grants"] == 1
    assert state["predicate"] == "api.asker_sees(v.channel_id, v.id)"


# --- performance ------------------------------------------------------------------


def median_ms(c, statement, repeats=7):
    c.execute(statement).fetchall()
    timings = []
    for _ in range(repeats):
        start = time.perf_counter()
        c.execute(statement).fetchall()
        timings.append(time.perf_counter() - start)
    return statistics.median(timings) * 1000


def test_performance_unchanged(world, record_property):
    """The unrestricted branch costs a bounded constant, not a different plan.

    tests/test_performance.py and the 100,000-row rebuild test were timed before
    and after this migration (docs/v11/access.md). This guards the read path:
    the gated view against its own ungated definition, on the same rows.
    """
    with connect() as c:
        bulk(
            c,
            world["channels"]["synthetic.chat"],
            "bulk",
            30000,
            "CASE WHEN i%%1000=0 THEN 'rare quince '||i ELSE 'common filler text '||i END",
        )
        c.execute("ANALYZE tl.messages")
        scan_all = median_ms(
            c,
            "SELECT count(*) FROM tl.message_versions_all WHERE superseded_by IS NULL",
        )
        scan_api = median_ms(c, "SELECT count(*) FROM api.messages")
        find_all = median_ms(
            c,
            "SELECT id FROM tl.message_versions_all WHERE superseded_by IS NULL AND tsv @@ websearch_to_tsquery('english','quince')",
        )
        find_api = median_ms(
            c,
            "SELECT id FROM api.messages WHERE tsv @@ websearch_to_tsquery('english','quince')",
        )
        search = median_ms(c, "SELECT id FROM api.search('quince',10)")
    access.register_asker("guest")
    access.grant("guest", channel="synthetic.chat")
    with as_asker("guest") as c:
        restricted = median_ms(c, "SELECT count(*) FROM api.messages", repeats=3)
    for name, value in dict(
        scan_ungated_ms=scan_all,
        scan_gated_view_ms=scan_api,
        keyword_ungated_ms=find_all,
        keyword_gated_view_ms=find_api,
        search_ms=search,
        restricted_scan_ms=restricted,
    ).items():
        record_property(name, round(value, 2))
    print(
        f"\nGate: scan {scan_all:.1f} -> {scan_api:.1f} ms, keyword {find_all:.2f} -> {find_api:.2f} ms, search {search:.2f} ms, restricted scan {restricted:.1f} ms"
    )
    # A full scan passes every row through two extra plan nodes; an indexed
    # lookup pays for planning the second branch. Losing push-down or the
    # index would cost orders of magnitude, not a fraction.
    assert scan_api <= scan_all * 1.6 + 5
    assert find_api <= find_all * 1.6 + 5
    assert search <= find_all * 3 + 10


# --- command line and HTTP ----------------------------------------------------------


def test_cli_asker_commands(world):
    group = typer.Typer()
    register(group)

    @group.command()
    def other():
        pass

    runner = CliRunner()

    def run(*args, ok=True):
        result = runner.invoke(group, ["asker", *args], color=False)
        assert (result.exit_code == 0) is ok, result.output
        return result.output

    assert json.loads(run("add", "guest", "--json")) == dict(
        name="guest", mode="restricted"
    )
    assert (
        json.loads(run("add", "boss", "--mode", "owner", "--json"))["mode"] == "owner"
    )
    run("add", "wrong", "--mode", "admin", ok=False)
    granted = json.loads(run("grant", "guest", "--channel", OTHER, "--json"))
    assert granted["channels"] == [OTHER] and granted["threads"] == []
    thread = world["threads"]["open"]
    assert json.loads(run("grant", "guest", "--thread", str(thread), "--json"))[
        "threads"
    ] == [thread]
    run("grant", "guest", ok=False)
    run("grant", "guest", "--channel", "synthetic.absent", ok=False)
    run("grant", "missing", "--channel", OTHER, ok=False)
    listed = json.loads(run("list", "--json"))
    assert [a["name"] for a in listed["askers"]] == ["boss", "guest"]
    assert listed["policy_version"] == access.policy_version()
    assert "guest" in run("list")
    with as_asker("guest") as c:
        assert ids(c, "SELECT id FROM api.messages") == sorted(
            world["notes"] + world["open"]
        )
    assert (
        json.loads(run("revoke", "guest", "--channel", OTHER, "--json"))["channels"]
        == []
    )
    run("revoke", "guest", "--channel", OTHER, ok=False)
    assert json.loads(run("remove", "guest", "--json")) == dict(
        name="guest", removed=True
    )
    run("remove", "guest", ok=False)


def test_wave_cli_lib_asker(world):
    from throughline.cli import app

    runner = CliRunner()
    if runner.invoke(app, ["lib", "asker", "--help"]).exit_code != 0:
        pytest.skip("the lib command group is not mounted yet")
    result = runner.invoke(app, ["lib", "asker", "add", "guest", "--json"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app, ["lib", "asker", "grant", "guest", "--channel", OTHER, "--json"]
    )
    assert result.exit_code == 0 and json.loads(result.output)["channels"] == [OTHER]
    result = runner.invoke(app, ["lib", "asker", "list", "--json"])
    assert [a["name"] for a in json.loads(result.output)["askers"]] == ["guest"]


def test_wave_http_restricted_asker_reads_only_granted_rows(world):
    if importlib.util.find_spec("throughline.api.deps") is None:
        pytest.skip("scope propagation is not in the service yet")
    from throughline.api import create_app, deps

    if not hasattr(deps, "scope_of"):
        pytest.skip("scope propagation is not in the service yet")
    access.register_asker("guest")
    access.grant("guest", channel=OTHER)
    token = create_token("synthetic agent", kind="agent", askers=["guest"])["token"]
    with TestClient(create_app()) as agent:
        agent.headers["Authorization"] = "Bearer " + token
        agent.headers["X-Throughline-Asker"] = "guest"
        r = agent.post(
            "/v1/sql", json={"statement": "SELECT id FROM api.messages ORDER BY id"}
        )
        assert r.status_code == 200, r.text
        assert [row["id"] for row in r.json()["rows"]] == world["notes"]
        probe = "SELECT id FROM api.messages WHERE " + PROBES["divide"].format(
            col="content"
        )
        r = agent.post("/v1/sql", json={"statement": probe})
        assert r.status_code == 200 and r.json()["rows"] == [], r.text
        r = agent.get("/v1/search", params={"q": "orchard", "max_results": 50})
        assert r.status_code == 200, r.text
        assert sorted(row["id"] for row in r.json()["rows"]) == world["notes"]
        r = agent.get("/v1/search", params={"q": "zebra"})
        assert r.status_code == 200 and r.json()["rows"] == [], r.text
        # App-role routes read below the gate, so they refuse this asker.
        assert agent.get("/v1/channels").status_code == 403
        r = agent.get("/v1/describe", params={"subject": "access"})
        if r.status_code != 404:
            assert r.status_code == 200, r.text
            text = r.text
            assert "guest" in text and OTHER in text and "synthetic.chat" not in text
            r = agent.get("/v1/describe", params={"subject": "channel:synthetic.chat"})
            assert r.status_code == 200 and "zebra" not in r.text
            assert "orchard" not in r.text
    # The owner token still reads everything, with or without an asker name.
    r = world["client"].post(
        "/v1/sql",
        json={"statement": "SELECT count(*) AS n FROM api.messages"},
        headers={"X-Throughline-Asker": "synthetic-owner"},
    )
    assert r.status_code == 200 and r.json()["rows"] == [dict(n=15)], r.text
