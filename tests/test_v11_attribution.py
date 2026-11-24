"""Attribution on every row (migration 0047): suspected
accounts excluded until confirmed, the migration's clean-up, attribution in
recall, result sets and packets, identity_quality and time_precision
filterable, speaker annotations, restricted inference over person counts and
ranks, one identity system, and the identifier status route with the write
contract. Synthetic data only."""

import json
import uuid
from datetime import timedelta

import pytest
from test_v11_people import (
    CH,
    SECRET,
    T0,
    World,
    headers,
    ok,
    owner_me,
    owner_rows,
    q,
    restricted_asker,
    run,
)

from throughline.db import connect

ATTRIBUTION = {
    "is_me",
    "direction",
    "attribution_status",
    "identity_quality",
    "time_precision",
}


def set_status(client, identifier_id, status, reason=None, h=None):
    return client.post(
        f"/v1/identity/identifiers/{identifier_id}/status",
        json=dict(status=status, reason=reason),
        headers=h or {},
    )


def m_ids(records):
    return {r["message_id"] for r in records if r["record_id"].startswith("m:")}


def alice_world(client):
    w = World(client)
    w.alice = w.person("synthetic-alice")
    w.load(
        w.say("a1", "dm", "alice-acct", 0, conversation_kind="dm"),
        owner_me(w, "o1", "dm", 60, conversation_kind="dm"),
        w.say("a2", "dm", "alice-acct", 120, conversation_kind="dm"),
    )
    w.attach("alice-acct", w.alice)
    w.index()
    return w


def test_suspected_link_excluded_until_confirmed(client):
    w = alice_world(client)
    ident = w.identifier("alice-acct")
    alice_messages = {w.id("a1"), w.id("a2")}

    def attributed():
        return {
            r["id"]
            for r in q("SELECT id FROM api.messages WHERE person_id=%s", (w.alice,))
        }

    def timeline():
        return {
            r["message_id"]
            for r in run("person_timeline", person_id=w.alice)
            if r["side"] == "from"
        }

    def legacy():
        return {
            r["id"] for r in q("SELECT id FROM api.person_messages(%s)", (w.alice,))
        }

    def recalled():
        body = ok(
            client.post(
                "/v1/recall",
                json=dict(
                    query="", scope=dict(persons=[w.alice]), exhaustive=True, k=50
                ),
            )
        )
        return m_ids(body["records"])

    assert attributed() == timeline() == legacy() == recalled() == alice_messages
    body = ok(set_status(client, ident, "suspected", "synthetic: shared device"))
    assert body["previous_status"] == "confirmed" and body["status"] == "suspected"
    assert body["attributed_messages"] == 0
    assert attributed() == timeline() == legacy() == recalled() == set()
    assert run("person_brief", person_id=w.alice)[0]["message_count"] == 0
    statuses = {
        r["message_id"]: r["attribution_status"]
        for r in q(
            "SELECT * FROM api.attribution(%s::bigint[])", (sorted(alice_messages),)
        )
    }
    assert statuses == dict.fromkeys(alice_messages, "suspected")
    suspected = q(
        "SELECT * FROM api.suspected_attributions(%s::bigint[])",
        (sorted(alice_messages),),
    )
    assert {(r["message_id"], r["suspected_person_id"]) for r in suspected} == {
        (m, w.alice) for m in alice_messages
    }
    # A new delivery from the suspected account carries no person either.
    w.load(w.say("a3", "dm", "alice-acct", 180, conversation_kind="dm"))
    assert (
        owner_rows("SELECT person_id FROM tl.messages WHERE id=%s", (w.id("a3"),))[0][
            "person_id"
        ]
        is None
    )
    alice_messages.add(w.id("a3"))
    body = ok(set_status(client, ident, "confirmed", "synthetic: verified"))
    assert body["attributed_messages"] == 3
    assert attributed() == timeline() == legacy() == recalled() == alice_messages
    ok(set_status(client, ident, "suspected"))
    assert attributed() == recalled() == set()
    events = owner_rows(
        "SELECT asserted_by,detail FROM tl.identity_events WHERE operation='identifier_status' ORDER BY id"
    )
    assert [(e["asserted_by"], e["detail"]["status"]) for e in events] == [
        ("owner", "suspected"),
        ("owner", "confirmed"),
        ("owner", "suspected"),
    ]
    assert events[0]["detail"]["reason"] == "synthetic: shared device"
    # The acting token is recorded with the event.
    assert all(e["detail"]["token_id"] for e in events)
    # Owner tokens only; validation before any change.
    agent = headers("agent")
    assert set_status(client, ident, "confirmed", h=agent).status_code == 403
    assert set_status(client, ident, "maybe").status_code == 422
    assert set_status(client, 999999, "confirmed").status_code == 404


def test_migration_clears_unconfirmed_attribution(client):
    w = alice_world(client)
    ident = w.identifier("alice-acct")
    with connect() as c:
        # Plant the pre-0047 state: a suspected identifier whose messages
        # still carry its person (triggers off for this transaction only).
        c.execute("SET LOCAL session_replication_role=replica")
        c.execute("UPDATE tl.identifiers SET status='suspected' WHERE id=%s", (ident,))
        c.execute(
            "UPDATE tl.messages SET person_id=%s WHERE author_identifier_id=%s",
            (w.alice, ident),
        )
    assert (
        len(owner_rows("SELECT id FROM tl.messages WHERE person_id=%s", (w.alice,)))
        == 2
    )
    with connect() as c:
        cleared = c.execute(
            "SELECT tl.clear_unconfirmed_attribution('synthetic-test') AS n"
        ).fetchone()["n"]
    assert cleared == 2
    assert owner_rows("SELECT id FROM tl.messages WHERE person_id=%s", (w.alice,)) == []
    (event,) = owner_rows(
        "SELECT asserted_by,detail FROM tl.identity_events WHERE operation='clear_unconfirmed_attribution'"
    )
    assert event["asserted_by"] == "synthetic-test"
    assert event["detail"]["messages_cleared"] == 2
    # The guard holds without the clean-up: a direct write is refused silently.
    with connect() as c:
        c.execute(
            "UPDATE tl.messages SET person_id=%s WHERE author_identifier_id=%s",
            (w.alice, ident),
        )
    assert owner_rows("SELECT id FROM tl.messages WHERE person_id=%s", (w.alice,)) == []
    # The migration itself ran the clean-up (its function is in place).
    assert owner_rows(
        "SELECT 1 FROM pg_proc WHERE proname='clear_unconfirmed_attribution'"
    )


def test_p3_attribution_on_every_record(client):
    w = alice_world(client)
    body = ok(
        client.post(
            "/v1/recall",
            json=dict(query="", exhaustive=True, k=50, save=True),
        )
    )
    records = {
        r["message_id"]: r for r in body["records"] if r["record_id"].startswith("m:")
    }
    assert set(records) == {w.id("a1"), w.id("o1"), w.id("a2")}
    for record in body["records"]:
        # attribution, then images (migration 0063), close the record.
        assert list(record)[-2:] == ["attribution", "images"]
    mine = records[w.id("o1")]["attribution"]
    assert set(mine) == ATTRIBUTION
    assert mine["is_me"] is True and mine["direction"] == "outgoing"
    assert mine["attribution_status"] == "self_direction"
    theirs = records[w.id("a1")]["attribution"]
    assert theirs["is_me"] is False and theirs["direction"] == "incoming"
    assert theirs["attribution_status"] == "confirmed"
    assert (
        theirs["identity_quality"] == "identifier"
        and theirs["time_precision"] == "exact"
    )
    handle = body["result_set"]
    assert handle
    read = ok(client.get(f"/v1/sets/{handle}"))
    from_set = {r["message_id"]: r["attribution"] for r in read["records"]}
    assert from_set == {k: v["attribution"] for k, v in records.items()}
    packet = ok(client.post("/v1/packets", json={"handle": handle}))
    in_packet = {
        p["revision_id"]["message_id"]: p["attribution"] for p in packet["records"]
    }
    assert in_packet == from_set


def test_p8_identity_quality_and_time_precision_filterable(client):
    w = World(client)
    w.load(
        w.say("n1", "t", "native-acct", 0, identity_quality="native"),
        w.say(
            "f1",
            "t",
            "fp-acct",
            60,
            identity_quality="fingerprint",
            time_precision="day",
        ),
        w.say("u1", "t", "plain-acct", 120),
    )
    rows = q(
        "SELECT message_id,identity_quality,time_precision FROM api.appearances WHERE identity_quality='fingerprint' OR time_precision='day'"
    )
    assert [
        (r["message_id"], r["identity_quality"], r["time_precision"]) for r in rows
    ] == [(w.id("f1"), "fingerprint", "day")]
    statement = (
        "SELECT count(*) AS n FROM api.appearances WHERE identity_quality='identifier'"
    )
    assert (
        ok(client.post("/v1/sql", json={"statement": statement}))["rows"][0]["n"] == 1
    )
    created = ok(
        client.post(
            "/v1/sets",
            json=dict(
                records=[{"record_id": f"m:{w.id(k)}"} for k in ("n1", "f1", "u1")]
            ),
        )
    )
    handle = created["handle"]

    def filtered(**params):
        made = ok(
            client.post(
                "/v1/sets/transform",
                json=dict(op="filter", handles=[handle], params=params),
            )
        )
        read = ok(client.get(f"/v1/sets/{made['handle']}"))
        return {r["message_id"] for r in read["records"]}

    assert filtered(identity_quality=["native"]) == {w.id("n1")}
    assert filtered(time_precision=["day"]) == {w.id("f1")}
    assert filtered(identity_quality=["native", "identifier"]) == {
        w.id("n1"),
        w.id("u1"),
    }
    assert filtered(attribution_status=["unknown"]) == {
        w.id("n1"),
        w.id("f1"),
        w.id("u1"),
    }
    assert filtered(attribution_status=["confirmed"]) == set()
    bad = client.post(
        "/v1/sets/transform",
        json=dict(op="filter", handles=[handle], params={"identity_quality": "native"}),
    )
    assert bad.status_code == 400


def test_speaker_annotation_is_annotated_not_confirmed(client):
    w = World(client)
    w.alice = w.person("synthetic-alice")
    w.load(w.say("s1", "rec", "speaker-1", 0))
    with connect() as c:
        c.execute(
            "INSERT INTO tl.annotations(target_channel_id,target_external_id,attribute,value,method,origin) SELECT id,'syn-s1','speaker',%s,'owner','owner' FROM tl.channels WHERE name=%s",
            (json.dumps({"person_id": w.alice}), CH),
        )
    (row,) = q("SELECT * FROM api.appearances WHERE message_id=%s", (w.id("s1"),))
    assert row["attribution_status"] == "annotated"
    assert row["annotated_person_id"] == w.alice
    assert row["person_id"] is None and row["party_id"] is None
    assert row["direction"] == "unknown"
    # Not a confirmed attribution: no person function counts it.
    assert run("person_timeline", person_id=w.alice) == []
    asker = restricted_asker()
    (hidden,) = q(
        "SELECT * FROM api.appearances WHERE message_id=%s", (w.id("s1"),), asker=asker
    )
    assert hidden["annotated_person_id"] is None
    assert hidden["attribution_status"] == "unknown"


def test_restricted_inference_person_counts_and_ranks(client):
    w = World(client)
    w.alice = w.person("synthetic-alice")
    w.bob = w.person("synthetic-bob")
    now = T0 + timedelta(days=1)
    w.load(
        # Granted channel: alice twice, bob once.
        w.say("a1", "dm-alice", "alice-acct", 0, conversation_kind="dm"),
        owner_me(w, "o1", "dm-alice", 60, conversation_kind="dm"),
        w.say(
            "a2",
            "dm-alice",
            "alice-acct",
            120,
            "Synthetic can you water it?",
            conversation_kind="dm",
        ),
        w.say("b1", "dm-bob", "bob-acct", 1000, conversation_kind="dm"),
        owner_me(w, "o2", "dm-bob", 1060, conversation_kind="dm"),
        # Hidden channel: much more with bob.
        *[
            item
            for i in range(4)
            for item in (
                w.say(
                    f"hb{i}",
                    "secret-bob",
                    "bob-acct",
                    5000 + 120 * i,
                    channel=SECRET,
                    conversation_kind="dm",
                ),
                owner_me(
                    w,
                    f"ho{i}",
                    "secret-bob",
                    5060 + 120 * i,
                    channel=SECRET,
                    conversation_kind="dm",
                ),
            )
        ],
        w.say(
            "hq",
            "secret-alice",
            "alice-acct",
            9000,
            "Synthetic secret question?",
            channel=SECRET,
        ),
        owner_me(w, "hq0", "secret-alice", 8000, channel=SECRET),
    )
    w.attach("alice-acct", w.alice)
    w.attach("bob-acct", w.bob)
    w.index()
    assert now
    asker = restricted_asker()
    full = {r["other_person_id"]: (r["strength"], r["rank"]) for r in run("circle")}
    seen = {
        r["other_person_id"]: (r["strength"], r["rank"])
        for r in run("circle", asker=asker)
    }
    assert full[w.bob][1] == 1 and full[w.bob][0] > full[w.alice][0]
    assert seen == {w.alice: (3, 1), w.bob: (2, 2)}
    rel = {
        r["other_person_id"]: float(r["relative_strength"])
        for r in run("circle", asker=asker)
    }
    assert rel == {w.alice: 0.6, w.bob: 0.4}
    rhythm = run("rhythm", asker=asker, person_id=w.bob, tz="UTC")
    assert sum(r["messages"] for r in rhythm if r["dimension"] == "month") == 1
    inter = run("interactions", asker=asker, person_id=w.bob, grain="month")
    assert [(r["messages_from"], r["messages_to"]) for r in inter] == [(1, 1)]
    arc = run("person_arc", asker=asker, person_id=w.bob, grain="year")
    assert [(r["messages_from"], r["exchanges"]) for r in arc] == [(1, 1)]
    brief = run("person_brief", asker=asker, person_id=w.bob)[0]
    assert brief["message_count"] == 1 and brief["exchange_count"] == 1
    assert brief["handle"] is None and brief["identifiers"] is None
    assert [c["channel"] for c in brief["coverage"]] == [CH]
    full_brief = run("person_brief", person_id=w.bob)[0]
    assert full_brief["message_count"] == 5
    loops = dict(older_than="1 second", horizon_days=100000)
    loops_full = {r["message_id"] for r in run("open_loops", **loops)}
    loops_seen = {r["message_id"] for r in run("open_loops", asker=asker, **loops)}
    assert w.id("hq") in loops_full and w.id("hq") not in loops_seen
    assert loops_seen == {w.id("a2")}
    (status,) = run("exchange_index_status", asker=asker)
    assert status["indexed_threads"] == 2
    # Division-by-zero probes through validated SQL raise nothing.
    agent = headers("agent", [asker])
    hidden = w.id("hb0")
    for statement in (
        f"SELECT * FROM api.appearances WHERE 1/(message_id-{hidden})=1",
        f"SELECT * FROM api.person_timeline({w.bob}) WHERE 1/(message_id-{hidden})=1",
        f"SELECT * FROM api.between(ARRAY[{w.owner},{w.bob}]::bigint[]) WHERE 1/(message_id-{hidden})=1",
        f"SELECT * FROM api.circle() WHERE 1/(exchanges-{full[w.bob][0]})=1",
        f"SELECT * FROM api.attribution(ARRAY[{hidden}]::bigint[]) WHERE 1/(message_id-{hidden})=1",
    ):
        response = client.post("/v1/sql", json={"statement": statement}, headers=agent)
        assert response.status_code == 200, (statement, response.text)
    timeline = ok(client.get(f"/v1/people/{w.bob}/timeline", headers=agent))
    assert {r["message_id"] for r in timeline["rows"]} == {w.id("b1"), w.id("o2")}


def test_single_identity_system(client):
    tables = {
        r["tablename"]
        for r in owner_rows("SELECT tablename FROM pg_tables WHERE schemaname='tl'")
    }
    identity_like = {
        t
        for t in tables
        if any(
            k in t
            for k in (
                "person",
                "people",
                "identit",
                "identifier",
                "contact",
                "crosswalk",
                "alias_people",
            )
        )
    }
    assert identity_like <= {
        "people",
        "person_names",
        "identifiers",
        "identifier_schemes",
        "identity_events",
    }, identity_like
    # The people read model keeps no person registry of its own: exchanges
    # are keyed by message source keys, never by person.
    columns = {
        r["column_name"]
        for r in owner_rows(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='tl' AND table_name IN ('exchanges','exchange_state')"
        )
    }
    assert not {c for c in columns if "person" in c or "handle" in c}


def test_identifier_status_route_idempotent_with_receipt(client):
    pytest.importorskip("throughline.writes")
    w = alice_world(client)
    ident = w.identifier("alice-acct")
    key = {"Idempotency-Key": "synthetic-" + uuid.uuid4().hex}
    first = ok(
        client.post(
            f"/v1/identity/identifiers/{ident}/status",
            json=dict(status="suspected", reason="synthetic"),
            headers=key,
        )
    )
    receipt = first["receipt"]
    assert receipt["kind"] == "identifier" and receipt["status"] == "suspected"
    assert receipt["replayed"] is False and receipt["stored_hash"]
    again = ok(
        client.post(
            f"/v1/identity/identifiers/{ident}/status",
            json=dict(status="suspected", reason="synthetic"),
            headers=key,
        )
    )
    assert again["receipt"]["replayed"] is True
    assert again["receipt"]["stored_hash"] == receipt["stored_hash"]
    events = owner_rows(
        "SELECT id FROM tl.identity_events WHERE operation='identifier_status'"
    )
    assert len(events) == 1
    reused = client.post(
        f"/v1/identity/identifiers/{ident}/status",
        json=dict(status="confirmed"),
        headers=key,
    )
    assert reused.status_code == 409
