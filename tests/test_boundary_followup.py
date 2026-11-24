"""Follow-up regressions for the API authority, SQL and role boundaries."""

import time
import tracemalloc
import uuid
from pathlib import Path

import psycopg
import pytest
from conftest import ingest, message
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from throughline import db
from throughline.db import connect, create_token, migrate
from throughline.query import FUNCTIONS, SCALAR_FUNCTIONS, query, validate_sql


def plugin_token(plugin="synthetic", name=None):
    created = create_token(name or "synthetic-plugin-" + plugin, "plugin", plugin)
    return {"Authorization": "Bearer " + created["token"]}, created["id"]


def agent_headers():
    return {
        "Authorization": "Bearer " + create_token("synthetic-agent", "agent")["token"]
    }


def foreign_message(client):
    """A message in a channel that belongs to a different plugin."""
    assert (
        client.post(
            "/v1/channels", json={"name": "other.chat", "plugin": "other"}
        ).status_code
        == 200
    )
    response = client.post(
        "/v1/ingest/messages",
        json={
            "plugin": "other",
            "messages": [
                message(channel="other.chat", external_id="foreign-0", fields={})
            ],
        },
    )
    assert response.status_code == 200 and response.json()["inserted"] == 1
    with connect() as c:
        return c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name='other.chat'"
        ).fetchone()["id"]


def end(i):
    return {"channel": "synthetic.chat", "external_id": f"fake-{i}"}


WORK = {"kind": "work", "external_ref": "plex://episode/1", "name": "Invented episode"}
SEASON = {"kind": "work", "external_ref": "plex://season/1", "name": "Season 1"}
SHOW = {"kind": "work", "external_ref": "plex://show/1", "name": "Invented show"}
PUBLISHER = {
    "kind": "organization",
    "external_ref": "youtube:fake-channel",
    "name": "Invented publisher",
}
TRACK = {"kind": "work", "external_ref": "spotify:track:fake", "name": "Invented"}

# The link shapes the shipped connectors emit, as (connector, link).
CONNECTOR_LINKS = [
    ("plex consumed", dict(type="consumed", **{"from": end(0), "to": WORK})),
    ("plex episode of show", dict(type="part_of", **{"from": WORK, "to": SHOW})),
    ("plex episode of season", dict(type="part_of", **{"from": WORK, "to": SEASON})),
    ("plex season of show", dict(type="part_of", **{"from": SEASON, "to": SHOW})),
    ("youtube saved", dict(type="saved", **{"from": end(1), "to": WORK})),
    (
        "youtube published_by",
        dict(type="published_by", **{"from": WORK, "to": PUBLISHER}),
    ),
    (
        "youtube subscribed_to",
        dict(type="subscribed_to", **{"from": end(1), "to": PUBLISHER}),
    ),
    ("spotify consumed", dict(type="consumed", **{"from": end(0), "to": TRACK})),
    (
        "spotify album",
        dict(
            type="part_of",
            **{
                "from": TRACK,
                "to": {
                    "kind": "album",
                    "external_ref": "spotify:album:fake",
                    "name": "Invented album",
                },
            },
        ),
    ),
    (
        "spotify artist",
        dict(
            type="created_by",
            **{
                "from": TRACK,
                "to": {
                    "kind": "artist",
                    "external_ref": "spotify:artist:fake",
                    "name": "Invented artist",
                },
            },
        ),
    ),
    (
        "spotify playlist",
        dict(
            type="played_from",
            **{
                "from": end(0),
                "to": {
                    "kind": "playlist",
                    "external_ref": "spotify:playlist:fake",
                    "name": None,
                },
            },
        ),
    ),
    ("beeper reply", dict(type="reply_to", **{"from": end(1), "to": end(0)})),
    (
        "wave recording",
        dict(
            type="part_of",
            **{
                "from": end(0),
                "to": {"kind": "recording", "external_ref": "wave:fake"},
            },
        ),
    ),
    (
        "gbrain timeline about page",
        dict(type="about", evidence={}, **{"from": end(1), "to": end(0)}),
    ),
    (
        # GBrain forwards the source's own link types, which its manifest
        # (link_types=["about"]) does not and cannot enumerate.
        "gbrain source-defined type",
        dict(
            type="mentions",
            evidence={"context": "invented", "source_record": {"id": 1}},
            **{"from": end(0), "to": end(1)},
        ),
    ),
]
for _, _link in CONNECTOR_LINKS:
    _link["method"] = "source"


def register_manifest(client, headers):
    # As GBrain does: a manifest that declares fewer types than it sends.
    response = client.post(
        "/v1/plugins/synthetic/manifest",
        json={"name": "synthetic", "link_types": ["about"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text


def test_plugin_token_ingests_messages_and_their_links_in_one_batch(client, channel):
    headers, token_id = plugin_token()
    register_manifest(client, headers)
    links = [link for _, link in CONNECTOR_LINKS]
    body = dict(plugin="synthetic", messages=[message(0), message(1)], links=links)
    first = client.post("/v1/ingest/messages", json=body, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["inserted"] == 2
    # A replay after a lost acknowledgement stores nothing twice.
    replay = client.post("/v1/ingest/messages", json=body, headers=headers)
    assert replay.status_code == 200, replay.text
    with connect() as c:
        rows = c.execute("SELECT * FROM tl.links ORDER BY id").fetchall()
    assert len(rows) == len(links)
    assert {r["method"] for r in rows} == {"source"}
    assert {r["token_id"] for r in rows} == {token_id}
    assert {r["type"] for r in rows} >= {"mentions", "part_of", "published_by"}


@pytest.mark.parametrize(
    "link", [link for _, link in CONNECTOR_LINKS], ids=[n for n, _ in CONNECTOR_LINKS]
)
def test_plugin_token_posts_each_connector_link_shape(client, channel, link):
    headers, token_id = plugin_token()
    register_manifest(client, headers)
    sent = client.post(
        "/v1/ingest/messages",
        json=dict(plugin="synthetic", messages=[message(0), message(1)]),
        headers=headers,
    )
    assert sent.status_code == 200, sent.text
    response = client.post("/v1/links", json=link, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["token_id"] == token_id
    assert response.json()["method"] == "source"


def test_plugin_token_cannot_forge_links_through_either_route(client, channel):
    foreign_id = foreign_message(client)
    person = client.post("/v1/identity/people", json={"handle": "synthetic-p"}).json()
    headers, _ = plugin_token()
    assert ingest(client, [message(0)]).status_code == 200
    work = {"kind": "work", "external_ref": "fake-work"}
    forged = [
        # Owner authority.
        dict(type="part_of", method="owner", **{"from": end(0), "to": work}),
        # Any method other than a source assertion.
        dict(type="part_of", method="model", **{"from": end(0), "to": work}),
        # Another plugin's message, by natural key and by id, on either side.
        dict(
            type="reply_to",
            **{
                "from": end(0),
                "to": {"channel": "other.chat", "external_id": "foreign-0"},
            },
        ),
        dict(
            type="reply_to",
            **{"from": {"kind": "message", "id": foreign_id}, "to": end(0)},
        ),
        dict(
            type="about", **{"from": {"kind": "message", "id": foreign_id}, "to": work}
        ),
        # Identity: who wrote or received a message is not a plugin's to assert.
        dict(
            type="author",
            **{"from": end(0), "to": {"kind": "person", "id": person["id"]}},
        ),
        # Kinds that are neither its message nor an entity.
        dict(
            type="synthetic",
            **{"from": {"kind": "page", "id": 1}, "to": {"kind": "table_row", "id": 2}},
        ),
    ]
    for link in forged:
        assert (
            client.post("/v1/links", json=link, headers=headers).status_code == 403
        ), link
        batch = client.post(
            "/v1/ingest/messages",
            json=dict(plugin="synthetic", messages=[message(7)], links=[link]),
            headers=headers,
        )
        assert batch.status_code == 403, (link, batch.text)
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.links").fetchone()["n"] == 0
        # A refused link rolls back the messages delivered with it.
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.messages WHERE external_id='fake-7'"
            ).fetchone()["n"]
            == 0
        )


def test_plugin_token_changes_only_its_own_plugins_links(client, channel):
    foreign_id = foreign_message(client)
    assert ingest(client, [message(0)]).status_code == 200
    work = {"kind": "work", "external_ref": "fake-work"}
    link = dict(type="part_of", method="source", **{"from": end(0), "to": work})
    headers, _ = plugin_token()
    other, _ = plugin_token("other")
    rotated, _ = plugin_token("synthetic", "synthetic-plugin-rotated")
    owner_link = client.post("/v1/links", json=dict(link, type="owner_made")).json()
    own = client.post("/v1/links", json=link, headers=headers).json()
    theirs = client.post(
        "/v1/links",
        json=dict(
            type="about",
            **{"from": {"kind": "message", "id": foreign_id}, "to": work},
        ),
        headers=other,
    ).json()
    removed = {"status": "removed"}

    def patch(row, body, auth):
        return client.patch(f"/v1/links/{row['id']}", json=body, headers=auth)

    # Not written by this plugin, even though one is on its own message.
    assert patch(owner_link, removed, headers).status_code == 403
    assert patch(theirs, removed, headers).status_code == 403
    assert patch(own, removed, other).status_code == 403
    # Never to owner authority, never away from a source assertion.
    assert patch(own, {"method": "owner"}, headers).status_code == 403
    assert patch(own, {"method": "model"}, headers).status_code == 403
    # Its own link, also after the plugin's token was reissued.
    assert patch(own, {"confidence": 0.5}, headers).status_code == 200
    assert patch(own, {"type": "episode_of"}, rotated).status_code == 200
    assert patch(own, removed, rotated).status_code == 200
    assert patch({"id": 999999}, removed, headers).status_code == 404
    with connect() as c:
        # Links stored before writers were recorded have no token.
        legacy = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) SELECT 'legacy','message',m.id,'entity',e.id FROM tl.messages m,tl.entities e WHERE m.external_id='fake-0' LIMIT 1 RETURNING id"
        ).fetchone()
        unknown = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) SELECT 'legacy','entity',e.id,'entity',e.id FROM tl.entities e LIMIT 1 RETURNING id"
        ).fetchone()
        foreign = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) SELECT 'legacy','message',%s,'entity',e.id FROM tl.entities e LIMIT 1 RETURNING id",
            (foreign_id,),
        ).fetchone()
        c.execute(
            "UPDATE tl.links SET token_id=NULL WHERE id=ANY(%s)",
            ([legacy["id"], unknown["id"], foreign["id"]],),
        )
    assert patch(legacy, removed, headers).status_code == 200
    assert patch(unknown, removed, headers).status_code == 403
    assert patch(foreign, removed, headers).status_code == 403


def test_reposting_a_legacy_link_does_not_transfer_it(client, channel):
    link = dict(type="part_of", **{"from": WORK, "to": SEASON})
    headers, _ = plugin_token()
    other, _ = plugin_token("other")
    row = client.post("/v1/links", json=link, headers=headers).json()
    with connect() as c:
        # As stored before links recorded their writer.
        c.execute("UPDATE tl.links SET token_id=NULL WHERE id=%s", (row["id"],))
    removed = {"status": "removed"}
    url = f"/v1/links/{row['id']}"
    assert client.patch(url, json=removed, headers=other).status_code == 403
    # Re-asserting the identical link returns the row and leaves its writer.
    for auth in (other, None):
        again = client.post("/v1/links", json=link, headers=auth)
        assert again.status_code == 200
        assert again.json()["id"] == row["id"]
        assert again.json()["token_id"] is None
    assert client.patch(url, json=removed, headers=other).status_code == 403
    assert client.patch(url, json=removed, headers=headers).status_code == 403
    with connect() as c:
        stored = c.execute(
            "SELECT status,token_id FROM tl.links WHERE id=%s", (row["id"],)
        ).fetchone()
    assert stored == {"status": "active", "token_id": None}


# --- role grants -----------------------------------------------------------


def role_connection(database, role):
    # Not from the pool: these tests change session settings.
    return psycopg.connect(make_conninfo(database, user="throughline_" + role))


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT pg_read_file('PG_VERSION')",
        "SELECT pg_read_binary_file('PG_VERSION')",
        "SELECT pg_ls_dir('.')",
        "SELECT pg_ls_logdir()",
        "SELECT pg_ls_waldir()",
        "SELECT lo_import('PG_VERSION')",
        "SELECT lo_export(0,'tl_test_escape')",
        "SELECT lo_creat(-1)",
        "SELECT lo_create(0)",
        "SELECT lo_from_bytea(0,'x')",
        "SELECT query_to_xml('SELECT 1',false,false,'')",
        "SELECT * FROM ts_stat('SELECT 1')",
    ],
)
def test_app_role_cannot_reach_server_files_or_large_objects(
    client, database, statement
):
    with role_connection(database, "app") as c:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(statement)


def test_app_role_keeps_only_what_the_api_calls(client, database):
    with role_connection(database, "app") as c:
        c.execute("SELECT pg_advisory_xact_lock(1)")
        c.execute("SELECT set_config('throughline.token_id','1',true)")
        c.rollback()
    with connect() as c:
        leaked = c.execute(
            "SELECT p.oid::regprocedure::text name FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname IN ('pg_catalog','public') AND p.proname ~ '^(lo_|pg_read_|pg_ls_|dblink|query_to_xml|table_to_xml|database_to_xml|schema_to_xml|ts_stat|cursor_to_xml)' AND (has_function_privilege('throughline_app',p.oid,'EXECUTE') OR has_function_privilege('throughline_reader',p.oid,'EXECUTE'))"
        ).fetchall()
    assert leaked == []


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TEMP TABLE escape(id int)",
        "SELECT lo_creat(-1)",
        "SELECT lo_create(0)",
        "SELECT lo_from_bytea(0,'x')",
        "CREATE TABLE public.escape(id int)",
        "INSERT INTO api.entities(kind) VALUES ('x')",
        "SELECT * FROM tl.conversations",
        "SELECT set_config('throughline.purge','on',false)",
    ],
)
def test_reader_cannot_write_after_leaving_read_only_mode(client, database, statement):
    with role_connection(database, "reader") as c:
        assert c.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        c.rollback()
        # The reader can turn its own default off; nothing may depend on it.
        c.execute("SET default_transaction_read_only=off")
        c.commit()
        assert c.execute("SHOW transaction_read_only").fetchone()[0] == "off"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute(statement)


def test_harden_fails_loudly_when_a_grant_survives(client, database):
    # A privilege the function cannot take away (here: inherited from a
    # predefined role) must stop the migration, not pass silently.
    # Serialize with other workers' migrations/harden_reader() runs on the same
    # cluster so GRANT / ALTER ROLE never collides on shared role catalogs.
    with db.cluster_lock(database):
        with psycopg.connect(database) as c:
            c.execute("GRANT pg_monitor TO throughline_app")
            with pytest.raises(psycopg.Error, match="role boundary was not applied"):
                c.execute("SELECT tl.harden_reader()")
            c.rollback()
        with connect() as c:
            c.execute("SELECT tl.harden_reader()")


# --- SQL validator ---------------------------------------------------------

ALIAS_ESCAPES = [
    "SELECT s.current_setting FROM lower('server_version') s",
    "SELECT s.pg_sleep FROM generate_series(25,25) s",
    "SELECT s.pg_get_userbyid FROM generate_series(10,20000) s",
    "SELECT s.pg_get_functiondef FROM generate_series(1,99999) s",
    "SELECT (s.ts_stat).* FROM lower('SELECT 1') s",
    "SELECT x.pg_sleep FROM coalesce(5) x",
    "SELECT generate_series.pg_sleep FROM generate_series(25,25)",
    "SELECT s.pg_sleep FROM generate_series(25,25) AS s(i)",
    "SELECT s.pg_sleep FROM generate_series(25,25) WITH ORDINALITY s",
    "SELECT r.pg_sleep FROM ROWS FROM (generate_series(25,25)) r",
    "SELECT c.jsonb_pretty FROM api.catalog() c",
    "SELECT catalog.jsonb_pretty FROM api.catalog()",
    "SELECT (SELECT s.pg_sleep) FROM generate_series(25,25) s",
    "SELECT 1 FROM api.messages m, LATERAL (SELECT s.pg_sleep FROM generate_series(25,25) s) x",
    "SELECT d.pg_sleep FROM current_date d",
    # WITH names are relations only where PostgreSQL makes them visible.
    "WITH pg_roles AS (SELECT * FROM pg_roles) SELECT * FROM pg_roles",
    "SELECT * FROM (WITH pg_roles AS (SELECT 1) SELECT 1) x, pg_roles",
    "WITH a AS (SELECT * FROM b), b AS (SELECT 1) SELECT * FROM a",
    "WITH x AS (SELECT 1) SELECT * FROM public.x",
    "SELECT pg_catalog.pg_sleep(25)",
    "SELECT pg_catalog.lower('x')",
]


@pytest.mark.parametrize("statement", ALIAS_ESCAPES)
def test_validator_rejects_alias_function_and_scope_escapes(statement):
    with pytest.raises(ValueError):
        validate_sql(statement)


ORDINARY = [
    ("WITH x AS (SELECT id FROM api.messages) SELECT * FROM x", 3),
    (
        "WITH x AS (SELECT id FROM api.messages), y AS (SELECT id FROM x) SELECT y.id FROM y JOIN x ON x.id=y.id",
        3,
    ),
    (
        "WITH RECURSIVE n AS (SELECT 1 AS i UNION ALL SELECT n.i+1 FROM n WHERE n.i<4) SELECT i FROM n",
        4,
    ),
    ("SELECT * FROM (WITH x AS (SELECT 1 AS i) SELECT i FROM x) s", 1),
    ("SELECT extract(year FROM happened_at) AS y FROM api.messages", 3),
    ("SELECT happened_at AT TIME ZONE 'UTC' AS t FROM api.messages", 3),
    ("SELECT substring(content FROM 1 FOR 9) AS s FROM api.messages", 3),
    ("SELECT substring(content, 1, 9) AS s FROM api.messages", 3),
    (
        "SELECT trim(both ' ' FROM content) AS s, trim(content) AS t FROM api.messages",
        3,
    ),
    ("SELECT position('orchard' IN content) AS p FROM api.messages", 3),
    ("SELECT m.id FROM api.messages m WHERE m.content LIKE 'Synth%' ESCAPE '!'", 3),
    ("SELECT s.s FROM generate_series(1,3) s", 3),
    ("SELECT s.i, s.ordinality FROM generate_series(1,3) WITH ORDINALITY AS s(i)", 3),
    ("SELECT generate_series.generate_series FROM generate_series(1,3)", 3),
    ("SELECT m.id, m.content FROM api.search('orchard') m", 3),
    ("SELECT c.c FROM api.catalog() c", 1),
    ("SELECT id FROM api.messages ORDER BY id; ", 3),
    ("SELECT id FROM api.messages -- trailing note", 3),
    ("SELECT id AS a, id AS a FROM api.messages /* note */", 3),
    # Output columns named like the result-cap wrapper's own aliases.
    ("SELECT id AS q FROM api.messages", 3),
    ("SELECT count(*) AS q FROM api.messages", 1),
    ("SELECT id AS q, id AS d, id AS ok FROM api.messages q", 3),
]


@pytest.mark.parametrize("statement,count", ORDINARY)
def test_validator_accepts_ordinary_select_shapes(client, channel, statement, count):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    response = client.post("/v1/sql", json={"statement": statement})
    assert response.status_code == 200, response.text
    assert response.json()["row_count"] == count


def test_sql_escapes_are_refused_over_http(client):
    for statement in ALIAS_ESCAPES[:6]:
        response = client.post("/v1/sql", json={"statement": statement})
        assert response.status_code == 400, (statement, response.text)


def test_row_returning_api_functions_are_classified_from_the_catalog(client):
    # alias.name is only left unchecked for functions whose row is composite.
    with connect() as c:
        rows = c.execute(
            "SELECT p.proname,p.proretset AND (t.typtype='c' OR p.prorettype='record'::regtype) composite FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_type t ON t.oid=p.prorettype WHERE n.nspname='api'"
        ).fetchall()
    composite = {r["proname"] for r in rows if r["composite"]}
    assert FUNCTIONS - SCALAR_FUNCTIONS <= composite
    assert not SCALAR_FUNCTIONS & composite


# --- result cap ------------------------------------------------------------


def test_oversized_row_is_never_fetched_or_decoded(client):
    tracemalloc.start()
    try:
        result = query(
            "SELECT array_agg(i) AS items FROM generate_series(1,4000000) AS s(i)"
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result == {"rows": [], "truncated": True, "row_count": 0}
    # About 31 MB of row text; decoding it takes several hundred MB.
    assert peak < 16 * 1024 * 1024
    # The connection is reusable and later rows are still bounded and ordered.
    result = query(
        "SELECT i, CASE WHEN i=3 THEN (SELECT array_agg(g) FROM generate_series(1,1500000) AS t(g)) END AS big FROM generate_series(1,5) AS s(i) ORDER BY i"
    )
    assert result["truncated"] and [r["i"] for r in result["rows"]] == [1, 2]
    assert query("SELECT i FROM generate_series(1,5) AS s(i) ORDER BY i DESC", 3) == {
        "rows": [{"i": 5}, {"i": 4}, {"i": 3}],
        "truncated": True,
        "row_count": 3,
    }
    assert query("SELECT i FROM generate_series(1,3) AS s(i)", 3)["truncated"] is False


# --- identity authority ----------------------------------------------------


def test_agent_cannot_bind_identifiers_or_names_to_protected_people(client):
    owner = client.post(
        "/v1/identity/people", json={"handle": "synthetic-owner", "is_owner": True}
    ).json()["id"]
    asserted = client.post(
        "/v1/identity/people", json={"handle": "synthetic-asserted"}
    ).json()["id"]
    agent = agent_headers()
    free = client.post(
        "/v1/identity/people", json={"handle": "synthetic-free"}, headers=agent
    ).json()["id"]
    for person in (owner, asserted):
        response = client.post(
            "/v1/registry/identifiers",
            json={
                "person_id": person,
                "system": "fake",
                "kind": "email",
                "value": f"mallory{person}@example.invalid",
            },
            headers=agent,
        )
        assert response.status_code == 400, response.text
        assert "protected" in response.json()["detail"]
        assert (
            client.post(
                "/v1/registry/names",
                json={"person_id": person, "name": "Invented alias"},
                headers=agent,
            ).status_code
            == 400
        )
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.identifiers").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) n FROM tl.person_names").fetchone()["n"] == 0
    # Agents still resolve identity for people the owner has not asserted.
    response = client.post(
        "/v1/registry/identifiers",
        json={
            "person_id": free,
            "system": "fake",
            "kind": "email",
            "value": "free@example.invalid",
        },
        headers=agent,
    )
    assert response.status_code == 200, response.text
    assert response.json()["asserted_by"] == "agent"


def test_owner_import_can_keep_agent_authority(client, channel):
    agent = agent_headers()
    person = client.post(
        "/v1/identity/people", json={"handle": "synthetic-proposed"}, headers=agent
    ).json()["id"]
    assert (
        client.post("/v1/registry/fact-keys", json={"key": "fake-city"}).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/registry/relation-kinds",
            json={"kind": "fake-peer", "inverse": "fake-peer"},
        ).status_code
        == 200
    )
    # The identifier already exists, unclaimed, from an ingested message.
    assert ingest(client, [message(0)]).status_code == 200
    imported = [
        (
            "identifiers",
            {
                "person_id": person,
                "system": "fake",
                "kind": "account",
                "value": "fake-account",
                "original_asserted_by": "model",
            },
        ),
        (
            "identifiers",
            {
                "person_id": person,
                "system": "fake",
                "kind": "email",
                "value": "p@example.invalid",
            },
        ),
        ("facts", {"person_id": person, "key": "fake-city", "value": "Invented"}),
        ("relations", {"from_id": person, "to_id": person, "kind": "fake-peer"}),
    ]
    for resource, body in imported:
        response = client.post(
            "/v1/registry/" + resource, json=dict(body, asserted_by="agent")
        )
        assert response.status_code == 200, response.text
        assert response.json()["asserted_by"] == "agent", resource
        assert response.json()["original_asserted_by"] in ("agent", "model")
    with connect() as c:
        row = c.execute("SELECT asserted_by FROM tl.people WHERE id=%s", (person,))
        assert row.fetchone()["asserted_by"] == "agent"
    # The person is not locked: an agent can still work on it.
    assert (
        client.post(
            "/v1/identity/rename",
            json={"person_id": person, "handle": "synthetic-renamed"},
            headers=agent,
        ).status_code
        == 200
    )
    # A client can lower authority only; anything else is the token's.
    response = client.post(
        "/v1/registry/identifiers",
        json={
            "system": "fake",
            "kind": "email",
            "value": "q@example.invalid",
            "asserted_by": "owner",
        },
        headers=agent,
    )
    assert response.json()["asserted_by"] == "agent"
    stamped = client.post(
        "/v1/registry/facts",
        json={
            "person_id": person,
            "key": "fake-city",
            "value": "Elsewhere",
            "asserted_by": "fake-source",
        },
    ).json()
    assert stamped["asserted_by"] == "owner"
    assert stamped["original_asserted_by"] == "fake-source"
    assert (
        client.post(
            "/v1/identity/rename",
            json={"person_id": person, "handle": "synthetic-stolen"},
            headers=agent,
        ).status_code
        == 400
    )


# --- auditing --------------------------------------------------------------


def audit_rows():
    with connect() as c:
        return c.execute("SELECT * FROM tl.audit_log ORDER BY id").fetchall()


def test_sql_audit_row_has_row_count_and_status(client, channel):
    assert ingest(client, [message(i) for i in range(5)]).status_code == 200
    before = len(audit_rows())
    statement = "SELECT id FROM api.messages ORDER BY id"
    response = client.post(
        "/v1/sql",
        json={"statement": statement, "row_cap": 2},
        headers={"X-Throughline-Asker": "synthetic-asker"},
    )
    assert response.status_code == 200
    assert (
        client.post(
            "/v1/sql", json={"statement": "SELECT * FROM tl.messages"}
        ).status_code
        == 400
    )
    assert client.get("/v1/search", params={"q": "orchard"}).status_code == 200
    rows = audit_rows()[before:]
    # One row per request, not one for the route and one for the statement.
    assert len(rows) == 3
    assert rows[0]["statement"] == statement
    assert rows[0]["row_count"] == 2 and rows[0]["http_status"] == 200
    assert rows[0]["asker"] == "synthetic-asker"
    assert rows[0]["detail"] == {"route": "POST /v1/sql"}
    assert rows[1]["statement"] == "SELECT * FROM tl.messages"
    assert rows[1]["row_count"] == 0 and rows[1]["http_status"] == 400
    assert "search_filtered" in rows[2]["statement"]
    assert rows[2]["row_count"] == 5 and rows[2]["http_status"] == 200
    assert rows[2]["detail"] == {"route": "GET /v1/search"}


def test_authenticated_reads_are_audited(client, channel):
    agent = agent_headers()
    client.post("/v1/identity/people", json={"handle": "synthetic-read"})
    client.post(
        "/v1/registry/identifiers",
        json={"system": "fake", "kind": "email", "value": "r@example.invalid"},
    )
    before = len(audit_rows())
    reads = [
        ("/v1/identity/people", 1),
        ("/v1/identity/people/1", 1),
        ("/v1/identity/unclaimed", 1),
        ("/v1/registry/identifiers", 1),
        ("/v1/channels", 1),
        ("/v1/status", None),
        ("/v1/plugins/synthetic/cursor/none", None),
    ]
    for path, _ in reads[:-1]:
        assert client.get(path, headers=agent).status_code == 200, path
    assert client.get(reads[-1][0]).status_code == 404
    rows = audit_rows()[before:]
    assert [(r["statement"], r["row_count"]) for r in rows[: len(reads)]] == [
        ("GET " + path, count) for path, count in reads
    ]
    assert [r["http_status"] for r in rows[: len(reads)]] == [200] * 6 + [404]
    with connect() as c:
        agent_id = c.execute(
            "SELECT id FROM tl.tokens WHERE name='synthetic-agent'"
        ).fetchone()["id"]
    assert {r["token_id"] for r in rows[:6]} == {agent_id}
    # Refused requests are attributed too.
    plugin, plugin_id = plugin_token()
    assert client.get("/v1/identity/people", headers=plugin).status_code == 403
    last = audit_rows()[-1]
    assert (last["statement"], last["http_status"], last["token_id"]) == (
        "GET /v1/identity/people",
        403,
        plugin_id,
    )


def test_entity_and_link_writes_are_attributed(client, channel):
    agent = agent_headers()
    with connect() as c:
        agent_id = c.execute(
            "SELECT id FROM tl.tokens WHERE name='synthetic-agent'"
        ).fetchone()["id"]
    first = client.post("/v1/entities", json={"kind": "work"}, headers=agent).json()
    second = client.post("/v1/entities", json={"kind": "work"}, headers=agent).json()
    assert first["token_id"] == agent_id
    plugin, plugin_id = plugin_token()
    assert ingest(client, [message(0)]).status_code == 200
    link = client.post(
        "/v1/links",
        json={
            "type": "part_of",
            "from": end(0),
            "to": {"kind": "work", "external_ref": "fake-placeholder"},
        },
        headers=plugin,
    ).json()
    before = len(audit_rows())
    assert (
        client.post(
            "/v1/entities/merge",
            json={"source": first["id"], "target": second["id"]},
            headers=agent,
        ).status_code
        == 200
    )
    merge = audit_rows()[before]
    assert merge["statement"] == "POST /v1/entities/merge"
    assert merge["token_id"] == agent_id and merge["http_status"] == 200
    assert merge["detail"] == {"source": first["id"], "target": second["id"]}
    with connect() as c:
        placeholder = c.execute(
            "SELECT token_id FROM tl.entities WHERE external_ref='fake-placeholder'"
        ).fetchone()
        assert placeholder["token_id"] == plugin_id
        created = c.execute(
            "SELECT detail FROM tl.audit_log WHERE statement='POST /v1/entities' ORDER BY id LIMIT 1"
        ).fetchone()
        assert created["detail"] == {"entity_id": first["id"]}
        linked = c.execute(
            "SELECT detail,token_id FROM tl.audit_log WHERE statement='POST /v1/links' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert linked == {"detail": {"link_id": link["id"]}, "token_id": plugin_id}


# --- connections and migration ---------------------------------------------


def terminate_other_backends(database):
    with psycopg.connect(database, autocommit=True) as admin:
        others = "FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()"
        killed = admin.execute(
            "SELECT count(pg_terminate_backend(pid)) " + others
        ).fetchone()[0]
        for _ in range(100):
            if admin.execute("SELECT count(*) " + others).fetchone()[0] == 0:
                break
            time.sleep(0.05)
    time.sleep(0.1)
    return killed


@pytest.mark.parametrize("probe", ["socket", "ping"])
def test_pool_never_hands_out_a_connection_the_server_closed(
    client, channel, database, monkeypatch, probe
):
    if probe == "ping":
        # A dead peer the socket does not report yet is caught by the ping.
        monkeypatch.setattr(db, "_socket_dead", lambda conn: False)
        monkeypatch.setattr(db, "_IDLE_CHECK_S", 0.0)
    for _ in range(3):
        assert client.get("/v1/channels").status_code == 200
    assert client.post("/v1/sql", json={"statement": "SELECT 1"}).status_code == 200
    assert terminate_other_backends(database) >= 2
    # As after a PostgreSQL restart: the very next requests must succeed.
    assert ingest(client, [message(0)]).status_code == 200
    assert client.get("/v1/channels").status_code == 200
    response = client.post("/v1/sql", json={"statement": "SELECT 1 AS one"})
    assert response.status_code == 200 and response.json()["rows"] == [{"one": 1}]


def test_connection_lost_during_a_request_reports_the_original_error(database):
    with pytest.raises(psycopg.OperationalError):
        with connect("app") as conn:
            pid = conn.execute("SELECT pg_backend_pid()").fetchone()["pg_backend_pid"]
            with psycopg.connect(database, autocommit=True) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
            time.sleep(0.2)
            conn.execute("SELECT 1")
    with connect("app") as conn:
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1


def test_migrate_does_not_need_the_maintenance_database(database, monkeypatch):
    real = psycopg.connect
    attempts = []

    def unreachable(conninfo="", **kwargs):
        if conninfo_to_dict(conninfo).get("dbname") == "postgres":
            attempts.append(conninfo)
            raise psycopg.OperationalError("database postgres is not reachable")
        return real(conninfo, **kwargs)

    db.close_runtime_connections()
    monkeypatch.setattr(db.psycopg, "connect", unreachable)
    migrate()
    assert attempts
    with connect() as c:
        assert c.execute(
            "SELECT 1 one FROM tl.schema_migrations WHERE version='0024_boundary_followup.sql'"
        ).fetchone()


def test_migrate_leaves_role_passwords_unchanged(database):
    admin = make_conninfo(database, dbname="postgres")
    name = "tl_test_" + uuid.uuid4().hex
    verifiers = "SELECT rolname,rolpassword FROM pg_authid WHERE rolname IN ('throughline_app','throughline_reader') ORDER BY rolname"
    with psycopg.connect(admin, autocommit=True) as c:
        before = c.execute(verifiers).fetchall()
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        # Every migration file on a new database, then a repeated run.
        migrate(make_conninfo(database, dbname=name))
        migrate(make_conninfo(database, dbname=name))
        migrate()
        with psycopg.connect(admin, autocommit=True) as c:
            assert c.execute(verifiers).fetchall() == before
    finally:
        db.close_runtime_connections()
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
    assert len(before) == 2


def test_compose_separates_migration_from_the_api_service():
    text = (Path(__file__).parents[1] / "docker-compose.yml").read_text()
    rest = text[text.index("\n  throughline:") :]
    api = rest[: rest.index("\n  connectors:")] if "\n  connectors:" in rest else rest
    migration = text[text.index("\n  migrate:") : text.index("\n  throughline:")]
    assert "throughline migrate && throughline configure-passwords" in migration
    assert "POSTGRES_PASSWORD" in migration
    # The API service is given coordinates and role passwords, no superuser.
    assert "POSTGRES_PASSWORD" not in api
    assert "THROUGHLINE_DATABASE_URL: postgresql://postgres:5432/throughline" in api
    assert "service_completed_successfully" in api
    assert "throughline migrate" not in api
    # Server connectors get plugin tokens only; only the opt-in maintenance
    # service holds the superuser URL besides migrate.
    if "\n  connectors:" in rest:
        connectors = rest[rest.index("\n  connectors:") : rest.index("\n  maintenance:")]
        assert "POSTGRES_PASSWORD" not in connectors
        assert "THROUGHLINE_DATABASE_URL" not in connectors
        assert "PGPASSWORD" not in connectors
        # Its pairing volume is mounted read-only; only migrate writes it.
        assert "connectors-pairing:/home/throughline/pairing:ro" in connectors
        maintenance = rest[rest.index("\n  maintenance:") :]
        assert 'profiles: ["maintenance"]' in maintenance
    # The superuser password never sits inside a URL (any characters work).
    assert "postgres:${POSTGRES_PASSWORD}@" not in text
    assert "PGPASSWORD: ${POSTGRES_PASSWORD}" in migration
    # One image for every service, built once.
    assert "connectors.Dockerfile" not in text
    assert text.count("build: .") == 1
