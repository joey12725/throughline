"""Onboarding: contract, settings store, first account, catalog, detection,
jobs (a real Google Timeline backfill through a live server), people
resolution and the MCP endpoint. Everything is synthetic."""

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from conftest import ingest
from throughline.api import create_app
from throughline.db import connect, create_token
from throughline.onboarding import contract, store
from throughline.onboarding.contract import ContractError, load_repo, parse_github

ROOT = Path(__file__).resolve().parents[1]


# ───────────── contract ─────────────


def test_starter_catalog_is_valid_and_private_plugins_are_absent():
    index = load_repo(ROOT / "plugins")
    ids = {p.id for p in index.plugins}
    assert len(ids) >= 40
    assert {"wave", "granola", "gbrain", "google_timeline", "beeper"} <= ids
    text = (ROOT / "plugins" / "throughline-plugins.toml").read_text().lower()
    for private in ("morning_context", "morning-context", "reddit-full"):
        assert private not in text
    assert "who" not in ids
    for spec in index.plugins:
        assert spec.category in contract.CATEGORIES
        assert (ROOT / "plugins" / spec.path / "pyproject.toml").exists(), spec.id


def test_contract_reports_every_problem(tmp_path):
    (tmp_path / "throughline-plugins.toml").write_text(
        'schema = 1\n[[plugins]]\nid = "Bad Id"\nname = "x"\nsummary = "y"\n'
        '[[plugins]]\nid = "ok"\nname = "x"\nsummary = "y"\ncategory = "nope"\n'
    )
    with pytest.raises(ContractError) as error:
        load_repo(tmp_path)
    wheres = " ".join(p["where"] for p in error.value.problems)
    assert "plugins[0]" in wheres and "plugins[1]" in wheres


def test_single_plugin_repo_and_missing_metadata(tmp_path):
    with pytest.raises(ContractError, match="no throughline-plugins.toml"):
        load_repo(tmp_path)
    (tmp_path / "throughline-plugin.toml").write_text(
        'id = "demo"\nname = "Demo"\nsummary = "A demo"\ncategory = "notes"\n'
        '[[setup.fields]]\nkey = "DEMO_KEY"\nlabel = "Key"\nkind = "secret"\nrequired = true\n'
    )
    index = load_repo(tmp_path)
    assert index.plugins[0].secret_keys() == {"DEMO_KEY"}


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/acme/tl-plugins", ("acme", "tl-plugins", None)),
        ("github.com/acme/tl-plugins.git", ("acme", "tl-plugins", None)),
        ("acme/tl-plugins", ("acme", "tl-plugins", None)),
        ("https://github.com/acme/tl-plugins/tree/dev", ("acme", "tl-plugins", "dev")),
    ],
)
def test_parse_github(url, expected):
    assert parse_github(url) == expected


@pytest.mark.parametrize("url", ["https://gitlab.com/a/b", "file:///etc", "https://github.com/a", "https://github.com/a/b/tree/../x"])
def test_parse_github_refuses(url):
    with pytest.raises(ContractError):
        parse_github(url)


# ───────────── store ─────────────


def test_store_secrets_are_private_masked_and_redacted():
    store.set_plugin_values("demo", {"DEMO_KEY": "supersecretvalue123", "DEMO_DIR": "/tmp"}, secret_keys={"DEMO_KEY"})
    secrets_file = store.setup_dir() / "secrets.json"
    assert oct(secrets_file.stat().st_mode & 0o777) == "0o600"
    public = store.public_plugin_values("demo", {"DEMO_KEY"})
    assert public["DEMO_KEY"] == "••••e123" and public["DEMO_DIR"] == "/tmp"
    # A masked value sent back leaves the secret unchanged.
    store.set_plugin_values("demo", {"DEMO_KEY": "••••e123"}, secret_keys={"DEMO_KEY"})
    assert store.plugin_values("demo")["DEMO_KEY"] == "supersecretvalue123"
    assert store.redact("token=supersecretvalue123!") == "token=[REDACTED]!"
    store.drop_plugin("demo")


def test_store_env_applies_and_respects_locked(monkeypatch):
    store.set_env({"THROUGHLINE_TEST_ONBOARDING_X": "1"})
    assert os.environ["THROUGHLINE_TEST_ONBOARDING_X"] == "1"
    store.set_env({"THROUGHLINE_TEST_ONBOARDING_X": None})
    assert "THROUGHLINE_TEST_ONBOARDING_X" not in os.environ
    monkeypatch.setitem(store._ORIGINAL, "THROUGHLINE_TEST_LOCKED", "server")
    with pytest.raises(PermissionError):
        store.set_env({"THROUGHLINE_TEST_LOCKED": "browser"})


# ───────────── routes ─────────────


def test_setup_routes_are_owner_only(client):
    agent = create_token("agent", "agent")["token"]
    r = client.get("/v1/setup/state", headers={"Authorization": "Bearer " + agent})
    assert r.status_code == 403
    r = client.get("/v1/setup/state")
    assert r.status_code == 200 and r.json()["status"] == "fresh"
    r = client.put("/v1/setup/state", json={"steps": {"models": {"status": "done"}}, "choices": {"sources": ["wave"]}})
    assert r.json()["steps"]["models"]["status"] == "done"
    assert r.json()["choices"]["sources"] == ["wave"]
    r = client.put("/v1/setup/state", json={"steps": {"nope": {"status": "done"}}})
    assert r.status_code == 400 and r.json()["detail"]["title"] == "Unknown step"
    assert client.post("/v1/setup/complete", json={}).json()["status"] == "done"


def test_first_account_needs_the_setup_code(client):
    from throughline.onboarding import owner

    anonymous = {"X-Throughline-Client": "web", "Authorization": ""}
    info = client.get("/v1/auth/setup", headers=anonymous).json()
    assert info["needs_account"] is True and info["code_required"] is True
    code = owner.setup_code()
    body = {"code": "WRONG-CODE-0", "handle": "owner", "display_name": "Owner", "password": "a long enough phrase"}
    r = client.post("/v1/auth/setup", json=body, headers=anonymous)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "bad_setup_code"
    r = client.post("/v1/auth/setup", json=dict(body, code=code, password="short"), headers=anonymous)
    assert r.status_code == 422
    r = client.post("/v1/auth/setup", json=dict(body, code=code.lower()), headers=anonymous)
    assert r.status_code == 201, r.text
    assert r.json()["account"]["handle"] == "owner"
    assert "set-cookie" in r.headers
    assert client.get("/v1/auth/setup", headers=anonymous).json()["needs_account"] is False
    r = client.post("/v1/auth/setup", json=dict(body, code=code), headers=anonymous)
    assert r.status_code == 409


def test_catalog_and_detection(client, channel):
    r = client.get("/v1/setup/catalog")
    assert r.status_code == 200, r.text
    view = r.json()
    wave = next(p for p in view["plugins"] if p["id"] == "wave")
    assert wave["status"]["connected"] is False and wave["repo_id"] == "starter"
    assert {c["id"] for c in view["categories"]} >= {"messages", "location"}
    # Data in a plugin's channel makes it "already connected".
    client.post("/v1/channels", json=dict(name="transcripts.wave", plugin="wave", fields=[]))
    r = client.post("/v1/ingest/messages", json=dict(plugin="wave", messages=[dict(channel="transcripts.wave", external_id="w1", thread={"external_id": "t"},
                             happened_at="2024-01-01T00:00:00+00:00", content="Synthetic meeting",
                             author=dict(system="wave", kind="speaker", value="Speaker A"), payload={})]))
    assert r.status_code == 200, r.text
    detected = client.get("/v1/setup/detect").json()
    assert detected["existing"] is True
    wave = next(s for s in detected["sources"] if s["plugin_id"] == "wave")
    assert wave["connected"] and wave["records"] == 1


def test_save_config_validates_fields(client):
    r = client.put("/v1/setup/plugins/google_timeline/config", json={"values": {"GOOGLE_TIMELINE_EXPORT": "/does/not/exist.json"}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["problems"][0]["where"] == "GOOGLE_TIMELINE_EXPORT"
    r = client.put("/v1/setup/plugins/google_timeline/config", json={"values": {"NOT_A_SETTING": "x"}})
    assert r.status_code == 400 and "doesn't read" in r.json()["detail"]["message"]
    assert client.post("/v1/setup/plugins/nope/check", json={}).status_code == 404


def test_add_repo_refuses_bad_links(client):
    r = client.post("/v1/setup/repos", json={"url": "https://example.com/x/y"})
    assert r.status_code == 400
    assert "GitHub" in r.json()["detail"]["title"]


def test_providers_view_and_test_reports_missing(client):
    view = client.get("/v1/setup/providers").json()
    ids = {p["id"] for p in view["providers"]}
    assert {"openrouter", "voyage", "openai", "anthropic", "cli_proxy", "local", "immich"} <= ids
    r = client.post("/v1/setup/providers/voyage/test", json={"values": {}}).json()
    if not store.get_env("VOYAGE_API_KEY"):
        assert r["ok"] is False and r["title"] == "Missing details"
    r = client.post("/v1/setup/providers/local/test", json={"values": {"THROUGHLINE_LOCAL_BASE_URL": "http://127.0.0.1:9/v1"}}).json()
    assert r["ok"] is False and "refused" in r["title"].lower()
    r = client.put("/v1/setup/roles", json={"embedding": {"provider": "anthropic"}})
    assert r.status_code == 400 and "can't do that" in r.json()["detail"]["title"]


def test_upload_rejects_non_json(client):
    r = client.post("/v1/setup/uploads?plugin=google_timeline&field=GOOGLE_TIMELINE_EXPORT",
                    content=b"not json", headers={"X-Filename": "Timeline.json", "Content-Type": "application/octet-stream"})
    assert r.status_code == 422 and "JSON" in r.json()["detail"]["title"]


# ───────────── live server: backfill job and MCP ─────────────


@pytest.fixture()
def live(client):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    os.environ["THROUGHLINE_PLUGIN_API_URL"] = f"http://127.0.0.1:{port}"
    yield f"http://127.0.0.1:{port}"
    os.environ.pop("THROUGHLINE_PLUGIN_API_URL", None)
    server.should_exit = True
    thread.join(timeout=10)


def _timeline_export(path):
    segments = []
    for day in range(3):
        start = f"2024-03-0{day + 1}T09:00:00.000Z"
        end = f"2024-03-0{day + 1}T11:00:00.000Z"
        segments.append({"startTime": start, "endTime": end, "visit": {"hierarchyLevel": 0, "topCandidate": {
            "placeLocation": {"latLng": "40.0°, -75.0°"}}}})
    path.write_text(json.dumps({"semanticSegments": segments, "rawSignals": []}))


def _wait(client, job_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/v1/setup/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def test_google_timeline_upload_install_check_backfill(client, live, tmp_path):
    export = tmp_path / "Timeline.json"
    _timeline_export(export)
    r = client.post("/v1/setup/uploads?plugin=google_timeline&field=GOOGLE_TIMELINE_EXPORT",
                    content=export.read_bytes(), headers={"X-Filename": "Timeline.json", "Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    uploaded = r.json()["path"]
    install = client.post("/v1/setup/plugins/google_timeline/install", json={}).json()
    assert _wait(client, install["id"])["status"] == "done"
    r = client.put("/v1/setup/plugins/google_timeline/config", json={"values": {"GOOGLE_TIMELINE_EXPORT": uploaded}})
    assert r.status_code == 200, r.text
    assert r.json()["status"]["configured"] and r.json()["status"]["installed"]
    check = client.post("/v1/setup/plugins/google_timeline/check", json={}).json()
    assert check["ok"], check
    job = client.post("/v1/setup/plugins/google_timeline/backfill", json={}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "done", done
    assert done["progress"]["tracks"] > 50
    assert done["progress"]["oldest"].startswith("2024-03-01")
    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM tl.location_track WHERE source='google_timeline'").fetchone()["n"]
    assert n == done["progress"]["tracks"]
    listing = client.get("/v1/setup/jobs").json()
    assert listing["summary"]["done"] >= 1
    detected = client.get("/v1/setup/detect").json()
    assert any(s["plugin_id"] == "google_timeline" and s["connected"] for s in detected["sources"])
    # A plugin process never sees the server's database URL.
    from throughline.onboarding import catalog

    assert "THROUGHLINE_DATABASE_URL" not in catalog.plugin_base_env()


def test_failed_backfill_explains_itself(client, live):
    client.post("/v1/setup/plugins/google_timeline/install", json={})
    store.set_plugin_values("google_timeline", {"GOOGLE_TIMELINE_EXPORT": None})
    job = client.post("/v1/setup/plugins/google_timeline/backfill", json={}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "failed"
    assert done["error"]["title"] == "A required setting is missing"
    assert "Timeline" in done["error"]["message"] or "GOOGLE_TIMELINE_EXPORT" in done["error"]["message"]


def test_mcp_initialize_list_and_call(client, live):
    import urllib.request

    agent = create_token("mcp agent", "agent")["token"]

    def rpc(body):
        request = urllib.request.Request(live + "/v1/mcp", data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": "Bearer " + agent, "Content-Type": "application/json",
                                                  "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"null")

    status, init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}})
    assert status == 200 and init["result"]["serverInfo"]["name"] == "throughline"
    status, _ = rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    _, tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"find", "timeline", "orient", "sql", "person", "describe"} <= names
    _, called = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "sql", "arguments": {"statement": "SELECT 1 AS one"}}})
    assert called["result"]["isError"] is False
    assert '"one"' in called["result"]["content"][0]["text"]
    _, missing = rpc({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert missing["error"]["code"] == -32601


# ───────────── people ─────────────


def _person_messages(client):
    for name, channel, system in (("sms", "synthetic.sms", "sms"), ("mail", "synthetic.mail", "email"), ("chat", "synthetic.discord", "discord")):
        client.post("/v1/channels", json=dict(name=channel, plugin="synthetic", fields=[]))
    rows = []
    i = 0

    def add(channel, thread, system, kind, value, name, n=3):
        nonlocal i
        for _ in range(n):
            i += 1
            rows.append(dict(channel=channel, external_id=f"p{i}", thread={"external_id": thread},
                             happened_at=f"2024-02-01T00:{i % 60:02d}:00+00:00", content=f"Synthetic note {i}",
                             author=dict(system=system, kind=kind, value=value, display_name=name),
                             payload={}))

    # Same e-mail address in two systems: certain.
    add("synthetic.mail", "m1", "email", "email", "zibble.quark@example.test", "Zibble Quark")
    add("synthetic.sms", "s1", "imap", "email", "Zibble.Quark@example.test", "Zibble Quark")
    # The same unique full name in two systems: certain.
    add("synthetic.discord", "d1", "discord", "account", "moonjam42", "Orla Fennick")
    add("synthetic.mail", "m2", "email", "email", "orla@example.test", "Orla Fennick")
    # A first name that is part of a full name elsewhere: a question.
    add("synthetic.discord", "d2", "discord", "account", "kitefan", "Brindle")
    add("synthetic.mail", "m4", "email", "email", "brindle.oak@example.test", "Brindle Oak")
    # Generic speaker labels never match anything.
    add("synthetic.sms", "s2", "sms", "speaker_label", "rec1:Speaker 1", "Speaker 1")
    add("synthetic.mail", "m5", "email", "email", "speaker1@example.test", "Speaker 1")
    # The owner: the busiest sender.
    add("synthetic.mail", "m3", "email", "email", "me@example.test", "Pat Owner", n=40)
    add("synthetic.discord", "d3", "discord", "account", "patowner", "Pat Owner")
    r = ingest(client, rows)
    assert r.status_code == 200, r.text


def test_people_resolution_auto_merges_and_asks(client):
    _person_messages(client)
    view = client.get("/v1/setup/people").json()
    me = next(c for c in view["me_candidates"] if any(h["value"] == "me@example.test" for h in c["handles"]))
    view = client.post("/v1/setup/people/me", json={"person_id": me["id"]}).json()
    assert view["me"] and any(h["value"] == "me@example.test" for h in view["me"]["handles"])
    job = client.post("/v1/setup/people/resolve", json={}).json()
    done = _wait(client, job["id"])
    assert done["status"] == "done", done
    view = client.get("/v1/setup/people").json()
    names = [a["name"] for a in view["auto"]]
    assert "Zibble Quark" in names
    with connect() as conn:
        zibble = conn.execute(
            "SELECT count(DISTINCT person_id) AS n, count(*) AS ids FROM tl.identifiers WHERE lower(value) LIKE 'zibble%'"
        ).fetchone()
    assert zibble["n"] == 1 and zibble["ids"] == 2
    assert "Orla Fennick" in names
    with connect() as conn:
        owner = conn.execute(
            "SELECT p.is_owner FROM tl.identifiers i JOIN tl.people p ON p.id=i.person_id WHERE i.value='patowner'"
        ).fetchone()
    assert owner and owner["is_owner"]
    assert not any("Speaker" in q["prompt"] for q in view["questions"])
    question = next(q for q in view["questions"] if "Brindle" in q["prompt"])
    assert question["prompt"].startswith("Is ") and question["reasons"]
    view = client.post(f"/v1/setup/people/questions/{question['id']}", json={"answer": "same"}).json()
    assert not any("Brindle" in q["prompt"] for q in view["questions"])
    with connect() as conn:
        for values in (("moonjam42", "orla@example.test"), ("kitefan", "brindle.oak@example.test")):
            n = conn.execute(
                "SELECT count(DISTINCT person_id) AS n FROM tl.identifiers WHERE value = ANY(%s)", (list(values),)
            ).fetchone()["n"]
            assert n == 1, values
    # Running again asks nothing already answered.
    job = client.post("/v1/setup/people/resolve", json={}).json()
    _wait(client, job["id"])
    assert not any("Brindle" in q["prompt"] for q in client.get("/v1/setup/people").json()["questions"])


def test_agent_view_and_prompt(client):
    view = client.get("/v1/setup/agent").json()
    assert view["mcp_url"].endswith("/v1/mcp")
    assert {c["id"] for c in view["clients"]} >= {"claude-code", "cursor", "codex"}
    minted = client.post("/v1/setup/agent/token", json={"name": "laptop claude", "client": "claude-code"}).json()
    assert minted["token"] and minted["token"] in minted["clients"][0]["snippet"]
    prompt = client.get("/v1/setup/prompt").json()["markdown"]
    assert "/v1/setup/detect" in prompt and "/v1/setup/people/resolve" in prompt


# ───────────── first lenses, pages and queries ─────────────


def _chat_history(client):
    from datetime import datetime, timedelta, timezone

    client.post("/v1/channels", json=dict(name="chat.sms", plugin="beeper", fields=[]))
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(12):
        rows.append(dict(channel="chat.sms", external_id=f"c{i}", thread={"external_id": f"t{i % 3}"},
                         happened_at=(now - timedelta(days=1 + i % 2, minutes=i)).isoformat(),
                         content=f"Synthetic plan {i}: dinner Friday at the invented cafe",
                         author=dict(system="sms", kind="phone", value=f"+1555000{i % 3}", display_name=f"Friend {i % 3}"),
                         payload={}))
    r = client.post("/v1/ingest/messages", json=dict(plugin="beeper", messages=rows))
    assert r.status_code == 200, r.text


@pytest.fixture()
def table_model(monkeypatch):
    for key, value in {"THROUGHLINE_TABLE_LLM_BASE_URL": "http://127.0.0.1:9/v1",
                       "THROUGHLINE_TABLE_LLM_MODEL": "fake-model",
                       "THROUGHLINE_TABLE_LLM_PRICE_PER_MILLION": "0"}.items():
        monkeypatch.setenv(key, value)


def test_starter_lenses_define_quote_and_run(client, table_model):
    from throughline import lenses

    view = client.get("/v1/setup/lenses").json()
    assert {t["id"] for t in view["templates"]} == {"plans", "recommendations", "important_dates"}
    assert view["templates"][0]["available"] is False
    r = client.put("/v1/setup/lenses", json={"templates": ["plans"], "pace": "balanced"})
    assert r.status_code == 409 and "Nothing for lenses" in r.json()["detail"]["title"]
    _chat_history(client)
    view = client.get("/v1/setup/lenses").json()
    assert "chat.sms" in view["channels"] and view["model"]["ready"]
    r = client.put("/v1/setup/lenses", json={"templates": ["plans", "recommendations"], "pace": "thrifty"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {lens["name"] for lens in body["lenses"]} == {"starter_plans", "starter_recommendations"}
    assert body["lenses"][0]["quote"]["items"] >= 1
    lens = lenses.get_lens("starter_plans")
    assert lens["schedule"] == "30 3 * * *" and lens["channels"] == ["chat.sms"]
    assert client.get("/v1/setup/state").json()["choices"]["pace"] == "thrifty"

    def fake(tdef, payload, attempt):
        first = payload["items"][0]
        return {"rows": [{"title": "Dinner at the invented cafe", "when_text": "Friday", "kind": "plan",
                          "source_message_ids": [first["message_id"]] if "message_id" in first else [], "confidence": 0.9}]}

    with lenses.override_worker("llm", fake):
        job = client.post("/v1/setup/lenses/starter_plans/run", json={}).json()
        done = _wait(client, job["id"])
    assert done["status"] == "done", done
    # Unticking switches a starter lens off without deleting it.
    client.put("/v1/setup/lenses", json={"templates": ["plans"], "pace": "thrifty"})
    assert lenses.get_lens("starter_recommendations")["enabled"] is False


def test_pages_setup_defines_pages_lenses(client, table_model, tmp_path, monkeypatch):
    import re

    from throughline import lenses, pages
    from throughline.pages import librarian

    monkeypatch.setenv("THROUGHLINE_PAGES_CONFIG", str(tmp_path / "pages.toml"))
    view = client.get("/v1/setup/pages").json()
    assert view["available"] and {f["id"] for f in view["families"]} == {"daily", "people", "events"}
    r = client.put("/v1/setup/pages", json={"families": ["daily"], "pace": "balanced"})
    assert r.status_code == 409  # nothing to read yet
    _chat_history(client)
    r = client.put("/v1/setup/pages", json={"families": ["daily"], "pace": "live",
                                            "markdown_dir": str(tmp_path / "brain")})
    assert r.status_code == 200, r.text
    assert r.json()["written"] == ["daily"] and "daily" in r.json()["plans"]
    daily = lenses.get_lens("daily_pages")
    assert daily["schedule"] == "*/30 * * * *" and "chat.sms" in daily["channels"]
    assert "brain" in (tmp_path / "pages.toml").read_text()
    families = {f["id"]: f for f in r.json()["view"]["families"]}
    assert families["daily"]["enabled"] and not families["people"]["enabled"]
    # A pages lens the owner defined is never redefined from here.
    own = lenses.load_spec_file(f"{librarian.__file__.rsplit('/', 1)[0]}/examples/people_pages.toml")
    own["schedule"] = "5 5 * * *"
    lenses.define_lens(own)
    r = client.put("/v1/setup/pages", json={"families": ["daily", "people"], "pace": "thrifty"})
    assert r.json()["kept"] == ["people"] and lenses.get_lens("people_pages")["schedule"] == "5 5 * * *"
    assert lenses.get_lens("daily_pages")["schedule"] == "0 4 * * *"
    # Unticking switches it off without deleting it; export off removes onboarding's sink file.
    client.put("/v1/setup/pages", json={"families": ["people"], "pace": "thrifty"})
    assert lenses.get_lens("daily_pages")["enabled"] is False
    assert not (tmp_path / "pages.toml").exists()
    (tmp_path / "pages.toml").write_text('[[sinks]]\nkind = "markdown"\npath = "/mine/{slug}.md"\n')
    r = client.put("/v1/setup/pages", json={"families": ["people"], "pace": "thrifty", "markdown_dir": "/elsewhere"})
    assert r.json()["sinks_note"] and "/mine/" in (tmp_path / "pages.toml").read_text()
    client.put("/v1/setup/pages", json={"families": ["daily"], "pace": "thrifty"})

    def writer(tdef, payload, turn):
        day = payload["window"].get("day")
        lines = "\n".join("\n".join(b.get("lines") or []) for b in payload.get("evidence") or [])
        ev = [int(m) for m in re.findall(r"^\[m:(\d+)\]", lines, re.M)][:1]
        if not day or not ev:
            return {"actions": []}
        return {"actions": [{"op": "write", "slug": f"days/{day}", "kind": "days", "title": day, "summary": "s",
                             "body": f"## Summary\nThings happened [m:{ev[0]}]."}]}

    with pages.override_model(writer):
        job = client.post("/v1/setup/pages/daily/run", json={}).json()
        done = _wait(client, job["id"])
    assert done["status"] == "done", done
    assert done["progress"]["delivered"] >= 1, done


def test_first_queries_all_run(client):
    _person_messages(client)
    assert client.get("/v1/setup/queries").json()["queries"]  # some exist
    view = client.get("/v1/setup/people").json()
    me = next(c for c in view["me_candidates"] if any(h["value"] == "me@example.test" for h in c["handles"]))
    client.post("/v1/setup/people/me", json={"person_id": me["id"]})
    _wait(client, client.post("/v1/setup/people/resolve", json={}).json()["id"])
    queries = client.get("/v1/setup/queries").json()["queries"]
    ids = {q["id"] for q in queries}
    assert {"search", "sources", "people", "brief"} <= ids
    for q in queries:
        path = {"sql": "/v1/sql", "find": "/v1/find", "timeline": "/v1/timeline"}[q["kind"]]
        r = client.post(path, json=q["request"])
        assert r.status_code == 200, (q["id"], r.text[:300])


def test_setup_restore_routes_preserve_existing_sources_and_allow_adding_new_ones(client, tmp_path, monkeypatch):
    from throughline import backups

    # Agent tokens cannot access /v1/setup/restore.
    agent = create_token("agent", "agent")["token"]
    assert client.get("/v1/setup/restore", headers={"Authorization": "Bearer " + agent}).status_code == 403
    assert client.post("/v1/setup/restore", json={}, headers={"Authorization": "Bearer " + agent}).status_code == 403

    # Existing connected source (wave) + completed setup state.
    client.post("/v1/channels", json=dict(name="transcripts.wave", plugin="wave", fields=[]))
    client.post(
        "/v1/ingest/messages",
        json=dict(
            plugin="wave",
            messages=[
                dict(
                    channel="transcripts.wave",
                    external_id="w-existing",
                    thread={"external_id": "t"},
                    happened_at="2024-02-01T00:00:00+00:00",
                    content="Existing restored meeting",
                    author=dict(system="wave", kind="speaker", value="Speaker A"),
                    payload={},
                )
            ],
        ),
    )
    client.post("/v1/setup/complete", json={})
    assert client.get("/v1/setup/state").json()["status"] == "done"

    monkeypatch.setenv("THROUGHLINE_BACKUP_CONFIG", str(tmp_path / "backup.json"))
    backups.save({"destination": {"dir": str(tmp_path / "bundles")}})

    view = client.get("/v1/setup/restore")
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["scopes"] == ["database", "config", "lenses", "pages"]
    assert body["detected"]["existing"] is True

    # Continuing onboarding with already-restored data re-opens setup in restore mode
    # while preserving connected sources and allowing a new source to be configured.
    res = client.post("/v1/setup/restore", json={"parts": [], "continue_onboarding": True})
    assert res.status_code == 200, res.text
    assert res.json()["state"]["mode"] == "restore"
    assert res.json()["state"]["status"] == "in_progress"
    assert res.json()["detected"]["restored_hint"] is True

    # Add a new source via onboarding while existing wave source remains connected and untouched.
    export = tmp_path / "Timeline.json"
    export.write_text('{"semanticSegments": []}', encoding="utf-8")
    save = client.put(
        "/v1/setup/plugins/google_timeline/config",
        json={"values": {"GOOGLE_TIMELINE_EXPORT": str(export)}, "enabled": True},
    )
    assert save.status_code == 200, save.text
    detected = client.get("/v1/setup/detect").json()
    by_id = {s["plugin_id"]: s for s in detected["sources"]}
    assert by_id["wave"]["connected"] is True and by_id["wave"]["records"] == 1
    assert "google_timeline" in by_id
    store.drop_plugin("google_timeline")
