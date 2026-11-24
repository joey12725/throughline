"""Account administration. Owner database connection only, like token
creation: accounts are made on the box (`throughline account ...`), never
from a browser or an HTTP token.

An account's permissions are its principal token's. Every account made here
gets a fresh owner token, so it sees and does what the owner does. Narrower
accounts (an agent token restricted to an asker) slot in by creating the
principal differently; nothing else about sign-in changes.
"""

import hashlib
import re
import secrets

from ..db import connect
from . import passwords

HANDLE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
COLUMNS = "a.id,a.handle,a.display_name,a.email,a.status,a.token_id,t.kind AS principal,t.askers,a.created_at,a.last_login_at"


def _normal_handle(handle):
    handle = (handle or "").strip().lower()
    if not HANDLE.match(handle):
        raise ValueError(
            "a handle is 1-63 lower-case letters, digits, '.', '_' or '-', starting with a letter or digit"
        )
    return handle


def _normal_email(email):
    email = (email or "").strip()
    if email and not re.match(r"^[^@\s]+@[^@\s]+$", email):
        raise ValueError("that is not an e-mail address")
    return email or None


def _one(conn, handle):
    row = conn.execute(
        f"SELECT {COLUMNS} FROM tl.accounts a JOIN tl.tokens t ON t.id=a.token_id WHERE a.handle=%s",
        (_normal_handle(handle),),
    ).fetchone()
    if not row:
        raise ValueError(f"no account {handle!r}")
    return row


def create(handle, display_name=None, email=None, password=None):
    handle, email = _normal_handle(handle), _normal_email(email)
    hashed = passwords.hash_password(password) if password else None
    with connect() as conn:
        if conn.execute("SELECT 1 FROM tl.accounts WHERE handle=%s", (handle,)).fetchone():
            raise ValueError(f"an account {handle!r} already exists")
        if email and conn.execute(
            "SELECT 1 FROM tl.accounts WHERE lower(email)=lower(%s)", (email,)
        ).fetchone():
            raise ValueError("another account already uses that e-mail address")
        # The principal's secret is never shown: the account signs in with its
        # own methods, and the token exists to carry kind, askers and limits.
        token = conn.execute(
            "INSERT INTO tl.tokens(name,token_hash,kind) VALUES (%s,%s,'owner') RETURNING id",
            (
                f"account:{handle}",
                hashlib.sha256(secrets.token_urlsafe(32).encode()).hexdigest(),
            ),
        ).fetchone()
        account = conn.execute(
            "INSERT INTO tl.accounts(handle,display_name,email,token_id) VALUES (%s,%s,%s,%s) RETURNING id",
            (handle, (display_name or "").strip() or None, email, token["id"]),
        ).fetchone()
        if hashed:
            conn.execute(
                "INSERT INTO tl.account_passwords(account_id,hash) VALUES (%s,%s)",
                (account["id"], hashed),
            )
        return _one(conn, handle)


def listing():
    with connect() as conn:
        return conn.execute(
            f"SELECT {COLUMNS},(SELECT count(*) FROM tl.account_passwords p WHERE p.account_id=a.id)>0 AS has_password,"
            " coalesce((SELECT array_agg(i.provider ORDER BY i.provider) FROM tl.account_links i WHERE i.account_id=a.id),'{}') AS providers,"
            " (SELECT count(*) FROM tl.web_sessions s WHERE s.account_id=a.id AND s.revoked_at IS NULL AND s.expires_at>now()) AS sessions"
            " FROM tl.accounts a JOIN tl.tokens t ON t.id=a.token_id ORDER BY a.id"
        ).fetchall()


def set_password(handle, password):
    hashed = passwords.hash_password(password) if password else None
    with connect() as conn:
        account = _one(conn, handle)
        if hashed:
            conn.execute(
                "INSERT INTO tl.account_passwords(account_id,hash) VALUES (%s,%s) ON CONFLICT (account_id) DO UPDATE SET hash=excluded.hash,updated_at=now()",
                (account["id"], hashed),
            )
        else:
            conn.execute("DELETE FROM tl.account_passwords WHERE account_id=%s", (account["id"],))
        # A changed or removed password ends the sessions it may have opened.
        conn.execute(
            "UPDATE tl.web_sessions SET revoked_at=now() WHERE account_id=%s AND method='password' AND revoked_at IS NULL",
            (account["id"],),
        )
        return account


def update(handle, display_name=None, email=None):
    with connect() as conn:
        account = _one(conn, handle)
        if display_name is not None:
            conn.execute(
                "UPDATE tl.accounts SET display_name=%s WHERE id=%s",
                (display_name.strip() or None, account["id"]),
            )
        if email is not None:
            conn.execute(
                "UPDATE tl.accounts SET email=%s WHERE id=%s",
                (_normal_email(email), account["id"]),
            )
        return _one(conn, handle)


def set_status(handle, active):
    with connect() as conn:
        account = _one(conn, handle)
        conn.execute(
            "UPDATE tl.accounts SET status=%s WHERE id=%s",
            ("active" if active else "disabled", account["id"]),
        )
        if not active:
            conn.execute(
                "UPDATE tl.web_sessions SET revoked_at=now() WHERE account_id=%s AND revoked_at IS NULL",
                (account["id"],),
            )
        return _one(conn, handle)


def link(handle, provider, subject, issuer=None):
    """Bind an external identity (provider id + subject) to an account."""
    provider, subject = (provider or "").strip().lower(), (subject or "").strip()
    if not re.match(r"^[a-z][a-z0-9_]{0,39}$", provider) or not subject:
        raise ValueError("name a provider id and the identity's subject")
    with connect() as conn:
        account = _one(conn, handle)
        taken = conn.execute(
            "SELECT a.handle FROM tl.account_links i JOIN tl.accounts a ON a.id=i.account_id WHERE i.provider=%s AND i.subject=%s",
            (provider, subject),
        ).fetchone()
        if taken:
            raise ValueError(f"that identity is already linked to {taken['handle']!r}")
        conn.execute(
            "INSERT INTO tl.account_links(account_id,provider,subject,issuer) VALUES (%s,%s,%s,%s)",
            (account["id"], provider, subject, issuer),
        )
        return identities(handle, conn)


def unlink(handle, provider, subject=None):
    with connect() as conn:
        account = _one(conn, handle)
        rows = conn.execute(
            "DELETE FROM tl.account_links WHERE account_id=%s AND provider=%s AND (%s::text IS NULL OR subject=%s) RETURNING id",
            (account["id"], provider, subject, subject),
        ).fetchall()
        if not rows:
            raise ValueError("no such linked identity")
        conn.execute(
            "UPDATE tl.web_sessions SET revoked_at=now() WHERE account_id=%s AND method=%s AND revoked_at IS NULL",
            (account["id"], provider),
        )
        return identities(handle, conn)


def identities(handle, conn=None):
    if conn is None:
        with connect() as conn:
            return identities(handle, conn)
    account = _one(conn, handle)
    return conn.execute(
        "SELECT provider,subject,issuer,email,username,linked_at,last_login_at FROM tl.account_links WHERE account_id=%s ORDER BY provider,linked_at",
        (account["id"],),
    ).fetchall()


def sessions(handle=None):
    with connect() as conn:
        return conn.execute(
            "SELECT s.id,a.handle,s.method,s.persistent,s.created_at,s.last_seen_at,s.expires_at,s.user_agent FROM tl.web_sessions s JOIN tl.accounts a ON a.id=s.account_id"
            " WHERE s.revoked_at IS NULL AND s.expires_at>now() AND (%s::text IS NULL OR a.handle=%s) ORDER BY s.last_seen_at DESC",
            (handle, handle),
        ).fetchall()


def revoke_sessions(handle=None, session_id=None):
    with connect() as conn:
        if handle is not None:
            _one(conn, handle)
        rows = conn.execute(
            "UPDATE tl.web_sessions s SET revoked_at=now() FROM tl.accounts a WHERE a.id=s.account_id AND s.revoked_at IS NULL"
            " AND (%s::text IS NULL OR a.handle=%s) AND (%s::bigint IS NULL OR s.id=%s) RETURNING s.id",
            (handle, handle, session_id, session_id),
        ).fetchall()
        return {"revoked": len(rows)}
