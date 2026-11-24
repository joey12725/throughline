"""Feeds, named cursors (migration 0046, throughline.feeds): arrival
keyed news, compare-and-set marking, the settle horizon and the settle window
enforced by the database for every role. Synthetic data only."""

import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from conftest import ingest, message
from typer.testing import CliRunner

from throughline import access, reparse
from throughline.db import connect, create_token
from throughline.ingest import ingest as ingest_rows
from throughline.models import Ingest

SECRET = "synthetic.secret"
NOTES = "synthetic.notes"


def headers(kind, plugin=None, askers=None, asker=None):
    token = create_token("synthetic-" + kind, kind, plugin, askers)["token"]
    out = {"Authorization": "Bearer " + token}
    if asker:
        out["X-Throughline-Asker"] = asker
    return out


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def settle(client, settle_s=2, margin_s=1):
    return ok(
        client.put(
            "/v1/feeds/settings", json={"settle_s": settle_s, "margin_s": margin_s}
        )
    )


def create(client, name="synthetic-feed", h=None, **body):
    return ok(
        client.post(
            "/v1/feeds",
            json=dict(name=name, start=body.pop("start", "beginning"), **body),
            headers=h or {},
        )
    )


def news(client, name="synthetic-feed", h=None, max=200):
    return ok(
        client.get(f"/v1/feeds/{name}/news", params={"max": max}, headers=h or {})
    )


def mark(client, to, name="synthetic-feed", h=None, expected=None):
    body = {"to": to}
    if expected is not None:
        body["expected"] = expected
    return client.post(f"/v1/feeds/{name}/mark", json=body, headers=h or {})


def wait_settled(client, seconds=2.2):
    time.sleep(seconds)


def ids(rows):
    return [r["message_id"] for r in rows]


def owner(statement, params=()):
    with connect() as c:
        return c.execute(statement, params).fetchall()


def add_channel(client, name, **extra):
    ok(client.post("/v1/channels", json=dict(name=name, plugin="synthetic", **extra)))


# --- P2 ----------------------------------------------------------------------------


def test_p2_news_keyed_on_ingested_at_late_backfill_delivered_once(client, channel):
    settle(client)
    create(client)
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    wait_settled(client)
    first = news(client)
    assert len(first["rows"]) == 3 and first["row_count"] == 3
    assert [r["kind"] for r in first["rows"]] == ["life_event"] * 3
    assert ok(mark(client, first["next_cursor"]))["seen_to"]
    # A late backfill: message time years earlier, arrival now.
    late = message(
        50,
        external_id="late-1",
        happened_at="2010-01-01T00:00:00+00:00",
    )
    assert ingest(client, [late]).status_code == 200
    wait_settled(client)
    second = news(client)
    assert [r["external_id"] for r in second["rows"]] == ["late-1"]
    ok(mark(client, second["next_cursor"]))
    assert news(client)["rows"] == []
    assert ok(client.get("/v1/feeds/synthetic-feed/pending"))["pending"] == 0
    # Rows are in arrival order with a cursor per row.
    cursors = [r["cursor"] for r in first["rows"]]
    assert cursors == sorted(cursors, key=lambda c: (c["ingested_at"], c["message_id"]))
    assert "coverage" in first["execution"]


def test_p2_mark_compare_and_set_and_repeat_noop_same_response(client, channel):
    settle(client)
    create(client)
    ingest(client, [message(i) for i in range(3)])
    wait_settled(client)
    rows = news(client)["rows"]
    middle, last = rows[1]["cursor"], rows[2]["cursor"]
    first = ok(mark(client, middle, expected=None))
    assert first["seen_to"]["message_id"] == middle["message_id"]
    assert first["pending_after"] == 1 and first["marked"] == 1
    # The same mark again: a no-op with the same answer.
    assert ok(mark(client, middle)) == first
    assert ok(mark(client, rows[0]["cursor"])) == first
    # Compare-and-set: a stale expected position is refused.
    stale = mark(client, last, expected=rows[0]["cursor"])
    assert stale.status_code == 409
    done = ok(mark(client, last, expected=middle))
    assert done["pending_after"] == 0 and done["marked"] == 2
    assert ok(mark(client, last, expected=middle)) == done
    assert [r["marks"] for r in owner("SELECT marks FROM tl.consumers")] == [2]
    assert mark(client, None).status_code == 400
    assert mark(client, {"ingested_at": "nope"}).status_code == 400


def test_p2_crash_before_mark_redelivers(client, channel):
    settle(client)
    create(client)
    ingest(client, [message(i) for i in range(2)])
    wait_settled(client)
    once = news(client)["rows"]
    # The feed reader crashed before marking: the same rows come again.
    again = news(client)["rows"]
    assert ids(once) == ids(again) and len(once) == 2
    ok(mark(client, again[-1]["cursor"]))
    assert news(client)["rows"] == []


def test_p2_horizon_excludes_unsettled_rows(client, channel):
    settings = settle(client, 3, 1)
    assert settings == {"settle_s": 3, "margin_s": 1}
    assert ok(client.get("/v1/feeds/settings")) == settings
    create(client)
    ingest(client, [message(0)])
    assert news(client)["rows"] == []
    pending = ok(client.get("/v1/feeds/synthetic-feed/pending"))
    assert pending["pending"] == 0 and pending["horizon"]
    time.sleep(3.3)
    rows = news(client)["rows"]
    assert len(rows) == 1
    assert ok(client.get("/v1/feeds/synthetic-feed/pending"))["pending"] == 1


def test_p2_settings_constraint_rejects_settle_not_above_margin(client):
    for settle_s, margin_s in ((5, 5), (4, 6), (1, 0), (10, -1)):
        r = client.put(
            "/v1/feeds/settings", json={"settle_s": settle_s, "margin_s": margin_s}
        )
        assert r.status_code == 400, (settle_s, margin_s, r.text)
    assert ok(client.get("/v1/feeds/settings")) == {"settle_s": 120, "margin_s": 15}
    with connect() as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("UPDATE tl.consumer_settings SET margin_s=200")


def _slow_ingest(role, external_id, bulk=False, sleep_s=2.4):
    batch = Ingest(plugin="synthetic", messages=[message(1, external_id=external_id)])
    with connect(role) as conn:
        if bulk:
            conn.execute("SET LOCAL throughline.bulk_restore='on'")
        conn.execute("SELECT pg_sleep(%s)", (sleep_s,))
        ingest_rows(conn, batch)


def test_p2_long_ingest_transaction_refused_for_app_and_owner_roles(client, channel):
    settle(client, 3, 1)  # window: 2 s
    for role, key in (("app", "slow-app"), (None, "slow-owner")):
        with pytest.raises(psycopg.errors.ProgramLimitExceeded) as error:
            _slow_ingest(role, key)
        assert "settle window" in str(error.value)
        assert not owner("SELECT 1 FROM tl.messages WHERE external_id=%s", (key,))
    # A quick transaction is accepted.
    _slow_ingest("app", "quick", sleep_s=0)
    assert owner("SELECT 1 FROM tl.messages WHERE external_id='quick'")


def test_p2_bulk_restore_flag_exempts(client, channel, tmp_path, monkeypatch):
    settle(client, 3, 1)
    _slow_ingest(None, "restored", bulk=True)
    assert owner("SELECT 1 FROM tl.messages WHERE external_id='restored'")
    # restore runs pg_restore with the flag and succeeds.
    from throughline import cli

    try:
        program = cli.pg_program("pg_dump")
    except Exception:
        pytest.skip("pg_dump not found; set THROUGHLINE_PG_BIN")
    monkeypatch.setenv("THROUGHLINE_PG_BIN", str(Path(program).parent))
    seen = []
    real = subprocess.run

    def spy(args, **kwargs):
        seen.append((args, kwargs.get("env")))
        return real(args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", spy)
    before = owner("SELECT id,external_id,ingested_at FROM tl.messages ORDER BY id")
    path = tmp_path / "synthetic.dump"
    runner = CliRunner()
    assert runner.invoke(cli.app, ["dump", str(path)]).exit_code == 0
    with connect() as c:
        c.execute("DROP SCHEMA api CASCADE; DROP SCHEMA tl CASCADE")
    result = runner.invoke(cli.app, ["restore", str(path)])
    assert result.exit_code == 0, result.output
    restore_env = [env for args, env in seen if "pg_restore" in str(args[0])]
    assert restore_env and "throughline.bulk_restore=on" in restore_env[0]["PGOPTIONS"]
    assert (
        owner("SELECT id,external_id,ingested_at FROM tl.messages ORDER BY id")
        == before
    )
    assert owner("SELECT settle_s FROM tl.consumer_settings") == [{"settle_s": 3}]


def test_p2_reparse_commits_in_batches_within_window(client, channel, monkeypatch):
    rows = [message(i) for i in range(240)]
    assert ingest(client, rows).status_code == 200
    settle(client, 2, 1)  # window: 1 s
    wait_settled(client)
    # 'now' starts at the horizon: the originals are already behind it.
    create(client, start="now")
    monkeypatch.setenv("THROUGHLINE_REPARSE_BATCH", "40")
    monkeypatch.setitem(
        reparse.MAPPERS,
        "synthetic.chat",
        lambda payload: {"content": f"Synthetic reparsed {payload['synthetic']}"},
    )
    result = ok(client.post("/v1/reparse", json={"channel": channel}))
    assert result["new_versions"] == 240 and result["rejected"] == []
    stamps = owner(
        "SELECT count(DISTINCT ingested_at) AS n FROM tl.messages WHERE version=2"
    )[0]["n"]
    assert stamps == 6  # one transaction per batch of 40
    wait_settled(client)
    delivered = []
    while True:
        page = news(client, max=100)["rows"]
        if not page:
            break
        delivered += page
        ok(mark(client, page[-1]["cursor"]))
    assert len(delivered) == 240
    assert len({r["message_id"] for r in delivered}) == 240
    assert all(r["is_new_version"] and r["version"] == 2 for r in delivered)


def test_p2_news_uses_arrival_index(client, channel):
    ingest(client, [message(i) for i in range(1500)])
    create(client)
    with connect() as c:
        c.execute("ANALYZE tl.messages")
        k = c.execute("SELECT id FROM tl.consumers").fetchone()["id"]
        cursor = c.execute(
            "SELECT ingested_at,id FROM tl.messages ORDER BY ingested_at,id OFFSET 700 LIMIT 1"
        ).fetchone()
        plan = "\n".join(
            r["QUERY PLAN"]
            for r in c.execute(
                "EXPLAIN SELECT * FROM tl.news_candidates(%s,%s,%s,now(),200)",
                (k, cursor["ingested_at"], cursor["id"]),
            ).fetchall()
        )
    assert "messages_arrival" in plan, plan
    assert "ROW(ingested_at, id) >" in plan, plan


def test_p2_scope_channels_kinds_persons_threads_and_ignored(client, channel):
    add_channel(client, NOTES)
    add_channel(client, "synthetic.ref", is_reference=True, record_kind="reference")
    settle(client)
    ingest(
        client,
        [
            message(0),
            message(1, thread={"external_id": "other-thread"}),
            message(2, channel=NOTES, external_id="note-2", fields={}),
            message(3, channel="synthetic.ref", external_id="ref-3", fields={}),
        ],
    )
    msg = {
        r["external_id"]: r
        for r in owner("SELECT id,external_id,thread_id FROM tl.messages")
    }
    with connect() as c:
        person = c.execute(
            "INSERT INTO tl.people(handle) VALUES ('synthetic-person') RETURNING id"
        ).fetchone()["id"]
        c.execute(
            "UPDATE tl.messages SET person_id=%s WHERE external_id='fake-0'", (person,)
        )
    for name, scope in (
        ("all", {}),
        ("notes", {"channels": [NOTES]}),
        ("refs", {"kinds": ["reference"]}),
        ("person", {"persons": [person]}),
        ("thread", {"thread_ids": [msg["fake-1"]["thread_id"]]}),
        ("ignored", {"include_ignored": True}),
    ):
        create(client, name, scope=scope)
    ok(
        client.post(
            "/v1/annotations",
            json={
                "message_id": msg["fake-0"]["id"],
                "attribute": "ignore",
                "value": {"ignored": True, "reason": "synthetic noise"},
            },
        )
    )
    wait_settled(client)

    def got(name):
        return sorted(r["external_id"] for r in news(client, name)["rows"])

    assert got("all") == ["fake-1", "note-2"]  # ignored and reference left out
    assert got("notes") == ["note-2"]
    assert got("refs") == ["ref-3"]
    assert got("person") == []  # its only message is ignored
    assert got("thread") == ["fake-1"]
    assert got("ignored") == ["fake-0", "fake-1", "note-2"]
    assert (
        client.post(
            "/v1/feeds", json={"name": "bad", "scope": {"colour": "red"}}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/v1/feeds", json={"name": "bad", "scope": {"kinds": ["nope"]}}
        ).status_code
        == 400
    )
    assert client.post("/v1/feeds", json={"name": "all"}).status_code == 409


def test_p2_new_versions_flagged(client, channel):
    settle(client)
    create(client)
    create(client, "originals", scope={"include_versions": False})
    ingest(client, [message(0)])
    ingest(client, [message(0, content="Synthetic orchard message changed")])
    wait_settled(client)
    rows = news(client)["rows"]
    assert [(r["version"], r["is_new_version"]) for r in rows] == [
        (1, False),
        (2, True),
    ]
    assert rows[0]["is_current"] is False and rows[1]["is_current"] is True
    assert [r["version"] for r in news(client, "originals")["rows"]] == [1]


def test_p2_restricted_feed_sees_only_granted_rows_and_counts(client, channel):
    add_channel(client, SECRET)
    settle(client)
    access.register_asker("guest")
    access.grant("guest", channel=channel)
    guest = {"X-Throughline-Asker": "guest"}
    create(client, h=guest)
    create(client)  # the owner's own feed of the same name
    ingest(
        client,
        [
            message(0),
            message(1, channel=SECRET, external_id="secret-1", fields={}),
            message(2),
        ],
    )
    wait_settled(client)
    rows = news(client, h=guest)["rows"]
    assert [r["channel"] for r in rows] == [channel, channel]
    pending = ok(client.get("/v1/feeds/synthetic-feed/pending", headers=guest))
    assert pending["pending"] == 2
    assert ok(client.get("/v1/feeds/synthetic-feed/pending"))["pending"] == 3
    coverage = news(client, h=guest)["execution"]["coverage"]
    assert [c["channel"] for c in coverage["channels"]] == [channel]
    listed = ok(client.get("/v1/feeds", headers=guest))["feeds"]
    assert [c["name"] for c in listed] == ["synthetic-feed"]
    # Marking one asker's feed leaves the other's cursor alone.
    ok(mark(client, rows[-1]["cursor"], h=guest))
    assert ok(client.get("/v1/feeds/synthetic-feed/pending"))["pending"] == 3


def test_p2_sql_forms_news_and_pending_via_v1_sql_read_only(client, channel):
    settle(client)
    create(client)
    ingest(client, [message(i) for i in range(3)])
    wait_settled(client)

    def sql(statement):
        return ok(client.post("/v1/sql", json={"statement": statement}))

    rows = sql(
        "SELECT record_id,is_new_version,cursor FROM api.news('synthetic-feed',2)"
    )
    assert len(rows["rows"]) == 2
    assert rows["rows"][0]["record_id"].startswith("m:")
    pending = sql("SELECT * FROM api.pending('synthetic-feed')")["rows"]
    assert pending[0]["pending"] == 3 and pending[0]["consumer"] == "synthetic-feed"
    assert sql("SELECT * FROM api.news('synthetic-absent')")["rows"] == []
    assert sql("SELECT * FROM api.pending('synthetic-absent')")["rows"] == []
    # Reading never moves the cursor.
    assert owner("SELECT seen_to_message_id FROM tl.consumers") == [
        {"seen_to_message_id": None}
    ]
    r = client.post(
        "/v1/sql",
        json={"statement": "SELECT * FROM api.news('synthetic-feed') FOR UPDATE"},
    )
    assert r.status_code == 400


def test_p2_foreign_feed_404(client, channel):
    create(client)
    other = headers("agent")
    for path in ("news", "pending"):
        foreign = client.get(f"/v1/feeds/synthetic-feed/{path}", headers=other)
        unknown = client.get(f"/v1/feeds/synthetic-absent/{path}", headers=other)
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json() == unknown.json()
    to = {"ingested_at": "2020-01-01T00:00:00Z", "message_id": 1}
    assert mark(client, to, h=other).status_code == 404
    assert (
        client.delete("/v1/feeds/synthetic-feed", headers=other).status_code
        == 404
    )
    assert ok(client.get("/v1/feeds", headers=other))["feeds"] == []
    plugin = headers("plugin", "synthetic")
    assert client.get("/v1/feeds", headers=plugin).status_code == 403
    assert ok(client.delete("/v1/feeds/synthetic-feed")) == {
        "name": "synthetic-feed",
        "dropped": True,
    }


def test_p2_mark_beyond_horizon_409(client, channel):
    create(client)
    ingest(client, [message(0)])
    now = datetime.now(timezone.utc).isoformat()
    r = mark(client, {"ingested_at": now, "message_id": 1})
    assert r.status_code == 409
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert ok(mark(client, {"ingested_at": past, "message_id": 0}))["marked"] in (0, 1)


def test_p2_agent_cannot_change_settings(client):
    agent = headers("agent")
    r = client.put(
        "/v1/feeds/settings", json={"settle_s": 5, "margin_s": 1}, headers=agent
    )
    assert r.status_code == 403
    assert ok(client.get("/v1/feeds/settings", headers=agent)) == {
        "settle_s": 120,
        "margin_s": 15,
    }
    guest_owner = {"X-Throughline-Asker": "guest"}
    access.register_asker("guest")
    r = client.put(
        "/v1/feeds/settings",
        json={"settle_s": 5, "margin_s": 1},
        headers=guest_owner,
    )
    assert r.status_code == 403


def test_p2_cli_lib_feed_flow(client, channel, monkeypatch):
    import json

    from throughline import cli

    def fake_request(method, path, data=None, params=None):
        return ok(client.request(method, "/v1/" + path, json=data, params=params))

    monkeypatch.setattr(cli, "request", fake_request)
    runner = CliRunner()

    def run(*args):
        result = runner.invoke(cli.app, ["lib", "feed", *args], color=False)
        assert result.exit_code == 0, result.output
        return json.loads(result.output)

    assert run("settings", "--settle-s", "2", "--margin-s", "1", "--json") == {
        "settle_s": 2,
        "margin_s": 1,
    }
    created = run(
        "create",
        "synthetic-cli",
        "--start",
        "beginning",
        "--scope",
        json.dumps({"channels": [channel]}),
        "--json",
    )
    assert created["name"] == "synthetic-cli" and created["seen_to"] is None
    ingest(client, [message(i) for i in range(2)])
    wait_settled(client)
    page = run("news", "synthetic-cli", "--max", "1", "--json")
    assert len(page["rows"]) == 1
    assert run("pending", "synthetic-cli", "--json")["pending"] == 2
    marked = run(
        "mark", "synthetic-cli", "--to", json.dumps(page["next_cursor"]), "--json"
    )
    assert marked["pending_after"] == 1 and marked["marked"] == 1
    assert [c["name"] for c in run("list", "--json")] == ["synthetic-cli"]
    assert run("drop", "synthetic-cli", "--json")["dropped"] is True
    bad = runner.invoke(
        cli.app, ["lib", "feed", "mark", "synthetic-cli", "--to", "{nope"]
    )
    assert bad.exit_code != 0


def test_consumers_routes_are_deprecated_aliases_of_feeds(client, channel):
    """Every /v1/consumers... route answers exactly like its /v1/feeds...
    twin, and only the feeds routes are in the OpenAPI document."""
    settings = {"settle_s": 2, "margin_s": 1}
    assert ok(client.put("/v1/consumers/settings", json=settings)) == settings
    assert ok(client.get("/v1/consumers/settings")) == ok(client.get("/v1/feeds/settings"))
    created = ok(client.post("/v1/consumers", json={"name": "synthetic-alias", "start": "beginning"}))
    assert created["name"] == "synthetic-alias"
    old_list, new_list = ok(client.get("/v1/consumers")), ok(client.get("/v1/feeds"))
    assert old_list == new_list
    assert [c["name"] for c in new_list["feeds"]] == [c["name"] for c in new_list["consumers"]] == ["synthetic-alias"]
    ingest(client, [message(i) for i in range(2)])
    wait_settled(client)
    old_news = ok(client.get("/v1/consumers/synthetic-alias/news", params={"max": 1}))
    new_news = ok(client.get("/v1/feeds/synthetic-alias/news", params={"max": 1}))
    assert old_news["rows"] == new_news["rows"] and len(new_news["rows"]) == 1
    assert new_news["feed"] == new_news["consumer"] == old_news["feed"] == "synthetic-alias"
    old_pending = ok(client.get("/v1/consumers/synthetic-alias/pending"))
    new_pending = ok(client.get("/v1/feeds/synthetic-alias/pending"))
    # horizon is now() - settle_s at each call; everything else is the same.
    assert {k: v for k, v in old_pending.items() if k != "horizon"} == {
        k: v for k, v in new_pending.items() if k != "horizon"
    }
    assert new_pending["pending"] == 2
    assert new_pending["feed"] == new_pending["consumer"] == "synthetic-alias"
    to = new_news["next_cursor"]
    old_mark = ok(client.post("/v1/consumers/synthetic-alias/mark", json={"to": to}))
    new_mark = ok(client.post("/v1/feeds/synthetic-alias/mark", json={"to": to}))
    assert old_mark == new_mark and new_mark["pending_after"] == 1
    assert client.get("/v1/consumers/synthetic-absent/pending").status_code == 404
    assert ok(client.delete("/v1/consumers/synthetic-alias")) == {"name": "synthetic-alias", "dropped": True}
    assert client.get("/v1/feeds/synthetic-alias/pending").status_code == 404
    from throughline.api import create_app

    paths = create_app().openapi()["paths"]
    assert "/v1/feeds" in paths and "/v1/feeds/{name}/mark" in paths
    assert not [p for p in paths if p.startswith("/v1/consumers")]


def test_consumers_module_and_cli_aliases(client, monkeypatch):
    import json

    from throughline import cli, consumers, feeds

    assert consumers is feeds and consumers.list_consumers is feeds.list_feeds

    def fake_request(method, path, data=None, params=None):
        return ok(client.request(method, "/v1/" + path, json=data, params=params))

    monkeypatch.setattr(cli, "request", fake_request)
    runner = CliRunner()
    for group in ("feed", "consumer"):
        result = runner.invoke(cli.app, ["lib", group, "settings", "--json"], color=False)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"settle_s": 120, "margin_s": 15}
    help_text = runner.invoke(cli.app, ["lib", "--help"], color=False).output
    assert "feed" in help_text and "consumer" not in help_text
