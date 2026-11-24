"""Web sign-in (docs/web/auth.md): accounts, password and OIDC sessions, the
cookie guard, and that a session is exactly its principal token."""

import base64
import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from throughline.api import create_app
from throughline.auth import accounts, oidc, passwords
from throughline.db import connect

WEB = {"X-Throughline-Client": "web"}
ISSUER = "http://127.0.0.1:9/application/o/throughline/"
PASSWORD = "correct horse battery"


@pytest.fixture()
def browser(client, monkeypatch):
    """A second client with no bearer token: a browser."""
    for name in list(passwords.by_account._failures) + list(passwords.by_address._failures):
        passwords.by_account.clear(name)
        passwords.by_address.clear(name)
    monkeypatch.delenv("THROUGHLINE_AUTH_PROVIDERS", raising=False)
    with TestClient(create_app(), base_url="http://127.0.0.1") as b:
        yield b


def login(b, username="alex", password=PASSWORD, remember=False):
    return b.post(
        "/v1/auth/login",
        json=dict(username=username, password=password, remember=remember),
        headers=WEB,
    )


def test_config_without_accounts_offers_password_and_token(browser):
    body = browser.get("/v1/auth/config").json()
    assert body == {"methods": {"password": True, "token": True}, "providers": [], "accounts": False}
    accounts.create("alex", "Alex", None, PASSWORD)
    assert browser.get("/v1/auth/config").json()["accounts"] is True


def test_password_session_is_the_principal_token(browser):
    accounts.create("alex", "Alex", "alex@example.test", PASSWORD)
    assert browser.get("/v1/auth/session").json() == {"account": None}
    r = login(browser)
    assert r.status_code == 200, r.text
    assert r.json()["account"]["handle"] == "alex"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "max-age" not in r.headers["set-cookie"].lower()  # not remembered
    boot = browser.get("/v1/web/bootstrap", headers=WEB)
    assert boot.status_code == 200, boot.text
    assert boot.json()["token"]["kind"] == "owner"
    assert browser.get("/v1/auth/session").json()["principal"] == {"kind": "owner", "restricted": False}
    with connect() as c:
        row = c.execute("SELECT detail FROM tl.audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["detail"]["via"] == "password" and row["detail"]["account_id"]
    # By e-mail too, case-insensitively.
    browser.cookies.clear()
    assert login(browser, "ALEX@example.test").status_code == 200


def test_remembered_session_is_persistent(browser):
    accounts.create("alex", None, None, PASSWORD)
    r = login(browser, remember=True)
    assert "max-age=2592000" in r.headers["set-cookie"].lower()


def test_wrong_password_and_throttle(browser):
    accounts.create("alex", None, None, PASSWORD)
    assert login(browser, password="nope nope nope").status_code == 401
    assert login(browser, "nobody").status_code == 401
    for _ in range(4):
        login(browser, password="still wrong!")
    r = login(browser)
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_cookie_requests_need_the_client_header_and_same_origin(browser):
    accounts.create("alex", None, None, PASSWORD)
    # Login itself needs the header (no login CSRF).
    assert browser.post("/v1/auth/login", json=dict(username="alex", password=PASSWORD)).status_code == 403
    assert login(browser).status_code == 200
    put = browser.put("/v1/web/state/prefs/app", json={"value": {"a": 1}})
    assert put.status_code == 403
    assert browser.put("/v1/web/state/prefs/app", json={"value": {"a": 1}}, headers=WEB).status_code == 200
    cross = browser.get("/v1/web/bootstrap", headers={**WEB, "Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403


def test_bearer_wins_over_cookie(browser, client):
    accounts.create("alex", None, None, PASSWORD)
    login(browser)
    r = browser.get("/v1/web/bootstrap", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_logout_disable_and_password_change_end_sessions(browser):
    accounts.create("alex", None, None, PASSWORD)
    login(browser)
    assert browser.post("/v1/auth/logout", headers=WEB).json() == {"signed_out": True}
    assert browser.get("/v1/web/bootstrap", headers=WEB).status_code == 401
    login(browser)
    accounts.set_status("alex", False)
    assert browser.get("/v1/web/bootstrap", headers=WEB).status_code == 401
    assert login(browser).status_code == 401
    accounts.set_status("alex", True)
    login(browser)
    accounts.set_password("alex", "another long password")
    assert browser.get("/v1/web/bootstrap", headers=WEB).status_code == 401


def test_password_sign_in_can_be_turned_off(browser, monkeypatch):
    accounts.create("alex", None, None, PASSWORD)
    monkeypatch.setenv("THROUGHLINE_AUTH_PASSWORD", "0")
    assert browser.get("/v1/auth/config").json()["methods"]["password"] is False
    assert login(browser).status_code == 404


def test_account_validation():
    with pytest.raises(ValueError):
        accounts.create("Bad Handle")
    with pytest.raises(ValueError):
        accounts.create("short", password="tiny")


# --- OpenID Connect ----------------------------------------------------------


def jwt(claims):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


class FakeIdP:
    def __init__(self):
        self.claims = {}
        self.nonce = None
        self.token_requests = []

    def handler(self, request):
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": "http://127.0.0.1:9/application/o/authorize/",
                    "token_endpoint": "http://127.0.0.1:9/application/o/token/",
                },
            )
        if request.url.path == "/application/o/token/":
            form = parse_qs(request.content.decode())
            self.token_requests.append(form)
            claims = {
                "iss": ISSUER,
                "aud": "tl-client",
                "exp": time.time() + 300,
                "iat": time.time(),
                "nonce": self.nonce,
                "sub": "ak-sub-1",
                "email": "alex@example.test",
                "email_verified": True,
                "preferred_username": "alex",
                "groups": ["family"],
            }
            claims.update(self.claims)
            return httpx.Response(200, json={"id_token": jwt(claims), "access_token": "x"})
        return httpx.Response(404)


@pytest.fixture()
def idp(browser, monkeypatch):
    fake = FakeIdP()
    monkeypatch.setenv("THROUGHLINE_AUTH_PROVIDERS", "authentik")
    monkeypatch.setenv("THROUGHLINE_AUTH_AUTHENTIK_ISSUER", ISSUER)
    monkeypatch.setenv("THROUGHLINE_AUTH_AUTHENTIK_CLIENT_ID", "tl-client")
    monkeypatch.setenv("THROUGHLINE_AUTH_AUTHENTIK_CLIENT_SECRET", "shh")
    monkeypatch.setattr(oidc, "_client", lambda: httpx.Client(transport=httpx.MockTransport(fake.handler)))
    oidc._cache.clear()
    return fake


def sign_in(browser, idp, return_to="/ui/day/2026-11-22", remember=False, state=None):
    start = browser.get(
        "/v1/auth/oidc/authentik/start",
        params={"return_to": return_to, "remember": remember},
        follow_redirects=False,
    )
    assert start.status_code == 303, start.text
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["client_id"] == ["tl-client"]
    idp.nonce = query["nonce"][0]
    return browser.get(
        "/v1/auth/oidc/authentik/return",
        params={"code": "the-code", "state": state or query["state"][0]},
        follow_redirects=False,
    )


def fragment(response):
    return parse_qs(urlsplit(response.headers["location"]).fragment)


def test_provider_is_listed_without_secrets(idp, browser):
    providers = browser.get("/v1/auth/config").json()["providers"]
    assert providers == [
        {"id": "authentik", "kind": "authentik", "label": "Authentik", "start": "/v1/auth/oidc/authentik/start"}
    ]


def test_oidc_links_by_verified_email_then_by_subject(idp, browser):
    accounts.create("alex", None, "alex@example.test")
    done = sign_in(browser, idp)
    assert done.status_code == 303 and done.headers["location"] == "/ui/day/2026-11-22"
    assert idp.token_requests[0]["code_verifier"]
    assert browser.get("/v1/auth/session").json()["session"]["method"] == "authentik"
    assert browser.get("/v1/web/bootstrap", headers=WEB).status_code == 200
    assert accounts.identities("alex")[0]["subject"] == "ak-sub-1"
    # Later the subject decides, whatever the e-mail now says.
    browser.cookies.clear()
    idp.claims = {"email": "changed@example.test"}
    assert sign_in(browser, idp).headers["location"] == "/ui/day/2026-11-22"


def test_oidc_unlinked_and_unverified(idp, browser):
    accounts.create("alex", None, "alex@example.test")
    idp.claims = {"email_verified": False}
    r = sign_in(browser, idp)
    assert fragment(r) == {"auth_error": ["unlinked"], "provider": ["authentik"], "subject": ["ak-sub-1"]}
    accounts.link("alex", "authentik", "ak-sub-1")
    assert sign_in(browser, idp).headers["location"].startswith("/ui/day")


def test_oidc_groups_nonce_state_and_return_to(idp, browser, monkeypatch):
    accounts.create("alex", None, "alex@example.test")
    monkeypatch.setenv("THROUGHLINE_AUTH_AUTHENTIK_ALLOWED_GROUPS", "admins")
    assert fragment(sign_in(browser, idp))["auth_error"] == ["not_permitted"]
    monkeypatch.setenv("THROUGHLINE_AUTH_AUTHENTIK_ALLOWED_GROUPS", "admins, family")
    idp.claims = {"nonce": "forged"}
    assert fragment(sign_in(browser, idp))["auth_error"] == ["provider"]
    idp.claims = {"aud": "someone-else"}
    assert fragment(sign_in(browser, idp))["auth_error"] == ["provider"]
    idp.claims = {}
    assert fragment(sign_in(browser, idp, state="not-the-state"))["auth_error"] == ["expired"]
    assert sign_in(browser, idp, return_to="https://evil.example/").headers["location"] == "/ui/"
    assert sign_in(browser, idp, return_to="//evil.example/ui").headers["location"] == "/ui/"


def test_oidc_error_and_unknown_provider(idp, browser):
    r = browser.get("/v1/auth/oidc/authentik/return", params={"error": "access_denied"}, follow_redirects=False)
    assert fragment(r)["auth_error"] == ["denied"]
    assert browser.get("/v1/auth/oidc/nope/start", follow_redirects=False).status_code == 404


def test_oidc_refuses_endpoints_off_the_issuer_host(idp, browser, monkeypatch):
    real = idp.handler

    def moved(request):
        response = real(request)
        if request.url.path.endswith("openid-configuration"):
            meta = response.json()
            meta["token_endpoint"] = "https://elsewhere.example/token"
            return httpx.Response(200, json=meta)
        return response

    monkeypatch.setattr(oidc, "_client", lambda: httpx.Client(transport=httpx.MockTransport(moved)))
    r = browser.get("/v1/auth/oidc/authentik/start", follow_redirects=False)
    assert fragment(r)["auth_error"] == ["provider"]
