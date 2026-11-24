"""Tests for Infisical secret references across Throughline core."""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from throughline import cli, db, ops, providers, secrets
from throughline.classifiers import bridge as classifier_bridge
from throughline.lenses import workers as lens_workers
from throughline.providers.base import ProviderUnavailable


def _mock_infisical(store: dict[tuple[str, str, str], str], *, require_reauth_once: bool = False):
    """Build an httpx.MockTransport simulating Infisical Universal Auth + v3 raw secrets."""
    state = {"logins": 0, "fetches": 0, "expired_once": False, "last_params": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/universal-auth/login":
            body = json.loads(request.content)
            if body.get("clientId") != "cid" or body.get("clientSecret") != "csec":
                return httpx.Response(401, json={"message": "invalid credentials"})
            state["logins"] += 1
            return httpx.Response(
                200,
                json={"accessToken": f"tok-{state['logins']}", "expiresIn": 3600},
            )

        auth = request.headers.get("authorization")
        if require_reauth_once and not state["expired_once"] and auth == "Bearer tok-1":
            state["expired_once"] = True
            return httpx.Response(401, json={"message": "expired"})
        if auth not in ("Bearer tok-1", "Bearer tok-2", "Bearer direct-tok"):
            return httpx.Response(401, json={"message": "unauthorized"})

        params = dict(request.url.params)
        state["last_params"] = params
        env = params.get("environment", "prod")
        path = params.get("secretPath", "/")

        if request.url.path == "/api/v3/secrets/raw":
            recursive = params.get("recursive") == "true"
            items = []
            for (s_env, s_path, s_key), s_val in store.items():
                if s_env != env:
                    continue
                if s_path == path or (recursive and (path == "/" or s_path.startswith(path + "/"))):
                    items.append(
                        {
                            "secretKey": s_key,
                            "secretValue": s_val,
                            "secretPath": s_path,
                            "environment": s_env,
                        }
                    )
            return httpx.Response(
                200,
                json={
                    "secrets": items,
                    "imports": [
                        {
                            "environment": env,
                            "secretPath": "/shared",
                            "secrets": [
                                {
                                    "secretKey": "SHARED_KEY",
                                    "secretValue": "super-secret-shared-val",
                                    "secretPath": "/shared",
                                    "environment": env,
                                }
                            ],
                        }
                    ],
                },
            )

        if request.url.path.startswith("/api/v3/secrets/raw/"):
            state["fetches"] += 1
            key = request.url.path.rsplit("/", 1)[-1]
            lookup = (env, path, key)
            if lookup not in store:
                return httpx.Response(404, json={"message": "Secret not found"})
            return httpx.Response(
                200,
                json={"secret": {"secretKey": key, "secretValue": store[lookup]}},
            )

        return httpx.Response(404, json={"message": "unknown route"})

    return httpx.MockTransport(handler), state


@pytest.fixture(autouse=True)
def _reset_secrets():
    secrets.set_client_override(None)
    secrets.clear_cache()
    yield
    secrets.set_client_override(None)
    secrets.clear_cache()


def test_parse_and_format_refs():
    ref = secrets.parse_ref("infisical:GITHUB_TOKEN")
    assert ref.name == "GITHUB_TOKEN"
    assert ref.secret_path is None
    assert ref.mode == "value"
    assert ref.label() == "infisical:GITHUB_TOKEN"

    ref2 = secrets.parse_ref("infisical:/connectors/github/GITHUB_TOKEN?env=staging&project_id=p1&as=file")
    assert ref2.name == "GITHUB_TOKEN"
    assert ref2.secret_path == "/connectors/github"
    assert ref2.environment == "staging"
    assert ref2.project_id == "p1"
    assert ref2.mode == "file"
    assert ref2.label() == "infisical:/connectors/github/GITHUB_TOKEN"

    ref3 = secrets.parse_ref({"infisical": "GOOGLE_SA", "path": "calendar/", "environment": "dev", "as": "file"})
    assert ref3.name == "GOOGLE_SA"
    assert ref3.secret_path == "/calendar"
    assert ref3.environment == "dev"
    assert ref3.mode == "file"

    assert secrets.format_ref("API_KEY", "/") == "infisical:API_KEY"
    assert secrets.format_ref("API_KEY", "/connectors/gh") == "infisical:/connectors/gh/API_KEY"
    assert secrets.normalize_site_url("https://infisical.internal/api/") == "https://infisical.internal"

    for bad in ("infisical:", "infisical:/folder/", "infisical:bad name", "infisical:KEY?as=bad"):
        with pytest.raises(ValueError):
            secrets.parse_ref(bad)
    with pytest.raises(ValueError):
        secrets.parse_ref({"infisical": "KEY", "unknown_field": 1})
    with pytest.raises(secrets.InfisicalError):
        secrets.normalize_site_url("ftp://example.com")


def test_universal_auth_caching_reauth_and_list_discards_values():
    store = {
        ("prod", "/", "OPENROUTER_API_KEY"): "sk-or-live-secret",
        ("prod", "/connectors/github", "GITHUB_TOKEN"): "ghp_secret_123",
        ("staging", "/", "OPENROUTER_API_KEY"): "sk-or-staging",
    }
    transport, state = _mock_infisical(store, require_reauth_once=True)
    cfg = secrets.InfisicalConfig(
        site_url="https://infisical.test",
        client_id="cid",
        client_secret="csec",
        project_id="proj-1",
        environment="prod",
        secret_path="/",
        cache_ttl_s=300.0,
    )
    client = secrets.InfisicalClient(cfg, transport=transport)

    # First fetch triggers login -> 401 -> re-login -> 200.
    assert client.get_secret("infisical:OPENROUTER_API_KEY") == "sk-or-live-secret"
    assert state["logins"] == 2
    assert state["fetches"] == 1

    # Second fetch hits TTL cache (no new HTTP request).
    assert client.get_secret("infisical:OPENROUTER_API_KEY") == "sk-or-live-secret"
    assert state["fetches"] == 1

    # Different path and environment resolve cleanly.
    assert client.get_secret("infisical:/connectors/github/GITHUB_TOKEN") == "ghp_secret_123"
    assert client.get_secret("infisical:OPENROUTER_API_KEY?env=staging") == "sk-or-staging"

    # list_secret_names returns metadata ONLY and never includes secret values.
    listed = client.list_secret_names(recursive=True)
    serialized = json.dumps(listed)
    assert "sk-or-live-secret" not in serialized
    assert "ghp_secret_123" not in serialized
    assert "super-secret-shared-val" not in serialized
    keys = {(item["path"], item["key"], item["imported"]) for item in listed}
    assert ("/", "OPENROUTER_API_KEY", False) in keys
    assert ("/connectors/github", "GITHUB_TOKEN", False) in keys
    assert ("/shared", "SHARED_KEY", True) in keys

    tested = client.test()
    assert tested["ok"] is True
    assert tested["auth_mode"] == "universal_auth"
    assert tested["secret_count"] >= 2


def test_plain_values_make_zero_network_calls(monkeypatch):
    def boom(request):
        raise AssertionError("network must not be touched for plain values")

    cfg = secrets.InfisicalConfig(token="direct-tok", project_id="proj-1")
    secrets.set_client_override(secrets.InfisicalClient(cfg, transport=httpx.MockTransport(boom)))
    monkeypatch.setenv("OPENROUTER_API_KEY", "plain-key")
    assert secrets.resolve("plain-key") == "plain-key"
    assert secrets.getenv("OPENROUTER_API_KEY") == "plain-key"


def test_core_providers_lenses_classifiers_db_and_ops_resolve_infisical(monkeypatch):
    store = {
        ("prod", "/", "VOYAGE_API_KEY"): "voy-secret-1",
        ("prod", "/", "HTTP_EMBED_KEY"): "http-embed-secret",
        ("prod", "/", "LENS_KEY"): "lens-llm-secret",
        ("prod", "/", "JEV_KEY"): "jev-or-secret",
        ("prod", "/", "APP_PASS"): "app-db-secret",
        ("prod", "/", "PG_SUPER"): "pg-super-secret",
    }
    transport, _ = _mock_infisical(store)
    cfg = secrets.InfisicalConfig(
        site_url="https://infisical.test",
        token="direct-tok",
        project_slug="my-proj",
        environment="prod",
    )
    secrets.set_client_override(secrets.InfisicalClient(cfg, transport=transport))

    # Vendor provider (Voyage)
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "infisical:VOYAGE_API_KEY")
    monkeypatch.delenv("THROUGHLINE_EMBEDDING_KEY", raising=False)
    embedder = providers.get("embedding")
    assert embedder.key == "voy-secret-1"

    # Generic HTTP provider
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_PROVIDER", "http")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_BASE_URL", "http://127.0.0.1:9999/v1")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_MODEL", "m1")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_DIMENSIONS", "256")
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_KEY", "infisical:HTTP_EMBED_KEY")
    assert providers.get("embedding").key == "http-embed-secret"

    # Missing secret degrades cleanly to ProviderUnavailable (which describe() catches).
    monkeypatch.setenv("THROUGHLINE_EMBEDDING_KEY", "infisical:MISSING_KEY")
    with pytest.raises(ProviderUnavailable, match="MISSING_KEY"):
        providers.get("embedding")

    # Lenses LLM key
    monkeypatch.setenv("CUSTOM_LENS_KEY", "infisical:LENS_KEY")
    assert lens_workers._resolve_api_key({"api_key_env": "CUSTOM_LENS_KEY"}, ()) == "lens-llm-secret"

    # Classifier Jev key
    monkeypatch.setenv("OPENROUTER_API_KEY", "infisical:JEV_KEY")
    assert classifier_bridge.jev_key() == "jev-or-secret"

    # Database role password + ops PGPASSWORD redaction & _libpq resolution
    monkeypatch.setenv("THROUGHLINE_APP_PASSWORD", "infisical:APP_PASS")
    assert db.role_password("app") == "app-db-secret"
    monkeypatch.setenv("PGPASSWORD", "infisical:PG_SUPER")
    _, libpq_env = ops._libpq("postgresql://postgres@127.0.0.1:5432/throughline")
    assert libpq_env["PGPASSWORD"] == "pg-super-secret"
    redacted = ops.redact("error with pg-super-secret inside")
    assert "pg-super-secret" not in redacted and "[redacted]" in redacted


def test_cli_lib_infisical_commands(monkeypatch, tmp_path):
    store = {
        ("prod", "/", "OPENROUTER_API_KEY"): "sk-secret",
        ("prod", "/connectors/github", "GITHUB_TOKEN"): "ghp-secret",
    }
    transport, _ = _mock_infisical(store)
    cfg = secrets.InfisicalConfig(
        site_url="https://infisical.test",
        token="direct-tok",
        project_id="proj-1",
        environment="prod",
    )
    secrets.set_client_override(secrets.InfisicalClient(cfg, transport=transport))
    monkeypatch.setenv("INFISICAL_TOKEN", "direct-tok")
    monkeypatch.setenv("INFISICAL_PROJECT_ID", "proj-1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "infisical:OPENROUTER_API_KEY")

    runner = CliRunner()
    r_status = runner.invoke(cli.app, ["lib", "infisical", "status", "--json"])
    assert r_status.exit_code == 0, r_status.output
    status_data = json.loads(r_status.output)
    assert status_data["configured"] is True
    assert status_data["auth_mode"] == "token"
    assert "direct-tok" not in r_status.output

    r_test = runner.invoke(cli.app, ["lib", "infisical", "test", "--json"])
    assert r_test.exit_code == 0, r_test.output
    assert json.loads(r_test.output)["ok"] is True

    r_list = runner.invoke(cli.app, ["lib", "infisical", "list", "--recursive", "--json"])
    assert r_list.exit_code == 0, r_list.output
    assert "sk-secret" not in r_list.output and "ghp-secret" not in r_list.output

    cfg_file = tmp_path / "connectors.json"
    cfg_file.write_text(
        json.dumps(
            {
                "plugins": {
                    "github": {
                        "enabled": True,
                        "env": {
                            "GITHUB_TOKEN": "infisical:/connectors/github/GITHUB_TOKEN",
                        },
                    }
                }
            }
        )
    )
    r_check = runner.invoke(cli.app, ["lib", "infisical", "check", "--config", str(cfg_file), "--json"])
    assert r_check.exit_code == 0, r_check.output
    check_data = json.loads(r_check.output)
    assert check_data["ok"] is True
    assert check_data["checked"] == 2
    assert "sk-secret" not in r_check.output and "ghp-secret" not in r_check.output

    # Failing check exits non-zero.
    cfg_file.write_text(
        json.dumps({"plugins": {"github": {"enabled": True, "env": {"GITHUB_TOKEN": "infisical:NOPE"}}}})
    )
    r_fail = runner.invoke(cli.app, ["lib", "infisical", "check", "--config", str(cfg_file), "--json"])
    assert r_fail.exit_code == 1
    assert json.loads(r_fail.output)["failed"] == 1
