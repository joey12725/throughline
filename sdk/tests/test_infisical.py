"""Tests for Infisical secret resolution in throughline-sdk."""

from __future__ import annotations

import json

import httpx
import pytest

from throughline_sdk import Channel, Declaration, Manifest, Plugin, infisical


class _DemoPlugin(Plugin):
    manifest = Manifest(
        name="demo_infisical",
        version="0.1.0",
        channels=[Channel(name="demo", description="Demo channel", record_kind="life_event")],
        settings={"endpoint": Declaration(env="DEMO_ENDPOINT", required=True)},
        secrets={"api_key": Declaration(env="DEMO_API_KEY", required=True)},
    )

    def records(self, cursor, mode):
        return iter(())


def test_sdk_plugin_resolves_infisical_secrets_and_redacts(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sdk-tok"
        if request.url.path == "/api/v3/secrets/raw/DEMO_KEY":
            return httpx.Response(200, json={"secret": {"secretValue": "resolved-sdk-secret"}})
        return httpx.Response(404, json={"message": "not found"})

    orig_client = infisical.InfisicalClient

    def patched_client(cfg, **kwargs):
        return orig_client(cfg, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(infisical, "InfisicalClient", patched_client)
    monkeypatch.setenv("INFISICAL_TOKEN", "sdk-tok")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "proj-sdk")
    monkeypatch.setenv("DEMO_ENDPOINT", "https://api.example.test")
    monkeypatch.setenv("DEMO_API_KEY", "infisical:DEMO_KEY")

    plugin = _DemoPlugin()
    assert plugin.settings["endpoint"] == "https://api.example.test"
    assert plugin.secrets["api_key"] == "resolved-sdk-secret"


def test_sdk_infisical_config_env_deref():
    cfg = infisical.InfisicalConfig.from_settings(
        {
            "site_url": "https://vault.local/api/",
            "client_id": "env:MY_CID",
            "client_secret": "env:MY_CSEC",
            "project_id": "p-123",
            "environment": "staging",
            "secret_path": "connectors/a/",
        },
        env={"MY_CID": "id-1", "MY_CSEC": "sec-1"},
    )
    assert cfg.site_url == "https://vault.local"
    assert cfg.client_id == "id-1"
    assert cfg.client_secret == "sec-1"
    assert cfg.auth_mode == "universal_auth"
    assert cfg.secret_path == "/connectors/a"

    with pytest.raises(infisical.InfisicalError, match="not configured"):
        infisical.InfisicalClient(infisical.InfisicalConfig()).get_secret("infisical:X")

    assert json.loads(json.dumps(infisical.parse_ref("infisical:/a/b/KEY").label())) == "infisical:/a/b/KEY"
