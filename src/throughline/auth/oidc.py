"""OpenID Connect sign-in: authorization code flow with PKCE.

Authentik is a preset of this module; any OIDC provider (Keycloak, Authelia,
Pocket ID, Google, ...) works with kind=oidc.

The ID token comes straight from the provider's token endpoint over TLS, with
this server authenticating as the client, so its signature is not checked
(OpenID Connect Core 3.1.3.7, step 6, allows TLS server validation in its
place). Issuer, audience, authorised party, expiry and nonce are checked.

Egress: the server talks only to the configured issuer's host, which the
owner named by configuring it. A discovered endpoint on any other host must
pass THROUGHLINE_EGRESS_ALLOW like every other outbound call. Nothing sent
there derives from records.
"""

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx

from ..db import connect
from ..providers import egress
from . import sessions

log = logging.getLogger("throughline.auth")

FLOW_TTL_S = 600
DISCOVERY_TTL_S = 3600
CLOCK_SKEW_S = 120

_cache: dict = {}
_lock = threading.Lock()


class SignInError(Exception):
    """A failed sign-in. `code` is shown to the user; `detail` is logged."""

    def __init__(self, code, detail="", subject=None, return_to=None):
        super().__init__(detail or code)
        self.code, self.detail, self.subject, self.return_to = code, detail, subject, return_to


def _b64url(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _host(url):
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _check_endpoint(provider, url, *, server_side):
    """An endpoint must be http(s), https unless loopback, and (when this
    server calls it) on the issuer's host or allowed by the egress list."""
    try:
        host, _port = egress.target(url)
    except egress.EgressDenied as exc:
        raise SignInError("provider", f"{provider.id}: endpoint {url!r}: {exc}") from None
    if urlsplit(url).scheme != "https" and not egress.is_loopback(host):
        raise SignInError("provider", f"{provider.id}: endpoint {url} is not https")
    if server_side and host != _host(provider.issuer):
        try:
            egress.check(url)
        except egress.EgressDenied as exc:
            raise SignInError("provider", f"{provider.id}: {exc}") from None
    return url


def _client():
    return httpx.Client(timeout=10, follow_redirects=False)


def discover(provider):
    """The provider's metadata, cached for an hour per issuer."""
    key = (provider.id, provider.issuer)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
    url = provider.issuer.rstrip("/") + "/.well-known/openid-configuration"
    _check_endpoint(provider, url, server_side=True)
    try:
        with _client() as client:
            response = client.get(url, headers={"Accept": "application/json"})
        response.raise_for_status()
        meta = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SignInError("unreachable", f"{provider.id}: discovery failed: {exc}") from None
    if not isinstance(meta, dict) or meta.get("issuer", "").rstrip("/") != provider.issuer.rstrip("/"):
        raise SignInError("provider", f"{provider.id}: discovery names another issuer")
    for name in ("authorization_endpoint", "token_endpoint"):
        if not isinstance(meta.get(name), str):
            raise SignInError("provider", f"{provider.id}: discovery has no {name}")
    _check_endpoint(provider, meta["authorization_endpoint"], server_side=False)
    _check_endpoint(provider, meta["token_endpoint"], server_side=True)
    with _lock:
        _cache[key] = (time.monotonic() + DISCOVERY_TTL_S, meta)
    return meta


def redirect_uri(provider, request, settings):
    if provider.redirect_uri:
        return provider.redirect_uri
    base = settings.public_url or str(request.base_url).rstrip("/")
    return f"{base}/v1/auth/{provider.protocol}/{provider.id}/return"


def safe_return(path):
    """Only a path inside the interface, never another origin."""
    if (
        isinstance(path, str)
        and path.startswith("/ui")
        and not path.startswith("//")
        and "\\" not in path
        and len(path) <= 1000
        and not any(ord(c) < 32 for c in path)
    ):
        return path
    return "/ui/"


def begin(
    provider,
    request,
    settings,
    return_to,
    persistent,
    *,
    purpose="login",
    account_id=None,
    handle_hint=None,
):
    """(authorization URL, state for the browser cookie)."""
    meta = discover(provider)
    state, nonce, verifier = (
        secrets.token_urlsafe(32),
        secrets.token_urlsafe(24),
        secrets.token_urlsafe(48),
    )
    callback = redirect_uri(provider, request, settings)
    with connect("app") as conn:
        conn.execute(
            "DELETE FROM tl.auth_flows WHERE created_at<now()-make_interval(secs=>%s)",
            (FLOW_TTL_S,),
        )
        conn.execute(
            "INSERT INTO tl.auth_flows"
            "(state_hash,provider,nonce,verifier,return_to,persistent,redirect_uri,purpose,account_id,handle_hint)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                sessions.digest(state),
                provider.id,
                nonce,
                verifier,
                safe_return(return_to),
                persistent,
                callback,
                purpose,
                account_id,
                (handle_hint or "").strip() or None,
            ),
        )
    query = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": callback,
        "scope": provider.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    endpoint = meta["authorization_endpoint"]
    joiner = "&" if "?" in endpoint else "?"
    return endpoint + joiner + urlencode(query), state


def _claims(id_token):
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, AttributeError):
        raise SignInError("provider", "the ID token is not a JWT") from None


def _validate(provider, meta, claims, nonce):
    now = time.time()
    if claims.get("iss") != meta["issuer"]:
        raise SignInError("provider", "ID token issuer mismatch")
    audience = claims.get("aud")
    audience = [audience] if isinstance(audience, str) else list(audience or [])
    if provider.client_id not in audience:
        raise SignInError("provider", "ID token audience mismatch")
    if len(audience) > 1 and claims.get("azp") != provider.client_id:
        raise SignInError("provider", "ID token authorised party mismatch")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - CLOCK_SKEW_S:
        raise SignInError("expired", "ID token expired")
    if isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + CLOCK_SKEW_S:
        raise SignInError("provider", "ID token issued in the future")
    if not sessions.same(claims.get("nonce"), nonce):
        raise SignInError("provider", "ID token nonce mismatch")
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise SignInError("provider", "ID token has no subject")


def _groups(claims):
    groups = claims.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return {str(g) for g in groups if isinstance(g, (str, int))}


def finish(provider, request, settings, code, state, browser_state, user_agent):
    """Complete the flow; returns (account row, cookie secret, return path,
    persistent)."""
    if not state or not sessions.same(state, browser_state):
        raise SignInError("expired", "state does not match this browser")
    with connect("app") as conn:
        flow = conn.execute(
            "DELETE FROM tl.auth_flows WHERE state_hash=%s AND provider=%s RETURNING *,created_at>now()-make_interval(secs=>%s) AS fresh",
            (sessions.digest(state), provider.id, FLOW_TTL_S),
        ).fetchone()
    if not flow or not flow["fresh"]:
        raise SignInError("expired", "unknown or expired sign-in")
    purpose = flow.get("purpose") or "login"
    err_return = flow["return_to"].split("?", 1)[0] if purpose != "login" else None
    if not code:
        raise SignInError("denied", "no authorization code", return_to=err_return)
    try:
        meta = discover(provider)
    except SignInError as exc:
        raise SignInError(exc.code, exc.detail, exc.subject, return_to=err_return) from None
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow["redirect_uri"],
        "code_verifier": flow["verifier"],
        "client_id": provider.client_id,
    }
    auth = (provider.client_id, provider.client_secret) if provider.client_secret else None
    try:
        with _client() as client:
            response = client.post(
                meta["token_endpoint"], data=form, auth=auth, headers={"Accept": "application/json"}
            )
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SignInError("unreachable", f"token exchange failed: {exc}", return_to=err_return) from None
    if response.status_code != 200 or not isinstance(body, dict) or not body.get("id_token"):
        error = body.get("error") if isinstance(body, dict) else None
        raise SignInError(
            "provider",
            f"token exchange answered {response.status_code} {error or ''}".strip(),
            return_to=err_return,
        )
    try:
        claims = _claims(body["id_token"])
        _validate(provider, meta, claims, flow["nonce"])
        if provider.allowed_groups and not _groups(claims) & set(provider.allowed_groups):
            raise SignInError("not_permitted", f"{claims['sub']} is in none of the allowed groups")
        if purpose == "claim_owner":
            account = _claim_owner(provider, meta, claims, flow.get("handle_hint"))
        elif purpose == "link_account":
            account = _link_account(provider, meta, claims, flow.get("account_id"))
        else:
            account = _account(provider, meta, claims)
    except SignInError as exc:
        raise SignInError(exc.code, exc.detail, exc.subject, return_to=err_return) from None
    with connect("app") as conn:
        secret = sessions.start(conn, account["id"], provider.id, flow["persistent"], user_agent, settings)
    return account, secret, flow["return_to"], flow["persistent"]


def _claim_owner(provider, meta, claims, handle_hint):
    from ..onboarding import owner
    from ..onboarding.errors import SetupError

    subject = claims["sub"]
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    username = claims.get("preferred_username") if isinstance(claims.get("preferred_username"), str) else None
    display_name = claims.get("name") if isinstance(claims.get("name"), str) else username
    try:
        row = owner.claim_first_account_oidc(
            handle_hint=handle_hint,
            display_name=display_name,
            email=email,
            provider_id=provider.id,
            subject=subject,
            issuer=meta["issuer"],
            username=username,
        )
    except SetupError as exc:
        code = "already_claimed" if exc.status == 409 else "provider"
        raise SignInError(code, exc.message) from None
    except ValueError as exc:
        raise SignInError("provider", str(exc)) from None
    log.info("created first owner account %s via %s", row["handle"], provider.id)
    return row


def _link_account(provider, meta, claims, account_id):
    if not account_id:
        raise SignInError("expired", "missing target account for link flow")
    subject = claims["sub"]
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    username = claims.get("preferred_username") if isinstance(claims.get("preferred_username"), str) else None
    with connect("app") as conn:
        row = conn.execute(
            "SELECT a.id,a.handle,a.status FROM tl.accounts a"
            " JOIN tl.tokens t ON t.id=a.token_id AND t.revoked_at IS NULL WHERE a.id=%s",
            (account_id,),
        ).fetchone()
        if not row:
            raise SignInError("disabled", "target account not found")
        if row["status"] != "active":
            raise SignInError("disabled", f"account {row['handle']} is disabled")
        existing = conn.execute(
            "SELECT account_id FROM tl.account_links WHERE provider=%s AND subject=%s",
            (provider.id, subject),
        ).fetchone()
        if existing and existing["account_id"] != account_id:
            raise SignInError("already_linked", f"{provider.id} identity {subject} belongs to another account", subject)
        if existing:
            conn.execute(
                "UPDATE tl.account_links SET last_login_at=now(),issuer=coalesce(%s,issuer),"
                "email=coalesce(%s,email),username=coalesce(%s,username)"
                " WHERE provider=%s AND subject=%s",
                (meta["issuer"], email, username, provider.id, subject),
            )
        else:
            conn.execute(
                "INSERT INTO tl.account_links(account_id,provider,subject,issuer,email,username,last_login_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,now())",
                (account_id, provider.id, subject, meta["issuer"], email, username),
            )
        log.info("linked %s identity %s to account %s", provider.id, subject, row["handle"])
        return row


def _account(provider, meta, claims):
    subject = claims["sub"]
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    username = claims.get("preferred_username") if isinstance(claims.get("preferred_username"), str) else None
    with connect("app") as conn:
        row = conn.execute(
            "SELECT a.id,a.handle,a.status,i.id AS identity_id FROM tl.account_links i JOIN tl.accounts a ON a.id=i.account_id"
            " JOIN tl.tokens t ON t.id=a.token_id AND t.revoked_at IS NULL WHERE i.provider=%s AND i.subject=%s",
            (provider.id, subject),
        ).fetchone()
        if row is None and provider.link_by_email and email and claims.get("email_verified") is True:
            # First sign-in: a verified address equal to an account's links
            # it, unless that account already has another identity here.
            row = conn.execute(
                "SELECT a.id,a.handle,a.status,NULL::bigint AS identity_id FROM tl.accounts a JOIN tl.tokens t ON t.id=a.token_id AND t.revoked_at IS NULL"
                " WHERE lower(a.email)=lower(%s) AND NOT EXISTS (SELECT 1 FROM tl.account_links i WHERE i.account_id=a.id AND i.provider=%s)",
                (email, provider.id),
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT INTO tl.account_links(account_id,provider,subject,issuer,email,username) VALUES (%s,%s,%s,%s,%s,%s)",
                    (row["id"], provider.id, subject, meta["issuer"], email, username),
                )
                log.info("linked %s identity %s to account %s by e-mail", provider.id, subject, row["handle"])
        if row is None:
            raise SignInError("unlinked", f"no account is linked to {provider.id} subject {subject}", subject)
        if row["status"] != "active":
            raise SignInError("disabled", f"account {row['handle']} is disabled")
        conn.execute(
            "UPDATE tl.account_links SET last_login_at=now(),email=coalesce(%s,email),username=coalesce(%s,username)"
            " WHERE provider=%s AND subject=%s",
            (email, username, provider.id, subject),
        )
        return row
