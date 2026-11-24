from conftest import ingest, message

from throughline.db import connect, migrate


def test_arc_2_arc_3_arc_4_schema_and_no_plugins(client, channel):
    import ast
    from pathlib import Path

    for path in Path("src/throughline").rglob("*.py"):
        for n in ast.walk(ast.parse(path.read_text())):
            if isinstance(n, ast.Import):
                assert all(not a.name.startswith(("plugins", "sdk")) for a in n.names)
            if isinstance(n, ast.ImportFrom):
                assert not (n.module or "").startswith(("plugins", "sdk"))
    with connect() as c:
        assert {
            r["extname"] for r in c.execute("SELECT extname FROM pg_extension")
        } >= {"vector", "pg_trgm", "cube", "earthdistance"}
    migrate()
    assert ingest(client, [message()]).json()["inserted"] == 1
    assert client.get("/v1/search", params={"q": "orchard"}).status_code == 200


def test_msg_1_validation_independent_rejections(client, channel):
    rows = [message(0)]
    for i, key in enumerate(("content", "channel", "external_id", "happened_at"), 1):
        m = message(i)
        del m[key]
        rows.append(m)
    r = ingest(client, rows)
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1
    assert len(r.json()["rejected"]) == 4


def test_msg_2_arc_5_versions_and_payloads(client, channel):
    m = message()
    assert ingest(client, [m]).json()["inserted"] == 1
    assert ingest(client, [m, m]).json()["unchanged"] == 2
    m["payload"]["edited"] = True
    assert ingest(client, [m]).json()["new_versions"] == 1
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.raw_payloads").fetchone()["n"] == 2
        assert c.execute("SELECT count(*) n FROM api.messages").fetchone()["n"] == 1
        assert (
            c.execute("SELECT count(*) n FROM api.message_versions").fetchone()["n"]
            == 2
        )


def test_msg_3_original_offset(client, channel):
    assert (
        ingest(client, [message(happened_at="2001-01-01T12:00:00+05:30")]).status_code
        == 200
    )
    with connect() as c:
        r = c.execute("SELECT * FROM api.messages").fetchone()
        assert r["happened_offset_min"] == 330
        assert r["happened_at"].hour == 6
        assert r["ingested_at"] > r["happened_at"]


def test_msg_4_qry_3_random_order_duplicates_context(client, channel):
    import random

    rows = [message(i) for i in range(40)]
    random.Random(42).shuffle(rows)
    for i in range(0, 40, 7):
        r = ingest(client, rows[i : i + 7] + rows[i : i + 3])
        assert r.status_code == 200, r.text
    with connect() as c:
        all_rows = c.execute("SELECT * FROM api.messages ORDER BY thread_no").fetchall()
        assert [r["thread_no"] for r in all_rows] == list(range(1, 41))
        for i, row in enumerate(all_rows):
            context = c.execute(
                "SELECT * FROM api.context(%s,10,5)", (row["id"],)
            ).fetchall()
            assert [r["id"] for r in context] == [
                r["id"] for r in all_rows[max(0, i - 10) : i + 6]
            ]


def test_msg_5_typed_view(client, channel):
    r = ingest(client, [message(), message(1, fields={"rating": "wrong"})]).json()
    assert r["inserted"] == 1 and len(r["rejected"]) == 1
    with connect() as c:
        assert (
            c.execute("SELECT rating FROM api.ch_synthetic_chat").fetchone()["rating"]
            == 0
        )
        assert (
            c.execute(
                "SELECT data_type FROM information_schema.columns WHERE table_schema='api' AND table_name='ch_synthetic_chat' AND column_name='rating'"
            ).fetchone()["data_type"]
            == "bigint"
        )


def test_msg_6_reparse_payload(client, channel):
    from throughline.reparse import MAPPERS, register_mapper, reparse

    ingest(client, [message()])
    register_mapper(
        channel,
        lambda payload: dict(
            content="Reparsed synthetic orchard",
            fields={"rating": payload["synthetic"] + 1},
        ),
    )
    try:
        with connect("app") as c:
            assert reparse(c, channel)["new_versions"] == 1
        with connect() as c:
            assert c.execute("SELECT fields FROM api.messages").fetchone()[
                "fields"
            ] == {"rating": 1}
            assert (
                c.execute("SELECT count(*) n FROM tl.raw_payloads").fetchone()["n"] == 2
            )
    finally:
        MAPPERS.clear()


def test_plg_3_dry_run(client, channel):
    batch = [message(i) for i in range(4)]
    dry = ingest(client, batch, dry_run=True)
    assert dry.status_code == 200, dry.text
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.messages").fetchone()["n"] == 0
    assert dry.json() == ingest(client, batch).json()


def test_msg_2_failing_batch_rolls_back(client, channel):
    r = ingest(
        client,
        [message()],
        links=[
            {
                "type": "fake",
                "from": {"channel": channel, "external_id": "missing"},
                "to": {"kind": "work", "external_ref": "fake"},
            }
        ],
    )
    assert r.status_code == 400
    with connect() as c:
        for table in ("messages", "threads", "identifiers", "raw_payloads"):
            assert c.execute(f"SELECT count(*) n FROM tl.{table}").fetchone()["n"] == 0


def test_plg_4_cursors_plg_7_health(client):
    assert (
        client.put("/v1/plugins/synthetic/cursor/history", json={"page": 3}).status_code
        == 200
    )
    assert client.get("/v1/plugins/synthetic/cursor/history").json()["cursor"] == {
        "page": 3
    }
    client.post("/v1/plugins/synthetic/manifest", json={"mode": "synthetic"})
    r = client.post(
        "/v1/plugins/synthetic/heartbeat",
        json={"interval_s": 60, "messages_ingested": 9},
    )
    assert r.status_code == 200, r.text
    assert not client.get("/v1/status").json()["plugins"][0]["stale"]
    with connect() as c:
        c.execute("UPDATE tl.plugins SET last_success_at=now()-interval '121 seconds'")
    assert client.get("/v1/status").json()["plugins"][0]["stale"]


def test_msg_1_malformed_external_id_does_not_break_batch(client, channel):
    r = ingest(client, [message(), message(1, external_id=123)])
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 1 and len(r.json()["rejected"]) == 1


def test_msg_2_multiple_edits_in_delivery_order(client, channel):
    original = message()
    changed = message(content="Synthetic edit")
    ingest(client, [original])
    r = ingest(client, [changed, original, changed, changed])
    assert r.status_code == 200, r.text
    assert r.json()["new_versions"] == 3 and r.json()["unchanged"] == 1
    with connect() as c:
        assert (
            c.execute("SELECT content FROM api.messages").fetchone()["content"]
            == "Synthetic edit"
        )
        assert (
            c.execute("SELECT count(*) n FROM api.message_versions").fetchone()["n"]
            == 4
        )


def test_msg_4_edit_moves_threads_without_number_gaps(client, channel):
    ingest(client, [message(i) for i in range(4)])
    r = ingest(client, [message(1, thread={"external_id": "fake-other-thread"})])
    assert r.status_code == 200, r.text
    with connect() as c:
        rows = c.execute(
            "SELECT thread_external_id,array_agg(thread_no ORDER BY thread_no) numbers FROM api.messages GROUP BY thread_external_id ORDER BY thread_external_id"
        ).fetchall()
        assert [r["numbers"] for r in rows] == [[1], [1, 2, 3]]
