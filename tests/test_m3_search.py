"""M3 acceptance tests: synthetic data and a loopback-only model stub."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import ingest, message
from throughline.db import connect
from throughline.embeddings import run_once


@pytest.fixture
def model(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            vectors = []
            for index, text in enumerate(body["input"]):
                v = [
                    1 + text.lower().count("orchard"),
                    1 + text.lower().count("ocean"),
                    1 + text.lower().count("stone"),
                ]
                vectors.append(dict(index=index, embedding=v))
            data = json.dumps(
                dict(
                    data=vectors,
                    usage={"total_tokens": sum(len(t) for t in body["input"]) // 4},
                )
            ).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "THROUGHLINE_EMBEDDING_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1"
    )
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "synthetic-v1")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "3")
    yield
    server.shutdown()
    server.server_close()
    thread.join()


def conversations():
    with connect() as c:
        return c.execute(
            "SELECT * FROM tl.conversations ORDER BY thread_id,chunk_no"
        ).fetchall()


def test_emb_1_deterministic_gaps_splits_and_affected_threads(client, channel):
    assert ingest(client, [message(0), message(10), message(4000)]).status_code == 200
    before = conversations()
    assert len(before) == 2
    with connect() as c:
        c.execute("SELECT tl.regroup_conversations()")
    assert conversations() == before
    response = client.post(
        "/v1/channels",
        json=dict(
            name=channel,
            plugin="synthetic",
            fields=[dict(name="rating", type="integer")],
            conversation_whole_thread=True,
            conversation_max_tokens=32,
        ),
    )
    assert response.status_code == 200
    ingest(client, [message(5000, content="orchard " * 100)])
    chunks = conversations()
    assert all(r["token_estimate"] <= 32 for r in chunks)
    assert len(chunks) > 2
    with connect() as c:
        c.execute("SELECT tl.regroup_conversations()")
    assert conversations() == chunks


def test_emb_2_emb_4_model_handover_ledger(client, channel, model, monkeypatch):
    ingest(client, [message(0), message(4000, content="ocean")])
    assert run_once() == 2
    assert run_once() == 0
    r = client.get("/v1/search", params=dict(q="orchard", mode="meaning"))
    assert r.status_code == 200, r.text
    assert r.json()["row_count"] == 2
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "synthetic-v2")
    assert run_once(1) == 1
    with connect() as c:
        assert (
            c.execute("SELECT model FROM tl.embedding_models WHERE active").fetchone()[
                "model"
            ]
            == "synthetic-v1"
        )
    assert (
        client.get("/v1/search", params=dict(q="orchard", mode="meaning")).status_code
        == 200
    )
    assert run_once() == 1
    with connect() as c:
        assert (
            c.execute("SELECT model FROM tl.embedding_models WHERE active").fetchone()[
                "model"
            ]
            == "synthetic-v2"
        )
        assert c.execute("SELECT count(*) n FROM tl.embeddings").fetchone()["n"] == 4
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "synthetic-v1")
    assert (
        run_once() == 0
    )  # Switching back also preserves the one-active-model invariant.
    ledger = client.get("/v1/status").json()["embeddings"]["ledger"]
    assert len(ledger) == 2
    for entry in ledger:
        assert float(entry["cost"]) == pytest.approx(entry["tokens"] * 0.02 / 1000000)


def test_emb_3_late_message_invalidates_and_refreshes(client, channel, model):
    ingest(client, [message(0), message(20)])
    run_once()
    old = conversations()[0]
    ingest(client, [message(10, content="new ocean")])
    new = conversations()[0]
    assert old["id"] == new["id"] and old["text_hash"] != new["text_hash"]
    with connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.embeddings e JOIN tl.conversations c ON c.id=e.conversation_id AND c.text_hash=e.text_hash"
            ).fetchone()["n"]
            == 0
        )
    assert run_once() == 1
    assert client.get("/v1/search", params={"q": "ocean"}).json()["row_count"] == 1


def test_qry_2_qry_10_exact_narrow_filters_and_spans(client, channel, model):
    # Ten eligible messages among 10,000 (0.1%); each has a distinct conversation.
    items = [
        message(
            i,
            thread={"external_id": f"fake-{i}"},
            content="orchard " * (1 + i % 9),
            happened_at="2020-01-01T00:00:00Z",
        )
        for i in range(10000)
    ]
    for start in range(0, len(items), 5000):
        assert ingest(client, items[start : start + 5000]).status_code == 200
    with connect() as c:
        # All vectors are deterministic, bypass network batching for this ranking corpus.
        c.execute("INSERT INTO tl.embedding_models VALUES('synthetic-v1',3,true)")
        c.execute(
            "INSERT INTO tl.embeddings SELECT id,'synthetic-v1',text_hash,('[1,'||(id%13+1)||',1]')::vector FROM tl.conversations"
        )
        ids = [
            r["id"]
            for r in c.execute("SELECT id FROM tl.messages ORDER BY id LIMIT 10")
        ]
        person = c.execute(
            "INSERT INTO tl.people(handle) VALUES('synthetic-filter') RETURNING id"
        ).fetchone()["id"]
        c.execute("UPDATE tl.messages SET person_id=%s WHERE id=ANY(%s)", (person, ids))
        expected = [
            r["id"]
            for r in c.execute(
                "SELECT m.id FROM tl.messages m JOIN tl.conversations c ON c.thread_id=m.thread_id JOIN tl.embeddings e ON e.conversation_id=c.id WHERE m.person_id=%s ORDER BY e.embedding <=> '[2,1,1]'::vector,m.id LIMIT 10",
                (person,),
            )
        ]
    with connect() as c:
        keyword_expected = [
            r["id"]
            for r in c.execute(
                "SELECT id FROM tl.messages WHERE person_id=%s ORDER BY ts_rank_cd(tsv,websearch_to_tsquery('english','orchard')) DESC,id LIMIT 10",
                (person,),
            )
        ]
    fused_scores = {}
    for ranking in (keyword_expected, expected):
        for rank, mid in enumerate(ranking, 1):
            fused_scores[mid] = fused_scores.get(mid, 0) + 1 / (60 + rank)
    fused_expected = sorted(fused_scores, key=lambda mid: (-fused_scores[mid], mid))
    for mode in ("keyword", "meaning", "fused"):
        response = client.get(
            "/v1/search",
            params=dict(q="orchard", mode=mode, person=person, max_results=10),
        )
        assert response.status_code == 200, response.text
        rows = response.json()["rows"]
        assert len(rows) == 10
        assert all(
            r["id"] in ids
            and r["conversation_id"]
            and r["thread_no"]
            and r["first_thread_no"] <= r["thread_no"] <= r["last_thread_no"]
            for r in rows
        )
        assert [r["id"] for r in rows] == {
            "keyword": keyword_expected,
            "meaning": expected,
            "fused": fused_expected,
        }[mode]
    response = client.post("/v1/embed", json={"text": "orchard"})
    assert response.status_code == 200
    literal = json.dumps(response.json()["embedding"])
    assert (
        client.post(
            "/v1/sql",
            json={
                "statement": f"SELECT * FROM api.search_meaning('{literal}',10,{person})"
            },
        ).status_code
        == 200
    )


def test_arc_4_unconfigured_embedding(client, channel, monkeypatch):
    monkeypatch.delenv("THROUGHLINE_EMBEDDING_MODEL", raising=False)
    ingest(client, [message()])
    assert client.get("/v1/search", params={"q": "orchard"}).status_code == 200
    for mode in ("meaning", "fused"):
        response = client.get("/v1/search", params=dict(q="orchard", mode=mode))
        assert response.status_code == 400 and "unavailable" in response.text


def test_emb_1_bulk_set_based_rebuild_100000(client, channel):
    with connect() as c:
        cid = c.execute(
            "SELECT id FROM tl.channels WHERE name=%s", (channel,)
        ).fetchone()["id"]
        tid = c.execute(
            "INSERT INTO tl.threads(channel_id,external_id) VALUES(%s,'synthetic-bulk') RETURNING id",
            (cid,),
        ).fetchone()["id"]
        c.execute(
            "INSERT INTO tl.messages(channel_id,external_id,version,thread_id,thread_no,happened_at,content,payload_hash) SELECT %s,'synthetic-bulk-'||i,1,%s,i,'2020-01-01Z'::timestamptz+i*interval '1 second','synthetic orchard','synthetic-hash' FROM generate_series(1,100000) i",
            (cid, tid),
        )
        c.execute("SELECT tl.regroup_conversations(%s::bigint[])", ([tid],))
        first = c.execute(
            "SELECT chunk_no,first_thread_no,last_thread_no,text_hash FROM tl.conversations ORDER BY chunk_no"
        ).fetchall()
        assert len(first) > 100
        c.execute("SELECT tl.regroup_conversations()")
        assert (
            c.execute(
                "SELECT chunk_no,first_thread_no,last_thread_no,text_hash FROM tl.conversations ORDER BY chunk_no"
            ).fetchall()
            == first
        )


def test_emb_3_idle_closure_live_reopen_unrelated_thread(client, channel, model):
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(seconds=1900)).isoformat()
    ingest(
        client,
        [
            message(0, happened_at=old),
            message(1, thread={"external_id": "synthetic-other"}),
        ],
    )
    run_once()
    unrelated = conversations()[1]
    ingest(client, [message(2, happened_at=datetime.now(timezone.utc).isoformat())])
    rows = conversations()
    # A 1900-second gap starts another conversation; backfill within its gap
    # extends the older span to a recent timestamp and reopens it.
    bridge = (datetime.now(timezone.utc) - timedelta(seconds=1700)).isoformat()
    ingest(client, [message(3, happened_at=bridge)])
    rows = conversations()
    assert len(rows) == 2
    assert not rows[0]["closed"]
    assert rows[1] == unrelated
    with connect() as c:
        c.execute("UPDATE tl.channels SET conversation_gap_s=0")
    run_once()
    assert all(r["closed"] for r in conversations())


def test_emb_2_emb_4_failed_batch_is_resumable(client, channel, model, monkeypatch):
    import throughline.embeddings as worker

    ingest(client, [message()])
    original = worker.embed

    def fail(*args, **kwargs):
        raise ValueError("synthetic model outage")

    monkeypatch.setattr(worker, "embed", fail)
    with pytest.raises(ValueError, match="synthetic model outage"):
        run_once()
    with connect() as c:
        assert (
            c.execute("SELECT count(*) n FROM tl.embedding_ledger").fetchone()["n"] == 0
        )
        assert c.execute("SELECT count(*) n FROM tl.embeddings").fetchone()["n"] == 0
    monkeypatch.setattr(worker, "embed", original)
    assert run_once() == 1
    assert run_once() == 0


def test_qry_10_best_message_and_qry_2_combined_filters(client, channel, model):
    ingest(
        client,
        [
            message(0, content="stone"),
            message(1, content="ocean orchard", location={"lat": 12, "lon": 34}),
        ],
    )
    client.post(
        "/v1/entities",
        json={
            "kind": "place",
            "name": "Synthetic orchard",
            "metadata": {"lat": 12, "lon": 34},
        },
    ).raise_for_status()
    run_once()
    for mode in ("keyword", "meaning", "fused"):
        r = client.get(
            "/v1/search",
            params=dict(
                q="orchard",
                mode=mode,
                channel=channel,
                place="Synthetic orchard",
                after_at="2020-01-01Z",
                before_at="2020-01-02Z",
            ),
        )
        assert r.status_code == 200, r.text
        assert [row["external_id"] for row in r.json()["rows"]] == ["fake-1"]
        if mode == "meaning":
            assert r.json()["rows"][0]["first_thread_no"] == 1
            assert r.json()["rows"][0]["last_thread_no"] == 2


def test_emb_1_migration_0008_populates_existing_messages(database):
    import uuid
    from pathlib import Path
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from throughline.db import migrate

    settings = conninfo_to_dict(database)
    settings["dbname"] = "postgres"
    admin_url = make_conninfo(**settings)
    name = "tl_test_" + uuid.uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings["dbname"] = name
    url = make_conninfo(**settings)
    try:
        with connect(url=url) as c:
            c.execute("CREATE SCHEMA tl")
            c.execute(
                "CREATE TABLE tl.schema_migrations(version text PRIMARY KEY,applied_at timestamptz DEFAULT now())"
            )
            for migration in sorted(Path("src/throughline/migrations").glob("*.sql")):
                if migration.name.startswith("0008"):
                    break
                c.execute(
                    migration.read_text().replace(
                        "CREATE SCHEMA tl;", "CREATE SCHEMA IF NOT EXISTS tl;"
                    )
                )
                c.execute(
                    "INSERT INTO tl.schema_migrations(version) VALUES(%s)",
                    (migration.name,),
                )
            c.execute(
                "INSERT INTO tl.channels(name,plugin) VALUES('synthetic.archive','synthetic')"
            )
            c.execute(
                "INSERT INTO tl.threads(channel_id,external_id) VALUES(1,'synthetic-thread')"
            )
            c.execute(
                "INSERT INTO tl.messages(channel_id,external_id,version,thread_id,thread_no,happened_at,content,payload_hash) VALUES(1,'synthetic-message',1,1,1,'2020-01-01Z','synthetic orchard','synthetic-hash')"
            )
        migrate(url)
        with connect(url=url) as c:
            assert (
                c.execute("SELECT count(*) n FROM tl.conversations").fetchone()["n"]
                == 1
            )
            assert c.execute(
                "SELECT conversation_id FROM api.search('orchard')"
            ).fetchone()["conversation_id"]
        migrate(url)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
