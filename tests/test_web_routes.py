"""The web interface's server side (docs/web/backend.md): the static shell,
bootstrap, the interface's own documents, arrivals and the audit client tag."""

import base64
import json
import threading
import time
from pathlib import Path

import pytest
from conftest import ingest, message
from fastapi import FastAPI
from fastapi.testclient import TestClient

from throughline import access, web
from throughline.api import create_app
from throughline.api.routes import web as web_routes
from throughline.db import connect, create_token

OPEN, HIDDEN, CANARY = "synthetic.open", "synthetic.hidden", "zzwebcanary"
GUEST = "web-guest"


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def audit_rows():
    with connect() as c:
        return c.execute("SELECT * FROM tl.audit_log ORDER BY id").fetchall()


@pytest.fixture()
def world(client):
    for name in (OPEN, HIDDEN):
        ok(
            client.post(
                "/v1/channels",
                json=dict(
                    name=name,
                    plugin="synthetic",
                    description=f"Synthetic {name}",
                    fields=[dict(name="rating", type="integer", description="Score")],
                ),
            )
        )
    items = [
        message(i * 60, channel=OPEN, external_id=f"open-{i}", fields={"rating": i})
        for i in range(6)
    ] + [
        message(
            i * 60 + 5,
            channel=HIDDEN,
            external_id=f"hidden-{i}",
            thread={"external_id": "hidden-thread"},
            content=f"Synthetic {CANARY} secret {i}",
            fields={"rating": i},
            location={"lat": 1, "lon": 2},
        )
        for i in range(4)
    ]
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    plugin = create_token("plugin", kind="plugin", plugin="synthetic")["token"]
    ok(
        client.post(
            "/v1/plugins/synthetic/heartbeat",
            headers={"Authorization": "Bearer " + plugin},
            json={"interval_s": 600, "messages_ingested": 10},
        )
    )
    access.register_asker(GUEST)
    access.grant(GUEST, channel=OPEN)
    agent = create_token("guest agent", kind="agent", askers=[GUEST])["token"]
    full = create_token("full agent", kind="agent")["token"]
    with TestClient(client.app) as guest:
        guest.headers["Authorization"] = "Bearer " + agent
        yield dict(
            client=client,
            guest=guest,
            plugin={"Authorization": "Bearer " + plugin},
            full={"Authorization": "Bearer " + full},
            as_guest={"X-Throughline-Asker": GUEST},
        )


# --- shell --------------------------------------------------------------------

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; "
    "worker-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)


@pytest.fixture()
def built(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>Synthetic shell</title><script type=module src=/ui/assets/app-abc123.js></script>"
    )
    (tmp_path / "assets" / "app-abc123.js").write_text("export const x = 1;")
    (tmp_path / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / ".secret").write_text("not served")
    app = FastAPI()
    assert web.install(app, static=tmp_path) is True
    with TestClient(app) as shell:
        yield shell


def test_shell_headers_deep_links_and_assets(built):
    for path in ("/ui", "/ui/", "/ui/people/3", "/ui/channel/a.b/records?x=1"):
        r = built.get(path)
        assert r.status_code == 200 and "Synthetic shell" in r.text, path
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["content-security-policy"] == CSP
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cross-origin-opener-policy"] == "same-origin"
    r = built.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui/"
    r = built.get("/ui/assets/app-abc123.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert r.headers["content-security-policy"] == CSP
    # A missing asset is a 404, never the shell (a stale page must not get
    # HTML where it expects a script).
    assert built.get("/ui/assets/nope.js").status_code == 404
    r = built.get("/ui/favicon.svg")
    assert r.status_code == 200 and "svg" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-store"
    assert built.post("/ui/x").status_code == 405


def test_shell_serves_nothing_outside_its_directory(built):
    for path in (
        "/ui/.secret",
        "/ui/..%2F..%2Fpyproject.toml",
        "/ui/%2e%2e/%2e%2e/pyproject.toml",
        "/ui/assets/..%2Findex.html",
        "/ui/index.html",
    ):
        r = built.get(path)
        assert "not served" not in r.text and "[project]" not in r.text, path
        assert r.status_code in (200, 404), path
        if r.status_code == 200:
            assert "Synthetic shell" in r.text, path


def test_shell_without_a_build_says_how_to_build(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, "STATIC", tmp_path / "absent")
    with TestClient(create_app()) as fresh:
        for path in ("/ui/", "/ui/some/deep/link"):
            r = fresh.get(path)
            assert r.status_code == 200 and "UI not built" in r.text
            assert "pnpm build" in r.text
            assert r.headers["content-security-policy"] == CSP
            assert r.headers["cache-control"] == "no-store"
        assert fresh.get("/ui/assets/app.js").status_code == 404
        # No external reference of any kind in the fallback page.
        assert "http" not in r.text.replace("http-equiv", "")


def test_shell_is_public_unaudited_and_outside_the_schema(client):
    before = len(audit_rows())
    anonymous = TestClient(client.app)
    assert anonymous.get("/ui/").status_code == 200
    assert client.get("/ui/anything").status_code == 200  # token sent, not read
    assert anonymous.get("/", follow_redirects=False).status_code == 307
    assert len(audit_rows()) == before
    # The API beside it is unchanged.
    assert anonymous.get("/v1/status").status_code == 401
    assert anonymous.get("/v1/web/bootstrap").status_code == 401
    assert client.get("/v1/nope").status_code == 404
    schema = client.get("/v1/openapi.json").json()
    assert not [p for p in schema["paths"] if not p.startswith("/v1/")]
    for path in ("/v1/web/bootstrap", "/v1/web/arrivals", "/v1/web/state/{kind}"):
        assert schema["paths"][path]["get"]["security"]


def test_shell_environment_switches(client, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_WEB", "0")
    with TestClient(create_app()) as off:
        assert off.get("/ui/").status_code == 404
        assert off.get("/", follow_redirects=False).status_code == 404
        assert (
            off.get("/v1/web/bootstrap", headers=dict(client.headers)).status_code
            == 200
        )
    monkeypatch.delenv("THROUGHLINE_WEB")
    monkeypatch.setenv("THROUGHLINE_WEB_TILE_ORIGIN", "https://tiles.invalid:8443/")
    policy = client.get("/ui/").headers["content-security-policy"]
    assert "img-src 'self' data: blob: https://tiles.invalid:8443;" in policy
    assert "connect-src 'self' https://tiles.invalid:8443;" in policy
    assert "script-src 'self';" in policy
    for bad in (
        "https://tiles.invalid; script-src *",
        "*",
        "https://a.invalid https://b.invalid",
        "javascript:alert(1)",
        "https://tiles.invalid/path",
        "https://tiles.invalid\r\nX-Injected: 1",
        # Tiles in the clear are refused, except from a loopback host.
        "http://tiles.invalid",
        "http://tiles.invalid:8080",
        "http://127.0.0.2.invalid",
    ):
        monkeypatch.setenv("THROUGHLINE_WEB_TILE_ORIGIN", bad)
        assert client.get("/ui/").headers["content-security-policy"] == CSP, bad
    for loopback in ("http://localhost:8090", "http://127.0.0.1:8090"):
        monkeypatch.setenv("THROUGHLINE_WEB_TILE_ORIGIN", loopback)
        assert f"connect-src 'self' {loopback};" in client.get("/ui/").headers[
            "content-security-policy"
        ]


def test_plain_http_tile_origin_is_refused_with_a_startup_warning(
    monkeypatch, caplog
):
    monkeypatch.setenv("THROUGHLINE_WEB_TILE_ORIGIN", "http://tiles.invalid")
    with caplog.at_level("WARNING", logger="throughline.web"):
        assert web.install(FastAPI()) is True
    assert "THROUGHLINE_WEB_TILE_ORIGIN is ignored" in caplog.text
    assert web.tile_origin() is None and "tiles.invalid" not in web.csp()
    caplog.clear()
    monkeypatch.setenv("THROUGHLINE_WEB_TILE_ORIGIN", "https://tiles.invalid")
    with caplog.at_level("WARNING", logger="throughline.web"):
        web.install(FastAPI())
    assert "ignored" not in caplog.text


def test_shell_starts_no_workers_and_the_front_end_uses_none():
    policy = web.csp()
    assert "worker-src 'none'" in policy and "blob:" not in policy.split("img-src")[0]
    assert "child-src" not in policy and "worker-src 'self'" not in policy
    source = Path(__file__).resolve().parents[1] / "web" / "src"
    for path in source.rglob("*.ts*"):
        text = path.read_text()
        assert "new Worker(" not in text and "new SharedWorker(" not in text, path
        assert "?worker" not in text, path


# --- bootstrap ----------------------------------------------------------------

CHANNEL_KEYS = {
    "name",
    "plugin",
    "description",
    "record_kind",
    "is_reference",
    "is_outside",
    "whole_thread",
    "embed",
    "fields",
    "view",
    "rows",
    "threads",
    "first_at",
    "last_at",
    "last_ingest_at",
    "with_geo",
    "with_ended_at",
    "with_images",
    "coverage",
}
PLUGIN_KEYS = {
    "name",
    "version",
    "mode",
    "interval_s",
    "last_success_at",
    "last_error",
    "messages_ingested",
    "stale",
    "updated_at",
    "open_outage",
    "last_attempt_at",
    "last_error_at",
    "consecutive_failures",
    "paused_at",
    "paused_reason",
}


def test_bootstrap_for_the_owner(world):
    client = world["client"]
    ok(
        client.post(
            "/v1/plugins/synthetic/manifest",
            json={"name": "synthetic", "version": "1.2.3", "run_mode": "one-shot"},
        )
    )
    body = ok(client.get("/v1/web/bootstrap"))
    assert set(body) == {
        "version",
        "now",
        "token",
        "features",
        "owner_person_id",
        "totals",
        "plugins",
        "channels",
    }
    assert body["token"] == {
        "id": body["token"]["id"],
        "kind": "owner",
        "name": "synthetic owner",
        "asker": None,
        "unrestricted": True,
        "can_write": True,
    }
    features = body["features"]
    assert set(features) == {
        "web_state",
        "source_health",
        "embeddings",
        "assistant",
        "egress",
    }
    assert features["web_state"] is True and features["source_health"] is True
    assert set(features["embeddings"]) == {"available", "model"}
    assert set(features["assistant"]) == {
        "provider",
        "model",
        "loopback",
        "default_tier",
        "tiers",
        "configured",
    }
    assert features["egress"]["providers"] and "rule" in features["egress"]
    assert body["totals"] == {
        "records": 10,
        "channels": 2,
        "plugins": 1,
        "people": 0,
        "entities": 0,
        "links": 0,
        "threads": 2,
        "vectors": 0,
    }
    assert [c["name"] for c in body["channels"]] == [HIDDEN, OPEN]
    hidden, open_ = body["channels"]
    assert set(hidden) == CHANNEL_KEYS
    assert hidden["view"] == "api.ch_synthetic_hidden"
    assert (hidden["rows"], hidden["threads"], hidden["with_geo"]) == (4, 1, 4)
    assert (open_["rows"], open_["with_geo"], open_["with_images"]) == (6, 0, 0)
    assert open_["first_at"] < open_["last_at"] and open_["last_ingest_at"]
    assert open_["embed"] is True and open_["whole_thread"] is False
    assert open_["fields"] == [
        {"name": "rating", "type": "integer", "description": "Score"}
    ]
    assert set(open_["coverage"]) == {
        "status",
        "reasons",
        "covered_from",
        "covered_to",
        "open_gaps",
    }
    assert open_["coverage"]["open_gaps"] == 0
    # The view named for a channel is queryable as given.
    rows = ok(
        client.post(
            "/v1/sql", json={"statement": f"SELECT count(*) AS n FROM {open_['view']}"}
        )
    )
    assert rows["rows"] == [{"n": 6}]
    (plugin,) = body["plugins"]
    assert set(plugin) == PLUGIN_KEYS
    assert (plugin["name"], plugin["version"], plugin["mode"]) == (
        "synthetic",
        "1.2.3",
        "one-shot",
    )
    assert plugin["interval_s"] == 600 and plugin["stale"] is False
    assert plugin["open_outage"] is None
    row = audit_rows()[-2]
    assert row["statement"] == "GET /v1/web/bootstrap" and row["row_count"] == 2
    # A full-read agent sees the same, but does not write records.
    agent = ok(client.get("/v1/web/bootstrap", headers=world["full"]))
    assert agent["token"]["unrestricted"] and not agent["token"]["can_write"]
    assert agent["totals"] == body["totals"]


def test_bootstrap_for_a_restricted_asker_goes_through_the_gate(world):
    for response in (
        world["guest"].get("/v1/web/bootstrap"),
        world["client"].get("/v1/web/bootstrap", headers=world["as_guest"]),
    ):
        body = ok(response)
        text = json.dumps(body)
        assert CANARY not in text and HIDDEN not in text
        assert "synthetic_hidden" not in text
        assert body["token"]["asker"] == GUEST
        assert body["token"]["unrestricted"] is False
        assert body["token"]["can_write"] is False
        assert [c["name"] for c in body["channels"]] == [OPEN]
        (channel,) = body["channels"]
        assert set(channel) == CHANNEL_KEYS
        assert channel["rows"] == 6 and channel["view"] == "api.ch_synthetic_open"
        # What only the app role can read is left out, not guessed.
        assert channel["embed"] is None
        assert body["plugins"] == []
        assert body["totals"]["vectors"] is None
        assert body["totals"]["records"] == 6 and body["totals"]["channels"] == 1
        assert body["totals"]["people"] == 0 and body["owner_person_id"] is None
        assert body["features"]["web_state"] is True


def test_bootstrap_refuses_a_plugin_token(world):
    r = world["client"].get("/v1/web/bootstrap", headers=world["plugin"])
    assert r.status_code == 403


def test_bootstrap_on_a_database_without_the_web_migration(world):
    client = world["client"]
    with connect() as c:
        c.execute("ALTER TABLE tl.web_state RENAME TO web_state_absent")
    try:
        body = ok(client.get("/v1/web/bootstrap"))
        assert body["features"]["web_state"] is False
        assert len(body["channels"]) == 2
        assert (
            ok(world["guest"].get("/v1/web/bootstrap"))["features"]["web_state"]
            is False
        )
        r = client.get("/v1/web/state/prefs")
        assert r.status_code == 400 and r.json()["code"] == "42P01"
    finally:
        with connect() as c:
            c.execute("ALTER TABLE tl.web_state_absent RENAME TO web_state")


def test_bootstrap_shows_source_health_columns_when_present(world):
    client = world["client"]
    with connect() as c:
        c.execute(
            "UPDATE tl.plugins SET last_attempt_at = now(), consecutive_failures = 3"
        )
    body = ok(client.get("/v1/web/bootstrap"))
    assert body["features"]["source_health"] is True
    (plugin,) = body["plugins"]
    assert set(plugin) == PLUGIN_KEYS
    assert plugin["consecutive_failures"] == 3 and plugin["last_attempt_at"]
    assert ok(client.get("/v1/web/arrivals"))["plugins"][0]["paused_at"] is None
    with connect() as c:
        c.execute(
            "ALTER TABLE tl.plugins DROP COLUMN last_attempt_at, DROP COLUMN last_error_at,"
            " DROP COLUMN consecutive_failures, DROP COLUMN paused_at, DROP COLUMN paused_reason"
        )
    try:
        assert ok(client.get("/v1/web/bootstrap"))["features"]["source_health"] is False
    finally:
        with connect() as c:
            c.execute(
                "ALTER TABLE tl.plugins ADD COLUMN last_attempt_at timestamptz,"
                " ADD COLUMN last_error_at timestamptz, ADD COLUMN consecutive_failures int NOT NULL DEFAULT 0,"
                " ADD COLUMN paused_at timestamptz, ADD COLUMN paused_reason text"
            )


# --- state --------------------------------------------------------------------


def test_state_crud(world):
    client = world["client"]
    assert ok(client.get("/v1/web/state/saved_query")) == []
    assert client.get("/v1/web/state/saved_query/first").status_code == 404
    value = {"title": "Quiet days", "sql": "SELECT 1", "tags": ["a", 2, None, True]}
    put = ok(client.put("/v1/web/state/saved_query/first", json={"value": value}))
    assert set(put) == {"key", "value", "updated_at"}
    assert put["key"] == "first" and put["value"] == value
    assert ok(client.get("/v1/web/state/saved_query/first")) == put
    ok(client.put("/v1/web/state/saved_query/another one", json={"value": [1, 2]}))
    ok(client.put("/v1/web/state/prefs/theme", json={"value": "dark"}))
    listed = ok(client.get("/v1/web/state/saved_query"))
    assert [d["key"] for d in listed] == ["another one", "first"]
    assert listed[1] == put
    # Replacing keeps one document and moves updated_at.
    again = ok(client.put("/v1/web/state/saved_query/first", json={"value": 7}))
    assert again["value"] == 7 and again["updated_at"] > put["updated_at"]
    assert len(ok(client.get("/v1/web/state/saved_query"))) == 2
    assert ok(client.delete("/v1/web/state/saved_query/first")) == {"deleted": True}
    assert ok(client.delete("/v1/web/state/saved_query/first")) == {"deleted": False}
    assert client.get("/v1/web/state/saved_query/first").status_code == 404
    assert ok(client.get("/v1/web/state/prefs/theme"))["value"] == "dark"
    # Each request left one audit row with identifiers only.
    rows = audit_rows()
    assert rows[-1]["statement"] == "GET /v1/web/state/prefs/theme"
    assert not [r for r in rows if "Quiet days" in json.dumps(r, default=str)]


def test_state_rejects_bad_requests_without_server_errors(world):
    client = world["client"]
    assert client.put("/v1/web/state/prefs/theme", json={}).status_code == 422
    assert (
        client.put("/v1/web/state/prefs/theme", json={"value": None}).status_code == 400
    )
    for kind in ("Prefs", "0", "a-b", "x" * 41, "prefs;drop"):
        assert client.get(f"/v1/web/state/{kind}").status_code == 400, kind
        r = client.put(f"/v1/web/state/{kind}/k", json={"value": 1})
        assert r.status_code == 400, kind
    assert (
        client.put("/v1/web/state/prefs/" + "k" * 201, json={"value": 1}).status_code
        == 400
    )
    big = {"value": "x" * (512 * 1024 + 1)}
    assert client.put("/v1/web/state/prefs/big", json=big).status_code == 413
    assert (
        client.put("/v1/web/state/prefs/nul", json={"value": "a\u0000b"}).status_code
        == 400
    )
    assert ok(client.get("/v1/web/state/prefs")) == []
    assert (
        client.get("/v1/web/state/prefs", headers=world["plugin"]).status_code == 403
    )
    r = client.put(
        "/v1/web/state/prefs/k", json={"value": 1}, headers=world["plugin"]
    )
    assert r.status_code == 403


def test_state_is_the_owners_and_separate_per_agent_token_and_asker(world):
    client, guest = world["client"], world["guest"]
    ok(client.put("/v1/web/state/board/home", json={"value": {"owner": True}}))
    # An unrestricted agent token has a namespace of its own: it can neither
    # read nor replace the owner's documents (a saved query it wrote would
    # otherwise run later in the owner's browser).
    full = world["full"]
    assert ok(client.get("/v1/web/state/board", headers=full)) == []
    assert client.get("/v1/web/state/board/home", headers=full).status_code == 404
    ok(
        client.put(
            "/v1/web/state/board/home", json={"value": {"agent": True}}, headers=full
        )
    )
    assert ok(client.get("/v1/web/state/board/home", headers=full))["value"] == {
        "agent": True
    }
    assert ok(client.delete("/v1/web/state/board/home", headers=full)) == {
        "deleted": True
    }
    assert ok(client.get("/v1/web/state/board/home"))["value"] == {"owner": True}
    # Another owner token is the owner's interface on another device.
    other_owner = {"Authorization": "Bearer " + create_token("second owner")["token"]}
    assert ok(client.get("/v1/web/state/board/home", headers=other_owner))[
        "value"
    ] == {"owner": True}
    # A restricted asker has its own namespace: it neither reads nor replaces
    # the owner's documents, whichever token names the asker.
    assert ok(guest.get("/v1/web/state/board")) == []
    assert guest.get("/v1/web/state/board/home").status_code == 404
    ok(guest.put("/v1/web/state/board/home", json={"value": {"guest": True}}))
    assert ok(client.get("/v1/web/state/board/home"))["value"] == {"owner": True}
    assert ok(client.get("/v1/web/state/board/home", headers=world["as_guest"]))[
        "value"
    ] == {"guest": True}
    assert ok(guest.delete("/v1/web/state/board/home")) == {"deleted": True}
    assert ok(client.get("/v1/web/state/board/home"))["value"] == {"owner": True}
    with connect() as c:
        rows = c.execute(
            "SELECT namespace,kind,key FROM tl.web_state ORDER BY 1,2,3"
        ).fetchall()
    assert rows == [{"namespace": "", "kind": "board", "key": "home"}]


def test_state_holds_at_most_a_bounded_number_of_documents(world, monkeypatch):
    from throughline.web import store

    monkeypatch.setattr(store, "MAX_DOCUMENTS", 3)
    client = world["client"]
    for i in range(3):
        ok(client.put(f"/v1/web/state/colors/c{i}", json={"value": i}))
    assert client.put("/v1/web/state/colors/c3", json={"value": 3}).status_code == 400
    ok(client.put("/v1/web/state/colors/c0", json={"value": "replaced"}))
    ok(client.put("/v1/web/state/prefs/c3", json={"value": 3}))


def test_state_kinds_are_the_interfaces_own(world):
    client = world["client"]
    for kind in ("prefs", "colors", "overrides", "saved_query", "board", "import_mapping"):
        ok(client.put(f"/v1/web/state/{kind}/k", json={"value": 1}))
        assert ok(client.get(f"/v1/web/state/{kind}"))[0]["value"] == 1
    # Any other kind is refused, so documents cannot spread over new kinds.
    for kind in ("notes", "prefs2", "saved_queries"):
        r = client.put(f"/v1/web/state/{kind}/k", json={"value": 1})
        assert r.status_code == 400 and "kind must be one of" in r.json()["detail"]
        assert client.get(f"/v1/web/state/{kind}").status_code == 400
    # The front end's list of kinds is the server's.
    import re

    from throughline.web import store

    source = (Path(__file__).resolve().parents[1] / "web/src/api/types.ts").read_text()
    block = re.search(r"export type UiStateKind =([^;]*);", source).group(1)
    assert set(re.findall(r'"([a-z_]+)"', block)) == set(store.KINDS)


def test_state_namespace_holds_a_bounded_number_of_bytes(world, monkeypatch):
    from throughline.web import store

    monkeypatch.setattr(store, "MAX_NAMESPACE_BYTES", 3000)
    client, guest = world["client"], world["guest"]
    chunk = "x" * 900
    for i in range(3):
        ok(client.put(f"/v1/web/state/board/b{i}", json={"value": chunk}))
    # Across kinds: the fourth document would pass the namespace's total.
    r = client.put("/v1/web/state/saved_query/q", json={"value": chunk})
    assert r.status_code == 413 and "exceed" in r.json()["detail"]
    # Replacing a document counts its new size only.
    ok(client.put("/v1/web/state/board/b0", json={"value": chunk + "y" * 100}))
    # Another namespace has its own total.
    ok(guest.put("/v1/web/state/board/b0", json={"value": chunk}))
    ok(client.delete("/v1/web/state/board/b1"))
    ok(client.put("/v1/web/state/saved_query/q", json={"value": chunk}))
    # The listing still returns values (bounded by the same total).
    assert all(d["value"] for d in ok(client.get("/v1/web/state/board")))


# --- arrivals -----------------------------------------------------------------

ROW_KEYS = {
    "id",
    "channel",
    "thread_id",
    "happened_at",
    "ingested_at",
    "author",
    "person_id",
    "excerpt",
    "version",
    "settled",
}


def decode(cursor):
    return json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))


def more(n, start=100, channel=OPEN, **extra):
    return [
        message(
            1000 + start + i, channel=channel, external_id=f"more-{start + i}", **extra
        )
        for i in range(n)
    ]


def test_arrivals_without_a_cursor_answer_immediately(world):
    client = world["client"]
    started = time.monotonic()
    body = ok(client.get("/v1/web/arrivals", params={"wait": 20, "limit": 4}))
    assert time.monotonic() - started < 5
    assert set(body) == {
        "cursor",
        "rows",
        "counts",
        "sampled",
        "plugins",
        "reads",
        "horizon",
        "settle_s",
    }
    assert len(body["rows"]) == 4 and set(body["rows"][0]) == ROW_KEYS
    ids = [r["id"] for r in body["rows"]]
    assert ids == sorted(ids)
    with connect() as c:
        assert ids[-1] == c.execute("SELECT max(id) AS n FROM tl.messages").fetchone()[
            "n"
        ]
    assert body["counts"] == {} and body["sampled"] is False
    assert [p["name"] for p in body["plugins"]] == ["synthetic"]
    assert body["settle_s"] == 120
    # Nothing new: the same question with the cursor answers empty at once.
    quiet = ok(client.get("/v1/web/arrivals", params={"after": body["cursor"]}))
    assert quiet["rows"] == [] and quiet["counts"] == {} and quiet["plugins"] == []
    # Only one channel.
    only = ok(client.get("/v1/web/arrivals", params={"channels": HIDDEN}))
    assert {r["channel"] for r in only["rows"]} == {HIDDEN}
    assert CANARY in only["rows"][0]["excerpt"]


def test_arrivals_excerpt_is_cut_and_rows_carry_their_version(world):
    client = world["client"]
    cursor = ok(client.get("/v1/web/arrivals"))["cursor"]
    long = message(5000, channel=OPEN, external_id="long", content="x" * 1000)
    assert ingest(client, [long]).status_code == 200
    assert (
        ingest(client, [dict(long, content="y" * 300, payload={"v": 2})]).status_code
        == 200
    )
    body = ok(client.get("/v1/web/arrivals", params={"after": cursor}))
    assert [(r["version"], len(r["excerpt"])) for r in body["rows"]] == [
        (1, 280),
        (2, 280),
    ]
    assert body["counts"] == {OPEN: 2}
    assert body["rows"][0]["thread_id"] and body["rows"][0]["author"] == "fake-account"


def test_arrivals_wait_returns_when_something_is_ingested(world):
    client = world["client"]
    cursor = ok(client.get("/v1/web/arrivals"))["cursor"]
    # Nothing arrives: the request is held for the wait, then answers empty.
    started = time.monotonic()
    quiet = ok(client.get("/v1/web/arrivals", params={"after": cursor, "wait": 1}))
    assert 0.9 < time.monotonic() - started < 5
    assert quiet["rows"] == []

    def later():
        time.sleep(0.6)
        with TestClient(client.app) as other:
            other.headers["Authorization"] = client.headers["Authorization"]
            assert ingest(other, more(3)).status_code == 200

    thread = threading.Thread(target=later)
    thread.start()
    started = time.monotonic()
    body = ok(
        client.get("/v1/web/arrivals", params={"after": quiet["cursor"], "wait": 20})
    )
    elapsed = time.monotonic() - started
    thread.join()
    assert 0.5 < elapsed < 10
    assert len(body["rows"]) == 3 and body["counts"] == {OPEN: 3}
    assert [r["excerpt"] for r in body["rows"]] == [
        f"Synthetic orchard message {1100 + i}" for i in range(3)
    ]
    # Delivered once.
    after = ok(client.get("/v1/web/arrivals", params={"after": body["cursor"]}))
    assert after["rows"] == [] and after["counts"] == {}


def test_arrivals_sample_a_bulk_load_with_exact_counts(world):
    client = world["client"]
    cursor = ok(client.get("/v1/web/arrivals"))["cursor"]
    assert ingest(client, more(450)).status_code == 200
    assert ingest(client, more(30, start=900, channel=HIDDEN)).status_code == 200
    body = ok(client.get("/v1/web/arrivals", params={"after": cursor, "limit": 50}))
    assert body["sampled"] is True and len(body["rows"]) == 50
    assert body["counts"] == {OPEN: 450, HIDDEN: 30}
    ids = [r["id"] for r in body["rows"]]
    with connect() as c:
        top = c.execute("SELECT max(id) AS n FROM tl.messages").fetchone()["n"]
    assert ids == sorted(set(ids)) and ids[-1] == top
    assert {r["channel"] for r in body["rows"]} == {OPEN, HIDDEN}
    # The cursor covers the whole batch, not only the sample.
    after = ok(client.get("/v1/web/arrivals", params={"after": body["cursor"]}))
    assert after["rows"] == [] and after["counts"] == {}
    # Default limit: at most 200 rows.
    assert ingest(client, more(250, start=2000)).status_code == 200
    body = ok(client.get("/v1/web/arrivals", params={"after": after["cursor"]}))
    assert body["sampled"] and len(body["rows"]) == 200
    assert body["counts"] == {OPEN: 250}
    # A channel filter counts and shows only that channel, and still moves on.
    assert ingest(client, more(5, start=3000)).status_code == 200
    assert ingest(client, more(2, start=3100, channel=HIDDEN)).status_code == 200
    body = ok(
        client.get(
            "/v1/web/arrivals", params={"after": body["cursor"], "channels": HIDDEN}
        )
    )
    assert body["counts"] == {HIDDEN: 2} and len(body["rows"]) == 2
    assert not body["sampled"]
    assert (
        ok(client.get("/v1/web/arrivals", params={"after": body["cursor"]}))["rows"]
        == []
    )


def test_arrivals_take_a_bounded_number_of_records_per_answer(world, monkeypatch):
    monkeypatch.setattr(web_routes, "SCAN", 40)
    client = world["client"]
    cursor = ok(client.get("/v1/web/arrivals"))["cursor"]
    assert ingest(client, more(100)).status_code == 200
    total, answers = 0, 0
    while True:
        body = ok(client.get("/v1/web/arrivals", params={"after": cursor}))
        if not body["rows"]:
            break
        cursor, answers = body["cursor"], answers + 1
        total += sum(body["counts"].values())
    assert (total, answers) == (100, 3)


def test_arrivals_snapshot_looks_at_a_bounded_window(world, monkeypatch):
    client = world["client"]
    assert ingest(client, more(30)).status_code == 200
    monkeypatch.setattr(web_routes, "SCAN", 8)
    with connect() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM tl.messages ORDER BY id")]
    body = ok(client.get("/v1/web/arrivals", params={"limit": 5}))
    _, w, seen, _, _ = decode(body["cursor"])
    # Only the newest SCAN ids are remembered; the floor sits just below them.
    covered = [i for lo, hi in seen for i in range(lo, hi + 1)]
    assert len(covered) <= 8 and w >= ids[-9]
    assert all(i > w for i in covered)
    # Nothing that existed at the snapshot comes back as an arrival.
    again = ok(client.get("/v1/web/arrivals", params={"after": body["cursor"]}))
    assert again["rows"] == [] and again["counts"] == {}


def test_arrivals_fragmented_cursor_folds_old_ranges_and_repeats_nothing(
    world, monkeypatch
):
    client = world["client"]
    with connect() as c:
        made = []
        for i in range(4):
            assert ingest(client, more(1, start=500 + i)).status_code == 200
            made.append(c.execute("SELECT max(id) AS id FROM tl.messages").fetchone()["id"])
            # Burn an id: the sequence gets holes, as rolled-back ingests leave.
            c.execute("SELECT nextval(pg_get_serial_sequence('tl.messages','id'))")
    a, b, d, e = made
    assert b > a + 1 and d > b + 1 and e > d + 1
    monkeypatch.setattr(web_routes, "MAX_RANGES", 2)
    # b and e were delivered already; a and d are the gaps.
    cursor = web_routes._encode(dict(w=a - 1, seen=[[b, b], [e, e]], a=0, p=None))
    delivered = []
    for _ in range(3):
        body = ok(client.get("/v1/web/arrivals", params={"after": cursor}))
        delivered += [r["id"] for r in body["rows"]]
        cursor = body["cursor"]
        assert len(decode(cursor)[2]) <= 2
    assert sorted(delivered) == sorted({a, d}) and len(delivered) == 2


def test_arrivals_mark_rows_newer_than_the_settle_horizon(world):
    client = world["client"]
    first = ok(client.get("/v1/web/arrivals"))
    assert first["rows"] and not any(r["settled"] for r in first["rows"])
    # While rows are unsettled the watermark stays below them: they are
    # remembered as delivered ranges instead.
    _, watermark, seen, _, _ = decode(first["cursor"])
    assert watermark == 0 and seen == [[1, 10]]
    with connect() as c:
        c.execute("UPDATE tl.consumer_settings SET settle_s=2,margin_s=1")
    time.sleep(2.2)
    second = ok(client.get("/v1/web/arrivals"))
    assert second["settle_s"] == 2 and all(r["settled"] for r in second["rows"])
    assert second["horizon"] > second["rows"][-1]["ingested_at"]
    # After twice the settle window nothing below those ids can still appear,
    # so the watermark moves over them.
    time.sleep(2.2)
    third = ok(client.get("/v1/web/arrivals", params={"after": first["cursor"]}))
    assert third["rows"] == [] and decode(third["cursor"])[1:3] == [10, []]
    assert ingest(client, more(2)).status_code == 200
    fourth = ok(client.get("/v1/web/arrivals", params={"after": third["cursor"]}))
    assert [r["settled"] for r in fourth["rows"]] == [False, False]
    assert decode(fourth["cursor"])[1:3] == [10, [[11, 12]]]


def test_arrivals_deliver_a_late_commit_exactly_once(world):
    client = world["client"]
    start = ok(client.get("/v1/web/arrivals"))["cursor"]
    # A transaction takes an id and commits after higher ids are visible.
    with connect() as c:
        channel_id = c.execute(
            "SELECT id FROM tl.channels WHERE name=%s", (OPEN,)
        ).fetchone()["id"]
    late = connect()
    slow = late.__enter__()
    try:
        slow_id = slow.execute(
            "INSERT INTO tl.messages(channel_id,external_id,version,happened_at,content,payload_hash)"
            " VALUES (%s,'late-commit',1,now(),'Synthetic late commit','synthetic-hash') RETURNING id",
            (channel_id,),
        ).fetchone()["id"]
        assert ingest(client, more(3)).status_code == 200
        body = ok(client.get("/v1/web/arrivals", params={"after": start}))
        ids = [r["id"] for r in body["rows"]]
        assert len(ids) == 3 and min(ids) > slow_id
        assert ok(client.get("/v1/web/arrivals", params={"after": body["cursor"]}))[
            "rows"
        ] == []
        # A fresh cursor taken now must not lose it either.
        fresh = ok(client.get("/v1/web/arrivals"))["cursor"]
    finally:
        late.__exit__(None, None, None)
    for cursor in (body["cursor"], fresh):
        got = ok(client.get("/v1/web/arrivals", params={"after": cursor}))
        assert [r["id"] for r in got["rows"]] == [slow_id], decode(cursor)
        assert got["rows"][0]["excerpt"] == "Synthetic late commit"
        assert got["counts"] == {OPEN: 1}
        again = ok(client.get("/v1/web/arrivals", params={"after": got["cursor"]}))
        assert again["rows"] == []


def test_arrivals_reads_lane_is_for_the_owner_and_skips_the_interface(world):
    client = world["client"]
    cursor = ok(client.get("/v1/web/arrivals"))["cursor"]
    statement = "SELECT id FROM api.messages WHERE content <> '" + "q" * 400 + "'"
    ok(client.post("/v1/sql", json={"statement": statement}, headers=world["full"]))
    ok(
        client.post(
            "/v1/sql",
            json={"statement": "SELECT 2 AS from_the_interface"},
            headers={"X-Throughline-Client": "web"},
        )
    )
    ok(world["guest"].post("/v1/sql", json={"statement": "SELECT 3 AS guest"}))
    ok(client.get("/v1/web/bootstrap"))
    assert ingest(client, more(1)).status_code == 200
    body = ok(client.get("/v1/web/arrivals", params={"after": cursor, "wait": 5}))
    assert [set(r) for r in body["reads"]] == [
        {"at", "token", "asker", "statement", "row_count", "status"}
    ] * 2
    first, second = body["reads"]
    assert first["statement"] == statement[:300] and first["token"] == "full agent"
    assert (first["row_count"], first["status"], first["asker"]) == (10, 200, None)
    assert second["statement"] == "SELECT 3 AS guest" and second["asker"] == GUEST
    # Polling does not wake itself: its own audit rows are not reads.
    started = time.monotonic()
    quiet = ok(
        client.get("/v1/web/arrivals", params={"after": body["cursor"], "wait": 1})
    )
    assert quiet["reads"] == [] and time.monotonic() - started > 0.9
    # A read alone ends a wait.
    ok(client.post("/v1/sql", json={"statement": "SELECT 4 AS n"}))
    woken = ok(
        client.get("/v1/web/arrivals", params={"after": quiet["cursor"], "wait": 20})
    )
    assert [r["statement"] for r in woken["reads"]] == ["SELECT 4 AS n"]
    # An agent, even a full-read one, gets no reads lane.
    for params in ({}, {"after": cursor}):
        agent = ok(client.get("/v1/web/arrivals", params=params, headers=world["full"]))
        assert agent["reads"] == []
        assert agent["rows"]


def test_arrivals_report_changed_plugins(world):
    client = world["client"]
    cursor = ok(client.get("/v1/web/arrivals"))["cursor"]
    ok(
        client.post(
            "/v1/plugins/synthetic/heartbeat",
            headers=world["plugin"],
            json={"interval_s": 60, "error": "synthetic failure"},
        )
    )
    body = ok(client.get("/v1/web/arrivals", params={"after": cursor, "wait": 10}))
    (plugin,) = body["plugins"]
    assert set(plugin) == PLUGIN_KEYS
    assert plugin["interval_s"] == 60 and plugin["last_error"] == "synthetic failure"
    assert (
        ok(client.get("/v1/web/arrivals", params={"after": body["cursor"]}))["plugins"]
        == []
    )


def test_arrivals_refuse_restricted_askers_plugins_and_bad_input(world):
    client, guest = world["client"], world["guest"]
    assert guest.get("/v1/web/arrivals").status_code == 403
    assert client.get("/v1/web/arrivals", headers=world["as_guest"]).status_code == 403
    assert client.get("/v1/web/arrivals", headers=world["plugin"]).status_code == 403
    for params in (
        {"wait": 26},
        {"wait": -1},
        {"limit": 0},
        {"limit": 501},
        {"wait": "soon"},
    ):
        assert client.get("/v1/web/arrivals", params=params).status_code == 422
    good = ok(client.get("/v1/web/arrivals"))["cursor"]
    for cursor in (
        "",
        "nonsense",
        good[:-4],
        base64.urlsafe_b64encode(b'[1,5,[[3,4]],0,null]').decode(),
        base64.urlsafe_b64encode(b'[1,0,[[4,3]],0,null]').decode(),
        base64.urlsafe_b64encode(b'[2,0,[],0,null]').decode(),
        base64.urlsafe_b64encode(b'[1,0,[],0,"now(); DROP"]').decode(),
        base64.urlsafe_b64encode(b'[1,"0",[],0,null]').decode(),
        base64.urlsafe_b64encode(b'{"w":0}').decode(),
    ):
        r = client.get("/v1/web/arrivals", params={"after": cursor})
        assert r.status_code == 400 and r.json() == {"detail": "invalid cursor"}, cursor


def test_arrivals_cursor_of_another_database_starts_over(world):
    client = world["client"]
    stale = base64.urlsafe_b64encode(b"[1,999999,[],999999,null]").decode()
    body = ok(client.get("/v1/web/arrivals", params={"after": stale, "wait": 20}))
    assert len(body["rows"]) == 10 and body["counts"] == {}
    assert decode(body["cursor"])[1] < 999999


def test_arrivals_cap_the_number_of_waiting_requests(world, monkeypatch):
    client = world["client"]
    cursor = ok(client.get("/v1/web/arrivals"))["cursor"]
    monkeypatch.setattr(web_routes, "_waiting", 16)
    r = client.get("/v1/web/arrivals", params={"after": cursor, "wait": 20})
    assert r.status_code == 429 and r.headers["retry-after"] == "2"
    # Not waiting is always allowed, and the refusal did not take a place.
    assert client.get("/v1/web/arrivals", params={"after": cursor}).status_code == 200
    assert web_routes._waiting == 16
    monkeypatch.setattr(web_routes, "_waiting", 0)
    monkeypatch.setenv("THROUGHLINE_WEB_WAITERS", "1")
    started = time.monotonic()
    ok(client.get("/v1/web/arrivals", params={"after": cursor, "wait": 1}))
    assert time.monotonic() - started > 0.9 and web_routes._waiting == 0


# --- audit client tag ---------------------------------------------------------


def test_audit_records_the_client_header(world):
    client = world["client"]
    before = len(audit_rows())
    ok(client.get("/v1/web/bootstrap", headers={"X-Throughline-Client": "web"}))
    ok(
        client.post(
            "/v1/sql",
            json={"statement": "SELECT 1 AS one"},
            headers={"X-Throughline-Client": "cli"},
        )
    )
    ok(client.get("/v1/catalog", headers={"X-Throughline-Client": "sdk"}))
    ok(client.get("/v1/web/bootstrap", headers={"X-Throughline-Client": "WEB"}))
    ok(client.get("/v1/web/bootstrap", headers={"X-Throughline-Client": "curl <b>"}))
    ok(client.get("/v1/web/bootstrap"))
    ok(client.post("/v1/sql", json={"statement": "SELECT 1 AS one"}))
    details = [r["detail"] for r in audit_rows()[before:]]
    assert details == [
        {"client": "web"},
        {"route": "POST /v1/sql", "client": "cli"},
        {"route": "GET /v1/catalog", "client": "sdk"},
        None,
        None,
        None,
        {"route": "POST /v1/sql"},
    ]
