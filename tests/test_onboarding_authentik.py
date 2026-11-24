"""Onboarding Authentik / OpenID Connect tests: pre-sign-in owner claim via
OIDC (`POST /v1/auth/setup/oidc`) and in-onboarding SSO configuration &
account linking (`/v1/setup/auth*`)."""

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from throughline.api import create_app
from throughline.auth import accounts, oidc
from throughline.db import connect
from throughline.onboarding import owner, store

WEB = {"X-Throughline-Client": "web"}
ISSUER = "http://127.0.0.1:9/application/o/throughline/"


def _jwt(claims):
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc(claims)}."


def _mock_oidc(monkeypatch, *, claims_fn):
    """Patch oidc._client() to emulate an Authentik OIDC discovery + token server."""
    oidc._cache.clear()
    meta = {
        "issuer": ISSUER,
        "authorization_endpoint": "http://127.0.0.1:9/application/o/authorize/",
        "token_endpoint": "http://127.0.0.1:9/application/o/token/",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=meta)
        if request.url.path.endswith("/token/"):
            body = parse_qs(request.content.decode())
            code = body["code"][0]
            return httpx.Response(200, json={"id_token": _jwt(claims_fn(code))})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(oidc, "_client", lambda: httpx.Client(transport=transport, timeout=10))


@pytest.fixture()
def isolated_setup(database, tmp_path, monkeypatch):
    """Isolate onboarding store + env vars per test."""
    saved_orig = dict(store._ORIGINAL)
    saved_applied = set(store._applied)
    monkeypatch.setenv("THROUGHLINE_SETUP_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("THROUGHLINE_SETUP_DATA", str(tmp_path / "data"))
    for k in list(store._ORIGINAL):
        if k.startswith("THROUGHLINE_AUTH_") or k == "THROUGHLINE_PUBLIC_URL":
            store._ORIGINAL.pop(k, None)
    for k in list(store._applied):
        if k.startswith("THROUGHLINE_AUTH_") or k == "THROUGHLINE_PUBLIC_URL":
            store._applied.discard(k)
    import os

    for k in list(os.environ):
        if k.startswith("THROUGHLINE_AUTH_") or k == "THROUGHLINE_PUBLIC_URL":
            monkeypatch.delenv(k, raising=False)
    oidc._cache.clear()
    yield tmp_path
    store._ORIGINAL.clear()
    store._ORIGINAL.update(saved_orig)
    store._applied.clear()
    store._applied.update(saved_applied)
    oidc._cache.clear()


def test_claim_first_owner_account_via_authentik_in_onboarding(client, isolated_setup, monkeypatch):
    nonces = {}

    def make_claims(code):
        return {
            "iss": ISSUER,
            "aud": "tl-client",
            "sub": "authentik-sub-1",
            "exp": time.time() + 300,
            "iat": time.time(),
            "nonce": nonces[code],
            "preferred_username": "Alex.Owner",
            "name": "Alex Owner",
            "email": "alex@example.test",
            "email_verified": True,
            "groups": ["throughline-users"],
        }

    _mock_oidc(monkeypatch, claims_fn=make_claims)
    code = owner.setup_code(create=True)

    with TestClient(create_app(), base_url="http://127.0.0.1") as browser:
        pub = browser.get("/v1/auth/setup").json()
        assert pub["needs_account"] is True
        assert pub["can_configure_oidc"] is True
        assert pub["redirect_uri"] == "http://127.0.0.1/v1/auth/oidc/authentik/return"

        # Wrong setup code is rejected before touching config.
        bad = browser.post(
            "/v1/auth/setup/oidc",
            json=dict(code="WRONG-CODE", issuer=ISSUER, client_id="tl-client"),
            headers=WEB,
        )
        assert bad.status_code == 403

        # Configure Authentik + begin claim_owner flow with the real setup code.
        start = browser.post(
            "/v1/auth/setup/oidc",
            json=dict(
                code=code,
                provider_id="authentik",
                kind="authentik",
                issuer=ISSUER,
                client_id="tl-client",
                client_secret="super-secret-value",
                allowed_groups="throughline-users",
                remember=True,
            ),
            headers=WEB,
        )
        assert start.status_code == 200, start.text
        auth_url = start.json()["authorize_url"]
        qs = parse_qs(urlsplit(auth_url).query)
        state = qs["state"][0]
        nonces["auth-code-1"] = qs["nonce"][0]

        # Callback creates the first owner account, links Authentik, consumes setup code, and signs in.
        cb = browser.get(
            f"/v1/auth/oidc/authentik/return?code=auth-code-1&state={state}",
            follow_redirects=False,
        )
        assert cb.status_code == 303
        assert cb.headers["location"] == "/ui/welcome"

        session = browser.get("/v1/auth/session", headers=WEB).json()
        assert session["account"]["handle"] == "alex.owner"
        assert session["session"]["method"] == "authentik"
        assert session["principal"]["kind"] == "owner"

        # Setup code is consumed and claiming again is refused.
        assert not owner._code_file().exists()
        again = browser.post(
            "/v1/auth/setup/oidc",
            json=dict(code=code, provider_id="authentik"),
            headers=WEB,
        )
        assert again.status_code == 409

        # Owner setup auth view exposes masked secret (never raw) and shows linked identity.
        view = browser.get("/v1/setup/auth", headers=WEB)
        assert view.status_code == 200, view.text
        body = view.json()
        assert len(body["providers"]) == 1
        assert body["providers"][0]["client_secret_set"] is True
        assert "super-secret-value" not in json.dumps(body)
        assert body["current_account"]["links"][0]["provider"] == "authentik"
        assert body["current_account"]["links"][0]["subject"] == "authentik-sub-1"


def test_setup_auth_configure_link_and_anti_lockout(client, isolated_setup, monkeypatch):
    nonces = {}

    def make_claims(code):
        return {
            "iss": ISSUER,
            "aud": "tl-client",
            "sub": "authentik-sub-2",
            "exp": time.time() + 300,
            "iat": time.time(),
            "nonce": nonces[code],
            "preferred_username": "sam",
            "email": "sam@example.test",
            "email_verified": True,
        }

    _mock_oidc(monkeypatch, claims_fn=make_claims)
    code = owner.setup_code(create=True)

    with TestClient(create_app(), base_url="http://127.0.0.1") as browser:
        # Start with a local password account (with optional email).
        created = browser.post(
            "/v1/auth/setup",
            json=dict(
                code=code,
                handle="sam",
                display_name="Sam",
                email="sam@example.test",
                password="correct horse battery",
            ),
            headers=WEB,
        )
        assert created.status_code == 201, created.text
        with connect() as conn:
            row = conn.execute("SELECT email FROM tl.accounts WHERE handle='sam'").fetchone()
        assert row["email"] == "sam@example.test"

        # Can't disable password sign-in before configuring & linking SSO.
        refused = browser.put("/v1/setup/auth/methods", json=dict(password=False), headers=WEB)
        assert refused.status_code == 409

        # Configure Authentik from AccountStep.
        saved = browser.put(
            "/v1/setup/auth/provider",
            json=dict(
                id="authentik",
                kind="authentik",
                issuer=ISSUER,
                client_id="tl-client",
                client_secret="secret-xyz-1234",
            ),
            headers=WEB,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["providers"][0]["id"] == "authentik"

        # Still can't disable password sign-in until an identity is actually linked.
        still_refused = browser.put("/v1/setup/auth/methods", json=dict(password=False), headers=WEB)
        assert still_refused.status_code == 409

        # Start explicit link flow from AccountStep.
        link_start = browser.post(
            "/v1/setup/auth/oidc/authentik/link",
            json=dict(return_to="/ui/welcome/account?linked=authentik"),
            headers=WEB,
        )
        assert link_start.status_code == 200, link_start.text
        qs = parse_qs(urlsplit(link_start.json()["authorize_url"]).query)
        state = qs["state"][0]
        nonces["link-code-1"] = qs["nonce"][0]

        cb = browser.get(
            f"/v1/auth/oidc/authentik/return?code=link-code-1&state={state}",
            follow_redirects=False,
        )
        assert cb.status_code == 303
        assert cb.headers["location"] == "/ui/welcome/account?linked=authentik"

        # Now disabling password sign-in succeeds, and removing the sole provider is refused.
        disabled_pw = browser.put("/v1/setup/auth/methods", json=dict(password=False), headers=WEB)
        assert disabled_pw.status_code == 200
        assert disabled_pw.json()["password_enabled"] is False

        cant_drop = browser.delete("/v1/setup/auth/provider/authentik", headers=WEB)
        assert cant_drop.status_code == 409

        # Re-enable password, then remove provider cleanly.
        assert browser.put("/v1/setup/auth/methods", json=dict(password=True), headers=WEB).status_code == 200
        removed = browser.delete("/v1/setup/auth/provider/authentik", headers=WEB)
        assert removed.status_code == 200
        assert removed.json()["providers"] == []


def test_preconfigured_env_provider_is_offered_at_gate_and_locked(client, isolated_setup, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_AUTH_PROVIDERS", "authentik")
    monkeypatch.setenv("THROUGHLINE_AUTH_AUTHENTIK_ISSUER", ISSUER)
    monkeypatch.setenv("THROUGHLINE_AUTH_AUTHENTIK_CLIENT_ID", "env-client")
    store._ORIGINAL["THROUGHLINE_AUTH_PROVIDERS"] = "authentik"
    store._ORIGINAL["THROUGHLINE_AUTH_AUTHENTIK_ISSUER"] = ISSUER
    store._ORIGINAL["THROUGHLINE_AUTH_AUTHENTIK_CLIENT_ID"] = "env-client"

    with TestClient(create_app(), base_url="http://127.0.0.1") as browser:
        pub = browser.get("/v1/auth/setup").json()
        assert pub["needs_account"] is True
        assert pub["can_configure_oidc"] is False
        assert pub["providers"][0]["id"] == "authentik"
        assert pub["providers"][0]["label"] == "Authentik"

    # Owner cannot overwrite an env-locked provider through setup API.
    accounts.create("owner1", "Owner", None, "correct horse battery")
    r = client.put(
        "/v1/setup/auth/provider",
        json=dict(id="authentik", issuer=ISSUER, client_id="other"),
    )
    assert r.status_code == 409
