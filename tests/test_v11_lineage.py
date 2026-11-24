"""Lineage (migration 0036): validity, link history and supersession,
provenance writers, source-inherited visibility, as-of functions and
reversible entity merges. Synthetic data only."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import message
from fastapi.testclient import TestClient
from psycopg import sql

from throughline import access, allowlist, lineage
from throughline.api import create_app
from throughline.db import connect, create_token
from throughline.query import validate_sql

AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
PAGES = "synthetic.pages"
SECRET = "synthetic.secret"
DERIVED = "synthetic.derived"
LINEAGE_FUNCTIONS = {
    "links_asof",
    "links_valid_at",
    "follow_asof",
    "relations_asof",
    "facts_asof",
    "sources",
}


@pytest.fixture(autouse=True)
def no_askers(database):
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


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


def as_guest():
    return reader("asker", "guest", "deny")


def rows(c, statement, params=None):
    return c.execute(statement, params).fetchall()


def day(n):
    return (AT + timedelta(days=n)).isoformat()


def record(channel, external_id, content, at=0, thread=None, **kwargs):
    return message(
        0,
        channel=channel,
        external_id=external_id,
        thread={"external_id": thread or external_id},
        content=content,
        happened_at=day(at),
        fields={},
        **kwargs,
    )


def key(channel, external_id):
    return {"channel": channel, "external_id": external_id}


def send(client, messages, links=None, plugin="synthetic"):
    r = client.post(
        "/v1/ingest/messages",
        json=dict(plugin=plugin, messages=messages, links=links or []),
    )
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    return r.json()


def current(channel, external_id):
    with connect() as c:
        return c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
            (channel, external_id),
        ).fetchone()["id"]


def plugin_headers(plugin="synthetic"):
    token = create_token("synthetic-plugin-" + plugin, "plugin", plugin)["token"]
    return {"Authorization": "Bearer " + token}


def agent_headers(askers=None):
    token = create_token("synthetic-agent", "agent", askers=askers)["token"]
    return {"Authorization": "Bearer " + token}


def clock():
    with connect() as c:
        return c.execute("SELECT clock_timestamp() t").fetchone()["t"]


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.fixture()
def world(client, channel):
    """synthetic.chat (life events, fake-0..2), synthetic.secret (secret-0),
    synthetic.pages (page_revision, provenance_link_types ['cites']) and
    synthetic.derived (derived)."""
    for body in (
        dict(name=SECRET, plugin="synthetic"),
        dict(
            name=PAGES,
            plugin="synthetic",
            record_kind="page_revision",
            provenance_link_types=["cites"],
        ),
        dict(name=DERIVED, plugin="synthetic", record_kind="derived"),
    ):
        r = client.post("/v1/channels", json=body)
        assert r.status_code == 200, r.text
    send(
        client,
        [message(i) for i in range(3)]
        + [record(SECRET, "secret-0", "Synthetic zebra secret")],
    )
    return client


def page(client, external_id, content, at=40, cites=(), other_links=()):
    links = [
        {"type": "cites", "from": key(PAGES, external_id), "to": target}
        for target in cites
    ] + [dict(link, **{"from": key(PAGES, external_id)}) for link in other_links]
    send(client, [record(PAGES, external_id, content, at)], links)
    return current(PAGES, external_id)


# --- BRN-14 validity ----------------------------------------------------------


def test_brn_14_validity_roundtrip_links_relations_facts_through_http(world):
    client = world
    work = {"kind": "work", "external_ref": "fake-work"}
    body = {
        "type": "about",
        "from": key("synthetic.chat", "fake-0"),
        "to": work,
        "valid_from": "2020-01-01",
        "valid_from_precision": "day",
        "valid_to": "2021-06-01T00:00:00Z",
        "valid_to_precision": "exact",
    }
    r = client.post("/v1/links", json=body)
    assert r.status_code == 200, r.text
    link = r.json()
    assert parse(link["valid_from"]) == AT and link["valid_from_precision"] == "day"
    assert parse(link["valid_to"]) == datetime(2021, 6, 1, tzinfo=timezone.utc)
    assert link["from_version_id"] == link["from_id"] and link["to_version_id"] is None
    # Validity is part of the replay-equality check.
    assert client.post("/v1/links", json=body).json()["id"] == link["id"]
    other = client.post("/v1/links", json=dict(body, valid_to_precision="month"))
    assert other.status_code == 200 and other.json()["id"] != link["id"]
    for bad in (
        dict(body, valid_from_precision="week"),
        dict(body, valid_from="2022-01-01"),  # after valid_to
        dict(body, valid_from="2020-01-01T00:00:00"),  # no offset
        {k: v for k, v in body.items() if k != "valid_from"},  # day on no bound
    ):
        assert client.post("/v1/links", json=bad).status_code == 400, bad
    url = f"/v1/links/{link['id']}"
    r = client.patch(url, json={"valid_to": None, "valid_to_precision": "unknown"})
    assert r.status_code == 200, r.text
    assert r.json()["valid_to"] is None and r.json()["valid_to_precision"] == "unknown"
    assert client.patch(url, json={"valid_to_precision": "decade"}).status_code == 400
    assert client.patch(url, json={"valid_from": None}).status_code == 400
    # Relations and facts through the registry import.
    people = [
        client.post("/v1/identity/people", json={"handle": h}).json()["id"]
        for h in ("synthetic-a", "synthetic-b")
    ]
    assert (
        client.post(
            "/v1/registry/relation-kinds", json={"kind": "parent", "inverse": "child"}
        ).status_code
        == 200
    )
    relation = {
        "from_id": people[0],
        "to_id": people[1],
        "kind": "parent",
        "valid_from": "2010-05-01",
        "valid_from_precision": "month",
        "valid_to_precision": "unknown",
    }
    r = client.post("/v1/registry/relations", json=relation)
    assert r.status_code == 200, r.text
    assert r.json()["valid_from_precision"] == "month"
    assert r.json()["valid_to"] is None and r.json()["valid_to_precision"] == "unknown"
    for bad in (
        dict(relation, valid_from_precision="soon"),
        dict(relation, valid_to="2001-01-01T00:00:00Z", valid_to_precision="year"),
        dict(relation, valid_to_precision="day"),
    ):
        assert client.post("/v1/registry/relations", json=bad).status_code == 400, bad
    assert (
        client.post(
            "/v1/registry/fact-keys", json={"key": "synthetic_city"}
        ).status_code
        == 200
    )
    fact = {
        "person_id": people[0],
        "key": "synthetic_city",
        "value": "Fake Town",
        "valid_from": "2015-01-01T00:00:00+02:00",
        "valid_from_precision": "year",
        "valid_to": "2018-01-01",
        "valid_to_precision": "day",
    }
    r = client.post("/v1/registry/facts", json=fact)
    assert r.status_code == 200, r.text
    assert parse(r.json()["valid_to"]) == datetime(2018, 1, 1, tzinfo=timezone.utc)
    assert (
        client.post(
            "/v1/registry/facts", json=dict(fact, valid_to_precision="fortnight")
        ).status_code
        == 400
    )
    listed = client.get("/v1/registry/facts").json()
    assert [f["valid_from_precision"] for f in listed] == ["year"]
    with connect() as c:
        columns = {
            r["table_name"]: r["cols"]
            for r in c.execute(
                "SELECT table_name,array_agg(column_name::text) cols FROM information_schema.columns WHERE table_schema='api' AND table_name IN ('links','relations','relations_history','facts','facts_history') GROUP BY table_name"
            )
        }
        assert set(columns) == {
            "links",
            "relations",
            "relations_history",
            "facts",
            "facts_history",
        }
        assert all(set(lineage.VALIDITY) <= set(v) for v in columns.values())
        both = c.execute(
            "SELECT kind,valid_from_precision FROM api.relations ORDER BY kind"
        ).fetchall()
        assert both == [
            {"kind": "child", "valid_from_precision": "month"},
            {"kind": "parent", "valid_from_precision": "month"},
        ]


def without_receipt(body):
    return {k: v for k, v in body.items() if k != "receipt"}


def registry_people(client):
    people = [
        client.post("/v1/identity/people", json={"handle": h}).json()["id"]
        for h in ("synthetic-a", "synthetic-b", "synthetic-c")
    ]
    client.post(
        "/v1/registry/relation-kinds", json={"kind": "parent", "inverse": "child"}
    )
    return people


def test_brn_14_relations_asof_both_directions_with_ended_at_and_unknown_bounds(
    client,
):
    a, b, c3 = registry_people(client)
    for body in (
        # Valid from 2010, ended (not given a valid_to) in 2015.
        {
            "from_id": a,
            "to_id": b,
            "kind": "parent",
            "created_at": "2009-01-01T00:00:00Z",
            "valid_from": "2010-01-01",
            "valid_from_precision": "year",
            "ended_at": "2015-01-01T00:00:00Z",
        },
        # Bounds that exist but are unknown: unbounded for applies_at.
        {
            "from_id": b,
            "to_id": c3,
            "kind": "parent",
            "created_at": "2000-01-01T00:00:00Z",
            "valid_from_precision": "unknown",
            "valid_to_precision": "unknown",
        },
        # Stored only in 2030: not known before then.
        {
            "from_id": a,
            "to_id": c3,
            "kind": "parent",
            "created_at": "2030-01-01T00:00:00Z",
        },
    ):
        r = client.post("/v1/registry/relations", json=body)
        assert r.status_code == 200, r.text

    def asof(at):
        with reader() as c:
            return sorted(
                (r["from_id"], r["kind"], r["to_id"], r["valid_from_precision"])
                for r in rows(c, "SELECT * FROM api.relations_asof(%s)", (at,))
            )

    unknown = [(b, "parent", c3, "unknown"), (c3, "child", b, "unknown")]
    assert asof("2005-01-01Z") == sorted(unknown)
    assert asof("2012-06-01Z") == sorted(
        unknown + [(a, "parent", b, "year"), (b, "child", a, "year")]
    )
    assert asof("2016-01-01Z") == sorted(unknown)
    assert asof("2031-01-01Z") == sorted(
        unknown + [(a, "parent", c3, None), (c3, "child", a, None)]
    )
    # api.relations shows current relations only; as-of includes ended ones.
    with reader() as c:
        assert {
            (r["from_id"], r["to_id"]) for r in rows(c, "SELECT * FROM api.relations")
        } == {
            (b, c3),
            (c3, b),
            (a, c3),
            (c3, a),
        }
    access.register_asker("guest")
    with as_guest() as c:
        assert rows(c, "SELECT * FROM api.relations_asof('2012-06-01Z')") == []


def test_brn_14_facts_asof(client):
    a, b, _ = registry_people(client)
    client.post("/v1/registry/fact-keys", json={"key": "synthetic_city"})
    for value, person, extra in (
        (
            "Fake Town",
            a,
            {
                "created_at": "2017-01-01T00:00:00Z",
                "valid_from": "2018-01-01",
                "valid_from_precision": "day",
                "valid_to": "2019-01-01",
                "valid_to_precision": "day",
            },
        ),
        (
            "Fake City",
            b,
            {
                "created_at": "2017-01-01T00:00:00Z",
                "valid_from": "2018-06-01",
                "retired_at": "2020-01-01T00:00:00Z",
            },
        ),
        ("Late Town", b, {"created_at": "2030-01-01T00:00:00Z"}),
    ):
        r = client.post(
            "/v1/registry/facts",
            json={"person_id": person, "key": "synthetic_city", "value": value} | extra,
        )
        assert r.status_code == 200, r.text

    def asof(at):
        with reader() as c:
            return sorted(
                r["value"] for r in rows(c, "SELECT * FROM api.facts_asof(%s)", (at,))
            )

    assert asof("2017-06-01Z") == []
    assert asof("2018-03-01Z") == ["Fake Town"]
    assert asof("2018-09-01Z") == ["Fake City", "Fake Town"]
    assert asof("2019-06-01Z") == ["Fake City"]
    assert asof("2021-01-01Z") == []
    assert asof("2031-01-01Z") == ["Late Town"]
    access.register_asker("guest")
    with as_guest() as c:
        assert rows(c, "SELECT * FROM api.facts_asof('2018-09-01Z')") == []


# --- BRN-13 history, supersession, merges ------------------------------------


def test_brn_13_link_history_on_patch_repoint_merge_and_links_asof(world):
    client = world
    placeholder = {"kind": "work", "external_ref": "fake-work"}
    link = client.post(
        "/v1/links",
        json={
            "type": "about",
            "from": key("synthetic.chat", "fake-0"),
            "to": placeholder,
        },
    ).json()
    v1 = link["from_id"]
    created = clock()
    r = client.patch(f"/v1/links/{link['id']}", json={"confidence": 0.5})
    assert r.status_code == 200, r.text
    patched = clock()
    # A new version of the message repoints the link; the pinned id stays.
    send(world, [message(0, content="Synthetic orchard edited")])
    v2 = current("synthetic.chat", "fake-0")
    repointed = clock()
    target = client.post(
        "/v1/entities", json={"kind": "work", "name": "Real work"}
    ).json()
    merge = client.post(
        "/v1/entities/merge", json={"source": link["to_id"], "target": target["id"]}
    )
    assert merge.status_code == 200, merge.text
    with connect() as c:
        stored = c.execute(
            "SELECT * FROM tl.links WHERE id=%s", (link["id"],)
        ).fetchone()
    assert (stored["from_id"], stored["from_version_id"]) == (v2, v1)
    assert stored["to_id"] == target["id"]
    r = client.get(f"/v1/links/{link['id']}/history")
    assert r.status_code == 200, r.text
    history = r.json()["history"]
    assert [h["op"] for h in history] == ["update", "update", "update"]
    assert [h["old"]["confidence"] for h in history] == [1, 0.5, 0.5]
    assert [h["old"]["from_id"] for h in history] == [v1, v1, v2]
    assert history[2]["old"]["to_id"] == link["to_id"]
    assert r.json()["link"]["to_id"] == target["id"]
    with connect() as c:
        tokens = {
            h["token_id"] for h in rows(c, "SELECT token_id FROM tl.link_history")
        }
    assert None not in tokens  # every HTTP change names its token

    def state(at):
        with reader() as c:
            found = rows(
                c, "SELECT * FROM api.links_asof(%s) WHERE id=%s", (at, link["id"])
            )
        return [(r["confidence"], r["from_id"], r["to_id"]) for r in found]

    assert state(created - timedelta(days=1)) == []
    assert state(created) == [(1, v1, link["to_id"])]
    assert state(patched) == [(0.5, v1, link["to_id"])]
    assert state(repointed) == [(0.5, v2, link["to_id"])]
    assert state(clock()) == [(0.5, v2, target["id"])]
    # A deleted link is present before its deletion and absent after it.
    with connect() as c:
        gone = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) VALUES('synthetic','message',%s,'entity',%s) RETURNING id",
            (v2, target["id"]),
        ).fetchone()["id"]
    before = clock()
    with connect() as c:
        c.execute("DELETE FROM tl.links WHERE id=%s", (gone,))
    with reader() as c:
        assert rows(c, "SELECT id FROM api.links_asof(%s) WHERE id=%s", (before, gone))
        assert not rows(c, "SELECT id FROM api.links_asof(now()) WHERE id=%s", (gone,))
        deleted = rows(c, "SELECT * FROM api.link_history WHERE link_id=%s", (gone,))
    assert [h["op"] for h in deleted] == ["delete"]
    assert client.get(f"/v1/links/{gone}/history").json()["link"] is None
    assert client.get("/v1/links/999999/history").status_code == 404
    # The current state equals api.links.
    with reader() as c:
        now_rows = rows(c, "SELECT * FROM api.links_asof(now()) ORDER BY id")
        assert now_rows == rows(c, "SELECT * FROM api.links ORDER BY id")
    # Restricted askers: app-role history route refused, gated view applies.
    access.register_asker("guest")
    access.grant("guest", channel=SECRET)
    agent = agent_headers(["guest"])
    assert (
        client.get(f"/v1/links/{link['id']}/history", headers=agent).status_code == 403
    )
    with as_guest() as c:
        assert rows(c, "SELECT * FROM api.link_history") == []
        assert rows(c, "SELECT * FROM api.links_asof(now())") == []
    access.grant("guest", channel="synthetic.chat")
    with as_guest() as c:
        assert (
            len(
                rows(
                    c, "SELECT * FROM api.link_history WHERE link_id=%s", (link["id"],)
                )
            )
            == 3
        )


def test_brn_13_supersede_keeps_both(world):
    client = world
    link = client.post(
        "/v1/links",
        json={
            "type": "about",
            "from": key("synthetic.chat", "fake-0"),
            "to": key("synthetic.chat", "fake-1"),
        },
    ).json()
    url = f"/v1/links/{link['id']}/supersede"
    # Nothing changed: refused, the link stays active.
    assert client.post(url, json={}).status_code == 400
    assert client.post(url, json={"colour": "red"}).status_code == 400
    r = client.post(
        url,
        json={
            "to": key("synthetic.chat", "fake-2"),
            "valid_from": "2020-01-01",
            "valid_from_precision": "day",
        },
    )
    assert r.status_code == 200, r.text
    old, new = r.json()["superseded"], r.json()["link"]
    assert old["id"] == link["id"] and old["status"] == "superseded"
    assert old["superseded_by"] == new["id"] and new["status"] == "active"
    assert new["type"] == "about" and new["to_id"] == current(
        "synthetic.chat", "fake-2"
    )
    assert new["valid_from_precision"] == "day"
    assert client.post(url, json={"confidence": 0.3}).status_code == 409
    with connect() as c:
        stored = rows(c, "SELECT id,status,superseded_by FROM tl.links ORDER BY id")
        history = rows(
            c,
            "SELECT old->>'status' s FROM tl.link_history WHERE link_id=%s",
            (link["id"],),
        )
    assert stored == [
        {"id": link["id"], "status": "superseded", "superseded_by": new["id"]},
        {"id": new["id"], "status": "active", "superseded_by": None},
    ]
    assert history == [{"s": "active"}]
    # follow uses active links only, so only the replacement is followed.
    with reader() as c:
        followed = rows(
            c, "SELECT id FROM api.follow('message',%s)", (link["from_id"],)
        )
    assert [r["id"] for r in followed] == [new["to_id"]]
    # Writers' rules are those of PATCH.
    plugin = plugin_headers()
    assert (
        client.post(
            f"/v1/links/{new['id']}/supersede", json={"confidence": 0.2}, headers=plugin
        ).status_code
        == 403
    )
    access.register_asker("guest")
    assert (
        client.post(
            f"/v1/links/{new['id']}/supersede",
            json={"confidence": 0.2},
            headers=agent_headers(["guest"]),
        ).status_code
        == 403
    )


def test_brn_13_entity_merge_split_exact(world):
    client = world
    a = client.post("/v1/entities", json={"kind": "topic", "name": "A"}).json()["id"]
    b = client.post("/v1/entities", json={"kind": "topic", "name": "B"}).json()["id"]
    ends = {
        "m0": key("synthetic.chat", "fake-0"),
        "m1": key("synthetic.chat", "fake-1"),
        "m2": key("synthetic.chat", "fake-2"),
    }
    links = [
        client.post(
            "/v1/links",
            json={
                "type": "about",
                "from": ends["m0"],
                "to": {"kind": "entity", "id": a},
            },
        ).json(),
        client.post(
            "/v1/links",
            json={
                "type": "mentions",
                "from": {"kind": "entity", "id": a},
                "to": ends["m1"],
            },
        ).json(),
        client.post(
            "/v1/links",
            json={
                "type": "about",
                "from": ends["m2"],
                "to": {"kind": "entity", "id": b},
            },
        ).json(),
    ]

    def snapshot():
        with connect() as c:
            return rows(
                c,
                "SELECT id,from_kind,from_id,to_kind,to_id,type FROM tl.links ORDER BY id",
            )

    before = snapshot()
    r = client.post("/v1/entities/merge", json={"source": a, "target": b})
    assert r.status_code == 200, r.text
    event = r.json()["event_id"]
    assert r.json()["moved"] == 2
    merged = snapshot()
    assert (
        merged[0]["to_id"] == b and merged[1]["from_id"] == b and merged[2] == before[2]
    )
    # A link added to the target after the merge is not part of the split.
    later = client.post(
        "/v1/links",
        json={"type": "about", "from": ends["m1"], "to": {"kind": "entity", "id": b}},
    ).json()
    r = client.post("/v1/entities/split", json={"event_id": event})
    assert r.status_code == 200, r.text
    assert r.json()["restored"] == 2
    restored = snapshot()
    assert (
        restored[:3] == before
        and restored[3]["id"] == later["id"]
        and restored[3]["to_id"] == b
    )
    with connect() as c:
        assert (
            c.execute(
                "SELECT merged_into FROM tl.entities WHERE id=%s", (a,)
            ).fetchone()["merged_into"]
            is None
        )
        events = rows(
            c,
            "SELECT op,source,target,merge_event,token_id IS NOT NULL t FROM tl.entity_events ORDER BY id",
        )
    assert events == [
        {"op": "merge", "source": a, "target": b, "merge_event": None, "t": True},
        {"op": "split", "source": a, "target": b, "merge_event": event, "t": True},
    ]
    assert (
        client.post("/v1/entities/split", json={"event_id": event}).status_code == 409
    )
    assert (
        client.post("/v1/entities/split", json={"event_id": 99999}).status_code == 404
    )
    # A moved link changed since the merge: refused, nothing restored.
    event = client.post("/v1/entities/merge", json={"source": a, "target": b}).json()[
        "event_id"
    ]
    client.patch(f"/v1/links/{links[0]['id']}", json={"confidence": 0.4})
    changed = snapshot()
    assert (
        client.post("/v1/entities/split", json={"event_id": event}).status_code == 409
    )
    assert snapshot() == changed
    access.register_asker("guest")
    agent = agent_headers(["guest"])
    assert (
        client.post(
            "/v1/entities/split", json={"event_id": event}, headers=agent
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/entities/merge", json={"source": a, "target": b}, headers=agent
        ).status_code
        == 403
    )


# --- BRN-15, BRN-10 -----------------------------------------------------------


def test_brn_15_model_link_requires_message_ids(world):
    client = world
    m0 = current("synthetic.chat", "fake-0")
    body = {
        "type": "about",
        "method": "model",
        "job": "synthetic-job",
        "job_version": "1",
        "from": key("synthetic.chat", "fake-0"),
        "to": {"kind": "topic", "external_ref": "fake-topic"},
    }
    assert client.post("/v1/links", json=body).status_code == 400
    assert (
        client.post(
            "/v1/links", json=dict(body, evidence={"message_ids": []})
        ).status_code
        == 400
    )
    assert (
        client.post("/v1/links", json=dict(body, evidence={"note": "x"})).status_code
        == 400
    )
    ok = client.post("/v1/links", json=dict(body, evidence={"message_ids": [m0]}))
    assert ok.status_code == 200, ok.text
    source = client.post(
        "/v1/links", json=dict(body, method="source", type="tagged")
    ).json()
    assert (
        client.patch(f"/v1/links/{source['id']}", json={"method": "model"}).status_code
        == 400
    )
    assert (
        client.patch(f"/v1/links/{ok.json()['id']}", json={"evidence": {}}).status_code
        == 400
    )
    # The database enforces it for every writer.
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect("app") as c:
            c.execute(
                "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,method,evidence) VALUES('x','message',%s,'message',%s,'model','{}')",
                (m0, m0),
            )


def test_brn_10_pinned_versions(world):
    client = world
    secret_v1 = current(SECRET, "secret-0")
    page_id = page(
        client, "page-a", "Synthetic lantern page", cites=[key(SECRET, "secret-0")]
    )
    send(client, [record(SECRET, "secret-0", "Synthetic zebra secret revised")])
    secret_v2 = current(SECRET, "secret-0")
    assert secret_v2 != secret_v1
    with connect() as c:
        prov = rows(
            c,
            "SELECT source_message_id,source_version,origin FROM tl.provenance WHERE derived_id=%s",
            (page_id,),
        )
        link = c.execute("SELECT * FROM tl.links WHERE type='cites'").fetchone()
        written = lineage.add_provenance(
            c, "synthetic-job", "passage", 7, [secret_v2], origin="job", job_version="1"
        )
        again = lineage.add_provenance(
            c, "synthetic-job", "passage", 7, [secret_v2], origin="job", job_version="1"
        )
        passage = lineage.sources_of(c, "passage", 7)
    assert prov == [
        {"source_message_id": secret_v1, "source_version": 1, "origin": "source"}
    ]
    assert (link["to_id"], link["to_version_id"]) == (secret_v2, secret_v1)
    assert (written, again) == (1, 0)
    assert [
        (p["source_message_id"], p["source_version"], p["job"]) for p in passage
    ] == [(secret_v2, 2, "synthetic-job")]
    # The source of the support is pinned; api.sources gives the current text.
    with reader() as c:
        support = c.execute(
            "SELECT support FROM api.records WHERE message_id=%s", (page_id,)
        ).fetchone()["support"]
        listed = rows(c, "SELECT id,content FROM api.sources('message',%s)", (page_id,))
    assert support == [
        {"message_id": secret_v1, "version": 1, "relation": "derived_from"}
    ]
    assert listed == [{"id": secret_v2, "content": "Synthetic zebra secret revised"}]
    with connect() as c:
        for bad in (
            dict(derived_kind="page", derived_id=1, sources=[secret_v2]),
            dict(derived_kind="message", derived_id=page_id, sources=[page_id]),
            dict(derived_kind="message", derived_id=page_id, sources=[999999]),
            dict(
                derived_kind="message",
                derived_id=page_id,
                sources=[secret_v2],
                origin="robot",
            ),
            dict(derived_kind="claim", derived_id=1, sources=[secret_v2], origin="job"),
        ):
            with pytest.raises(ValueError):
                lineage.add_provenance(c, {"id": None}, **bad)


# --- BRN-6 automatic writer and the plugin route -------------------------------


def test_brn_6_provenance_from_declared_link_types(world):
    client = world
    chat0 = current("synthetic.chat", "fake-0")
    chat1 = current("synthetic.chat", "fake-1")
    v1 = page(
        client,
        "page-a",
        "Synthetic lantern page",
        cites=[key("synthetic.chat", "fake-0")],
        other_links=[{"type": "mentions", "to": key("synthetic.chat", "fake-1")}],
    )
    # A cites link from a life event writes nothing.
    client.post(
        "/v1/links",
        json={
            "type": "cites",
            "from": key("synthetic.chat", "fake-2"),
            "to": key("synthetic.chat", "fake-1"),
        },
    )

    def provenance():
        with connect() as c:
            return rows(
                c,
                "SELECT derived_kind,derived_id,source_message_id,source_version,relation,origin FROM tl.provenance ORDER BY derived_id,source_message_id",
            )

    assert provenance() == [
        {
            "derived_kind": "message",
            "derived_id": v1,
            "source_message_id": chat0,
            "source_version": 1,
            "relation": "derived_from",
            "origin": "source",
        }
    ]
    # A new revision inherits the sources of its predecessor through the
    # repointed link.
    send(
        client, [record(PAGES, "page-a", "Synthetic lantern page, second revision", 50)]
    )
    v2 = current(PAGES, "page-a")
    assert [(p["derived_id"], p["source_message_id"]) for p in provenance()] == [
        (v1, chat0),
        (v2, chat0),
    ]
    # Declaring a type later does not rewrite history, but new links count;
    # an owner link writes owner provenance.
    r = client.post(
        "/v1/links",
        json={
            "type": "cites",
            "method": "owner",
            "from": {"kind": "message", "id": v2},
            "to": {"kind": "message", "id": chat1},
        },
    )
    assert r.status_code == 200, r.text
    assert [
        (p["derived_id"], p["source_message_id"], p["origin"]) for p in provenance()
    ] == [
        (v1, chat0, "source"),
        (v2, chat0, "source"),
        (v2, chat1, "owner"),
    ]
    with reader() as c:
        assert {
            r["message_id"]
            for r in c.execute(
                "SELECT jsonb_array_elements(support)->>'message_id' message_id FROM api.records WHERE message_id=%s",
                (v2,),
            )
        } == {str(chat0), str(chat1)}


def foreign_channels(client):
    for body in (
        dict(name="other.chat", plugin="other"),
        dict(name="other.pages", plugin="other", record_kind="page_revision"),
    ):
        assert client.post("/v1/channels", json=body).status_code == 200
    send(
        client,
        [
            record("other.chat", "foreign-0", "Synthetic foreign"),
            record("other.pages", "foreign-page", "Synthetic foreign page"),
        ],
        plugin="other",
    )


def test_plugin_provenance_scope(world):
    client = world
    foreign_channels(client)
    page_id = page(client, "page-a", "Synthetic lantern page")
    plugin = plugin_headers()
    body = {
        "derived": key(PAGES, "page-a"),
        "sources": [
            key("other.chat", "foreign-0"),
            key("synthetic.chat", "fake-0"),
            key("other.chat", "no-such"),
            key("no.such", "fake-0"),
        ],
        "relation": "cites",
    }
    r = client.post("/v1/provenance", json=body, headers=plugin)
    assert r.status_code == 200, r.text
    # Writes now carry a receipt (write contract); compare the counts only.
    assert without_receipt(r.json()) == {"written": 2, "unresolved": 2}
    again = client.post("/v1/provenance", json=body, headers=plugin).json()
    assert without_receipt(again) == {"written": 0, "unresolved": 2}
    with connect() as c:
        stored = rows(
            c,
            "SELECT derived_id,source_message_id,relation,origin,token_id IS NOT NULL t FROM tl.provenance ORDER BY source_message_id",
        )
    assert {(s["derived_id"], s["relation"], s["origin"], s["t"]) for s in stored} == {
        (page_id, "cites", "source", True)
    }
    assert {s["source_message_id"] for s in stored} == {
        current("other.chat", "foreign-0"),
        current("synthetic.chat", "fake-0"),
    }
    refused = [
        dict(body, derived=key("other.pages", "foreign-page")),  # another plugin's
        dict(body, derived=key("synthetic.chat", "fake-0")),  # not generated
        dict(body, derived={"kind": "message", "id": page_id}),  # by id
    ]
    for data in refused:
        assert (
            client.post("/v1/provenance", json=data, headers=plugin).status_code == 403
        ), data
    assert (
        client.post(
            "/v1/provenance",
            json=dict(body, derived=key(PAGES, "no-such")),
            headers=plugin,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/provenance", json=dict(body, sources=[{"id": 1}]), headers=plugin
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/provenance", json=dict(body, sources=[]), headers=plugin
        ).status_code
        == 400
    )
    assert (
        client.post("/v1/provenance", json=body, headers=agent_headers()).status_code
        == 403
    )
    # The owner writes owner provenance, also for other derived kinds.
    r = client.post(
        "/v1/provenance",
        json={
            "derived": {"kind": "passage", "id": 11},
            "sources": [{"id": current(SECRET, "secret-0")}],
        },
    )
    assert r.status_code == 200, r.text
    assert without_receipt(r.json()) == {"written": 1, "unresolved": 0}
    with connect() as c:
        assert (
            c.execute(
                "SELECT origin FROM tl.provenance WHERE derived_kind='passage'"
            ).fetchone()["origin"]
            == "owner"
        )
    assert (
        client.post(
            "/v1/provenance",
            json={"derived": {"kind": "page", "id": 1}, "sources": [{"id": 1}]},
        ).status_code
        == 400
    )


# --- ACL-9 visibility ----------------------------------------------------------


def test_acl_9_page_with_hidden_source_invisible_everywhere(world):
    client = world
    page_id = page(
        client, "page-a", "Synthetic lantern page", cites=[key(SECRET, "secret-0")]
    )
    secret = current(SECRET, "secret-0")
    access.register_asker("guest")
    access.grant("guest", channel=PAGES)
    statements = {
        "messages": "SELECT id FROM api.messages WHERE channel='synthetic.pages'",
        "message_versions": "SELECT id FROM api.message_versions WHERE channel='synthetic.pages'",
        "records": "SELECT message_id FROM api.records WHERE channel='synthetic.pages'",
        "record_versions": "SELECT message_id FROM api.record_versions WHERE channel='synthetic.pages'",
        "search_all": "SELECT * FROM api.search_all('lantern',20)",
        "search": "SELECT * FROM api.search('lantern',20)",
        "sources": f"SELECT id FROM api.sources('message',{page_id})",
        "provenance": "SELECT * FROM api.provenance",
        "asof": "SELECT message_id FROM api.records_asof(now()) WHERE channel='synthetic.pages'",
    }
    with as_guest() as c:
        for name, statement in statements.items():
            assert rows(c, statement) == [], name
        assert (
            c.execute("SELECT api.message_visible(%s) v", (page_id,)).fetchone()["v"]
            is False
        )
    agent = agent_headers(["guest"])
    r = client.get("/v1/search", params={"q": "lantern"}, headers=agent)
    assert r.status_code == 200 and r.json()["rows"] == [], r.text
    r = client.get(
        "/v1/sources", params={"kind": "message", "id": page_id}, headers=agent
    )
    assert r.status_code == 200 and r.json()["rows"] == [], r.text
    # Granting the source channel makes the page visible everywhere.
    access.grant("guest", channel=SECRET)
    with as_guest() as c:
        for name, statement in statements.items():
            assert rows(c, statement) != [], name
        assert [r["id"] for r in rows(c, statements["sources"])] == [secret]
    r = client.get("/v1/search", params={"q": "lantern"}, headers=agent)
    assert [row["id"] for row in r.json()["rows"]] == [page_id], r.text
    r = client.get(
        "/v1/sources", params={"kind": "message", "id": page_id}, headers=agent
    )
    assert [row["id"] for row in r.json()["rows"]] == [secret], r.text
    assert (
        client.get("/v1/sources", params={"kind": "page", "id": 1}).status_code == 400
    )
    # A page without sources is gated by its own channel grant only.
    unsourced = page(client, "page-b", "Synthetic unsourced lantern")
    access.revoke("guest", channel=SECRET)
    with as_guest() as c:
        assert [r["id"] for r in rows(c, statements["messages"])] == [unsourced]


def test_brn_5_falls_back_to_latest_authorized_revision(world):
    client = world
    v1 = page(
        client,
        "page-a",
        "Synthetic lantern first",
        at=40,
        cites=[key("synthetic.chat", "fake-0")],
    )
    v2 = page(
        client,
        "page-a",
        "Synthetic lantern second",
        at=50,
        cites=[key(SECRET, "secret-0")],
    )
    assert v1 != v2
    access.register_asker("guest")
    access.grant("guest", channel=PAGES)
    access.grant("guest", channel="synthetic.chat")
    asof = "SELECT message_id,version,is_current FROM api.records_asof(now()) WHERE channel='synthetic.pages'"
    with as_guest() as c:
        assert rows(c, asof) == [{"message_id": v1, "version": 1, "is_current": False}]
        assert (
            rows(c, "SELECT * FROM api.records WHERE channel='synthetic.pages'") == []
        )
        assert [
            r["message_id"]
            for r in rows(
                c,
                "SELECT message_id FROM api.record_versions WHERE channel='synthetic.pages'",
            )
        ] == [v1]
        assert rows(
            c,
            f"SELECT message_id FROM api.records_asof('{day(45)}') WHERE channel='synthetic.pages'",
        ) == [{"message_id": v1}]
    with reader() as c:
        assert rows(c, asof) == [{"message_id": v2, "version": 2, "is_current": True}]
        assert [
            r["message_id"]
            for r in rows(
                c, "SELECT message_id FROM api.records WHERE channel='synthetic.pages'"
            )
        ] == [v2]
    access.grant("guest", channel=SECRET)
    with as_guest() as c:
        assert rows(c, asof) == [{"message_id": v2, "version": 2, "is_current": True}]


def derived(client, *names):
    send(client, [record(DERIVED, n, f"Synthetic derived {n}") for n in names])
    return {n: current(DERIVED, n) for n in names}


def test_acl_9_transitive_and_depth_cap_fail_closed(world):
    client = world
    ids = derived(client, *[f"e{i}" for i in range(1, 11)])
    chat = current("synthetic.chat", "fake-0")
    secret = current(SECRET, "secret-0")
    edges = [
        ("e1", ids["e2"]),
        ("e2", ids["e3"]),
        ("e3", ids["e4"]),
        ("e4", ids["e5"]),
        ("e5", chat),
        ("e6", ids["e7"]),
        ("e7", secret),
        ("e8", ids["e9"]),
        ("e9", ids["e8"]),
    ]
    with connect() as c:
        for name, source in edges:
            lineage.add_provenance(
                c, "synthetic-job", "message", ids[name], [source], origin="job"
            )
    access.register_asker("guest")
    access.grant("guest", channel=DERIVED)
    access.grant("guest", channel="synthetic.chat")

    def visible():
        with as_guest() as c:
            found = {
                r["id"]
                for r in rows(
                    c, "SELECT id FROM api.messages WHERE channel='synthetic.derived'"
                )
            }
        return sorted(n for n, i in ids.items() if i in found)

    # e2..e5 reach the chat source within 4 steps; e1 needs 5 and fails
    # closed; e6/e7 rest on a hidden source; e8/e9 form a cycle; e10 has none.
    assert visible() == ["e10", "e2", "e3", "e4", "e5"]
    access.grant("guest", channel=SECRET)
    assert visible() == ["e10", "e2", "e3", "e4", "e5", "e6", "e7"]
    with reader() as c:
        assert (
            len(
                rows(c, "SELECT id FROM api.messages WHERE channel='synthetic.derived'")
            )
            == 10
        )


def test_agent_provenance_never_source_grade(world):
    client = world
    page_id = page(client, "page-a", "Synthetic lantern page")
    with connect() as c:
        lineage.add_provenance(
            c,
            {"id": None, "asker": "guest"},
            "message",
            page_id,
            [current(SECRET, "secret-0")],
            origin="agent",
        )
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.provenance WHERE origin='agent'"
            ).fetchone()["n"]
            == 1
        )
    access.register_asker("guest")
    access.grant("guest", channel=PAGES)
    with as_guest() as c:
        assert [
            r["id"]
            for r in rows(
                c, "SELECT id FROM api.messages WHERE channel='synthetic.pages'"
            )
        ] == [page_id]
    with reader() as c:
        rec = c.execute(
            "SELECT support,unsupported FROM api.records WHERE message_id=%s",
            (page_id,),
        ).fetchone()
        assert rec == {"support": [], "unsupported": True}
        assert rows(c, "SELECT * FROM api.provenance") == []
        assert rows(c, "SELECT * FROM api.sources('message',%s)", (page_id,)) == []
    with connect() as c:
        assert [
            p["origin"]
            for p in lineage.sources_of(c, "message", page_id, include_agent=True)
        ] == ["agent"]
        assert lineage.sources_of(c, "message", page_id) == []


def test_support_of_page_is_its_sources(world):
    client = world
    chat0, chat1 = (
        current("synthetic.chat", "fake-0"),
        current("synthetic.chat", "fake-1"),
    )
    sourced = page(
        client,
        "page-a",
        "Synthetic lantern",
        cites=[key("synthetic.chat", "fake-1"), key("synthetic.chat", "fake-0")],
    )
    bare = page(client, "page-b", "Synthetic bare")
    with reader() as c:
        found = {
            r["message_id"]: r
            for r in rows(
                c,
                "SELECT message_id,kind,generated,unsupported,support FROM api.records WHERE message_id=ANY(%s)",
                ([sourced, bare, chat0],),
            )
        }
    assert found[sourced]["generated"] and not found[sourced]["unsupported"]
    assert found[sourced]["support"] == [
        {"message_id": chat0, "version": 1, "relation": "derived_from"},
        {"message_id": chat1, "version": 1, "relation": "derived_from"},
    ]
    assert found[bare]["unsupported"] and found[bare]["support"] == []
    assert found[chat0]["support"] == [
        {"message_id": chat0, "version": 1, "relation": "self"}
    ]


def test_qry_11_api_sources_one_call(world):
    client = world
    page_id = page(
        client,
        "page-a",
        "Synthetic lantern",
        cites=[key("synthetic.chat", "fake-0"), key(SECRET, "secret-0")],
    )
    expected = sorted(
        [current("synthetic.chat", "fake-0"), current(SECRET, "secret-0")]
    )
    statement = (
        f"SELECT id,channel,content FROM api.sources('message',{page_id}) ORDER BY id"
    )
    validate_sql(statement)
    r = client.post("/v1/sql", json={"statement": statement})
    assert r.status_code == 200, r.text
    assert [row["id"] for row in r.json()["rows"]] == expected
    assert {row["channel"] for row in r.json()["rows"]} == {"synthetic.chat", SECRET}
    r = client.get("/v1/sources", params={"kind": "message", "id": page_id})
    assert [row["id"] for row in r.json()["rows"]] == expected
    with reader() as c:
        columns = [
            d.name
            for d in c.execute(
                "SELECT * FROM api.sources('message',%s)", (page_id,)
            ).description
        ]
        assert columns == [
            d.name for d in c.execute("SELECT * FROM api.messages LIMIT 0").description
        ]
        assert rows(c, "SELECT * FROM api.sources('passage',1)") == []
    with connect() as c:
        audit = c.execute(
            "SELECT statement FROM tl.audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert "api.sources('message'," in audit["statement"]


def test_follow_asof_matches_follow_for_now(world):
    client = world
    before = clock()
    entity = client.post("/v1/entities", json={"kind": "topic", "name": "T"}).json()[
        "id"
    ]
    for body in (
        {
            "type": "about",
            "from": key("synthetic.chat", "fake-0"),
            "to": {"kind": "entity", "id": entity},
        },
        {
            "type": "about",
            "from": {"kind": "entity", "id": entity},
            "to": key("synthetic.chat", "fake-1"),
        },
        {
            "type": "reply_to",
            "from": key("synthetic.chat", "fake-1"),
            "to": key("synthetic.chat", "fake-2"),
        },
    ):
        assert client.post("/v1/links", json=body).status_code == 200
    start = current("synthetic.chat", "fake-0")
    with reader() as c:
        for link_type, depth in ((None, 1), (None, 3), ("about", 3)):
            now = sorted(
                (r["kind"], r["id"], r["depth"])
                for r in rows(
                    c,
                    "SELECT * FROM api.follow('message',%s,%s,%s)",
                    (start, link_type, depth),
                )
            )
            asof = sorted(
                (r["kind"], r["id"], r["depth"])
                for r in rows(
                    c,
                    "SELECT * FROM api.follow_asof('message',%s,%s,%s,now())",
                    (start, link_type, depth),
                )
            )
            assert now == asof and now, (link_type, depth)
        assert (
            rows(
                c,
                "SELECT * FROM api.follow_asof('message',%s,NULL,3,%s)",
                (start, before),
            )
            == []
        )
    # A link whose validity ended is not followed as of now, nor listed by
    # links_valid_at, but it is followed as of a time inside its interval.
    with connect() as c:
        reply = c.execute("SELECT id FROM tl.links WHERE type='reply_to'").fetchone()[
            "id"
        ]
    r = client.patch(
        f"/v1/links/{reply}",
        json={
            "valid_from": "2019-01-01",
            "valid_from_precision": "day",
            "valid_to": "2020-01-01",
            "valid_to_precision": "day",
        },
    )
    assert r.status_code == 200, r.text
    with reader() as c:
        reached = {
            r["id"]
            for r in rows(
                c, "SELECT * FROM api.follow_asof('message',%s,NULL,3,now())", (start,)
            )
        }
        assert current("synthetic.chat", "fake-2") not in reached
        valid = {
            r["id"] for r in rows(c, "SELECT id FROM api.links_valid_at('2019-06-01Z')")
        }
        assert reply in valid
        assert reply not in {
            r["id"] for r in rows(c, "SELECT id FROM api.links_valid_at(now())")
        }


def test_purge_hooks_remove_history_and_provenance(world):
    from throughline.cli import purge

    client = world
    page_id = page(
        client, "page-a", "Synthetic lantern", cites=[key(SECRET, "secret-0")]
    )
    with connect() as c:
        link = c.execute("SELECT id FROM tl.links WHERE type='cites'").fetchone()["id"]
        secret = current(SECRET, "secret-0")
        chat_link = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,evidence) VALUES('synthetic','message',%s,'message',%s,%s) RETURNING id",
            (
                current("synthetic.chat", "fake-0"),
                current("synthetic.chat", "fake-1"),
                psycopg.types.json.Jsonb({"message_ids": [secret]}),
            ),
        ).fetchone()["id"]
    client.patch(f"/v1/links/{link}", json={"confidence": 0.9})
    client.patch(f"/v1/links/{chat_link}", json={"confidence": 0.8})
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.link_history").fetchone()["n"] == 2
        hooks = {r["name"] for r in rows(c, "SELECT name FROM tl.purge_hooks")}
    assert {"link_history", "provenance"} <= hooks
    purge(SECRET, confirm=True)
    with connect() as c:
        # History naming a purged message (as an end or as evidence) is gone;
        # the purge itself wrote none.
        assert rows(c, "SELECT * FROM tl.link_history") == []
        assert rows(c, "SELECT * FROM tl.provenance") == []
    lineage_rows = []
    with connect() as c:
        lineage.add_provenance(
            c,
            "synthetic-job",
            "message",
            page_id,
            [current("synthetic.chat", "fake-0")],
            origin="job",
        )
        lineage.add_provenance(
            c,
            "synthetic-job",
            "passage",
            3,
            [current("synthetic.chat", "fake-0")],
            origin="job",
        )
    purge(PAGES, confirm=True)
    with connect() as c:
        lineage_rows = rows(c, "SELECT derived_kind FROM tl.provenance")
    assert lineage_rows == [{"derived_kind": "passage"}]


def test_arc_5_history_not_writable_by_runtime_roles(world):
    client = world
    page(client, "page-a", "Synthetic lantern", cites=[key("synthetic.chat", "fake-0")])
    with connect() as c:
        link = c.execute("SELECT id FROM tl.links").fetchone()["id"]
    client.patch(f"/v1/links/{link}", json={"confidence": 0.5})
    a = client.post("/v1/entities", json={"kind": "topic"}).json()["id"]
    b = client.post("/v1/entities", json={"kind": "topic"}).json()["id"]
    client.post("/v1/entities/merge", json={"source": a, "target": b})
    with connect("app") as c:
        assert rows(c, "SELECT * FROM tl.link_history")
        assert rows(c, "SELECT * FROM tl.provenance")
        assert rows(c, "SELECT * FROM tl.entity_events")
    refused = [
        "INSERT INTO tl.link_history(link_id,op,old) VALUES(1,'update','{}')",
        "UPDATE tl.link_history SET op='delete'",
        "DELETE FROM tl.link_history",
        "UPDATE tl.provenance SET origin='owner'",
        "DELETE FROM tl.provenance",
        "UPDATE tl.entity_events SET moved='[]'",
        "DELETE FROM tl.entity_events",
    ]
    for statement in refused:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect("app") as c:
                c.execute(statement)
    for statement in (
        "SELECT * FROM tl.link_history",
        "INSERT INTO api.link_history(link_id,op,old) VALUES(1,'update','{}')",
        "DELETE FROM api.provenance",
    ):
        with pytest.raises(psycopg.Error):
            with connect("reader") as c:
                c.execute(statement)
    with pytest.raises(psycopg.Error):
        with connect("app") as c:
            c.execute("INSERT INTO api.provenance(derived_kind) VALUES('message')")


def test_registry_rows_and_allowlist(client):
    functions, scalar = allowlist.load()
    assert LINEAGE_FUNCTIONS <= functions and not LINEAGE_FUNCTIONS & scalar
    with connect() as c:
        registry = {
            r["name"]: r
            for r in rows(
                c,
                "SELECT * FROM tl.api_functions WHERE name=ANY(%s)",
                (list(LINEAGE_FUNCTIONS),),
            )
        }
        section = c.execute(
            "SELECT body FROM tl.catalog_sections WHERE subject='lineage'"
        ).fetchone()
        gates = {
            r["view_name"]: r["predicate"] for r in rows(c, "SELECT * FROM tl.gates")
        }
        returns = {
            r["proname"]: r["proretset"]
            for r in rows(
                c,
                "SELECT p.proname,p.proretset FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.proname=ANY(%s)",
                (list(LINEAGE_FUNCTIONS),),
            )
        }
    assert set(registry) == LINEAGE_FUNCTIONS
    assert returns == dict.fromkeys(LINEAGE_FUNCTIONS, True)
    for name, row in registry.items():
        assert (
            row["signature"].startswith(name + "(") and "RETURNS" in row["signature"]
        ), row
        validate_sql(row["example"])
        with reader() as c:
            c.execute(row["example"])
    assert set(section["body"]["functions"]) == LINEAGE_FUNCTIONS
    assert "unsourced" in section["body"]["provenance"]
    assert {"provenance", "link_history", "links"} <= set(gates)
    for name in ("relations", "relations_history", "facts", "facts_history"):
        assert gates[name] == "false"
    with reader() as c:
        catalog = c.execute("SELECT api.catalog() c").fetchone()["c"]
        assert "lineage" in catalog["sections"]
        assert LINEAGE_FUNCTIONS <= {f["name"] for f in catalog["functions"]}
        lineage_section = c.execute("SELECT api.describe('lineage') d").fetchone()["d"]
    assert lineage_section == section["body"]
    with TestClient(create_app()) as other:
        other.headers.update(client.headers)
        r = other.post(
            "/v1/sql",
            json={"statement": "SELECT count(*) n FROM api.links_asof(now())"},
        )
        assert r.status_code == 200 and r.json()["rows"] == [{"n": 0}], r.text


def test_acl_9_provenance_of_hidden_link_or_entity_is_not_shown(world):
    """A restricted asker learns nothing about the lineage of a link it cannot
    see or of an entity (identity is unrestricted-only), even when every
    source is visible to it."""
    client = world
    chat = current("synthetic.chat", "fake-0")
    secret = current(SECRET, "secret-0")
    entity = client.post("/v1/entities", json={"kind": "topic", "name": "T"}).json()[
        "id"
    ]
    with connect() as c:
        hidden_link = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id) VALUES('synthetic','message',%s,'message',%s) RETURNING id",
            (chat, secret),
        ).fetchone()["id"]
        for kind, ident in (("link", hidden_link), ("entity", entity)):
            lineage.add_provenance(
                c, "synthetic-job", kind, ident, [chat], origin="job"
            )
    access.register_asker("guest")
    access.grant("guest", channel="synthetic.chat")
    with as_guest() as c:
        assert rows(c, "SELECT * FROM api.provenance") == []
        for kind, ident in (("link", hidden_link), ("entity", entity)):
            assert rows(c, f"SELECT id FROM api.sources('{kind}',{ident})") == []
    access.grant("guest", channel=SECRET)
    with as_guest() as c:
        assert [
            (r["derived_kind"], r["derived_id"])
            for r in rows(c, "SELECT * FROM api.provenance")
        ] == [("link", hidden_link)]
        assert [
            r["id"]
            for r in rows(c, f"SELECT id FROM api.sources('link',{hidden_link})")
        ] == [chat]
        assert rows(c, f"SELECT id FROM api.sources('entity',{entity})") == []
    with reader() as c:
        assert len(rows(c, "SELECT * FROM api.provenance")) == 2
        assert [
            r["id"] for r in rows(c, f"SELECT id FROM api.sources('entity',{entity})")
        ] == [chat]


def test_brn_10_every_link_writer_pins_versions_and_they_never_move(world):
    chat0 = current("synthetic.chat", "fake-0")
    chat1 = current("synthetic.chat", "fake-1")
    with connect() as c:
        # A writer that does not go through ingest.add_link (location rules).
        link = c.execute(
            "INSERT INTO tl.links(type,from_kind,from_id,to_kind,to_id,method) VALUES('synthetic','message',%s,'message',%s,'rule') RETURNING *",
            (chat0, chat1),
        ).fetchone()
    assert (link["from_version_id"], link["to_version_id"]) == (chat0, chat1)
    for statement in (
        "UPDATE tl.links SET from_version_id=NULL WHERE id=%s",
        "UPDATE tl.links SET to_version_id=to_version_id+1 WHERE id=%s",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            with connect() as c:
                c.execute(statement, (link["id"],))
    with connect() as c:
        c.execute("UPDATE tl.links SET to_id=%s WHERE id=%s", (chat0, link["id"]))
        moved = c.execute(
            "SELECT to_id,to_version_id FROM tl.links WHERE id=%s", (link["id"],)
        ).fetchone()
    assert moved == {"to_id": chat0, "to_version_id": chat1}
