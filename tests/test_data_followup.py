"""Follow-up regressions: deferred thread maintenance, conversations, location
sources and visits, and merge/split attribution. Synthetic data only."""

import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from conftest import ingest, message
from typer.testing import CliRunner

import throughline.derived as derived
import throughline.embeddings as worker
from throughline.cli import app
from throughline.db import connect, create_token
from throughline.ingest import ingest as core_ingest
from throughline.ingest import register_channel
from throughline.location import detect_visits, retire_track
from throughline.models import Ingest

AT = datetime(2020, 1, 1, tzinfo=timezone.utc)


def sql(client, statement):
    r = client.post("/v1/sql", json={"statement": statement})
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def point(seconds, lat=40.0, lon=-75.0, source="synthetic", **extra):
    return dict(
        at=(AT + timedelta(seconds=seconds)).isoformat(),
        lat=lat,
        lon=lon,
        source=source,
        **extra,
    )


def post_points(client, points):
    r = client.post("/v1/ingest/track", json={"points": points})
    assert r.status_code == 200, r.text
    return r.json()


def large_thread(client, count=300):
    r = ingest(client, [message(i) for i in range(count)])
    assert r.status_code == 200 and r.json()["inserted"] == count, r.text


def dirty_count():
    with connect() as c:
        return c.execute("SELECT count(*) n FROM tl.dirty_threads").fetchone()["n"]


def channel_id(name="synthetic.chat"):
    with connect() as c:
        return c.execute(
            "SELECT id FROM tl.channels WHERE name=%s", (name,)
        ).fetchone()["id"]


# --- identity -------------------------------------------------------------


def test_idn_5_followup_split_restores_message_attribution_exactly(client, channel):
    p = client.post("/v1/identity/people", json={"handle": "fake-a"}).json()["id"]
    q = client.post("/v1/identity/people", json={"handle": "fake-b"}).json()["id"]
    other = dict(system="fake", kind="account", value="fake-other")
    ingest(
        client,
        [
            message(0),
            message(1),
            message(2, author=other),
            message(3, author=None),
            message(4, author=dict(system="fake", kind="account", value="fake-nobody")),
        ],
    )
    identifiers = {
        r["value"]: r["id"] for r in client.get("/v1/identity/unclaimed").json()
    }
    for value, person in (("fake-account", p), ("fake-other", q)):
        r = client.post(
            "/v1/identity/attach",
            json={"identifier_id": identifiers[value], "person_id": person},
        )
        assert r.status_code == 200, r.text
    with connect() as c:
        # An attribution that no identifier explains must survive too.
        c.execute(
            "UPDATE tl.messages SET person_id=%s WHERE external_id='fake-3'", (p,)
        )

    def attribution():
        with connect() as c:
            return {
                r["external_id"]: r["person_id"]
                for r in c.execute("SELECT external_id,person_id FROM tl.messages")
            }

    before = attribution()
    assert before == {
        "fake-0": p,
        "fake-1": p,
        "fake-2": q,
        "fake-3": p,
        "fake-4": None,
    }
    merge = client.post("/v1/identity/merge", json={"source": p, "target": q})
    assert merge.status_code == 200, merge.text
    # Attributed after the merge, through the source person's identifier.
    assert ingest(client, [message(5)]).json()["inserted"] == 1
    merged = attribution()
    assert all(
        merged[k] == q for k in ("fake-0", "fake-1", "fake-2", "fake-3", "fake-5")
    )
    with connect() as c:
        detail = c.execute(
            "SELECT detail FROM tl.identity_events WHERE id=%s", (merge.json()["id"],)
        ).fetchone()["detail"]
        fake_3 = c.execute(
            "SELECT id FROM tl.messages WHERE external_id='fake-3'"
        ).fetchone()["id"]
    # The snapshot lists only what identifiers cannot restore, not every message.
    assert detail["messages"] == [{"id": fake_3, "person_id": p}]
    r = client.post("/v1/identity/split", json={"event_id": merge.json()["id"]})
    assert r.status_code == 200, r.text
    assert attribution() == {**before, "fake-5": p}


def test_idn_5_followup_split_replays_a_full_legacy_snapshot(client, channel):
    # Events written before the snapshot was reduced list every message.
    p = client.post("/v1/identity/people", json={"handle": "fake-a"}).json()["id"]
    q = client.post("/v1/identity/people", json={"handle": "fake-b"}).json()["id"]
    ingest(client, [message(i, author=None) for i in range(3)])
    with connect() as c:
        c.execute("UPDATE tl.messages SET person_id=%s", (p,))
    merge = client.post("/v1/identity/merge", json={"source": p, "target": q})
    assert merge.status_code == 200, merge.text
    with connect() as c:
        assert {
            r["person_id"] for r in c.execute("SELECT person_id FROM tl.messages")
        } == {q}
    assert (
        client.post(
            "/v1/identity/split", json={"event_id": merge.json()["id"]}
        ).status_code
        == 200
    )
    with connect() as c:
        assert {
            r["person_id"] for r in c.execute("SELECT person_id FROM tl.messages")
        } == {p}


# --- location sources -----------------------------------------------------


def test_loc_followup_location_source_capability(client, channel):
    owner = client.headers["Authorization"]
    approved = create_token("synthetic tracker", "plugin", "tracker", None, True)
    plain = create_token("synthetic plain", "plugin", "tracker")
    client.headers["Authorization"] = "Bearer " + plain["token"]
    r = client.post("/v1/ingest/track", json={"points": [point(0, source="tracker")]})
    assert r.status_code == 403, r.text
    client.headers["Authorization"] = "Bearer " + approved["token"]
    r = client.post("/v1/ingest/track", json={"points": [point(0, source="other")]})
    assert r.status_code == 403, r.text
    r = client.post("/v1/ingest/track", json={"points": [point(0, source="tracker")]})
    assert r.status_code == 200 and r.json()["received"] == 1, r.text
    client.headers["Authorization"] = owner
    with connect() as c:
        assert [
            r["source"] for r in c.execute("SELECT source FROM tl.location_track")
        ] == ["tracker"]


def located(external_id="fake-0"):
    with connect() as c:
        return c.execute(
            "SELECT m.lat,m.lon,m.loc_method,m.loc_reason,l.track_id,l.source FROM api.messages m LEFT JOIN api.message_locations l ON l.message_id=m.id WHERE m.external_id=%s",
            (external_id,),
        ).fetchone()


def track_id(source):
    with connect() as c:
        return c.execute(
            "SELECT id FROM tl.location_track WHERE source=%s ORDER BY id", (source,)
        ).fetchone()["id"]


def test_loc_followup_nearer_point_replaces_and_records_provenance(client, channel):
    assert ingest(client, [message(0)]).status_code == 200
    assert post_points(client, [point(100, lat=10, source="far")])["attached"] == 1
    row = located()
    assert (row["lat"], row["loc_method"], row["source"]) == (10, "track", "far")
    assert row["track_id"] == track_id("far")
    assert post_points(client, [point(10, lat=20, source="near")])["attached"] == 1
    row = located()
    assert (row["lat"], row["source"]) == (20, "near")
    assert row["track_id"] == track_id("near")
    # A farther point arriving later changes nothing.
    assert post_points(client, [point(-200, lat=30, source="older")])["attached"] == 0
    assert located()["track_id"] == track_id("near")
    # A message ingested afterwards records its point as well.
    assert ingest(client, [message(90)]).status_code == 200
    assert located("fake-90")["source"] == "far"


def test_loc_followup_owner_retires_bad_point_and_source(client, channel):
    assert ingest(client, [message(0)]).status_code == 200
    post_points(
        client, [point(0, lat=66, source="bad"), point(60, lat=20, source="good")]
    )
    assert located()["source"] == "bad"
    # Only the database owner can withdraw: runtime roles cannot write the table.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with connect("app") as c:
            c.execute(
                "INSERT INTO tl.retired_track(track_id) VALUES (%s)", (track_id("bad"),)
            )
    with connect() as c:
        result = retire_track(c, source="bad", reason="synthetic spoof")
    assert result["points"] == 1 and result["cleared"] == 1 and result["attached"] == 1
    row = located()
    assert (row["lat"], row["source"]) == (20, "good")
    # Late attachment and new messages both ignore the withdrawn point.
    post_points(client, [point(500, lat=30, source="good")])
    assert located()["lat"] == 20
    assert ingest(client, [message(1)]).status_code == 200
    assert located("fake-1")["source"] == "good"
    # The point itself is kept as ground truth.
    with connect() as c:
        assert (
            c.execute("SELECT count(*) n FROM tl.location_track").fetchone()["n"] == 3
        )
    # Withdrawing the only remaining points leaves an explicit reason.
    result = CliRunner().invoke(app, ["track", "retire", "--source", "good", "--json"])
    assert result.exit_code == 0, result.output
    row = located()
    assert row["lat"] is None and row["loc_method"] is None and row["track_id"] is None
    assert row["loc_reason"] == "no_track_point_within_tolerance"
    assert ingest(client, [message(2)]).status_code == 200
    assert located("fake-2")["lat"] is None
    with connect() as c:
        restored = retire_track(c, ids=[track_id("bad")], restore=True)
    assert restored["changed"] == 1
    assert located()["source"] == "bad"
    assert CliRunner().invoke(app, ["track", "retire"]).exit_code != 0


def test_loc_followup_retired_points_leave_stop_detection(client):
    post_points(client, [point(i * 60, source="bad") for i in range(16)])
    assert client.post("/v1/track/detect-visits").json()["inserted"] == 1
    with connect() as c:
        retire_track(c, source="bad")
    assert client.post("/v1/track/detect-visits").json()["retired"] == 1
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM api.visits").fetchone()["n"] == 0


# --- visits ---------------------------------------------------------------


def visits():
    with connect() as c:
        return c.execute(
            "SELECT external_id,happened_at,ended_at,version FROM api.visits ORDER BY happened_at"
        ).fetchall()


def test_loc_4_followup_sparse_stationary_reports_are_one_stay(client, monkeypatch):
    # Overnight at one place with two reports, and a tracker sampling every 61
    # minutes: both are stays.
    post_points(
        client,
        [point(0, source="overnight"), point(8 * 3600, source="overnight")]
        + [point(i * 61 * 60, lat=50, source="hourly") for i in range(4)],
    )
    r = client.post("/v1/track/detect-visits")
    assert r.status_code == 200 and r.json()["inserted"] == 2, r.text
    rows = visits()
    assert sorted((r["ended_at"] - r["happened_at"]).total_seconds() for r in rows) == [
        3 * 61 * 60,
        8 * 3600,
    ]
    # The silence allowed inside a stay is a setting.
    monkeypatch.setenv("THROUGHLINE_VISIT_MAX_GAP_S", "3600")
    assert client.post("/v1/track/detect-visits").json()["retired"] == 2
    assert visits() == []
    with connect() as c:
        with pytest.raises(ValueError):
            detect_visits(c, max_gap=0)


def two_stays():
    # Two places about 150 m apart, five samples each.
    return [point(i * 300) for i in range(5)] + [
        point(1500 + i * 300, lat=40.00135) for i in range(5)
    ]


def test_loc_4_followup_stays_merge_then_split_again(client):
    post_points(client, two_stays())
    first = client.post("/v1/track/detect-visits?radius_m=100")
    assert first.status_code == 200 and first.json()["inserted"] == 2, first.text
    original = [r["external_id"] for r in visits()]
    assert len(set(original)) == 2
    merged = client.post("/v1/track/detect-visits?radius_m=300")
    assert merged.status_code == 200 and merged.json()["retired"] == 1, merged.text
    assert [r["external_id"] for r in visits()] == original[:1]
    split = client.post("/v1/track/detect-visits?radius_m=100")
    assert split.status_code == 200, split.text
    rows = visits()
    # Both stays are back under the ids they always had.
    assert [r["external_id"] for r in rows] == original
    assert [(r["ended_at"] - r["happened_at"]).total_seconds() for r in rows] == [
        1200,
        1200,
    ]
    again = client.post("/v1/track/detect-visits?radius_m=100").json()
    assert (again["inserted"], again["new_versions"], again["retired"]) == (0, 0, 0)
    with connect() as c:
        # A visit is versioned under its own id only, never cross-linked.
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.messages old JOIN tl.messages new ON new.id=old.superseded_by WHERE old.external_id<>new.external_id"
            ).fetchone()["n"]
            == 0
        )


def test_loc_4_followup_every_stay_keeps_a_distinct_id(client):
    minute = 60
    post_points(client, [point(i * minute) for i in range(10, 41)])
    assert client.post("/v1/track/detect-visits").json()["inserted"] == 1
    late_start = visits()[0]["external_id"]
    post_points(client, [point(i * minute) for i in range(-5, 10)])
    extended = client.post("/v1/track/detect-visits").json()
    assert (extended["inserted"], extended["new_versions"]) == (0, 1)
    assert [r["external_id"] for r in visits()] == [late_start]
    # Two samples a kilometre away cut the stay in two. The later half begins
    # at the point the visit was named after.
    post_points(
        client, [point(9.3 * minute, lat=40.01), point(9.6 * minute, lat=40.01)]
    )
    r = client.post("/v1/track/detect-visits")
    assert r.status_code == 200, r.text
    rows = visits()
    assert len(rows) == 2 and len({r["external_id"] for r in rows}) == 2
    assert rows[1]["external_id"] == late_start
    assert [(r["ended_at"] - r["happened_at"]).total_seconds() for r in rows] == [
        14 * minute,
        30 * minute,
    ]
    assert client.post("/v1/track/detect-visits").json()["new_versions"] == 0


def test_loc_4_followup_cross_linked_history_does_not_block_detection(client):
    # An earlier reconciliation superseded one visit by a different one, leaving
    # an external id with history but no current row.
    post_points(client, two_stays())
    assert client.post("/v1/track/detect-visits?radius_m=100").json()["inserted"] == 2
    with connect() as c:
        rows = c.execute("SELECT id FROM api.visits ORDER BY happened_at").fetchall()
        c.execute(
            "UPDATE tl.messages SET superseded_by=%s WHERE id=%s",
            (rows[0]["id"], rows[1]["id"]),
        )
    r = client.post("/v1/track/detect-visits?radius_m=100")
    assert r.status_code == 200, r.text
    rows = visits()
    assert len(rows) == 2 and len({r["external_id"] for r in rows}) == 2


# --- deferred thread maintenance -------------------------------------------


def test_msg_4_followup_read_does_not_wait_for_ingest_lock(client, channel):
    large_thread(client)
    assert dirty_count() == 1
    cid = channel_id()
    release = threading.Event()
    held = threading.Event()

    def hold():
        # Stands in for a long ingest or reparse transaction on the channel.
        with psycopg.connect(__import__("os").environ["THROUGHLINE_DATABASE_URL"]) as c:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (cid,))
            held.set()
            release.wait(60)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(10)
    done = {}

    def read():
        start = time.perf_counter()
        done["rows"] = sql(
            client,
            "SELECT thread_no,conversation_id FROM api.search('orchard',NULL) ORDER BY happened_at",
        )
        done["conversations"] = sql(
            client, "SELECT count(*) AS n FROM api.conversations"
        )
        done["seconds"] = time.perf_counter() - start

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(20)
    blocked = reader.is_alive()
    release.set()
    holder.join(10)
    reader.join(60)
    assert not blocked, "read waited for the ingest lock"
    # Exact numbers, and no conversation rather than a stale one.
    assert [r["thread_no"] for r in done["rows"]] == list(range(1, 301))
    assert {r["conversation_id"] for r in done["rows"]} == {None}
    assert done["conversations"][0]["n"] == 0
    assert dirty_count() == 1
    # Once the channel is free the next read materializes.
    rows = sql(
        client,
        "SELECT thread_no,conversation_id,first_thread_no,last_thread_no FROM api.search('orchard',NULL)",
    )
    assert dirty_count() == 0
    assert all(
        r["conversation_id"] is not None
        and r["first_thread_no"] <= r["thread_no"] <= r["last_thread_no"]
        for r in rows
    )


def test_msg_4_followup_read_survives_failing_or_slow_maintenance(
    client, channel, monkeypatch
):
    large_thread(client)

    def broken(wait=False):
        raise RuntimeError("synthetic maintenance failure")

    monkeypatch.setattr(derived, "materialize", broken)
    assert len(sql(client, "SELECT id FROM api.messages")) == 300
    started = threading.Event()
    finish = threading.Event()

    def slow(wait=False):
        started.set()
        finish.wait(30)
        return dict(done=0, skipped=0)

    monkeypatch.setattr(derived, "materialize", slow)
    monkeypatch.setenv("THROUGHLINE_READ_MATERIALIZE_WAIT_S", "0.2")
    begin = time.perf_counter()
    assert len(sql(client, "SELECT id FROM api.messages")) == 300
    elapsed = time.perf_counter() - begin
    finish.set()
    assert started.is_set() and elapsed < 10
    # Let the background pass end before the next test replaces the data.
    assert derived.materialize_for_read(timeout=30) or True


def test_emb_1_followup_direct_reads_hide_stale_conversations(client, channel):
    large_thread(client)
    assert sql(client, "SELECT count(*) AS n FROM api.conversations")[0]["n"] > 0
    assert dirty_count() == 0
    # One message at the very start shifts every stored number and span.
    early = (AT - timedelta(days=1)).isoformat()
    assert ingest(client, [message(999, happened_at=early)]).json()["inserted"] == 1
    assert dirty_count() == 1
    with connect("reader") as c:
        rows = c.execute(
            "SELECT external_id,thread_no,conversation_id FROM api.search('orchard',NULL) ORDER BY thread_no"
        ).fetchall()
        assert [r["thread_no"] for r in rows] == list(range(1, 302))
        assert rows[0]["external_id"] == "fake-999"
        assert {r["conversation_id"] for r in rows} == {None}
        assert (
            c.execute("SELECT count(*) n FROM api.conversations").fetchone()["n"] == 0
        )
    assert derived.materialize() == dict(done=1, skipped=0)
    with connect("reader") as c:
        rows = c.execute(
            "SELECT thread_no,conversation_id,first_thread_no,last_thread_no FROM api.search('orchard',NULL)"
        ).fetchall()
    assert all(
        r["conversation_id"] is not None
        and r["first_thread_no"] <= r["thread_no"] <= r["last_thread_no"]
        for r in rows
    )


def test_emb_1_followup_rebuild_conversations_on_deferred_threads(client, channel):
    large_thread(client)
    with connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.messages WHERE thread_no IS NULL"
            ).fetchone()["n"]
            == 300
        )
    result = CliRunner().invoke(app, ["rebuild-conversations"])
    assert result.exit_code == 0, result.output
    assert dirty_count() == 0
    with connect() as c:
        spans = c.execute(
            "SELECT first_thread_no,last_thread_no,content FROM tl.conversations ORDER BY chunk_no"
        ).fetchall()
        assert spans[0]["first_thread_no"] == 1 and spans[-1]["last_thread_no"] == 300
        assert spans[0]["content"].startswith("Synthetic orchard message 0\n")
        numbers = c.execute(
            "SELECT thread_no FROM tl.messages ORDER BY happened_at"
        ).fetchall()
        assert [r["thread_no"] for r in numbers] == list(range(1, 301))


def test_emb_1_followup_regroup_rewrites_only_changed_chunks(client, channel):
    assert ingest(client, [message(i * 4000) for i in range(3)]).status_code == 200

    def stored():
        with connect() as c:
            return {
                r["chunk_no"]: r["ctid"]
                for r in c.execute("SELECT chunk_no,ctid::text FROM tl.conversations")
            }

    before = stored()
    assert len(before) == 3
    assert ingest(client, [message(12000)]).status_code == 200
    after = stored()
    assert len(after) == 4
    assert {k: after[k] for k in before} == before


def test_emb_1_followup_register_channel_regroups_under_the_ingest_lock(
    client, channel
):
    assert ingest(client, [message(0)]).status_code == 200
    settings = dict(
        name=channel,
        plugin="synthetic",
        fields=[dict(name="rating", type="integer", description="Synthetic score")],
        conversation_gap_s=5000,
    )
    finished = threading.Event()

    def register():
        with connect("app") as c:
            register_channel(c, settings)
        finished.set()

    with connect("app") as c:
        result = core_ingest(c, Ingest(plugin="synthetic", messages=[message(10)]))
        assert result["inserted"] == 1
        thread = threading.Thread(target=register, daemon=True)
        thread.start()
        # The uncommitted batch holds the channel; the regroup must wait for it.
        assert not finished.wait(1.5)
    assert finished.wait(30)
    thread.join(10)
    with connect() as c:
        rows = c.execute("SELECT content FROM tl.conversations").fetchall()
    assert [r["content"] for r in rows] == [
        "Synthetic orchard message 0\nSynthetic orchard message 10"
    ]


def embedding_settings(monkeypatch):
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "synthetic-v1")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "3")


def test_emb_2_followup_worker_holds_no_locks_while_embedding(
    client, channel, monkeypatch
):
    embedding_settings(monkeypatch)
    large_thread(client)
    assert dirty_count() == 1
    cid = channel_id()
    seen = {}

    def embed(texts, model=None, dimensions=None):
        # What an ingest arriving during the request would need.
        with connect() as c:
            seen["channel_free"] = c.execute(
                "SELECT pg_try_advisory_xact_lock(%s) ok", (cid,)
            ).fetchone()["ok"]
            seen["open_transactions"] = c.execute(
                "SELECT count(*) n FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND state LIKE 'idle in transaction%'"
            ).fetchone()["n"]
            c.execute("SELECT id FROM tl.conversations FOR UPDATE NOWAIT")
        r = ingest(client, [message(5000)])
        seen["ingested"] = r.status_code == 200 and r.json()["inserted"] == 1
        return [[1.0, 0.0, 0.0] for _ in texts], 7

    monkeypatch.setattr(worker, "embed", embed)
    count = worker.run_once()
    assert count > 0
    assert seen == dict(channel_free=True, open_transactions=0, ingested=True)
    with connect() as c:
        # Chunks rewritten by the ingest during the request are not stored
        # against text they no longer have.
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.embeddings e JOIN tl.conversations c ON c.id=e.conversation_id WHERE c.text_hash<>e.text_hash"
            ).fetchone()["n"]
            == 0
        )
        assert (
            c.execute("SELECT count(*) n FROM tl.embedding_ledger").fetchone()["n"] == 1
        )
        # The worker's own lock is released for the next run.
        assert c.execute("SELECT pg_try_advisory_xact_lock(71824009) ok").fetchone()[
            "ok"
        ]


def test_emb_2_followup_worker_skips_stale_conversations(client, channel, monkeypatch):
    embedding_settings(monkeypatch)
    large_thread(client)
    assert derived.materialize() == dict(done=1, skipped=0)
    early = (AT - timedelta(days=1)).isoformat()
    assert ingest(client, [message(999, happened_at=early)]).json()["inserted"] == 1
    texts_seen = []

    def embed(texts, model=None, dimensions=None):
        texts_seen.extend(texts)
        return [[1.0, 0.0, 0.0] for _ in texts], 1

    monkeypatch.setattr(worker, "embed", embed)
    # The channel is busy, so the thread cannot be materialized: its stored
    # (older) conversations are not sent for embedding.
    monkeypatch.setattr(worker, "materialize", lambda wait=False: None)
    assert worker.run_once() == 0 and texts_seen == []
    monkeypatch.undo()
    embedding_settings(monkeypatch)
    monkeypatch.setattr(worker, "embed", embed)
    assert worker.run_once() > 0
    assert texts_seen[0] == "Synthetic orchard message 999"


def test_emb_2_followup_worker_loop_survives_an_outage(monkeypatch):
    embedding_settings(monkeypatch)
    calls = []

    class Stop(Exception):
        pass

    def failing():
        calls.append(1)
        raise ValueError("synthetic model outage")

    def sleep(seconds):
        if len(calls) == 2:
            raise Stop

    monkeypatch.setattr(worker, "run_once", failing)
    monkeypatch.setattr(worker.time, "sleep", sleep)
    with pytest.raises(Stop):
        worker.run()
    assert len(calls) == 2
    with pytest.raises(ValueError, match="synthetic model outage"):
        worker.run(once=True)


# --- checker follow-up ------------------------------------------------------


def active_models():
    with connect() as c:
        return [
            r["model"]
            for r in c.execute(
                "SELECT model FROM tl.embedding_models WHERE active ORDER BY model"
            )
        ]


def test_emb_2_followup_model_not_activated_over_hidden_conversations(
    client, channel, monkeypatch
):
    embedding_settings(monkeypatch)
    large_thread(client)
    assert derived.materialize() == dict(done=1, skipped=0)

    def embed(texts, model=None, dimensions=None):
        return [[1.0, 0.0, 0.0] for _ in texts], 1

    monkeypatch.setattr(worker, "embed", embed)
    assert worker.run_once() > 0
    assert active_models() == ["synthetic-v1"]
    # A model switch while the thread awaits maintenance on a busy channel.
    early = (AT - timedelta(days=1)).isoformat()
    assert ingest(client, [message(999, happened_at=early)]).json()["inserted"] == 1
    assert dirty_count() == 1
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "synthetic-v2")
    monkeypatch.setattr(worker, "materialize", lambda wait=False: None)
    assert worker.run_once() == 0
    with connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.embeddings WHERE model='synthetic-v2'"
            ).fetchone()["n"]
            == 0
        )
    # Nothing visible is missing, yet nothing is covered: the old model stays.
    assert active_models() == ["synthetic-v1"]
    monkeypatch.setattr(worker, "materialize", derived.materialize)
    while worker.run_once():
        pass
    assert dirty_count() == 0
    assert active_models() == ["synthetic-v2"]


def test_emb_2_followup_first_model_waits_for_dirty_threads(
    client, channel, monkeypatch
):
    embedding_settings(monkeypatch)
    large_thread(client)
    assert dirty_count() == 1
    monkeypatch.setattr(
        worker, "embed", lambda texts, *a: ([[1.0, 0.0, 0.0] for _ in texts], 1)
    )
    monkeypatch.setattr(worker, "materialize", lambda wait=False: None)
    assert worker.run_once() == 0
    assert active_models() == []


def test_loc_followup_retire_reaches_messages_without_provenance(client, channel):
    def versions():
        with connect() as c:
            return c.execute(
                "SELECT m.lat,m.loc_method,m.loc_reason,m.superseded_by IS NULL current,t.source FROM tl.messages m LEFT JOIN tl.message_locations ml ON ml.message_id=m.id LEFT JOIN tl.location_track t ON t.id=ml.track_id WHERE m.external_id='fake-0' ORDER BY m.id"
            ).fetchall()

    post_points(client, [point(30, lat=66, source="bad")])
    assert ingest(client, [message(0)]).json()["inserted"] == 1
    assert (
        ingest(client, [message(0, content="Synthetic edit")]).json()["new_versions"]
        == 1
    )
    # A nearer good point moves the current version only.
    post_points(client, [point(10, lat=20, source="good")])
    assert [(r["lat"], r["current"]) for r in versions()] == [(66, False), (20, True)]
    # A database upgraded from before provenance was recorded.
    with connect() as c:
        c.execute("DELETE FROM tl.message_locations")
    with connect() as c:
        result = retire_track(c, source="bad", reason="synthetic spoof")
    assert result["cleared"] == 1, result
    old, new = versions()
    assert (old["lat"], old["loc_method"], old["source"]) == (None, None, None)
    assert old["loc_reason"] == "no_track_point_within_tolerance"
    # The version placed by the good point keeps it, now with provenance.
    assert (new["lat"], new["loc_method"], new["source"]) == (20, "track", "good")


def test_loc_followup_rerun_repairs_an_earlier_incomplete_retire(client, channel):
    assert ingest(client, [message(0)]).status_code == 200
    post_points(client, [point(0, lat=66, source="bad")])
    assert located()["source"] == "bad"
    with connect() as c:
        # What a withdrawal that could not see the message left behind.
        c.execute("DELETE FROM tl.message_locations")
        c.execute(
            "INSERT INTO tl.retired_track(track_id) VALUES (%s)", (track_id("bad"),)
        )
    assert located()["lat"] == 66
    with connect() as c:
        result = retire_track(c, source="bad")
    assert (result["changed"], result["cleared"]) == (0, 1)
    row = located()
    assert row["lat"] is None and row["loc_method"] is None and row["track_id"] is None


def test_loc_followup_location_source_granted_to_existing_token(client, channel):
    import json

    plain = create_token("synthetic plain", "plugin", "tracker")
    agent = create_token("synthetic agent", "agent")
    headers = {"Authorization": "Bearer " + plain["token"]}
    body = {"points": [point(0, source="tracker")]}
    assert (
        client.post("/v1/ingest/track", json=body, headers=headers).status_code == 403
    )
    runner = CliRunner()
    granted = runner.invoke(
        app, ["token", "location-source", str(plain["id"]), "--json"]
    )
    assert granted.exit_code == 0, granted.output
    assert json.loads(granted.output) == dict(
        id=plain["id"], name="synthetic plain", plugin="tracker", location_source=True
    )
    # The same token, not a reissued one, may now write its own source only.
    r = client.post("/v1/ingest/track", json=body, headers=headers)
    assert r.status_code == 200 and r.json()["received"] == 1, r.text
    other = {"points": [point(0, source="other")]}
    assert (
        client.post("/v1/ingest/track", json=other, headers=headers).status_code == 403
    )
    listed = json.loads(runner.invoke(app, ["token", "list", "--json"]).output)
    assert {t["id"]: t["location_source"] for t in listed}[plain["id"]] is True
    withdrawn = runner.invoke(
        app, ["token", "location-source", str(plain["id"]), "--off"]
    )
    assert withdrawn.exit_code == 0, withdrawn.output
    later = {"points": [point(60, source="tracker")]}
    assert (
        client.post("/v1/ingest/track", json=later, headers=headers).status_code == 403
    )
    # Only a live plugin token can hold the capability.
    assert (
        runner.invoke(app, ["token", "location-source", str(agent["id"])]).exit_code
        != 0
    )
    assert runner.invoke(app, ["token", "location-source", "999999"]).exit_code != 0
    assert runner.invoke(app, ["token", "revoke", str(plain["id"])]).exit_code == 0
    assert (
        runner.invoke(app, ["token", "location-source", str(plain["id"])]).exit_code
        != 0
    )
    with connect() as c:
        rows = c.execute(
            "SELECT id FROM tl.tokens WHERE location_source ORDER BY id"
        ).fetchall()
    assert rows == []


def test_loc_4_followup_maximum_gap_is_a_request_parameter(client, monkeypatch):
    import throughline.cli as cli

    # Two reports eight hours apart at one place.
    post_points(client, [point(0), point(8 * 3600)])
    r = client.post("/v1/track/detect-visits?max_gap_s=3600")
    assert r.status_code == 200 and r.json()["inserted"] == 0, r.text
    assert visits() == []
    r = client.post("/v1/track/detect-visits?max_gap_s=28800")
    assert r.status_code == 200 and r.json()["inserted"] == 1, r.text
    # The parameter wins over the environment default, and is validated.
    monkeypatch.setenv("THROUGHLINE_VISIT_MAX_GAP_S", "60")
    again = client.post("/v1/track/detect-visits?max_gap_s=28800").json()
    assert (again["inserted"], again["retired"]) == (0, 0)
    assert client.post("/v1/track/detect-visits?max_gap_s=0").status_code == 400
    assert len(visits()) == 1

    def through_test_client(method, path, data=None, params=None):
        response = client.request(method, "/v1/" + path, json=data, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(cli, "request", through_test_client)
    runner = CliRunner()
    kept = runner.invoke(
        app, ["track", "detect-visits", "--max-gap-s", "28800", "--json"]
    )
    assert kept.exit_code == 0, kept.output
    assert len(visits()) == 1
    # Without the option the CLI leaves the server default in force.
    default = runner.invoke(app, ["track", "detect-visits", "--json"])
    assert default.exit_code == 0, default.output
    assert visits() == []
    short = runner.invoke(
        app, ["track", "detect-visits", "--max-gap-s", "30000", "--min-stay-s", "60"]
    )
    assert short.exit_code == 0, short.output
    assert len(visits()) == 1
