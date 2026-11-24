"""Record kinds, subject time, location applicability, the record contract,
as-of selection and the catalogue registry (migration 0032). Synthetic data."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import ingest, message
from psycopg import sql

from throughline.db import connect
from throughline.models import Message
from throughline.query import FUNCTIONS

AT = datetime(2020, 1, 1, tzinfo=timezone.utc)

CONTRACT = [
    "record_id",
    "kind",
    "ref_id",
    "message_id",
    "version",
    "is_current",
    "channel",
    "external_id",
    "thread_id",
    "thread_no",
    "content",
    "record_time",
    "ended_at",
    "ingested_at",
    "subject_from",
    "subject_to",
    "subject_from_precision",
    "subject_to_precision",
    "person_id",
    "author_handle",
    "lat",
    "lon",
    "loc_reason",
    "is_outside",
    "heading_path",
    "char_start",
    "char_end",
    "claim",
    "generated",
    "unsupported",
    "provenance",
    "support",
    "expansion",
]
LEGACY_FUNCTIONS = {
    "context",
    "search",
    "search_all",
    "search_filtered",
    "search_fused",
    "search_meaning",
    "near",
    "near_place",
    "overlapping",
    "during",
    "person",
    "person_messages",
    "follow",
    "catalog",
}
HASH_BEFORE_SUBJECT = "173ab389ffd7b1ddd99f903f66b9446ecfa83276d6aba002d10ff07c5545c017"
SUBJECT = dict(
    start="2019-06-01T00:00:00+02:00",
    end="2019-08-31T00:00:00+02:00",
    start_precision="day",
    end_precision="month",
)


def register(client, name, **kwargs):
    r = client.post("/v1/channels", json=dict(name=name, plugin="synthetic", **kwargs))
    assert r.status_code == 200, r.text
    return r.json()


def kinds(client):
    """One channel of every record kind."""
    register(client, "fake.life")
    register(client, "fake.reference", is_reference=True)
    register(client, "fake.page", is_reference=True, record_kind="page_revision")
    register(client, "fake.derived", record_kind="derived")
    return ["fake.life", "fake.reference", "fake.page", "fake.derived"]


def other(i, channel, **kwargs):
    return message(i, channel=channel, fields={}, **kwargs)


def reader(statement, params=None, settings=None):
    with connect("reader") as c:
        for name, value in (settings or {}).items():
            # The reader may not call set_config; SET LOCAL is what the
            # service uses as well.
            c.execute(
                sql.SQL("SET LOCAL {} = {}").format(sql.SQL(name), sql.Literal(value))
            )
        return c.execute(statement, params).fetchall()


def describe(subject=None, settings=None):
    return reader("SELECT api.describe(%s) d", (subject,), settings)[0]["d"]


# --- MSG-8 record kinds ------------------------------------------------------


def test_msg_8_channel_kind_default_declared_reserved(client):
    assert register(client, "fake.life")["record_kind"] == "life_event"
    assert (
        register(client, "fake.reference", is_reference=True)["record_kind"]
        == "reference"
    )
    declared = register(
        client,
        "fake.page",
        is_reference=True,
        record_kind="page_revision",
        passage_text_paths=["/compiled_truth|/body", "/timeline"],
        provenance_link_types=["cites"],
    )
    assert declared["record_kind"] == "page_revision"
    assert register(client, "fake.derived", record_kind="derived")["record_kind"] == (
        "derived"
    )
    # Not declaring the kind again keeps the stored one.
    assert register(client, "fake.page", is_reference=True)["record_kind"] == (
        "page_revision"
    )
    with connect("reader") as c:
        rows = {
            r["name"]: r
            for r in c.execute("SELECT * FROM api.channels WHERE name LIKE 'fake.%'")
        }
    assert {n: r["record_kind"] for n, r in rows.items()} == {
        "fake.life": "life_event",
        "fake.reference": "reference",
        "fake.page": "page_revision",
        "fake.derived": "derived",
    }
    # Re-registration without the optional declarations clears them.
    assert rows["fake.page"]["passage_text_paths"] is None
    register(
        client,
        "fake.page",
        is_reference=True,
        passage_text_paths=["/compiled_truth|/body", "/timeline"],
        provenance_link_types=["cites"],
    )
    with connect() as c:
        row = c.execute("SELECT * FROM tl.channels WHERE name='fake.page'").fetchone()
        assert row["passage_text_paths"] == ["/compiled_truth|/body", "/timeline"]
        assert row["provenance_link_types"] == ["cites"]
        assert row["record_kind"] == "page_revision"
    # 'reference' is only a kind of reference channels; unknown kinds, bad
    # pointers and reserved field names are refused.
    for bad in (
        dict(record_kind="reference"),
        dict(record_kind="page"),
        dict(passage_text_paths=["compiled_truth"]),
        dict(passage_text_paths=["/a|"]),
        dict(provenance_link_types=[""]),
    ):
        r = client.post(
            "/v1/channels", json=dict(name="fake.bad", plugin="synthetic", **bad)
        )
        assert r.status_code == 422, (bad, r.text)
    for name in ("record_id", "kind", "subject_from", "support", "char_start"):
        r = client.post(
            "/v1/channels",
            json=dict(
                name="fake.bad",
                plugin="synthetic",
                fields=[dict(name=name, type="text")],
            ),
        )
        assert r.status_code == 422 and "reserved" in r.text, (name, r.text)
    for statement in (
        "UPDATE tl.channels SET record_kind='bogus' WHERE name='fake.life'",
        "UPDATE tl.channels SET record_kind='reference' WHERE name='fake.life'",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            with connect() as c:
                c.execute(statement)
    # A channel that stops being a reference channel gets the default again.
    assert register(client, "fake.reference")["record_kind"] == "life_event"


def test_msg_8_fresh_visits_channel_is_derived_and_keeps_source_location(client):
    points = [
        dict(
            at=(AT + timedelta(minutes=i)).isoformat(),
            lat=40,
            lon=-75,
            source="fake-track",
        )
        for i in range(16)
    ]
    assert client.post("/v1/ingest/track", json={"points": points}).status_code == 200
    r = client.post("/v1/track/detect-visits")
    assert r.status_code == 200 and r.json()["inserted"] == 1, r.text
    with connect() as c:
        channel = c.execute("SELECT * FROM tl.channels WHERE name='visits'").fetchone()
        assert channel["record_kind"] == "derived" and not channel["is_reference"]
        row = c.execute("SELECT * FROM api.records WHERE channel='visits'").fetchone()
    assert row["kind"] == "derived" and row["generated"]
    assert (row["lat"], row["lon"], row["loc_reason"]) == (40, -75, None)
    with connect() as c:
        assert (
            c.execute("SELECT loc_method FROM api.visits").fetchone()["loc_method"]
            == "source"
        )
    # Registering again (every detection run does) keeps the kind.
    assert client.post("/v1/track/detect-visits").status_code == 200
    with connect() as c:
        assert (
            c.execute(
                "SELECT record_kind FROM tl.channels WHERE name='visits'"
            ).fetchone()["record_kind"]
            == "derived"
        )


# --- MSG-9 subject time ------------------------------------------------------


def test_msg_9_subject_roundtrip_immutable(client, channel):
    r = ingest(client, [message(0, subject=SUBJECT), message(1)])
    assert r.status_code == 200 and r.json()["inserted"] == 2, r.text
    with connect("reader") as c:
        rows = c.execute("SELECT * FROM api.records ORDER BY external_id").fetchall()
    assert rows[0]["subject_from"] == datetime.fromisoformat(SUBJECT["start"])
    assert rows[0]["subject_to"] == datetime.fromisoformat(SUBJECT["end"])
    assert rows[0]["subject_from_precision"] == "day"
    assert rows[0]["subject_to_precision"] == "month"
    assert all(
        rows[1][k] is None
        for k in (
            "subject_from",
            "subject_to",
            "subject_from_precision",
            "subject_to_precision",
        )
    )
    # A dump validates back to the same message (no aliases).
    model = Message.model_validate(message(0, subject=SUBJECT))
    assert Message.model_validate(model.model_dump(mode="json")) == model
    assert model.model_dump(mode="json")["subject"]["start_precision"] == "day"
    # An open interval is valid; precision defaults to exact.
    r = ingest(client, [message(2, subject={"end": "2019-01-01T00:00:00Z"})])
    assert r.json()["inserted"] == 1, r.text
    with connect() as c:
        row = c.execute(
            "SELECT * FROM tl.messages WHERE external_id='fake-2'"
        ).fetchone()
        assert row["subject_from"] is None and row["subject_to"] is not None
        assert row["subject_to_precision"] == "exact"
        # A precision qualifies a bound: the open end has none.
        assert row["subject_from_precision"] is None
    # Invalid subjects are rejected per item.
    bad = [
        message(3, subject={"start": "2020-01-02T00:00:00Z", "end": "2020-01-01Z"}),
        message(4, subject={"start": "2020-01-02T00:00:00"}),
        message(5, subject={"start": "2020-01-02T00:00:00Z", "start_precision": "x"}),
        message(6, subject={}),
    ]
    result = ingest(client, bad).json()
    assert result["inserted"] == 0 and len(result["rejected"]) == 4, result
    # Subject time is ground truth: no role may rewrite it.
    for column, value in (
        ("subject_from", "2001-01-01Z"),
        ("subject_to", "2030-01-01Z"),
        ("subject_from_precision", "year"),
        ("subject_to_precision", "year"),
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            with connect() as c:
                c.execute(
                    f"UPDATE tl.messages SET {column}=%s WHERE external_id='fake-0'",
                    (value,),
                )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect("app") as c:
                c.execute(
                    f"UPDATE tl.messages SET {column}=%s WHERE external_id='fake-0'",
                    (value,),
                )
    with pytest.raises(psycopg.errors.CheckViolation):
        with connect() as c:
            c.execute(
                "INSERT INTO tl.messages(channel_id,external_id,version,happened_at,content,payload_hash,subject_from,subject_to) SELECT id,'fake-x',1,now(),'x','x','2020-01-02Z','2020-01-01Z' FROM tl.channels LIMIT 1"
            )


def test_msg_9_repeated_key_with_subject_in_one_batch(client, channel):
    later = dict(SUBJECT, end="2019-09-30T00:00:00+02:00")
    r = ingest(
        client,
        [
            message(0, subject=SUBJECT),
            message(0, content="Synthetic orchard edited", subject=later),
            message(0, content="Synthetic orchard edited", subject=later),
        ],
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["rejected"] == []
    assert (result["inserted"], result["new_versions"], result["unchanged"]) == (
        1,
        1,
        1,
    )
    with connect() as c:
        rows = c.execute(
            "SELECT version,subject_to,is_current FROM api.record_versions ORDER BY version"
        ).fetchall()
    assert [r["version"] for r in rows] == [1, 2]
    assert rows[0]["subject_to"] == datetime.fromisoformat(SUBJECT["end"])
    assert rows[1]["subject_to"] == datetime.fromisoformat(later["end"])
    assert [r["is_current"] for r in rows] == [False, True]


def _digest(dump):
    if not dump["thread"]["metadata"]:
        del dump["thread"]["metadata"]
    return hashlib.sha256(
        json.dumps(
            dump,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def test_msg_2_hash_unchanged_without_subject(client, channel):
    data = message(0, location={"lat": 1, "lon": 2}, ended_at="2020-01-01T00:10:00Z")
    assert ingest(client, [data]).json()["inserted"] == 1
    with connect() as c:
        stored = c.execute("SELECT payload_hash FROM tl.messages").fetchone()[
            "payload_hash"
        ]
        envelope = c.execute("SELECT envelope FROM tl.message_envelopes").fetchone()[
            "envelope"
        ]
    # The hashed form has no subject key at all, exactly as before the field.
    dump = Message.model_validate(data).model_dump(mode="json")
    assert dump.pop("subject") is None
    assert stored == _digest(dump)
    assert "subject" not in envelope
    # Digest of this message computed by the release before subject time.
    assert stored == HASH_BEFORE_SUBJECT
    # So redelivery of a stored record makes no version.
    result = ingest(client, [data]).json()
    assert (result["unchanged"], result["new_versions"]) == (1, 0)


def test_msg_9_subject_change_one_version(client, channel):
    assert ingest(client, [message(0, subject=SUBJECT)]).json()["inserted"] == 1
    assert ingest(client, [message(0, subject=SUBJECT)]).json()["unchanged"] == 1
    changed = dict(SUBJECT, start_precision="year")
    assert ingest(client, [message(0, subject=changed)]).json()["new_versions"] == 1
    assert ingest(client, [message(0, subject=changed)]).json()["unchanged"] == 1
    # Adding or removing subject time is one version as well.
    assert ingest(client, [message(1)]).json()["inserted"] == 1
    assert ingest(client, [message(1, subject=SUBJECT)]).json()["new_versions"] == 1
    assert ingest(client, [message(1)]).json()["new_versions"] == 1
    with connect() as c:
        rows = c.execute(
            "SELECT external_id,count(*) n,max(version) v FROM tl.messages GROUP BY external_id ORDER BY external_id"
        ).fetchall()
        assert [(r["n"], r["v"]) for r in rows] == [(2, 2), (3, 3)]
        current = c.execute(
            "SELECT external_id,subject_from_precision FROM api.records ORDER BY external_id"
        ).fetchall()
        assert [r["subject_from_precision"] for r in current] == ["year", None]
        envelope = c.execute(
            "SELECT e.envelope FROM tl.message_envelopes e JOIN api.messages m ON m.id=e.message_id WHERE m.external_id='fake-0'"
        ).fetchone()["envelope"]
    assert envelope["subject"]["start"].endswith("+02:00")


def test_msg_9_reparse_carries_subject(client, channel):
    from throughline.reparse import register_mapper, reparse

    assert ingest(client, [message(0, subject=SUBJECT)]).json()["inserted"] == 1
    register_mapper(channel, lambda payload: {})
    with connect("app") as c:
        assert reparse(c, channel)["unchanged"] == 1
    register_mapper(channel, lambda payload: {"content": "Synthetic reparsed"})
    with connect("app") as c:
        assert reparse(c, channel)["new_versions"] == 1
    # Rows stored without an envelope are rebuilt from the columns.
    with connect() as c:
        c.execute("DELETE FROM tl.message_envelopes")
    register_mapper(channel, lambda payload: {"content": "Synthetic reparsed twice"})
    with connect("app") as c:
        assert reparse(c, channel)["new_versions"] == 1
    with connect() as c:
        row = c.execute("SELECT * FROM api.records").fetchone()
    assert row["version"] == 3 and row["content"] == "Synthetic reparsed twice"
    assert row["subject_from"] == datetime.fromisoformat(SUBJECT["start"])
    assert row["subject_to_precision"] == "month"


def test_msg_9_open_subject_bound_has_no_precision(client, channel):
    from throughline.reparse import register_mapper, reparse

    subject = {"start": "2019-06-01T00:00:00Z", "start_precision": "year"}
    assert ingest(client, [message(0, subject=subject)]).json()["inserted"] == 1
    with connect() as c:
        row = c.execute("SELECT * FROM tl.messages").fetchone()
        assert (row["subject_from_precision"], row["subject_to_precision"]) == (
            "year",
            None,
        )
        # Rebuilt from the columns alone, the subject is the same message.
        c.execute("DELETE FROM tl.message_envelopes")
    register_mapper(channel, lambda payload: {})
    with connect("app") as c:
        assert reparse(c, channel)["unchanged"] == 1
    assert ingest(client, [message(0, subject=subject)]).json()["unchanged"] == 1
    for columns in (
        "subject_from_precision='day'",
        "subject_to_precision='day'",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            with connect() as c:
                c.execute(
                    "INSERT INTO tl.messages(channel_id,external_id,version,happened_at,content,payload_hash,"
                    + columns.split("=")[0]
                    + ") SELECT id,'fake-x',1,now(),'x','x',"
                    + columns.split("=")[1]
                    + " FROM tl.channels LIMIT 1"
                )


# --- MSG-10 location applicability ------------------------------------------


def test_msg_10_not_applicable(client):
    channels = kinds(client)
    here = {"lat": 10, "lon": 10}
    batch = []
    for n, name in enumerate(channels):
        batch.append(other(n * 10, name, location=here))
        batch.append(other(n * 10 + 1, name))
    batch.append(other(90000, "fake.life"))
    assert ingest(client, batch).json()["inserted"] == 9
    point = dict(at=AT.isoformat(), lat=1, lon=2, accuracy_m=3, source="fake-track")
    for _ in range(2):  # late attachment, then a no-op repeat
        assert client.post(
            "/v1/ingest/track", json={"points": [point]}
        ).status_code == (200)
    # Ingest with the track already present takes the same decisions.
    late = [other(n * 10 + 2, name) for n, name in enumerate(channels)]
    assert ingest(client, late).json()["inserted"] == 4
    with connect() as c:
        rows = {
            (r["channel"], r["external_id"]): r
            for r in c.execute("SELECT * FROM api.messages")
        }

    def loc(channel, i):
        r = rows[(channel, f"fake-{i}")]
        return r["lat"], r["loc_method"], r["loc_reason"]

    assert loc("fake.life", 0) == (10, "source", None)
    assert loc("fake.life", 1) == (1, "track", None)
    assert loc("fake.life", 2) == (1, "track", None)
    assert loc("fake.life", 90000) == (None, None, "no_track_point_within_tolerance")
    for name, base in (("fake.reference", 10), ("fake.page", 20)):
        for i in range(3):
            assert loc(name, base + i) == (None, None, "not_applicable"), (name, i)
    assert loc("fake.derived", 30) == (10, "source", None)
    assert loc("fake.derived", 31) == (None, None, "not_applicable")
    assert loc("fake.derived", 32) == (None, None, "not_applicable")
    with connect() as c:
        assert (
            c.execute("SELECT count(*) n FROM tl.message_locations").fetchone()["n"]
            == 2
        )
    for reason in ("reference_channel", "bogus"):
        with pytest.raises(psycopg.errors.CheckViolation):
            with connect() as c:
                c.execute("UPDATE tl.messages SET loc_reason=%s", (reason,))


# --- MSG-7 record contract ---------------------------------------------------


def test_msg_7_record_contract_columns_exact_order(client):
    channels = kinds(client)
    batch = [other(n, name) for n, name in enumerate(channels)]
    assert ingest(client, batch).json()["inserted"] == 4
    assert (
        ingest(client, [other(0, "fake.life", content="Synthetic edit")]).json()[
            "new_versions"
        ]
        == 1
    )
    with connect("reader") as c:
        for view in ("records", "record_versions"):
            cursor = c.execute(f"SELECT * FROM api.{view} LIMIT 0")
            assert [d.name for d in cursor.description] == CONTRACT, view
        types = {
            r["column_name"]: r["data_type"]
            for r in c.execute(
                "SELECT column_name,data_type FROM information_schema.columns WHERE table_schema='api' AND table_name='record_versions'"
            )
        }
        current = {
            r["channel"]: r for r in c.execute("SELECT * FROM api.records").fetchall()
        }
        versions = c.execute(
            "SELECT * FROM api.record_versions WHERE channel='fake.life' ORDER BY version"
        ).fetchall()
        asof = c.execute("SELECT * FROM api.records_asof(now()) LIMIT 0")
        assert [d.name for d in asof.description] == CONTRACT
    assert types["record_id"] == "text" and types["heading_path"] == "ARRAY"
    assert types["support"] == types["provenance"] == types["expansion"] == "jsonb"
    assert types["ref_id"] == types["message_id"] == "bigint"
    assert set(current) == set(channels)
    assert {r["kind"] for r in current.values()} == {
        "life_event",
        "reference",
        "page_revision",
        "derived",
    }
    life = current["fake.life"]
    assert life["record_id"] == f"m:{life['message_id']}"
    assert life["ref_id"] == life["message_id"] and life["version"] == 2
    assert life["is_current"] and not life["generated"] and not life["unsupported"]
    assert life["support"] == [
        {"message_id": life["message_id"], "version": 2, "relation": "self"}
    ]
    assert life["provenance"] == {
        "plugin": "synthetic",
        "channel": "fake.life",
        "external_id": "fake-0",
        "version": 2,
        "method": "source",
    }
    assert life["expansion"] == {
        "context": life["message_id"],
        "thread": life["thread_id"],
        "key": ["fake.life", "fake-0"],
    }
    assert life["record_time"] == AT and life["content"] == "Synthetic edit"
    assert all(
        life[k] is None for k in ("heading_path", "char_start", "char_end", "claim")
    )
    assert [(r["version"], r["is_current"]) for r in versions] == [
        (1, False),
        (2, True),
    ]
    reference = current["fake.reference"]
    assert not reference["generated"] and not reference["unsupported"]
    assert reference["support"][0]["relation"] == "self"
    # Generated records are never their own evidence.
    for name in ("fake.page", "fake.derived"):
        record = current[name]
        assert record["generated"] and record["unsupported"], name
        assert record["support"] == [], name
    with connect() as c:
        assert c.execute(
            "SELECT relation FROM tl.record_sources WHERE prefix='m'"
        ).fetchone() == {"relation": "api.record_versions"}
        with pytest.raises(psycopg.errors.CheckViolation):
            with c.transaction():
                c.execute("INSERT INTO tl.record_sources VALUES ('z','tl.messages')")


# --- BRN-5 as-of selection ---------------------------------------------------


def test_brn_5_records_asof_by_record_time_and_known_by(client):
    kinds(client)

    def day(n):
        return (AT + timedelta(days=n)).isoformat()

    def one(statement_at, known_by=None, channel="fake.page"):
        return reader(
            "SELECT * FROM api.records_asof(%s,%s) WHERE channel=%s",
            (statement_at, known_by, channel),
        )

    # Three revisions of one page; the third was imported last but is older.
    for content, at in (("first", 0), ("second", 31), ("backfilled", 10)):
        r = ingest(
            client,
            [
                other(
                    0, "fake.page", content=f"Synthetic {content}", happened_at=day(at)
                )
            ],
        )
        assert r.status_code == 200 and not r.json()["rejected"], r.text
    assert one(day(-1)) == []
    assert [r["content"] for r in one(day(5))] == ["Synthetic first"]
    assert [r["content"] for r in one(day(15))] == ["Synthetic backfilled"]
    hit = one(day(60))
    assert [(r["content"], r["version"], r["is_current"]) for r in hit] == [
        ("Synthetic second", 2, False)
    ]
    assert hit[0]["kind"] == "page_revision" and hit[0]["record_time"] == AT + (
        timedelta(days=31)
    )
    with connect() as c:
        stored = {
            r["version"]: r["ingested_at"]
            for r in c.execute(
                "SELECT version,ingested_at FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name='fake.page'"
            )
        }
    # known_by hides revisions stored later, whatever their record time.
    assert [r["version"] for r in one(day(60), stored[1])] == [1]
    assert [r["version"] for r in one(day(15), stored[2])] == [1]
    assert [r["version"] for r in one(day(15), stored[3])] == [3]
    assert one(day(60), stored[1] - timedelta(seconds=1)) == []

    # A life event edited twice: its edits have no time besides ingest time.
    for content in ("original", "edited", "edited again"):
        assert (
            ingest(
                client, [other(1, "fake.life", content=f"Synthetic {content}")]
            ).json()["rejected"]
            == []
        )
    with connect() as c:
        stored = {
            r["version"]: r["ingested_at"]
            for r in c.execute(
                "SELECT version,ingested_at FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name='fake.life'"
            )
        }
    assert one(AT, channel="fake.life") == []
    now = one(day(1), channel="fake.life")
    assert [(r["version"], r["is_current"]) for r in now] == [(3, True)]
    assert [r["version"] for r in one(day(1), stored[1], "fake.life")] == [1]
    assert [r["version"] for r in one(day(1), stored[2], "fake.life")] == [2]
    assert [r["version"] for r in one(day(1), stored[3], "fake.life")] == [3]
    assert one(AT, stored[3], "fake.life") == []
    # One row per record over every channel.
    rows = reader("SELECT channel,external_id FROM api.records_asof(%s)", (day(60),))
    assert sorted((r["channel"], r["external_id"]) for r in rows) == [
        ("fake.life", "fake-1"),
        ("fake.page", "fake-0"),
    ]
    checks = reader(
        "SELECT api.applies_at(NULL,NULL,now()) a, api.applies_at('2020-01-01Z','2021-01-01Z','2020-01-01Z') b, api.applies_at('2020-01-01Z','2021-01-01Z','2021-01-01Z') c, api.applies_at('2020-01-01Z',NULL,'2019-12-31Z') d, api.applies_at(NULL,'2021-01-01Z','2019-12-31Z') e"
    )[0]
    assert checks == dict(a=True, b=True, c=False, d=False, e=True)


# --- INT-8 registry, catalogue and describe ---------------------------------


def test_int_8_registry_examples_execute_and_old_catalog_keys_kept(client):
    with connect() as c:
        registry = {
            r["name"]: r for r in c.execute("SELECT * FROM tl.api_functions").fetchall()
        }
        known = {
            r["proname"]
            for r in c.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api'"
            )
        }
    assert LEGACY_FUNCTIONS | {"records_asof", "applies_at", "describe"} <= set(
        registry
    )
    # Semantic rows (kind 'semantic', such as api.recall) describe functions of
    # the semantic SQL runtime: they are never Postgres functions and never on
    # the allow-list. Every other row names a real api function.
    semantic = {name for name, r in registry.items() if r["kind"] == "semantic"}
    assert not semantic & known
    assert not semantic & FUNCTIONS
    assert set(registry) - semantic <= known
    assert {
        name: (r["ranked_limit_arg"], r["default_limit"])
        for name, r in registry.items()
        if r["ranked_limit_arg"] is not None and name in LEGACY_FUNCTIONS
    } == {
        "search": (2, 20),
        "search_all": (2, 20),
        "search_filtered": (2, 20),
        "search_meaning": (2, 20),
        "search_fused": (3, 20),
    }
    for name, row in registry.items():
        assert row["signature"].startswith(name + "("), row
        assert f"api.{name}(" in row["example"], row
        if name in semantic:
            # Run by the semantic runtime, whose own tests execute them.
            continue
        # Every example runs for the reader on an empty database.
        reader(row["example"])
    assert "max_results integer DEFAULT 20" in registry["search"]["signature"]
    catalog = reader("SELECT api.catalog() c")[0]["c"]
    assert {
        "channels",
        "views",
        "functions",
        "link_types",
        "meaning_search",
        "identity_columns",
        "identity_import",
        "sections",
        "catalog_version",
    } <= set(catalog)
    assert catalog["channels"] == [] and catalog["link_types"] == []
    assert {"messages", "records", "record_versions"} <= {
        v["name"] for v in catalog["views"]
    }
    assert "people" in catalog["identity_columns"]
    functions = {f["name"]: f for f in catalog["functions"]}
    assert set(functions) == set(registry)
    assert functions["search"]["example"] == "SELECT * FROM api.search('orchard',20)"
    assert all(
        {"description", "example", "exactness", "cost_class", "level"} <= set(f)
        for f in functions.values()
    )
    assert {"record_kinds", "time", "location", "levels"} <= set(catalog["sections"])
    # The version moves with the registry.
    with connect() as c:
        with c.transaction(force_rollback=True):
            c.execute("UPDATE tl.catalog_sections SET version=version+1")
            changed = c.execute("SELECT api.catalog()->>'catalog_version' v").fetchone()
    assert changed["v"] != catalog["catalog_version"]
    assert (
        client.get("/v1/catalog").json()["catalog_version"]
        == (catalog["catalog_version"])
    )


def test_int_8_describe_subjects(client, channel):
    assert ingest(client, [message(i) for i in range(3)]).json()["inserted"] == 3
    index = describe()
    subjects = {s["subject"] for s in index["subjects"]}
    assert {
        "functions",
        "function:<name>",
        "channel:<name>",
        "record_kinds",
        "time",
        "location",
        "levels",
    } <= subjects
    assert index["channels"] == ["synthetic.chat"]
    assert "search" in index["functions"] and index["catalog_version"]
    functions = describe("functions")
    assert {f["name"] for f in functions} >= LEGACY_FUNCTIONS
    one = describe("function:search")
    assert one["ranked_limit_arg"] == 2 and one["default_limit"] == 20
    assert one == next(f for f in functions if f["name"] == "search")
    assert set(describe("time")) >= {
        "record_time",
        "ingested_at",
        "subject",
        "precision",
        "time_basis",
    }
    assert "not_applicable" in describe("location")["loc_reason"]
    assert set(describe("record_kinds")["kinds"]) == {
        "life_event",
        "reference",
        "page_revision",
        "derived",
    }
    coverage = describe("channel:synthetic.chat")
    assert coverage["name"] == "synthetic.chat"
    assert coverage["record_kind"] == "life_event" and coverage["description"] == ""
    assert coverage["messages"] == 3
    assert datetime.fromisoformat(coverage["first_record_time"]) == AT
    assert datetime.fromisoformat(coverage["last_record_time"]) == AT + timedelta(
        seconds=2
    )
    assert coverage["last_ingested_at"]
    maintenance = coverage["maintenance"]
    assert maintenance["dirty_threads"] == 0
    assert maintenance["closed_conversations"] >= 0
    assert set(maintenance) >= {"embedded_share", "embedded_conversations", "plugin"}
    # An explicit owner scope sees the same; an asker scope gets no numbers.
    assert describe("channel:synthetic.chat", {"throughline.scope_mode": "owner"}) == (
        coverage
    )
    asker = describe(
        "channel:synthetic.chat",
        {"throughline.scope_mode": "asker", "throughline.asker": "fake-asker"},
    )
    assert asker is not None and asker["maintenance"] is None
    assert asker["messages"] == 3
    # Unknown subjects of every form are null, never an error.
    for subject in (
        "channel:missing",
        "function:missing",
        "missing",
        "channel:",
        "Functions",
        "describe",
        "levels; DROP",
        "",
    ):
        assert describe(subject) is None, subject
    # Other packages add a subject by creating api.describe_<subject>().
    with connect() as c:
        c.execute(
            "CREATE FUNCTION api.describe_fake_subject() RETURNS jsonb LANGUAGE sql STABLE AS $$ SELECT jsonb_build_object('answer',42,'mode',current_setting('throughline.scope_mode',true)) $$"
        )
        c.execute(
            "COMMENT ON FUNCTION api.describe_fake_subject() IS 'Synthetic subject'"
        )
    try:
        assert describe("fake_subject")["answer"] == 42
        assert (
            describe("fake_subject", {"throughline.scope_mode": "asker"})["mode"]
            == "asker"
        )
        listed = {s["subject"]: s["description"] for s in describe()["subjects"]}
        assert listed["fake_subject"] == "Synthetic subject"
    finally:
        with connect() as c:
            c.execute("DROP FUNCTION api.describe_fake_subject()")
    assert describe("fake_subject") is None


def test_describe_reads_only_api_views(client):
    with connect() as c:
        source = c.execute(
            "SELECT pg_get_functiondef('api.describe(text)'::regprocedure) s"
        ).fetchone()["s"]
        definer = c.execute(
            "SELECT prosecdef,proconfig FROM pg_proc WHERE oid='api.describe(text)'::regprocedure"
        ).fetchone()
        depends = c.execute(
            "SELECT count(*) n FROM pg_depend d JOIN pg_class r ON r.oid=d.refobjid JOIN pg_namespace n ON n.oid=r.relnamespace WHERE d.objid='api.describe(text)'::regprocedure AND n.nspname='tl' AND r.relname IN ('messages','channels','threads','embeddings')"
        ).fetchone()
    for private in ("tl.messages", "tl.channels", "tl.threads", "tl.embeddings"):
        assert private not in source, private
    assert "tl.conversations" not in source and "tl.raw_payloads" not in source
    for view in ("api.channels", "api.messages", "api.plugins"):
        assert view in source, view
    assert depends["n"] == 0
    assert definer["prosecdef"]
    assert definer["proconfig"] == ["search_path=pg_catalog, api, public"]
    # The private helper is not reachable by a runtime role.
    for role in ("reader", "app"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connect(role) as c:
                c.execute("SELECT tl.channel_maintenance(1)")


def test_qry_16_levels_section(client):
    levels = describe("levels")
    assert [entry["level"] for entry in levels["levels"]] == [0, 1, 2, 3]
    assert [entry["name"] for entry in levels["levels"]] == [
        "exact SQL",
        "discovery",
        "expansion, rerank and selection",
        "librarian",
    ]
    assert "not modes" in levels["description"]
    assert "Optional" in levels["levels"][3]["description"]
    functions = describe("functions")
    assert all(f["level"] in (0, 1, 2, 3) for f in functions)
    by_name = {f["name"]: f["level"] for f in functions}
    assert by_name["overlapping"] == 0 and by_name["search"] == 1
    assert by_name["context"] == 2
    assert reader("SELECT api.catalog()->'sections'->'levels' s")[0]["s"] == levels


# --- optional features of sibling packages ------------------------------------------


def test_describe_route(client, channel):
    if "/v1/describe" not in client.app.openapi()["paths"]:
        pytest.skip("route discovery (plumbing) is not present")
    assert ingest(client, [message(0)]).json()["inserted"] == 1
    r = client.get("/v1/describe")
    assert r.status_code == 200, r.text
    assert "levels" in {s["subject"] for s in r.json()["subjects"]}
    r = client.get("/v1/describe", params={"subject": "function:search"})
    assert r.status_code == 200 and r.json()["ranked_limit_arg"] == 2, r.text
    r = client.get("/v1/describe", params={"subject": "channel:synthetic.chat"})
    assert r.json()["messages"] == 1 and r.json()["maintenance"] is not None
    r = client.get("/v1/describe", params={"subject": "it's unknown"})
    assert r.status_code == 200 and r.json() is None, r.text
    r = client.post("/v1/sql", json={"statement": "SELECT api.describe('levels') AS d"})
    assert r.status_code == 200 and r.json()["rows"][0]["d"]["levels"], r.text
    r = client.post(
        "/v1/sql",
        json={"statement": "SELECT record_id FROM api.records_asof('2021-01-01Z')"},
    )
    assert r.status_code == 200 and r.json()["row_count"] == 1, r.text
    with connect() as c:
        row = c.execute(
            "SELECT statement FROM tl.audit_log WHERE statement LIKE '%api.describe(NULL)%'"
        ).fetchone()
    assert row is not None
    from throughline.db import create_token

    plugin = create_token("synthetic plugin", "plugin", "synthetic")["token"]
    r = client.get("/v1/describe", headers={"Authorization": "Bearer " + plugin})
    assert r.status_code == 403


def test_lib_describe_command(client, channel, monkeypatch):
    from typer.testing import CliRunner

    import throughline.cli as cli

    if "/v1/describe" not in client.app.openapi()["paths"]:
        pytest.skip("route discovery (plumbing) is not present")
    runner = CliRunner()
    if "describe" not in runner.invoke(cli.app, ["lib", "--help"]).output:
        pytest.skip("the lib command group (plumbing) is not present")

    def request(method, path, data=None, params=None):
        response = client.request(method, "/v1/" + path, json=data, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(cli, "request", request)
    r = runner.invoke(cli.app, ["lib", "describe", "function:search", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["name"] == "search"
    r = runner.invoke(cli.app, ["lib", "describe", "--json"])
    assert r.exit_code == 0 and "subjects" in json.loads(r.output), r.output
    r = runner.invoke(cli.app, ["lib", "describe", "missing", "--json"])
    assert r.exit_code == 0 and json.loads(r.output) is None, r.output
    assert runner.invoke(cli.app, ["lib", "describe", "levels"]).exit_code == 0


def test_describe_hidden_channel_is_null_for_restricted_asker(client):
    with connect() as c:
        gated = c.execute("SELECT to_regclass('tl.asker_grants') t").fetchone()["t"]
    if gated is None:
        pytest.skip("the asker gate is not present")
    register(client, "fake.shown")
    register(client, "fake.hidden")
    batch = [other(0, "fake.shown"), other(1, "fake.hidden"), other(2, "fake.hidden")]
    assert ingest(client, batch).json()["inserted"] == 3
    with connect() as c:
        c.execute("INSERT INTO tl.askers(name,mode) VALUES ('fake-asker','restricted')")
        c.execute(
            "INSERT INTO tl.asker_grants(asker,channel_id) SELECT 'fake-asker',id FROM tl.channels WHERE name='fake.shown'"
        )
    scope = {
        "throughline.scope_mode": "asker",
        "throughline.asker": "fake-asker",
        "throughline.unknown_asker": "deny",
    }
    try:
        assert describe("channel:fake.hidden") is not None
        assert describe("channel:fake.hidden", scope) is None
        assert describe("channel:fake.missing", scope) is None
        shown = describe("channel:fake.shown", scope)
        assert shown["messages"] == 1 and shown["maintenance"] is None
        assert describe(None, scope)["channels"] == ["fake.shown"]
        assert describe(None, {"throughline.scope_mode": "deny"})["channels"] == []
        rows = reader("SELECT channel FROM api.records", settings=scope)
        assert [r["channel"] for r in rows] == ["fake.shown"]
        rows = reader("SELECT channel FROM api.records_asof(now())", settings=scope)
        assert [r["channel"] for r in rows] == ["fake.shown"]
    finally:
        with connect() as c:
            c.execute("DELETE FROM tl.askers WHERE name='fake-asker'")
