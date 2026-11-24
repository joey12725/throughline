import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from conftest import ingest, message
from throughline.db import connect, create_token
from throughline.query import query, validate_sql
from throughline.reparse import register_mapper, reparse


def test_qry_2_review_channel_filters(client, channel):
    assert (
        client.post(
            "/v1/channels", json={"name": "synthetic.other", "plugin": "synthetic"}
        ).status_code
        == 200
    )
    assert (
        ingest(
            client, [message(0), message(1, channel="synthetic.other", fields={})]
        ).status_code
        == 200
    )
    with connect() as c:
        c.execute("INSERT INTO tl.embedding_models VALUES ('fake',3,true)")
        c.execute(
            "INSERT INTO tl.embeddings SELECT id,'fake',text_hash,'[1,1,1]'::vector FROM tl.conversations"
        )
        for call in (
            "api.search_filtered('orchard',20,NULL,%s)",
            "api.search_meaning('[1,1,1]',20,NULL,%s)",
            "api.search_fused('orchard','[1,1,1]',20,NULL,%s)",
        ):
            for channel_name, count in [("synthetic.other", 1), ("missing", 0)]:
                rows = c.execute("SELECT * FROM " + call, (channel_name,)).fetchall()
                assert len(rows) == count
                assert all(r["channel"] == channel_name for r in rows)


def test_lnk_3_msg_2_review_version_links_and_merged_placeholder(client, channel):
    link = {
        "type": "part_of",
        "from": {"channel": channel, "external_id": "fake-0"},
        "to": {"kind": "work", "external_ref": "fake-work"},
    }
    assert ingest(client, [message()], links=[link]).status_code == 200
    with connect() as c:
        old = c.execute("SELECT id FROM api.messages").fetchone()["id"]
        placeholder = c.execute("SELECT id FROM tl.entities").fetchone()["id"]
    target = client.post(
        "/v1/entities", json={"kind": "work", "name": "Synthetic work"}
    ).json()["id"]
    assert (
        client.post(
            "/v1/entities/merge", json={"source": placeholder, "target": target}
        ).status_code
        == 200
    )
    assert (
        ingest(client, [message(content="Synthetic edited")], links=[link]).status_code
        == 200
    )
    assert (
        ingest(client, [message(content="Synthetic edited")], links=[link]).status_code
        == 200
    )
    with connect() as c:
        current = c.execute("SELECT id FROM api.messages").fetchone()["id"]
        assert c.execute("SELECT count(*) n FROM tl.links").fetchone()["n"] == 1
        assert c.execute("SELECT from_id,to_id FROM tl.links").fetchone() == {
            "from_id": current,
            "to_id": target,
        }
        assert (
            c.execute("SELECT id FROM api.context(%s,0,0)", (old,)).fetchone()["id"]
            == current
        )
        assert (
            c.execute("SELECT id FROM api.follow('message',%s)", (old,)).fetchone()[
                "id"
            ]
            == target
        )


def test_msg_3_msg_6_review_noop_reparse_envelope(client, channel):
    original = message(
        happened_at="2020-01-01T12:00:00+05:30",
        author={
            "system": "fake",
            "kind": "account",
            "value": "fake-account",
            "display_name": "Synthetic author",
        },
        thread={
            "external_id": "fake-thread",
            "title": "Synthetic title",
            "metadata": {"synthetic": True},
        },
    )
    assert ingest(client, [original]).json()["inserted"] == 1
    register_mapper(channel, lambda payload: {})
    with connect("app") as c:
        assert reparse(c, channel)["unchanged"] == 1
    with connect() as c:
        assert (
            c.execute("SELECT happened_offset_min FROM api.messages").fetchone()[
                "happened_offset_min"
            ]
            == 330
        )
        assert c.execute("SELECT metadata FROM api.threads").fetchone()["metadata"] == {
            "synthetic": True
        }


def test_msg_4_review_move_and_append(client, channel):
    ingest(client, [message(0), message(1), message(2)])
    result = ingest(client, [message(1, thread={"external_id": "other"}), message(3)])
    assert result.status_code == 200, result.text
    with connect() as c:
        rows = c.execute(
            "SELECT external_id,thread_no FROM api.messages WHERE thread_external_id='fake-thread' ORDER BY thread_no"
        ).fetchall()
        assert rows == [
            {"external_id": "fake-0", "thread_no": 1},
            {"external_id": "fake-2", "thread_no": 2},
            {"external_id": "fake-3", "thread_no": 3},
        ]


@pytest.mark.parametrize("registry_first", [False, True])
def test_idn_2_idn_9_review_registry_import_order(client, channel, registry_first):
    person = client.post(
        "/v1/identity/people", json={"handle": "synthetic-person"}
    ).json()["id"]

    def registry():
        assert (
            client.post(
                "/v1/registry/schemes",
                json={
                    "system": "fake",
                    "kind": "email",
                    "same_system": None,
                    "same_kind": None,
                    "normalizer": "email",
                },
            ).status_code
            == 200
        )
        response = client.post(
            "/v1/registry/identifiers",
            json={
                "person_id": person,
                "system": "fake",
                "kind": "email",
                "value": " Fake@Example.invalid ",
                "value_norm": "wrong-client-norm",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["value_norm"] == "fake@example.invalid"

    row = message(
        author={"system": "fake", "kind": "email", "value": "Fake@Example.invalid"}
    )
    if registry_first:
        registry()
    assert ingest(client, [row]).status_code == 200
    if not registry_first:
        registry()
    assert ingest(client, [message(1, author=row["author"])]).status_code == 200
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.identifiers").fetchone()["n"] == 1
        assert all(
            r["person_id"] == person
            for r in c.execute("SELECT person_id FROM tl.messages")
        )


def test_idn_5_review_chained_merge_lifo_and_owner_rename(client):
    ids = [
        client.post(
            "/v1/identity/people", json={"handle": "synthetic-" + str(i)}
        ).json()["id"]
        for i in range(3)
    ]

    def merge(a, b):
        return client.post(
            "/v1/identity/merge", json={"source": a, "target": b}
        ).json()["id"]

    first = merge(*ids[:2])
    second = merge(*ids[1:])
    assert (
        client.post("/v1/identity/split", json={"event_id": first}).status_code == 400
    )
    assert (
        client.post(
            "/v1/identity/rename",
            json={"person_id": ids[2], "handle": "synthetic-renamed"},
        ).status_code
        == 200
    )
    assert (
        client.post("/v1/identity/split", json={"event_id": second}).status_code == 200
    )
    assert (
        client.post("/v1/identity/split", json={"event_id": first}).status_code == 200
    )
    with connect() as c:
        assert (
            c.execute("SELECT handle FROM tl.people WHERE id=%s", (ids[2],)).fetchone()[
                "handle"
            ]
            == "synthetic-renamed"
        )
    assert (
        client.post(
            "/v1/identity/merge", json={"source": ids[0], "target": ids[1]}
        ).status_code
        == 200
    )
    identifier = client.post(
        "/v1/registry/identifiers",
        json={
            "system": "fake",
            "kind": "account",
            "value": "synthetic-id",
            "value_norm": "synthetic-id",
        },
    ).json()["id"]
    assert (
        client.post(
            "/v1/identity/attach",
            json={"identifier_id": identifier, "person_id": ids[0]},
        ).status_code
        == 400
    )


def test_idn_5_acl_6_review_owner_protection(client):
    owner = client.post(
        "/v1/identity/people", json={"handle": "synthetic-owner", "is_owner": True}
    ).json()["id"]
    other = client.post(
        "/v1/identity/people", json={"handle": "synthetic-other"}
    ).json()["id"]
    token = create_token("synthetic-agent", kind="agent")["token"]
    client.headers["Authorization"] = "Bearer " + token
    assert (
        client.post(
            "/v1/identity/rename", json={"person_id": owner, "handle": "stolen"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/identity/merge", json={"source": owner, "target": other}
        ).status_code
        == 400
    )
    response = client.post(
        "/v1/registry/identifiers",
        json={
            "system": "fake",
            "kind": "account",
            "value": "fake",
            "value_norm": "fake",
            "asserted_by": "owner",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["asserted_by"] == "agent"


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT ('SELECT * FROM tl.conversations').ts_stat",
        "SELECT ts_stat('SELECT * FROM tl.messages')",
        "SELECT ('x').set_config",
    ],
)
def test_qry_1_review_attribute_escape_rejected(statement):
    with pytest.raises(ValueError):
        validate_sql(statement)


def test_qry_1_review_database_boundary(client):
    import psycopg

    for statement in (
        "SELECT * FROM tl.conversations",
        "SELECT * FROM ts_stat('SELECT * FROM tl.messages')",
        "SELECT set_config('default_transaction_read_only','off',false)",
        "SELECT lo_create(0)",
        "SELECT query_to_xml('SELECT * FROM tl.messages',false,false,'')",
        "SELECT pg_advisory_xact_lock(1)",
        "CREATE TEMP TABLE escape(id int)",
    ):
        with connect("reader") as c:
            with pytest.raises(psycopg.Error):
                c.execute(statement)
    assert (
        query(
            "SELECT jsonb_agg(repeat) FROM (SELECT content AS repeat FROM api.messages) s"
        )["row_count"]
        == 1
    )


def test_qry_1_review_byte_cap(client):
    # A single aggregate row can exceed the whole response budget. Generate
    # synthetic scalar values directly so this test isolates the query boundary.
    result = query(
        "SELECT array_agg(i) AS items FROM generate_series(1,700000) AS s(i)"
    )
    assert result["truncated"] and result["rows"] == []


def test_loc_3_loc_4_review_sparse_jitter_backfill(client, channel):
    at = datetime(2020, 1, 1, tzinfo=timezone.utc)

    def points(minutes, jitter=False):
        return [
            {
                "at": (at + timedelta(minutes=i)).isoformat(),
                "lat": 40.01 if jitter and i == 9 else 40.0,
                "lon": -75.0,
                "source": "synthetic",
            }
            for i in minutes
        ]

    assert (
        client.post(
            "/v1/ingest/track", json={"points": points(range(5, 20), True)}
        ).status_code
        == 200
    )
    assert client.post("/v1/track/detect-visits").json()["inserted"] == 1
    assert (
        client.post("/v1/ingest/track", json={"points": points(range(5))}).status_code
        == 200
    )
    assert client.post("/v1/track/detect-visits").status_code == 200
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM api.visits").fetchone()["n"] == 1
    assert (
        ingest(
            client, [message(happened_at=(at + timedelta(hours=6)).isoformat())]
        ).status_code
        == 200
    )
    assert (
        client.post("/v1/ingest/track", json={"points": points([355])}).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/ingest/track",
            json={
                "points": [
                    {
                        "at": (at + timedelta(hours=6)).isoformat(),
                        "lat": 41.0,
                        "lon": -75.0,
                        "source": "synthetic",
                    }
                ]
            },
        ).status_code
        == 200
    )
    with connect() as c:
        assert (
            c.execute(
                "SELECT lat FROM api.messages WHERE channel='synthetic.chat'"
            ).fetchone()["lat"]
            == 41.0
        )
    sparse = [
        {
            "at": (at + timedelta(days=2, minutes=i)).isoformat(),
            "lat": 42.0,
            "lon": -75.0,
            "source": "sparse",
        }
        for i in range(0, 181, 15)
    ]
    moving = [
        {
            "at": (at + timedelta(days=3, minutes=i)).isoformat(),
            "lat": 44 + i * 0.002,
            "lon": -75,
            "source": "moving",
        }
        for i in range(30)
    ]
    client.post("/v1/ingest/track", json={"points": sparse + moving})
    assert client.post("/v1/track/detect-visits").status_code == 200
    with connect() as c:
        assert (
            c.execute("SELECT count(*) n FROM api.visits WHERE lat=42").fetchone()["n"]
            == 1
        )
        detected = list(c.execute("SELECT lat FROM api.visits"))
        true_positive = sum(
            any(abs(r["lat"] - lat) < 0.0001 for r in detected) for lat in (40, 42)
        )
        false_positive = sum(
            all(abs(r["lat"] - lat) >= 0.0001 for lat in (40, 42)) for r in detected
        )
        assert true_positive / 2 >= 0.9
        assert false_positive / max(1, len(detected)) <= 0.05


def test_msg_4_review_random_backfill_60000(client, channel, record_property):
    # Shuffle only IDs, then generate each delivery batch. This matches a
    # streaming historical plugin and avoids retaining 60,000 request objects.
    # Generation is inside the measured interval, so the throughput threshold
    # remains at least as strict as a pre-built payload benchmark.
    count = 60000
    order = list(range(count))
    random.Random(481).shuffle(order)
    start = time.perf_counter()
    for i in range(0, count, 2000):
        rows = [
            message(j, thread={"external_id": f"synthetic-{j % 5}"})
            for j in order[i : i + 2000]
        ]
        result = ingest(client, rows)
        assert result.status_code == 200, result.text
        assert result.json()["inserted"] == 2000
        if (i + 2000) % 10000 == 0:
            print(
                f"Backfill {i + 2000}/60000: {(i + 2000) / (time.perf_counter() - start):.2f} messages/second",
                flush=True,
            )
    with connect("app") as c:
        c.execute("SELECT tl.refresh_derived()")
    rate = count / (time.perf_counter() - start)
    record_property("out_of_order_messages_per_second", rate)
    print(f"\nRandom-order backfill: {rate:.2f} messages/second")
    with connect() as c:
        bad = c.execute(
            "WITH expected AS (SELECT thread_no,row_number() OVER (PARTITION BY thread_id ORDER BY happened_at,external_id) n FROM api.messages) SELECT count(*) n FROM expected WHERE thread_no<>n"
        ).fetchone()["n"]
        assert bad == 0
        assert c.execute("SELECT count(*) n FROM api.messages").fetchone()["n"] == 60000
    assert rate >= 500


def test_acl_6_review_plugin_link_ownership_and_channel_names(client, channel):
    assert ingest(client, [message()]).status_code == 200
    entity = client.post("/v1/entities", json={"kind": "work"}).json()["id"]
    link = {
        "type": "part_of",
        "from": {"channel": channel, "external_id": "fake-0"},
        "to": {"kind": "entity", "id": entity},
        "method": "owner",
    }
    owner_link = client.post("/v1/links", json=link).json()["id"]
    raw = create_token("synthetic-plugin", "plugin", "synthetic")["token"]
    headers = {"Authorization": "Bearer " + raw}
    assert client.post("/v1/links", json=link, headers=headers).status_code == 403
    assert (
        client.patch(
            f"/v1/links/{owner_link}", json={"status": "removed"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/entities/merge",
            json={"source": entity, "target": 999},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/channels",
            json={"name": "visits", "plugin": "synthetic"},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/channels",
            json={"name": "synthetic." + "a" * 60, "plugin": "synthetic"},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/ingest/track",
            json={
                "points": [
                    {
                        "at": "2020-01-01T00:00:00Z",
                        "lat": 40,
                        "lon": -75,
                        "source": "synthetic",
                    }
                ]
            },
            headers=headers,
        ).status_code
        == 403
    )
    link["method"] = "source"
    created = client.post("/v1/links", json=link, headers=headers)
    assert created.status_code == 200, created.text
    assert (
        client.patch(
            f"/v1/links/{created.json()['id']}",
            json={"status": "removed"},
            headers=headers,
        ).status_code
        == 200
    )


def test_acl_6_acl_7_review_audit_tokens_append_only(client, channel):
    import psycopg

    assert ingest(client, [message()]).status_code == 200
    assert (
        client.post(
            "/v1/identity/people", json={"handle": "synthetic-audited"}
        ).status_code
        == 200
    )
    with connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.audit_log WHERE token_id IS NOT NULL AND statement LIKE 'POST /v1/%'"
            ).fetchone()["n"]
            >= 3
        )
        assert (
            c.execute(
                "SELECT token_id FROM tl.identity_events WHERE operation='create'"
            ).fetchone()["token_id"]
            is not None
        )
    for table in ("audit_log", "identity_events"):
        for operation in (
            "UPDATE tl." + table + " SET id=id",
            "DELETE FROM tl." + table,
        ):
            with connect("app") as c:
                with pytest.raises(psycopg.Error):
                    c.execute(operation)


def test_arc_5_review_purge_derived_copies(client, channel):
    from throughline.cli import purge

    sentinel = "syntheticpurgesentinel"
    original = message(
        content=sentinel, thread={"external_id": "fake-thread", "title": sentinel}
    )
    assert (
        ingest(
            client,
            [original],
            links=[
                {
                    "type": "part_of",
                    "from": {"channel": channel, "external_id": "fake-0"},
                    "to": {"kind": "work", "external_ref": "fake-work"},
                }
            ],
        ).status_code
        == 200
    )
    with connect() as c:
        c.execute(
            "INSERT INTO tl.embeddings SELECT id,'fake',text_hash,'[1,1,1]'::vector FROM tl.conversations"
        )
    purge(channel, confirm=True)
    with connect() as c:
        for table in (
            "conversations",
            "embeddings",
            "threads",
            "links",
            "messages",
            "raw_payloads",
            "message_envelopes",
        ):
            assert c.execute("SELECT count(*) n FROM tl." + table).fetchone()["n"] == 0


def test_emb_1_review_channel_settings_regroup_immediately(client, channel):
    assert ingest(client, [message(0), message(4000)]).status_code == 200
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.conversations").fetchone()["n"] == 2
    result = client.post(
        "/v1/channels",
        json={
            "name": channel,
            "plugin": "synthetic",
            "fields": [{"name": "rating", "type": "integer"}],
            "conversation_whole_thread": True,
        },
    )
    assert result.status_code == 200, result.text
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.conversations").fetchone()["n"] == 1


def test_migration_review_passwords_explicit_and_migration_does_not_rotate(
    client, monkeypatch
):
    from throughline.db import migrate, role_password

    monkeypatch.delenv("THROUGHLINE_DEV", raising=False)
    monkeypatch.delenv("THROUGHLINE_APP_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="PASSWORD is required"):
        migrate()
    with pytest.raises(ValueError, match="PASSWORD is required"):
        role_password("app")


def test_idn_5_review_person_response_authority(client):
    response = client.post(
        "/v1/identity/people", json={"handle": "synthetic-authority"}
    )
    assert response.status_code == 200
    assert response.json()["asserted_by"] == "owner"


def test_idn_5_review_owner_fact_protects_agent_created_person(client):
    owner_header = client.headers["Authorization"]
    agent_header = "Bearer " + create_token("synthetic-agent", kind="agent")["token"]
    client.headers["Authorization"] = agent_header
    first = client.post(
        "/v1/identity/people", json={"handle": "synthetic-first"}
    ).json()["id"]
    second = client.post(
        "/v1/identity/people", json={"handle": "synthetic-second"}
    ).json()["id"]
    client.headers["Authorization"] = owner_header
    assert (
        client.post(
            "/v1/registry/fact-keys", json={"key": "synthetic-city"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/registry/facts",
            json={
                "person_id": first,
                "key": "synthetic-city",
                "value": "Invented City",
            },
        ).status_code
        == 200
    )
    client.headers["Authorization"] = agent_header
    assert (
        client.post(
            "/v1/identity/merge", json={"source": first, "target": second}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/identity/rename", json={"person_id": first, "handle": "stolen"}
        ).status_code
        == 400
    )


def test_msg_3_msg_6_review_legacy_noop_reparse(client, channel):
    original = message(
        happened_at="2020-01-01T12:00:00+05:30",
        author={
            "system": "fake",
            "kind": "account",
            "value": "fake-account",
            "display_name": "Synthetic original",
        },
    )
    assert ingest(client, [original]).json()["inserted"] == 1
    with connect() as c:
        c.execute("DELETE FROM tl.message_envelopes")
    register_mapper(channel, lambda payload: {"content": original["content"]})
    with connect("app") as c:
        result = reparse(c, channel)
        assert (
            result["received"] == 1
            and result["unchanged"] == 1
            and result["new_versions"] == 0
        )
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.messages").fetchone()["n"] == 1
        assert (
            c.execute("SELECT happened_offset_min FROM tl.messages").fetchone()[
                "happened_offset_min"
            ]
            == 330
        )


def test_msg_3_msg_6_review_changed_reparse_keeps_source_envelope(client, channel):
    original = message(
        happened_at="2020-01-01T12:00:00+05:30",
        ended_at="2020-01-01T12:05:00+05:30",
        author={
            "system": "fake",
            "kind": "account",
            "value": "fake-account",
            "display_name": "Synthetic original",
        },
    )
    assert ingest(client, [original]).json()["inserted"] == 1
    register_mapper(channel, lambda payload: {"content": "Synthetic edited"})
    with connect("app") as c:
        assert reparse(c, channel)["new_versions"] == 1
    with connect() as c:
        row = c.execute(
            "SELECT m.happened_offset_min,e.envelope FROM api.messages m JOIN tl.message_envelopes e ON e.message_id=m.id"
        ).fetchone()
        assert row["happened_offset_min"] == 330
        assert row["envelope"]["author"]["display_name"] == "Synthetic original"
        assert row["envelope"]["ended_at"].endswith("+05:30")


def test_loc_4_review_backfill_can_invalidate_a_stop(client):
    at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    endpoints = [
        {
            "at": (at + timedelta(minutes=i)).isoformat(),
            "lat": 40,
            "lon": -75,
            "source": "synthetic",
        }
        for i in (0, 20)
    ]
    assert (
        client.post("/v1/ingest/track", json={"points": endpoints}).status_code == 200
    )
    assert client.post("/v1/track/detect-visits").json()["inserted"] == 1
    moving = [
        {
            "at": (at + timedelta(minutes=i)).isoformat(),
            "lat": 40 + i * 0.01,
            "lon": -75,
            "source": "synthetic",
        }
        for i in range(1, 20)
    ]
    assert client.post("/v1/ingest/track", json={"points": moving}).status_code == 200
    result = client.post("/v1/track/detect-visits")
    assert result.status_code == 200, result.text
    assert result.json()["retired"] == 1
    assert client.post("/v1/track/detect-visits").json()["retired"] == 0
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM api.visits").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) n FROM tl.messages").fetchone()["n"] == 2


def test_idn_5_idn_6_review_thread_metadata_cannot_override_owner(client, channel):
    assert ingest(client, [message()]).status_code == 200
    with connect() as c:
        thread_id = c.execute("SELECT id FROM tl.threads").fetchone()["id"]
    assert (
        client.post("/v1/identity/ignore", json={"thread_id": thread_id}).status_code
        == 200
    )
    source = {"ignored": False, "asserted_by": "owner", "synthetic": "updated"}
    assert (
        ingest(
            client, [message(thread={"external_id": "fake-thread", "metadata": source})]
        ).status_code
        == 200
    )
    with connect() as c:
        metadata = c.execute(
            "SELECT metadata FROM api.threads WHERE id=%s", (thread_id,)
        ).fetchone()["metadata"]
        assert metadata["ignored"] is True and metadata["asserted_by"] == "owner"
        assert metadata["source_metadata"] == source
    agent = create_token("synthetic-agent", kind="agent")["token"]
    headers = {"Authorization": "Bearer " + agent}
    assert (
        client.post(
            "/v1/identity/ignore", json={"thread_id": thread_id}, headers=headers
        ).status_code
        == 400
    )
    assert (
        ingest(
            client,
            [message(1, thread={"external_id": "synthetic-spoof", "metadata": source})],
        ).status_code
        == 200
    )
    with connect() as c:
        fresh = c.execute(
            "SELECT id,metadata FROM tl.threads WHERE external_id='synthetic-spoof'"
        ).fetchone()
        assert "asserted_by" not in fresh["metadata"]
    assert (
        client.post(
            "/v1/identity/ignore", json={"thread_id": fresh["id"]}, headers=headers
        ).status_code
        == 200
    )
