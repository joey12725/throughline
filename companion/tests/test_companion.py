"""Throughline Companion: config, catalog, runner, providers, dashboard."""

import base64
import json
import sys
import urllib.request
from urllib.error import HTTPError

import pytest

from throughline_companion import catalog, paths
from throughline_companion.app import Companion
from throughline_companion.bridge import decode_pairing
from throughline_companion.config import Config
from throughline_companion.providers import Providers, Refused, _domain_allowed
from throughline_companion.runner import Job, Runner, resolve_env


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_COMPANION_HOME", str(tmp_path / "home"))
    return tmp_path


def pairing(bundle):
    raw = json.dumps(bundle).encode()
    return "tlc1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")


BUNDLE = dict(
    v=1,
    url="http://127.0.0.1:9/",
    device="laptop",
    device_token="device-secret",
    plugin_tokens={"claude_code": "plugin-secret"},
)


def test_pairing_stores_tokens_privately_and_redacts(home):
    config = Config()
    config.apply_pairing(decode_pairing(pairing(BUNDLE)))
    assert config.paired
    assert config.server["url"] == "http://127.0.0.1:9"
    assert config.data["plugins"]["claude_code"] == {
        "enabled": True,
        "token": "plugin-secret",
        "interval_s": None,
        "env": {},
    }
    if paths.platform() != "windows":
        assert (config.path.stat().st_mode & 0o777) == 0o600
    shown = json.dumps(config.redacted())
    assert "device-secret" not in shown and "plugin-secret" not in shown
    # Reloading keeps everything.
    assert Config().data == config.data


def test_bad_pairing_codes_are_refused():
    with pytest.raises(ValueError):
        decode_pairing("hello")
    with pytest.raises(ValueError):
        decode_pairing("tlc1.%%%")


def test_catalog_entries_are_complete():
    names = [entry["name"] for entry in catalog.CATALOG]
    assert len(names) == len(set(names))
    for entry in catalog.CATALOG:
        assert ":" in entry["entry"]
        assert set(entry["os"]) <= {"windows", "macos", "linux"}
        assert isinstance(entry["detect"](), list)
    assert catalog.BY_NAME["imessage"]["os"] == ("macos",)


def test_detect_finds_claude_code_projects(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude"))
    (home / "claude" / "projects").mkdir(parents=True)
    assert catalog.detected(catalog.BY_NAME["claude_code"]) == [str(home / "claude" / "projects")]


def test_runner_runs_a_job_with_only_its_own_environment(home, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_TOKEN", "leaked-owner-token")
    config = Config()
    config.server.update(url="http://127.0.0.1:9", device_token="d")
    out = home / "env.json"
    job = Job(
        "probe",
        [sys.executable, "-c", f"import json,os; json.dump(dict(os.environ), open({str(out)!r}, 'w')); print('done secret-1')"],
        900,
        {"PROBE_SETTING": "yes"},
        token="secret-1",
    )
    runner = Runner(config)
    assert runner.run(job)
    env = json.loads(out.read_text())
    assert env["THROUGHLINE_TOKEN"] == "secret-1"
    assert env["THROUGHLINE_URL"] == "http://127.0.0.1:9"
    assert env["PROBE_SETTING"] == "yes"
    assert job.state == "ok"
    assert "secret-1" not in job.last_output
    log = (paths.logs_dir() / "probe.log").read_text()
    assert "done" in log


def test_failed_job_backs_off_and_redacts(home):
    config = Config()
    config.server.update(url="http://127.0.0.1:9")
    job = Job("bad", [sys.executable, "-c", "import sys; print('token tok-9 broke'); sys.exit(3)"], 3600, {}, token="tok-9")
    runner = Runner(config)
    assert not runner.run(job)
    assert job.state == "failed"
    assert "tok-9" not in job.last_error and "[REDACTED]" in job.last_error
    import time

    assert 50 < job.next_run_at - time.time() <= 61


def test_unpaired_plugin_job_fails_clearly(home):
    config = Config()
    job = Job("p", [sys.executable, "-c", "pass"], 900, {}, token=None)
    assert not Runner(config).run(job)
    assert "not paired" in job.last_error


def test_desktop_references_resolve_through_the_bridge(tmp_path):
    calls = []

    def call(url, token, capability, args):
        calls.append((token, capability, args))
        return {"header": "SID=1", "text": "# Netscape\n", "value": 7}

    env = resolve_env(
        {
            "PLAIN": "x",
            "COOKIE": {"desktop": "cookies", "args": {"domain": "youtube.com"}, "as": "header"},
            "COOKIES_FILE": {"desktop": "cookies", "args": {"domain": "instagram.com"}, "as": "file"},
            "N": {"desktop": "status", "as": "value"},
        },
        "http://x",
        "tok",
        str(tmp_path),
        call=call,
    )
    assert env["PLAIN"] == "x" and env["COOKIE"] == "SID=1" and env["N"] == "7"
    assert open(env["COOKIES_FILE"]).read() == "# Netscape\n"
    assert calls[0] == ("tok", "cookies", {"domain": "youtube.com"})


def test_file_read_stays_inside_shared_folders(home, tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.txt").write_text("hello")
    (tmp_path / "secret.txt").write_text("no")
    config = Config()
    config.data["bridge"]["capabilities"]["file.read"].update(
        enabled=True, allowed_plugins=["notes"], roots=[str(shared)]
    )
    providers = Providers(config)
    answer = providers.handle(dict(capability="file.read", plugin="notes", args={"path": str(shared / "a.txt")}))
    assert answer["text"] == "hello"
    for path in (tmp_path / "secret.txt", shared / ".." / "secret.txt"):
        with pytest.raises(Refused):
            providers.handle(dict(capability="file.read", plugin="notes", args={"path": str(path)}))
    with pytest.raises(Refused):
        providers.handle(dict(capability="file.read", plugin="other", args={"path": str(shared / "a.txt")}))
    listing = providers.handle(dict(capability="file.list", plugin="notes", args={"path": str(shared)}))
    assert [i["name"] for i in listing["items"]] == ["a.txt"]
    declared = {c["name"] for c in providers.declared()}
    assert {"file.read", "file.list", "status", "plugin.run"} <= declared
    assert "cookies" not in declared


def test_cookie_and_local_http_rules(home):
    assert _domain_allowed("www.youtube.com", ["youtube.com"])
    assert not _domain_allowed("evilyoutube.com", ["youtube.com"])
    config = Config()
    config.data["bridge"]["capabilities"]["cookies"].update(enabled=True, allowed_plugins=["youtube"], domains=["youtube.com"])
    providers = Providers(config)
    with pytest.raises(Refused, match="not allowed"):
        providers.handle(dict(capability="cookies", plugin="youtube", args={"domain": "google.com"}))
    config.data["bridge"]["capabilities"]["http.local"].update(
        enabled=True, allowed_plugins=["*"], targets={"far": "http://example.com"}
    )
    with pytest.raises(Refused, match="localhost"):
        providers.handle(dict(capability="http.local", plugin="x", args={"target": "far"}))
    with pytest.raises(Refused, match="no local service"):
        providers.handle(dict(capability="http.local", plugin="x", args={"target": "nope"}))


def _request(url, *, method="GET", headers=None, data=None):
    request = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def test_dashboard_requires_the_session_key_and_a_local_host(home):
    from throughline_companion.ui import Dashboard

    companion = Companion(Config(), server=True)
    dashboard = Dashboard(companion, port=0)
    dashboard.start()
    try:
        base = f"http://127.0.0.1:{dashboard.port}"
        assert _request(base + "/api/state")[0] == 401
        assert _request(base + "/api/state", headers={"X-Companion-Key": "wrong"})[0] == 401
        status, body = _request(base + "/api/state", headers={"X-Companion-Key": dashboard.key})
        assert status == 200
        state = json.loads(body)
        assert state["paired"] is False
        assert {p["name"] for p in state["plugins"]} >= {"claude_code", "activitywatch"}
        # The key in the URL becomes a cookie; the page is served with it.
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", dashboard.port)
        conn.request("GET", "/?key=" + dashboard.key)
        response = conn.getresponse()
        response.read()
        assert response.status == 303
        cookie = response.getheader("Set-Cookie").split(";")[0]
        conn.request("GET", "/", headers={"Cookie": cookie})
        response = conn.getresponse()
        assert response.status == 200
        assert b"Throughline Companion" in response.read()
        conn.close()
        # DNS rebinding: a foreign Host header is refused even with the key.
        assert _request(base + "/api/state", headers={"X-Companion-Key": dashboard.key, "Host": "evil.example"})[0] == 403
        # Cross-origin writes are refused.
        status, _ = _request(
            base + "/api/pause",
            method="POST",
            headers={"X-Companion-Key": dashboard.key, "Origin": "http://evil.example", "Content-Type": "application/json"},
            data=b'{"paused": true}',
        )
        assert status == 403
        status, body = _request(
            base + "/api/plugins/claude_code",
            method="POST",
            headers={"X-Companion-Key": dashboard.key, "Content-Type": "application/json"},
            data=json.dumps({"enabled": True, "env": {"CLAUDE_CODE_HOMES": "local=/x"}}).encode(),
        )
        assert status == 200, body
        assert companion.config.data["plugins"]["claude_code"]["env"] == {"CLAUDE_CODE_HOMES": "local=/x"}
        assert "claude_code" in companion.runner.jobs
    finally:
        dashboard.stop()


def test_plugin_command_runs_a_real_connector_manifest(home):
    companion = Companion(Config(), server=True)
    manifest = companion.manifest("claude_code")
    assert manifest["name"] == "claude_code"
    assert any(s["env"] == "CLAUDE_CODE_HOMES" for s in manifest["settings"].values())


def test_server_mode_isolates_plugins_but_not_maintenance_commands(home, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "model-key")
    monkeypatch.setenv("PGPASSWORD", "pg-secret")
    monkeypatch.setenv("THROUGHLINE_DATABASE_URL", "postgresql://postgres@db/throughline")
    config = Config()
    config.server.update(url="http://127.0.0.1:9", device_token="d")
    runner = Runner(config, isolate=True)

    def environment(kind):
        out = home / f"{kind}.json"
        job = Job(
            kind,
            [sys.executable, "-c", f"import json,os; json.dump(dict(os.environ), open({str(out)!r}, 'w'))"],
            900,
            {},
            token="tok-1",
            kind=kind,
        )
        assert runner.run(job), job.last_error
        return json.loads(out.read_text())

    plugin = environment("plugin")
    assert "OPENROUTER_API_KEY" not in plugin and "PGPASSWORD" not in plugin
    assert "THROUGHLINE_DATABASE_URL" not in plugin
    command = environment("command")
    # Lenses, curation and the embedding worker need the model keys and the
    # database password the maintenance service was given.
    assert command["OPENROUTER_API_KEY"] == "model-key"
    assert command["PGPASSWORD"] == "pg-secret"
    assert command["THROUGHLINE_DATABASE_URL"] == "postgresql://postgres@db/throughline"


def test_pairing_file_and_default_config(home, tmp_path, monkeypatch):
    from throughline_companion import cli

    raw = json.dumps(dict(v=1, url="http://x", device="docker", device_token="dt",
                          plugin_tokens={"github": "gt"})).encode()
    code = "tlc1." + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    pairing = tmp_path / "pairing"
    pairing.write_text(code + "\n")
    monkeypatch.delenv("THROUGHLINE_COMPANION_PAIRING", raising=False)
    monkeypatch.setenv("THROUGHLINE_COMPANION_PAIRING_FILE", str(pairing))
    monkeypatch.setenv("THROUGHLINE_COMPANION_URL", "http://throughline:8000/")
    config = Config(tmp_path / "missing.json")
    cli._environment_pairing(config)
    assert config.server == {"url": "http://throughline:8000", "device": "docker", "device_token": "dt"}
    assert config.plugin("github") == dict(config.plugin("github"), token="gt", enabled=True)
    assert not (tmp_path / "missing.json").exists()

    default = tmp_path / "default.json"
    default.write_text(json.dumps({"jobs": [{"name": "embed", "command": ["true"], "interval_s": 60}]}))
    monkeypatch.setenv("THROUGHLINE_COMPANION_DEFAULT_CONFIG", str(default))

    class Args:
        config = str(tmp_path / "mounted")

    (tmp_path / "mounted").mkdir()  # a bind mount of a missing file
    assert [j["name"] for j in cli._config(Args).data["jobs"]] == ["embed"]
    (tmp_path / "mounted").rmdir()
    (tmp_path / "mounted").write_text(json.dumps({"jobs": []}))
    assert cli._config(Args).data["jobs"] == []
