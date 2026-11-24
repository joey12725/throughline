"""The write contract (migration 0048, throughline.writes):
idempotency keys scoped to the token, server-side readback receipts,
compare-and-set (expected_version, expected_hash), per-item results for link
and entity batches with the whole-batch authorization rule, and purge.
Synthetic data only."""

import threading

import pytest
from conftest import ingest, message

from throughline.db import connect, create_token

KEY = "synthetic-key-0001"
OTHER = "other.chat"


def headers(kind="owner", plugin=None, key=None):
    token = create_token("synthetic-" + kind, kind, plugin)["token"]
    out = {"Authorization": "Bearer " + token}
    if key:
        out["Idempotency-Key"] = key
    return out


def keyed(key=KEY, base=None):
    return dict(base or {}, **{"Idempotency-Key": key})


def current(external_id, channel="synthetic.chat"):
    with connect() as c:
        return c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
            (channel, external_id),
        ).fetchone()["id"]


def count(table, where="true", params=()):
    with connect() as c:
        return c.execute(
            f"SELECT count(*) AS n FROM tl.{table} WHERE {where}", params
        ).fetchone()["n"]


def sql(statement, params=()):
    with connect() as c:
        return c.execute(statement, params).fetchall()


def key(external_id, channel="synthetic.chat"):
    return {"channel": channel, "external_id": external_id}


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()


@pytest.fixture()
def world(client, channel):
    r = ingest(client, [message(i) for i in range(6)])
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    return client


@pytest.fixture()
def plugin(world):
    ok(world.post("/v1/plugins/synthetic/manifest", json={"link_types": []}))
    return headers("plugin", "synthetic")


def foreign_message(client):
    ok(client.post("/v1/channels", json={"name": OTHER, "plugin": "other"}))
    r = client.post(
        "/v1/ingest/messages",
        json={
            "plugin": "other",
            "messages": [
                message(
                    0,
                    channel=OTHER,
                    external_id="other-0",
                    thread={"external_id": "other"},
                    fields={},
                )
            ],
        },
    )
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    return current("other-0", OTHER)


# --- replay ------------------------------------------------------------------------


def test_write_replay_same_key_noop_same_receipt(world):
    client = world
    m1 = current("fake-1")
    cases = [
        (
            "/v1/links",
            {"type": "about", "from": key("fake-0"), "to": key("fake-1")},
            "links",
        ),
        (
            "/v1/annotations",
            {"message_id": m1, "attribute": "ignore", "value": {"ignored": True}},
            "annotations",
        ),
        (
            "/v1/provenance",
            {"derived": key("fake-2"), "sources": [key("fake-3")]},
            "provenance",
        ),
        (
            "/v1/entities",
            {"kind": "synthetic_work", "name": "Synthetic Work"},
            "entities",
        ),
    ]
    for index, (path, body, table) in enumerate(cases):
        h = keyed(f"synthetic-replay-{index:04d}")
        first = ok(client.post(path, json=body, headers=h))
        rows = count(table)
        audit_rows = count("audit_log")
        again = ok(client.post(path, json=body, headers=h))
        assert count(table) == rows, path
        assert count("write_receipts") == index + 1
        assert first["receipt"]["replayed"] is False, path
        assert again["receipt"]["replayed"] is True, path
        for field in ("kind", "id", "record_id", "version", "stored_hash", "counts"):
            assert again["receipt"][field] == first["receipt"][field], (path, field)
        # The replay is audited like any request, and wrote nothing else.
        assert count("audit_log") == audit_rows + 1
    # An entity without a key is a new row each time; with the key it is not.
    body = {"kind": "synthetic_work", "name": "Synthetic Work"}
    ok(client.post("/v1/entities", json=body))
    assert count("entities", "kind='synthetic_work'") == 2


def test_key_reuse_different_body_409(world):
    client = world
    ok(client.post("/v1/entities", json={"kind": "synthetic_a"}, headers=keyed()))
    r = client.post("/v1/entities", json={"kind": "synthetic_b"}, headers=keyed())
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "idempotency key reused with a different request"
    assert count("entities", "kind='synthetic_b'") == 0
    # The same key on another route is a different request as well.
    r = client.post(
        "/v1/links",
        json={"type": "about", "from": key("fake-0"), "to": key("fake-1")},
        headers=keyed(),
    )
    assert r.status_code == 409
    assert count("links") == 0
    # A key outside 8..200 characters is refused before any work.
    short = client.post("/v1/entities", json={"kind": "x"}, headers=keyed("short"))
    assert short.status_code == 400
    assert count("entities", "kind='x'") == 0


def test_concurrent_duplicate_key_single_write(world):
    client = world
    body = {"kind": "synthetic_race", "name": "Synthetic race"}
    results, errors = [], []
    start = threading.Barrier(4)

    def post():
        try:
            start.wait()
            results.append(client.post("/v1/entities", json=body, headers=keyed()))
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=post) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert [r.status_code for r in results] == [200] * 4, [r.text for r in results]
    assert count("entities", "kind='synthetic_race'") == 1
    receipts = [r.json()["receipt"] for r in results]
    assert len({r["id"] for r in receipts}) == 1
    assert sorted(r["replayed"] for r in receipts) == [False, True, True, True]


def test_key_scoped_to_token(world):
    client = world
    body = {"kind": "synthetic_scoped"}
    first = ok(client.post("/v1/entities", json=body, headers=keyed()))
    other = ok(client.post("/v1/entities", json=body, headers=headers(key=KEY)))
    assert other["receipt"]["replayed"] is False
    assert other["id"] != first["id"]
    assert count("entities", "kind='synthetic_scoped'") == 2


def test_expired_keys_pruned_and_reusable(world):
    client = world
    body = {"kind": "synthetic_expiring"}
    first = ok(client.post("/v1/entities", json=body, headers=keyed()))
    with connect() as c:
        c.execute(
            "UPDATE tl.write_receipts SET expires_at=now()-interval '1 second' WHERE idempotency_key=%s",
            (KEY,),
        )
    # An expired key is never replayed: the write happens again.
    again = ok(client.post("/v1/entities", json=body, headers=keyed()))
    assert again["receipt"]["replayed"] is False and again["id"] != first["id"]
    with connect() as c:
        c.execute(
            "UPDATE tl.write_receipts SET expires_at=now()-interval '1 second' WHERE idempotency_key=%s",
            (KEY,),
        )
    # Storing another key prunes the expired row.
    ok(client.post("/v1/entities", json=body, headers=keyed("synthetic-key-0002")))
    assert count("write_receipts", "idempotency_key=%s", (KEY,)) == 0
    assert count("write_receipts", "idempotency_key='synthetic-key-0002'") == 1


def test_required_idempotency_for_agents_when_configured(world, monkeypatch):
    client = world
    agent = headers("agent")
    body = {"kind": "synthetic_work", "name": "Agent work"}
    # Off by default.
    ok(client.post("/v1/entities", json=body, headers=agent))
    monkeypatch.setenv("THROUGHLINE_REQUIRE_IDEMPOTENCY", "true")
    r = client.post("/v1/entities", json=body, headers=agent)
    assert r.status_code == 400 and "Idempotency-Key" in r.json()["detail"]
    ok(client.post("/v1/entities", json=body, headers=dict(agent, **keyed())))
    r = client.post(
        "/v1/links",
        json={"type": "about", "from": key("fake-0"), "to": key("fake-1")},
        headers=agent,
    )
    assert r.status_code == 400
    # Owner tokens are not affected.
    ok(
        client.post(
            "/v1/links",
            json={"type": "about", "from": key("fake-0"), "to": key("fake-1")},
        )
    )


# --- receipts ----------------------------------------------------------------------


def test_receipt_hash_version_counts_match_a_later_read(world):
    client = world
    r = ok(
        ingest(
            client,
            [message(0, content="Synthetic orchard message 0, edited")],
            receipts=True,
        )
    )
    (receipt,) = r["receipts"]
    m0 = current("fake-0")
    assert receipt["kind"] == "message" and receipt["id"] == m0
    assert receipt["record_id"] == f"m:{m0}" and receipt["index"] == 0
    assert receipt["version"] == 2 and receipt["status"] == "current"
    (later,) = sql("SELECT tl.stored_hash('message',%s) AS h", (m0,))
    assert receipt["stored_hash"] == later["h"]
    assert receipt["counts"]["links_from"] == 0
    link = ok(
        client.post(
            "/v1/links",
            json={"type": "about", "from": key("fake-0"), "to": key("fake-1")},
        )
    )
    assert link["receipt"]["outcome"] == "created"
    (row,) = sql("SELECT tl.stored_hash('link',%s) AS h", (link["id"],))
    assert link["receipt"]["stored_hash"] == row["h"]
    ok(
        client.post(
            "/v1/provenance",
            json={"derived": key("fake-0"), "sources": [key("fake-2")]},
        )
    )
    again = ok(
        ingest(
            client,
            [message(0, content="Synthetic orchard message 0, edited")],
            receipts=True,
        )
    )
    (receipt,) = again["receipts"]
    assert again["unchanged"] == 1
    assert receipt["version"] == 2
    assert receipt["counts"] == {
        "links_from": 1,
        "links_to": 0,
        "provenance_citing": 1,
        "passages": 0,
        "timeline": 0,
        "sources": 0,
    }
    # The stored hash changes with the stored row.
    patched = ok(client.patch(f"/v1/links/{link['id']}", json={"confidence": 0.5}))
    assert patched["receipt"]["stored_hash"] != link["receipt"]["stored_hash"]
    (row,) = sql("SELECT tl.stored_hash('link',%s) AS h", (link["id"],))
    assert patched["receipt"]["stored_hash"] == row["h"]
    # Receipts are at most 1,000 messages per request.
    many = ingest(client, [message(i) for i in range(1001)], receipts=True)
    assert many.status_code == 400


# --- compare-and-set ------------------------------------------------------------------


def test_expected_version_conflict_per_ingest_item(world):
    client = world
    r = ok(
        ingest(
            client,
            [
                message(10, expected_version=0),
                message(0, expected_version=0, content="Synthetic changed 0"),
                message(1, expected_version=1, content="Synthetic changed 1"),
                message(2, expected_version=5, content="Synthetic changed 2"),
            ],
        )
    )
    assert r["inserted"] == 1 and r["new_versions"] == 1
    conflicts = {x["index"]: x for x in r["rejected"]}
    assert set(conflicts) == {1, 3}
    assert conflicts[1]["reason"] == "version_conflict"
    assert conflicts[1]["current_version"] == 1
    assert conflicts[3]["current_version"] == 1
    assert count("messages", "external_id='fake-0'") == 1
    assert count("messages", "external_id='fake-1'") == 2
    # expected_version is outside the payload hash: a replay is unchanged.
    again = ok(ingest(client, [message(10)]))
    assert again["unchanged"] == 1


def test_expected_hash_conflict_link_patch_claim_supersede_annotation(world):
    client = world
    link = ok(
        client.post(
            "/v1/links",
            json={"type": "about", "from": key("fake-0"), "to": key("fake-1")},
        )
    )
    seen = link["receipt"]["stored_hash"]
    ok(client.patch(f"/v1/links/{link['id']}", json={"confidence": 0.9}))
    stale = client.patch(
        f"/v1/links/{link['id']}", json={"confidence": 0.1, "expected_hash": seen}
    )
    assert stale.status_code == 409, stale.text
    detail = stale.json()["detail"]
    assert detail["reason"] == "hash_conflict"
    (row,) = sql(
        "SELECT tl.stored_hash('link',%s) AS h, confidence FROM tl.links WHERE id=%s",
        (link["id"], link["id"]),
    )
    assert detail["current_hash"] == row["h"] and row["confidence"] == pytest.approx(
        0.9
    )
    fresh = ok(
        client.patch(
            f"/v1/links/{link['id']}",
            json={"confidence": 0.2, "expected_hash": row["h"]},
        )
    )
    stale = client.post(
        f"/v1/links/{link['id']}/supersede",
        json={"type": "mentions", "expected_hash": row["h"]},
    )
    assert stale.status_code == 409
    ok(
        client.post(
            f"/v1/links/{link['id']}/supersede",
            json={"type": "mentions", "expected_hash": fresh["receipt"]["stored_hash"]},
        )
    )
    # Annotations: supersede and retract.
    note = ok(
        client.post(
            "/v1/annotations",
            json={
                "message_id": current("fake-2"),
                "attribute": "ignore",
                "value": {"ignored": True},
            },
        )
    )
    seen = note["receipt"]["stored_hash"]
    replaced = ok(
        client.post(
            f"/v1/annotations/{note['annotation']['id']}/supersede",
            json={"value": {"ignored": False}, "expected_hash": seen},
        )
    )
    r = client.post(
        f"/v1/annotations/{replaced['annotation']['id']}/retract",
        json={"expected_hash": seen},
    )
    assert r.status_code == 409
    ok(
        client.post(
            f"/v1/annotations/{replaced['annotation']['id']}/retract",
            json={"expected_hash": replaced["receipt"]["stored_hash"]},
        )
    )
    # Entities: merge compares the source entity.
    a = ok(client.post("/v1/entities", json={"kind": "synthetic_place", "name": "A"}))
    b = ok(client.post("/v1/entities", json={"kind": "synthetic_place", "name": "B"}))
    r = client.post(
        "/v1/entities/merge",
        json={"source": a["id"], "target": b["id"], "expected_hash": "0" * 64},
    )
    assert r.status_code == 409
    merged = ok(
        client.post(
            "/v1/entities/merge",
            json={
                "source": a["id"],
                "target": b["id"],
                "expected_hash": a["receipt"]["stored_hash"],
            },
        )
    )
    assert merged["receipt"]["status"] == "merged"


# --- link and entity batches ------------------------------------------------------------


def test_links_batch_per_item_results(world, plugin):
    client = world
    ok(
        client.post(
            "/v1/links",
            json={"type": "about", "from": key("fake-0"), "to": key("fake-5")},
            headers=plugin,
        )
    )
    batch = [
        {"type": "about", "from": key("fake-0"), "to": key("fake-1")},
        {"type": "about", "from": key("fake-0"), "to": key("fake-5")},
        {"type": "replies_to", "from": key("fake-1"), "to": key("fake-0")},
        {"type": "synthetic_waved", "from": key("fake-2"), "to": key("fake-3")},
        {"type": "about", "from": key("fake-missing"), "to": key("fake-1")},
        {"type": "mentions", "from": key("fake-3"), "to": key("fake-4")},
    ]
    r = ok(client.post("/v1/links/batch", json={"links": batch}, headers=plugin))
    statuses = [x["status"] for x in r["results"]]
    assert statuses == [
        "created",
        "existing",
        "folded",
        "candidate",
        "error",
        "created",
    ]
    assert [x["index"] for x in r["results"]] == list(range(6))
    assert r["results"][4]["reason"] == "link message does not exist"
    assert r["results"][4]["link_id"] is None and r["results"][4]["receipt"] is None
    for item in r["results"]:
        if item["link_id"] is not None:
            assert item["receipt"]["id"] == item["link_id"]
    assert r["counts"] == {
        "created": 2,
        "existing": 1,
        "folded": 1,
        "candidate": 1,
        "error": 1,
    }
    assert count("links") == 5
    # A vocabulary refusal (rule method, unregistered type) is per item.
    owner = ok(
        client.post(
            "/v1/links/batch",
            json={
                "links": [
                    {
                        "type": "synthetic_guessed",
                        "from": key("fake-4"),
                        "to": key("fake-5"),
                        "method": "rule",
                        "job": "synthetic-job",
                        "job_version": "1",
                    },
                    {"type": "about", "from": key("fake-4"), "to": key("fake-5")},
                ]
            },
        )
    )
    assert [x["status"] for x in owner["results"]] == ["refused", "created"]
    assert "unregistered link type" in owner["results"][0]["reason"]
    assert count("links", "type='synthetic_guessed'") == 0
    assert client.post("/v1/links/batch", json={"links": []}).status_code == 400
    too_many = [batch[0]] * 1001
    assert client.post("/v1/links/batch", json={"links": too_many}).status_code == 400


def test_links_batch_foreign_channel_whole_batch_403_nothing_written(world, plugin):
    client = world
    foreign = foreign_message(client)
    for bad in (
        {"type": "about", "from": key("fake-0"), "to": key("other-0", OTHER)},
        {
            "type": "about",
            "from": key("fake-0"),
            "to": {"kind": "message", "id": foreign},
        },
        {
            "type": "about",
            "from": key("fake-0"),
            "to": {"kind": "message", "id": 987654},
        },
    ):
        r = client.post(
            "/v1/links/batch",
            json={
                "links": [
                    {"type": "about", "from": key("fake-0"), "to": key("fake-1")},
                    bad,
                ]
            },
            headers=plugin,
        )
        assert r.status_code == 403, r.text
        # The same answer for a foreign and an unknown message.
        assert (
            r.json()["detail"] == "a link of this batch is not allowed for this token"
        )
    assert count("links") == 0


def test_ingest_links_vocab_refusal_per_item_messages_kept(world):
    client = world
    r = ok(
        ingest(
            client,
            [message(20), message(21)],
            links=[
                {
                    "type": "synthetic_guessed",
                    "from": key("fake-20"),
                    "to": key("fake-21"),
                    "method": "rule",
                    "job": "synthetic-job",
                    "job_version": "1",
                },
                {"type": "about", "from": key("fake-20"), "to": key("fake-21")},
            ],
        )
    )
    assert r["inserted"] == 2
    assert [x["status"] for x in r["link_results"]] == ["refused", "created"]
    assert count("messages", "external_id IN ('fake-20','fake-21')") == 2
    assert count("links") == 1


def test_ingest_link_auth_failure_still_whole_batch_403(world, plugin):
    client = world
    foreign = foreign_message(client)
    r = client.post(
        "/v1/ingest/messages",
        json={
            "plugin": "synthetic",
            "messages": [message(30)],
            "links": [
                {"type": "about", "from": key("fake-0"), "to": key("fake-1")},
                {
                    "type": "about",
                    "from": key("fake-30"),
                    "to": {"kind": "message", "id": foreign},
                },
            ],
        },
        headers=plugin,
    )
    assert r.status_code == 403, r.text
    assert count("messages", "external_id='fake-30'") == 0
    assert count("links") == 0


def test_entities_batch_per_item(world):
    client = world
    existing = ok(
        client.post(
            "/v1/entities", json={"kind": "synthetic_work", "external_ref": "work-1"}
        )
    )
    r = ok(
        client.post(
            "/v1/entities/batch",
            json={
                "entities": [
                    {"kind": "synthetic_work", "external_ref": "work-2", "name": "Two"},
                    {"kind": "synthetic_work", "external_ref": "work-1"},
                    {"name": "No kind"},
                    "not an object",
                    {"kind": "synthetic_place", "name": "Somewhere"},
                ]
            },
        )
    )
    statuses = [x["status"] for x in r["results"]]
    assert statuses == ["created", "existing", "error", "error", "created"]
    assert r["results"][1]["entity_id"] == existing["id"]
    assert r["results"][2]["reason"] == "kind is required"
    assert r["results"][0]["receipt"]["kind"] == "entity"
    assert r["counts"] == {"created": 2, "existing": 1, "error": 2}
    assert count("entities") == 3


def test_receipts_hold_no_record_text(world):
    client = world
    secret = "Synthetic zebra lantern"
    ok(
        ingest(
            client,
            [message(40, content=secret + " message")],
            receipts=True,
            links=[
                {
                    "type": "about",
                    "from": key("fake-40"),
                    "to": key("fake-0"),
                    "evidence": {"note": secret},
                }
            ],
        ),
    )
    ok(
        client.post(
            "/v1/entities",
            json={"kind": "synthetic_work", "name": secret},
            headers=keyed("synthetic-k-0002"),
        )
    )
    ok(
        client.post(
            "/v1/links",
            json={
                "type": "mentions",
                "from": key("fake-40"),
                "to": key("fake-1"),
                "evidence": {"note": secret},
            },
            headers=keyed("synthetic-k-0003"),
        )
    )
    r = client.post(
        "/v1/ingest/messages",
        json={
            "plugin": "synthetic",
            "messages": [message(41, content=secret + " again")],
            "receipts": True,
        },
        headers=keyed("synthetic-k-0004"),
    )
    ok(r)
    rows = sql("SELECT response::text AS t FROM tl.write_receipts")
    assert len(rows) == 3
    for row in rows:
        assert "zebra" not in row["t"] and "lantern" not in row["t"]
    replay = ok(
        client.post(
            "/v1/ingest/messages",
            json={
                "plugin": "synthetic",
                "messages": [message(41, content=secret + " again")],
                "receipts": True,
            },
            headers=keyed("synthetic-k-0004"),
        )
    )
    assert replay["inserted"] == 1 and replay["receipts"][0]["replayed"] is True


def test_receipts_drop_record_content_of_any_type(world):
    """Numbers, booleans and nested keys inside record content (entity
    metadata, link evidence, annotation values) are not kept either."""
    client = world
    ok(
        client.post(
            "/v1/entities",
            json={
                "kind": "synthetic_work",
                "metadata": {"synthetic_quokka": 424242, "flag": True},
            },
            headers=keyed("synthetic-c-0001"),
        )
    )
    batch = {
        "links": [
            {
                "type": "about",
                "from": key("fake-0"),
                "to": key("fake-1"),
                "evidence": {"synthetic_quokka": 424242},
            }
        ]
    }
    first = ok(
        client.post("/v1/links/batch", json=batch, headers=keyed("synthetic-c-0002"))
    )
    ok(
        client.post(
            "/v1/annotations",
            json={
                "message_id": current("fake-2"),
                "attribute": "ignore",
                "value": {"ignored": True, "reason": "synthetic quokka"},
            },
            headers=keyed("synthetic-c-0003"),
        )
    )
    rows = sql("SELECT response::text AS t FROM tl.write_receipts")
    assert len(rows) == 3
    for row in rows:
        assert "quokka" not in row["t"] and "424242" not in row["t"], row["t"]
    # The batch replays its per-item results and receipts.
    again = ok(
        client.post("/v1/links/batch", json=batch, headers=keyed("synthetic-c-0002"))
    )
    assert [x["status"] for x in again["results"]] == ["created"]
    assert again["results"][0]["link_id"] == first["results"][0]["link_id"]
    assert again["results"][0]["receipt"]["replayed"] is True
    assert (
        again["results"][0]["receipt"]["stored_hash"]
        == first["results"][0]["receipt"]["stored_hash"]
    )
    assert count("links") == 1


def test_purge_removes_receipts_aliases_and_deliveries_of_channel(world):
    from throughline.cli import purge

    client = world
    ok(
        client.post(
            "/v1/channels", json={"name": "synthetic.doomed", "plugin": "synthetic"}
        )
    )
    doomed = dict(
        channel="synthetic.doomed", thread={"external_id": "doomed"}, fields={}
    )
    ok(ingest(client, [message(0, external_id="fp-0", **doomed)]))
    r = ok(
        ingest(client, [message(0, external_id="native-0", aliases=["fp-0"], **doomed)])
    )
    assert r["aliased"][0]["status"] == "aliased"
    ok(
        client.post(
            "/v1/links",
            json={
                "type": "about",
                "from": key("fp-0", "synthetic.doomed"),
                "to": key("fake-0"),
            },
            headers=keyed("synthetic-purge-1"),
        )
    )
    ok(
        client.post(
            "/v1/entities",
            json={"kind": "synthetic_kept"},
            headers=keyed("synthetic-purge-2"),
        )
    )
    assert count("message_aliases") == 1 and count("message_deliveries") == 2
    assert count("write_receipts") == 2
    purge("synthetic.doomed", confirm=True)
    assert count("message_aliases") == 0 and count("message_deliveries") == 0
    assert count("write_receipts") == 1
    (kept,) = sql("SELECT idempotency_key FROM tl.write_receipts")
    assert kept["idempotency_key"] == "synthetic-purge-2"


def test_existing_ingest_and_link_tests_green(world):
    """The semantics older clients rely on are unchanged."""
    client = world
    body = {"type": "about", "from": key("fake-0"), "to": key("fake-1")}
    first, again = (
        ok(client.post("/v1/links", json=body)),
        ok(client.post("/v1/links", json=body)),
    )
    assert first["id"] == again["id"] and again["receipt"]["outcome"] == "existing"
    assert ok(ingest(client, [message(i) for i in range(6)]))["unchanged"] == 6
    refused = client.post(
        "/v1/links",
        json=dict(body, type="synthetic free text"),
        headers=headers("agent"),
    )
    assert refused.status_code == 400 and refused.json()["code"] == "23514"
    # A link to a message that does not exist still rolls the batch back.
    r = ingest(
        client,
        [message(50)],
        links=[{"type": "about", "from": key("fake-missing"), "to": key("fake-0")}],
    )
    assert r.status_code == 400 and r.json()["detail"] == "link message does not exist"
    assert count("messages", "external_id='fake-50'") == 0
