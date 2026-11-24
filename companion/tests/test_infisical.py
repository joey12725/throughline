"""Tests for Infisical secret integration in Throughline Companion."""

from __future__ import annotations

import json
import sys
import urllib.request
from urllib.error import HTTPError

import httpx
import pytest
from throughline_sdk import infisical

from throughline_companion import cli, paths
from throughline_companion.app import Companion
from throughline_companion.capture.engine import CaptureEngine
from throughline_companion.config import Config
from throughline_companion.runner import Job, Runner, resolve_env
from throughline_companion.ui import Dashboard


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_COMPANION_HOME", str(tmp_path / "home"))
    return tmp_path


def _make_client(store: dict[tuple[str, str, str], str]) -> infisical.InfisicalClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer test-infisical-token":
            return httpx.Response(401, json={"message": "unauthorized"})
        env = request.url.params.get("environment", "prod")
        path = request.url.params.get("secretPath", "/")
        if request.url.path == "/api/v3/secrets/raw":
            items = [
                {"secretKey": k, "secretValue": v, "secretPath": p, "environment": e}
                for (e, p, k), v in store.items()
                if e == env and (p == path or request.url.params.get("recursive") == "true")
            ]
            return httpx.Response(200, json={"secrets": items, "imports": []})
        if request.url.path.startswith("/api/v3/secrets/raw/"):
            key = request.url.path.rsplit("/", 1)[-1]
            if (env, path, key) not in store:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={"secret": {"secretKey": key, "secretValue": store[(env, path, key)]}})
        return httpx.Response(404, json={"message": "unknown"})

    cfg = infisical.InfisicalConfig(
        site_url="https://infisical.test",
        token="test-infisical-token",
        project_id="proj-comp",
        environment="prod",
        secret_path="/",
    )
    return infisical.InfisicalClient(cfg, transport=httpx.MockTransport(handler))


def test_resolve_env_infisical_string_object_env_chain_and_file(tmp_path, monkeypatch):
    client = _make_client(
        {
            ("prod", "/", "GH_TOKEN"): "ghp_live_123",
            ("prod", "/connectors/google", "SA_JSON"): '{"type":"service_account"}',
            ("staging", "/custom", "OTHER"): "staging-val",
        }
    )
    monkeypatch.setenv("INDIRECT_REF", "infisical:GH_TOKEN")
    resolved = []
    env = resolve_env(
        {
            "GITHUB_TOKEN": "infisical:GH_TOKEN",
            "INDIRECT": "env:INDIRECT_REF",
            "SA_FILE": {"infisical": "SA_JSON", "path": "/connectors/google", "as": "file"},
            "STAGING_VAL": {"infisical": "OTHER", "path": "/custom", "environment": "staging"},
            "SA_FILE_QUERY": "infisical:/connectors/google/SA_JSON?as=file",
        },
        "http://127.0.0.1:9",
        "tok",
        str(tmp_path),
        infisical_client=client,
        resolved_secrets=resolved,
    )
    assert env["GITHUB_TOKEN"] == "ghp_live_123"
    assert env["INDIRECT"] == "ghp_live_123"
    assert env["STAGING_VAL"] == "staging-val"
    assert open(env["SA_FILE"]).read() == '{"type":"service_account"}'
    assert open(env["SA_FILE_QUERY"]).read() == '{"type":"service_account"}'
    if paths.platform() != "windows":
        assert (paths.Path(env["SA_FILE"]).stat().st_mode & 0o777) == 0o600
    assert "ghp_live_123" in resolved
    assert "staging-val" in resolved


def test_runner_resolves_infisical_strips_creds_and_redacts_arbitrary_var_names(home, monkeypatch):
    monkeypatch.setenv("INFISICAL_CLIENT_SECRET", "top-secret-infisical-cred")
    monkeypatch.setenv("INFISICAL_TOKEN", "top-secret-infisical-tok")
    client = _make_client(
        {
            ("prod", "/", "CUSTOM_CRED"): "xyz-unusual-secret-value",
            ("prod", "/", "PLUGIN_TOK"): "tl-plugin-token-99",
        }
    )
    config = Config()
    config.server.update(url="http://127.0.0.1:9", device_token="d")
    runner = Runner(config, isolate=False, infisical_client=client)

    out = home / "env.json"
    job = Job(
        "probe",
        [
            sys.executable,
            "-c",
            (
                f"import json,os; json.dump(dict(os.environ), open({str(out)!r}, 'w')); "
                "print('leaked xyz-unusual-secret-value and tl-plugin-token-99')"
            ),
        ],
        900,
        # Note: env var name has NO 'TOKEN'/'SECRET' substring — redaction must still catch it!
        {"MY_FIELD": "infisical:CUSTOM_CRED"},
        token="infisical:PLUGIN_TOK",
    )
    assert runner.run(job), job.last_error
    env = json.loads(out.read_text())
    assert env["MY_FIELD"] == "xyz-unusual-secret-value"
    assert env["THROUGHLINE_TOKEN"] == "tl-plugin-token-99"
    # Plugin subprocess must NEVER inherit INFISICAL_* credentials.
    assert "INFISICAL_CLIENT_SECRET" not in env
    assert "INFISICAL_TOKEN" not in env
    # Resolved secret and token must be redacted from last_output.
    assert "xyz-unusual-secret-value" not in (job.last_output or "")
    assert "tl-plugin-token-99" not in (job.last_output or "")
    assert "[REDACTED]" in (job.last_output or "")

    # Maintenance commands DO inherit INFISICAL_* so core commands can resolve throughline.env refs.
    cmd_out = home / "cmd_env.json"
    cmd_job = Job(
        "maint",
        [sys.executable, "-c", f"import json,os; json.dump(dict(os.environ), open({str(cmd_out)!r}, 'w'))"],
        900,
        {},
        kind="command",
    )
    assert runner.run(cmd_job), cmd_job.last_error
    cmd_env = json.loads(cmd_out.read_text())
    assert cmd_env["INFISICAL_TOKEN"] == "top-secret-infisical-tok"


def test_config_redaction_and_capture_api_key_reference(home, monkeypatch):
    config = Config()
    config.data["infisical"].update(
        client_id="cid-1",
        client_secret="raw-client-secret",
        token="env:MY_INFISICAL_TOK",
    )
    config.data["capture"]["transcription"]["api_key"] = "infisical:GROQ_API_KEY"
    config.data["plugins"]["github"] = {
        "enabled": True,
        "token": "infisical:TL_GITHUB_TOKEN",
        "interval_s": 900,
        "env": {"GITHUB_TOKEN": "infisical:GITHUB_TOKEN"},
    }
    redacted = config.redacted()
    assert redacted["infisical"]["client_secret"] == "set"
    assert redacted["infisical"]["token"] == "env:MY_INFISICAL_TOK"
    assert redacted["capture"]["transcription"]["api_key"] == "infisical:GROQ_API_KEY"
    assert redacted["plugins"]["github"]["token"] == "infisical:TL_GITHUB_TOKEN"

    # CaptureEngine resolves api_key in memory without mutating config.
    client = _make_client({("prod", "/", "GROQ_API_KEY"): "gsk_live_secret"})
    monkeypatch.setattr(infisical, "InfisicalClient", lambda cfg, **kw: client)
    engine = CaptureEngine(config)
    snap = engine.snapshot()
    assert snap["settings"]["transcription"]["api_key"] == "infisical:GROQ_API_KEY"
    resolved_opts = engine._engine()
    assert resolved_opts["api_key"] == "gsk_live_secret"
    # Stored config still only holds the reference, never the secret!
    assert config.data["capture"]["transcription"]["api_key"] == "infisical:GROQ_API_KEY"


def _http(url, *, method="GET", headers=None, data=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except HTTPError as err:
        return err.code, err.read()


def test_dashboard_and_cli_infisical_endpoints(home, monkeypatch):
    client = _make_client(
        {
            ("prod", "/", "IMMICH_API_KEY"): "immich-secret-val",
            ("prod", "/desktop", "BEEPER_TOKEN"): "beeper-secret-val",
        }
    )
    monkeypatch.setattr(infisical, "InfisicalClient", lambda cfg, **kw: client)

    companion = Companion(Config(), server=True)
    dashboard = Dashboard(companion, port=0)
    dashboard.start()
    try:
        base = f"http://127.0.0.1:{dashboard.port}"
        headers = {"X-Companion-Key": dashboard.key, "Content-Type": "application/json"}

        status, body = _http(
            base + "/api/infisical",
            method="POST",
            headers=headers,
            data=json.dumps(
                {
                    "site_url": "https://infisical.test",
                    "project_id": "proj-comp",
                    "environment": "prod",
                    "client_id": "cid-1",
                    "client_secret": "raw-secret-never-shown",
                }
            ).encode(),
        )
        assert status == 200, body
        data = json.loads(body)
        assert data["configured"] is True
        assert data["auth_mode"] == "universal_auth"
        assert "raw-secret-never-shown" not in body.decode()

        status, body = _http(base + "/api/infisical/test", method="POST", headers=headers, data=b"{}")
        assert status == 200, body
        assert json.loads(body)["ok"] is True

        status, body = _http(base + "/api/infisical/secrets?recursive=true", headers=headers)
        assert status == 200, body
        text = body.decode()
        assert "immich-secret-val" not in text
        assert "beeper-secret-val" not in text
        items = json.loads(text)["secrets"]
        assert {i["ref"] for i in items} == {"infisical:IMMICH_API_KEY", "infisical:/desktop/BEEPER_TOKEN"}
    finally:
        dashboard.stop()

    # CLI subcommands work end-to-end.
    assert cli.main(["infisical", "set", "environment=staging"]) == 0
    assert cli.main(["infisical", "status"]) == 0
    assert cli.main(["infisical", "test"]) == 0
    assert cli.main(["infisical", "list", "--recursive", "--json"]) == 0
