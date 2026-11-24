"""Message aliases and per-key deliveries (migration 0048;
MSG-2): provisional ids upgrade to native ids without a new record, the
archive id stays canonical, alternating sources never flip versions, real
edits under either key still create one version, existing pairs are linked
same_as and collapse in recall. Synthetic data only."""

import pytest
from conftest import ingest, message

from throughline.db import connect

CHANNEL = "fake.aliases"


@pytest.fixture()
def aliases(client):
    r = client.post(
        "/v1/channels",
        json=dict(
            name=CHANNEL,
            plugin="synthetic",
            fields=[
                dict(name="identity_quality", type="text", description="Synthetic"),
                dict(name="source", type="text", description="Synthetic"),
            ],
        ),
    )
    assert r.status_code == 200, r.text
    return client


def archive(i, content=None, **extra):
    """An archive row: identified by a fingerprint, archive payload shape."""
    return message(
        i,
        channel=CHANNEL,
        external_id=f"fp-{i}",
        thread={"external_id": f"fake-thread-{i}"},
        content=content or f"Synthetic orchard talk {i}",
        fields={"identity_quality": "fingerprint", "source": "archive"},
        payload={"archive_row": i, "fingerprint": f"fp-{i}"},
        **extra,
    )


def live(i, content=None, aliases=True, **extra):
    """A live delivery: native id, another payload shape, aliases the
    fingerprint id."""
    data = message(
        i,
        channel=CHANNEL,
        external_id=f"native-{i}",
        thread={"external_id": f"fake-thread-{i}"},
        content=content or f"Synthetic orchard talk {i}",
        fields={"identity_quality": "native_id", "source": "live"},
        payload={"event": {"id": f"native-{i}", "body": content or i}},
        **extra,
    )
    if aliases:
        data["aliases"] = [f"fp-{i}"]
    return data


def send(client, messages, **kwargs):
    r = ingest(client, messages, **kwargs)
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body["rejected"], body
    return body


def versions(external_id):
    with connect() as c:
        return c.execute(
            "SELECT m.version,m.content,m.superseded_by IS NULL AS current FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s ORDER BY m.version",
            (CHANNEL, external_id),
        ).fetchall()


def rows(statement, params=()):
    with connect() as c:
        return c.execute(statement, params).fetchall()


def current(external_id):
    with connect() as c:
        row = c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
            (CHANNEL, external_id),
        ).fetchone()
    return row["id"] if row else None


def test_p8_alias_upgrade_no_new_record(aliases):
    client = aliases
    # Equal payload: the same delivery under the native id with the alias.
    same = archive(1)
    send(client, [same])
    upgraded = dict(same, external_id="native-1", aliases=["fp-1"])
    r = send(client, [upgraded])
    assert r["unchanged"] == 1 and r["inserted"] == r["new_versions"] == 0
    assert r["aliased"] == [
        {
            "index": 0,
            "status": "aliased",
            "external_id": "native-1",
            "canonical": "fp-1",
            "link_id": None,
        }
    ]
    assert versions("native-1") == []
    assert len(versions("fp-1")) == 1
    assert rows(
        "SELECT alias,external_id,origin FROM tl.message_aliases ORDER BY alias"
    ) == [{"alias": "native-1", "external_id": "fp-1", "origin": "source"}]
    # Different payload and a higher identity quality: one new version of the
    # same record, which keeps its archive id.
    send(client, [archive(2)])
    r = send(client, [live(2, content="Synthetic orchard talk 2, native")])
    assert r["new_versions"] == 1 and r["inserted"] == 0
    history = versions("fp-2")
    assert [v["version"] for v in history] == [1, 2]
    assert history[-1]["content"] == "Synthetic orchard talk 2, native"
    assert versions("native-2") == []


def test_p8_lower_rank_first_delivery_records_only(aliases):
    client = aliases
    native = live(3, aliases=False)
    native["external_id"] = "native-3"
    send(client, [native])
    # A fingerprint delivery of the same record names the native id: it is a
    # lower quality and only records its delivery.
    fingerprint = archive(3, content="Synthetic orchard talk 3, archive text")
    fingerprint["aliases"] = ["native-3"]
    r = send(client, [fingerprint])
    assert r["unchanged"] == 1 and r["new_versions"] == 0
    assert [v["version"] for v in versions("native-3")] == [1]
    assert versions("fp-3") == []
    deliveries = {
        r["delivered_key"]: r
        for r in rows("SELECT * FROM tl.message_deliveries ORDER BY delivered_key")
    }
    assert set(deliveries) == {"fp-3", "native-3"}
    assert deliveries["fp-3"]["external_id"] == "native-3"
    assert deliveries["fp-3"]["identity_quality"] == "fingerprint"
    assert deliveries["native-3"]["identity_quality"] == "native_id"


def test_msg_2_alternating_archive_and_live_runs_zero_new_versions(aliases):
    client = aliases
    batch = [archive(i) for i in range(4, 7)]
    send(client, batch)
    upgrade = send(client, [live(i) for i in range(4, 7)])
    assert upgrade["new_versions"] == 3
    counts = rows("SELECT count(*) AS n FROM tl.messages")[0]["n"]
    for _ in range(2):
        a = send(client, [archive(i) for i in range(4, 7)])
        b = send(client, [live(i) for i in range(4, 7)])
        assert a["unchanged"] == b["unchanged"] == 3
        assert a["new_versions"] == b["new_versions"] == 0
        assert a["inserted"] == b["inserted"] == 0
    assert rows("SELECT count(*) AS n FROM tl.messages")[0]["n"] == counts
    for i in range(4, 7):
        assert [v["version"] for v in versions(f"fp-{i}")] == [1, 2]
    # Both shapes in one batch are applied in order and change nothing.
    mixed = send(client, [archive(4), live(4), archive(4)])
    assert mixed["unchanged"] == 3


def test_real_edit_under_either_key_creates_one_version(aliases):
    client = aliases
    send(client, [archive(7)])
    send(client, [live(7)])
    assert len(versions("fp-7")) == 2
    # A real edit under the archive key: one version.
    edited = send(client, [archive(7, content="Synthetic orchard talk 7, edited")])
    assert edited["new_versions"] == 1
    assert versions("fp-7")[-1]["content"] == "Synthetic orchard talk 7, edited"
    # The live source repeating its last delivery changes nothing.
    assert send(client, [live(7)])["unchanged"] == 1
    assert (
        send(client, [archive(7, content="Synthetic orchard talk 7, edited")])[
            "unchanged"
        ]
        == 1
    )
    # A real edit under the live key: one version.
    r = send(client, [live(7, content="Synthetic orchard talk 7, edited live")])
    assert r["new_versions"] == 1
    history = versions("fp-7")
    assert [v["version"] for v in history] == [1, 2, 3, 4]
    assert history[-1]["content"] == "Synthetic orchard talk 7, edited live"


def test_p8_alias_resolves_future_deliveries(aliases):
    client = aliases
    send(client, [archive(8)])
    send(client, [live(8)])
    # A later live delivery without aliases resolves through the alias table.
    r = send(client, [live(8, aliases=False)])
    assert r["unchanged"] == 1
    assert r["aliased"][0]["canonical"] == "fp-8"
    assert versions("native-8") == []
    edited = send(client, [live(8, aliases=False, content="Synthetic talk 8 edited")])
    assert edited["new_versions"] == 1 and edited["inserted"] == 0
    assert versions("fp-8")[-1]["content"] == "Synthetic talk 8 edited"
    # A new record that names aliases nobody has used yet keeps them: a later
    # delivery under such an alias resolves to it.
    fresh = archive(9)
    fresh["aliases"] = ["beeper-9"]
    send(client, [fresh])
    later = archive(9)
    later["external_id"] = "beeper-9"
    r = send(client, [later])
    assert r["aliased"][0]["canonical"] == "fp-9"
    assert versions("beeper-9") == []
    # Within one batch: the record, then a delivery naming it as an alias.
    r = send(client, [archive(10), live(10)])
    assert r["inserted"] == 1 and r["new_versions"] == 1
    assert versions("native-10") == []


def test_p8_existing_pair_linked_same_as_and_recall_returns_one(aliases):
    client = aliases
    content = "Synthetic quokka harvest meeting"
    send(client, [archive(11, content=content)])
    separate = live(11, content=content, aliases=False)
    send(client, [separate])
    fp, native = current("fp-11"), current("native-11")
    assert fp and native and fp != native
    before = client.post(
        "/v1/recall", json={"query": "quokka harvest", "k": 10, "strategy": "keyword"}
    )
    assert before.status_code == 200, before.text
    assert sorted(r["message_id"] for r in before.json()["records"]) == sorted(
        [fp, native]
    )
    r = send(client, [live(11, content=content)])
    assert r["aliased"][0]["status"] == "pair_linked"
    assert r["aliased"][0]["canonical"] == "fp-11"
    link = rows("SELECT * FROM tl.links WHERE type='same_as'")
    assert len(link) == 1 and link[0]["id"] == r["aliased"][0]["link_id"]
    assert {link[0]["from_id"], link[0]["to_id"]} == {fp, native}
    assert link[0]["method"] == "source"
    # Both records stay (archive ids never change); recall returns one.
    assert current("fp-11") == fp and current("native-11") == native
    after = client.post(
        "/v1/recall", json={"query": "quokka harvest", "k": 10, "strategy": "keyword"}
    )
    assert after.status_code == 200, after.text
    assert [r["message_id"] for r in after.json()["records"]] == [native]
    # Replaying the pair delivery writes no second link.
    send(client, [live(11, content=content)])
    assert len(rows("SELECT id FROM tl.links WHERE type='same_as'")) == 1


def test_p8_endpoint_resolves_alias_for_links(aliases):
    client = aliases
    send(client, [archive(12), archive(13)])
    send(client, [live(12)])
    r = client.post(
        "/v1/links",
        json={
            "type": "reply_to",
            "from": {"channel": CHANNEL, "external_id": "native-12"},
            "to": {"channel": CHANNEL, "external_id": "fp-13"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["from_id"] == current("fp-12")
    prov = client.post(
        "/v1/provenance",
        json={
            "derived": {"channel": CHANNEL, "external_id": "fp-13"},
            "sources": [{"channel": CHANNEL, "external_id": "native-12"}],
        },
    )
    assert prov.status_code == 200 and prov.json()["written"] == 1, prov.text
    missing = client.post(
        "/v1/links",
        json={
            "type": "reply_to",
            "from": {"channel": CHANNEL, "external_id": "native-unknown"},
            "to": {"channel": CHANNEL, "external_id": "fp-13"},
        },
    )
    assert missing.status_code == 400
    assert missing.json()["detail"] == "link message does not exist"


def test_msg_2_replay_with_aliases_idempotent(aliases):
    client = aliases
    batch = [archive(14), live(15, aliases=False)]
    batch[1]["aliases"] = ["provisional-15", "beeper-15"]
    first = send(client, batch)
    assert first["inserted"] == 2
    state = rows("SELECT * FROM tl.message_aliases ORDER BY alias")
    for _ in range(2):
        again = send(client, batch)
        assert again["unchanged"] == 2
        assert again["inserted"] == again["new_versions"] == 0
    assert rows("SELECT * FROM tl.message_aliases ORDER BY alias") == state
    assert [r["alias"] for r in state] == ["beeper-15", "provisional-15"]
    # aliases are outside the payload hash: the same delivery without them is
    # unchanged too.
    plain = send(client, [archive(14)])
    assert plain["unchanged"] == 1
    # A record's own id is never its alias; at most 10 aliases.
    own = archive(16)
    own["aliases"] = ["fp-16"]
    assert send(client, [own])["inserted"] == 1
    assert rows("SELECT * FROM tl.message_aliases WHERE external_id='fp-16'") == []
    many = archive(17)
    many["aliases"] = [f"x-{i}" for i in range(11)]
    r = ingest(client, [many])
    assert r.status_code == 200 and r.json()["rejected"][0]["index"] == 0


def test_describe_advertises_message_aliases(client):
    index = client.get("/v1/describe")
    assert index.status_code == 200, index.text
    subjects = {s["subject"] for s in index.json()["subjects"]}
    assert {"message_aliases", "write_contract"} <= subjects
    section = client.get("/v1/describe", params={"subject": "message_aliases"}).json()
    assert section["feature"] == "message_aliases"
    assert any("identity_quality" in rule for rule in section["rules"])
