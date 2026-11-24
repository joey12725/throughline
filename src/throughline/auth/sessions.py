"""Browser sessions: the cookie, its row, and how a request is resolved to the
account's principal token. App role."""

import hashlib
import hmac
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from ..db import connect
from . import config, passwords

COOKIE = "tl_session"
# With Secure the __Host- prefix also pins the cookie to this host and path /.
SECURE_COOKIE = "__Host-tl_session"
STATE_COOKIE = "tl_auth_state"


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def available(conn):
    return conn.execute(
        "SELECT to_regclass('tl.web_sessions') IS NOT NULL AS ok"
    ).fetchone()["ok"]


def secure(request, settings=None):
    settings = settings or config.settings()
    if settings.cookie_secure in ("1", "true"):
        return True
    if settings.cookie_secure in ("0", "false"):
        return False
    if settings.public_url:
        return urlsplit(settings.public_url).scheme == "https"
    return request.url.scheme == "https"


def cookie_secret(request):
    return request.cookies.get(SECURE_COOKIE) or request.cookies.get(COOKIE)


def set_cookie(response, request, secret, persistent, settings):
    is_secure = secure(request, settings)
    response.set_cookie(
        SECURE_COOKIE if is_secure else COOKIE,
        secret,
        max_age=int(settings.session_days * 86400) if persistent else None,
        path="/",
        secure=is_secure,
        httponly=True,
        samesite="lax",
    )


def clear_cookie(response, request):
    for name in (SECURE_COOKIE, COOKIE):
        if name in request.cookies:
            response.delete_cookie(
                name, path="/", secure=name == SECURE_COOKIE, httponly=True, samesite="lax"
            )


def start(conn, account_id, method, persistent, user_agent, settings):
    """Open a session; returns the secret for the cookie (never stored)."""
    secret = secrets.token_urlsafe(32)
    lifetime = (
        timedelta(days=settings.session_days)
        if persistent
        else timedelta(hours=settings.session_hours)
    )
    conn.execute(
        "INSERT INTO tl.web_sessions(secret_hash,account_id,method,persistent,expires_at,user_agent) VALUES (%s,%s,%s,%s,now()+%s,%s)",
        (digest(secret), account_id, method, persistent, lifetime, (user_agent or "")[:300] or None),
    )
    conn.execute("UPDATE tl.accounts SET last_login_at=now() WHERE id=%s", (account_id,))
    return secret


def resolve(secret):
    """The principal token row of a live session, with `account` and
    `session` added; None for an unknown, expired, revoked or disabled one."""
    if not secret or len(secret) > 200:
        return None
    with connect("app") as conn:
        if not available(conn):
            return None
        session = conn.execute(
            "SELECT s.id,s.method,s.persistent,s.created_at,s.expires_at,a.id AS account_id,a.handle,a.display_name,a.email,a.token_id"
            " FROM tl.web_sessions s JOIN tl.accounts a ON a.id=s.account_id"
            " WHERE s.secret_hash=%s AND s.revoked_at IS NULL AND s.expires_at>now() AND a.status='active'",
            (digest(secret),),
        ).fetchone()
        if not session:
            return None
        token = conn.execute(
            "SELECT * FROM tl.tokens WHERE id=%s AND revoked_at IS NULL",
            (session["token_id"],),
        ).fetchone()
        if not token:
            return None
        conn.execute(
            "UPDATE tl.web_sessions SET last_seen_at=now() WHERE id=%s AND last_seen_at<now()-interval '1 minute'",
            (session["id"],),
        )
    token["account"] = {
        "id": session["account_id"],
        "handle": session["handle"],
        "display_name": session["display_name"],
        "email": session["email"],
    }
    token["session"] = {
        "id": session["id"],
        "method": session["method"],
        "persistent": session["persistent"],
        "created_at": session["created_at"],
        "expires_at": session["expires_at"],
    }
    return token


def end(secret):
    if not secret:
        return
    with connect("app") as conn:
        if available(conn):
            conn.execute(
                "UPDATE tl.web_sessions SET revoked_at=now() WHERE secret_hash=%s AND revoked_at IS NULL",
                (digest(secret),),
            )


def accounts_exist():
    with connect("app") as conn:
        if not available(conn):
            return None
        return conn.execute("SELECT EXISTS(SELECT 1 FROM tl.accounts) AS ok").fetchone()["ok"]


def password_login(username, password, persistent, user_agent, address, settings):
    """(account row, cookie secret). Raises PermissionError with a reason
    code: 'throttled:<seconds>' or 'invalid'."""
    key = (username or "").strip().lower()[:320]
    wait = max(passwords.by_account.retry_after(key), passwords.by_address.retry_after(address))
    if wait:
        raise PermissionError(f"throttled:{wait}")
    with connect("app") as conn:
        row = None
        if available(conn) and key:
            row = conn.execute(
                "SELECT a.id,a.handle,a.status,p.hash FROM tl.accounts a JOIN tl.account_passwords p ON p.account_id=a.id"
                " JOIN tl.tokens t ON t.id=a.token_id AND t.revoked_at IS NULL"
                " WHERE a.handle=%s OR lower(a.email)=%s ORDER BY a.handle=%s DESC LIMIT 1",
                (key, key, key),
            ).fetchone()
        good = passwords.verify(password or "", row["hash"] if row else None)
        if not good or row["status"] != "active":
            passwords.by_account.fail(key)
            passwords.by_address.fail(address)
            raise PermissionError("invalid")
        passwords.by_account.clear(key)
        secret = start(conn, row["id"], "password", persistent, user_agent, settings)
        return row, secret


def same(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))
