"""The few onboarding steps that need the owner database connection.

Tokens, accounts and companion devices are created "on the box": the API's
runtime role cannot insert tokens. The web onboarding may do these steps only
when the server process itself holds an owner database URL (a local or
single-user install). When it doesn't (e.g. the Docker service, which only
gets runtime roles), each step answers with the exact CLI command to run
instead, so the owner is never stuck.

The first owner account is protected by a one-time **setup code**: the
server prints it (with a ready-made link) when it starts with no account,
and writes it to ``<setup dir>/setup-code``. Anyone who can reach the server
but cannot see its log or files cannot claim it.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading

from ..db import connect, create_token
from . import store
from .errors import SetupError

log = logging.getLogger("throughline.setup")
_lock = threading.Lock()
_ready_cache: dict = {}


def owner_ready() -> tuple[bool, str | None]:
    """(True, None) when this process can mint tokens; else (False, hint)."""
    url = os.getenv("THROUGHLINE_DATABASE_URL", "")
    if url in _ready_cache:
        return _ready_cache[url]
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT has_table_privilege('tl.tokens','INSERT') AS ok"
            ).fetchone()
        answer = (True, None) if row and row["ok"] else (False, _hint())
    except Exception:
        answer = (False, _hint())
    _ready_cache[url] = answer
    return answer


def _hint():
    return (
        "This server runs without the owner database connection, so it can't create tokens itself."
        " Run the shown `throughline` command on the server (for Docker: `docker compose run --rm migrate …`)."
    )


def require_owner(command: str):
    ready, hint = owner_ready()
    if not ready:
        raise SetupError(
            "Run one command on the server",
            hint,
            f"Run: {command}",
            status=409,
            code="owner_required",
        )


def owner_create_token(name, kind="plugin", plugin=None, askers=None, location_source=False):
    command = f"throughline token create {name.replace(':', '-')} --kind {kind}" + (
        f" --plugin {plugin}" if plugin else ""
    ) + (" --location-source" if location_source else "") + " --json"
    require_owner(command)
    return create_token(name, kind, plugin, askers, location_source)


# ───────────── setup code ─────────────


def _code_file():
    return store.setup_dir() / "setup-code"


def accounts_exist() -> bool:
    try:
        with connect("app") as conn:
            return bool(conn.execute("SELECT 1 FROM tl.accounts LIMIT 1").fetchone())
    except Exception:
        with connect() as conn:
            return bool(conn.execute("SELECT 1 FROM tl.accounts LIMIT 1").fetchone())


def setup_code(create=True) -> str | None:
    """The current one-time code (created on first use)."""
    with _lock:
        path = _code_file()
        try:
            code = path.read_text().strip()
            if code:
                return code
        except FileNotFoundError:
            pass
        if not create:
            return None
        code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(code + "\n")
        return code


def announce(base_url: str):
    """Log the setup link at startup when nobody can sign in yet."""
    try:
        if accounts_exist():
            return
    except Exception:
        return
    code = setup_code()
    link = base_url.rstrip("/") + f"/ui/welcome?setup={code}"
    banner = "─" * 64
    log.warning(
        "\n%s\n  Welcome to Throughline. Finish setup in your browser:\n\n    %s\n\n"
        "  Setup code: %s  (also in %s)\n%s",
        banner, link, code, _code_file(), banner,
    )


def check_code(code: str):
    expected = setup_code(create=False)
    if not expected:
        raise SetupError(
            "No setup code yet",
            "The server hasn't issued a setup code.",
            "Restart `throughline serve` (it prints the code), or run `throughline lib setup code`.",
            status=409,
        )
    given = (code or "").strip().upper().replace(" ", "")
    if not hmac.compare_digest(given.encode(), expected.replace(" ", "").upper().encode()):
        raise SetupError(
            "That setup code isn't right",
            "The code doesn't match the one this server printed.",
            f"Copy it from the server log or from {_code_file()} (`throughline lib setup code`).",
            status=403,
            code="bad_setup_code",
        )


def create_first_account(code, handle, display_name, password, email=None):
    from ..auth import accounts

    if accounts_exist():
        raise SetupError(
            "An account already exists",
            "Throughline already has an owner account.",
            "Sign in with it instead.",
            status=409,
        )
    check_code(code)
    if not password or len(password) < 10:
        raise SetupError(
            "Choose a longer password",
            "Use at least 10 characters.",
            "A short phrase of a few words is easy to remember and hard to guess.",
            status=422,
            problems=[{"where": "password", "message": "at least 10 characters"}],
        )
    require_owner(f"throughline lib account create {handle} --password")
    try:
        row = accounts.create(handle, display_name=display_name, email=email, password=password)
    except ValueError as error:
        raise SetupError("Couldn't create the account", str(error).capitalize() + ".", status=422,
                         problems=[{"where": "handle", "message": str(error)}]) from None
    try:
        _code_file().unlink()
    except FileNotFoundError:
        pass
    return row


def _sanitize_handle(raw: str | None) -> str:
    import re

    candidate = re.sub(r"[^a-z0-9._-]+", "-", (raw or "").strip().lower()).strip("._-")
    if not candidate or not candidate[0].isalnum():
        candidate = "owner"
    return candidate[:63]


def claim_first_account_oidc(*, handle_hint=None, display_name=None, email=None,
                             provider_id: str, subject: str, issuer: str | None,
                             username: str | None = None):
    """Create the first owner account from a verified OIDC login (called only
    from a setup-code-gated `claim_owner` auth flow) and link the identity."""
    from ..auth import accounts

    if accounts_exist():
        raise SetupError(
            "An account already exists",
            "Throughline already has an owner account.",
            "Sign in with it instead.",
            status=409,
        )
    raw_handle = handle_hint or username or (email.split("@", 1)[0] if email and "@" in email else None) or "owner"
    handle = _sanitize_handle(raw_handle)
    require_owner(f"throughline lib account create {handle}")
    clean_email = email if (email and "@" in email) else None
    row = accounts.create(handle, display_name=display_name or username, email=clean_email, password=None)
    with connect() as conn:
        conn.execute(
            "INSERT INTO tl.account_links(account_id,provider,subject,issuer,email,username,last_login_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,now())",
            (row["id"], provider_id, subject, issuer, clean_email, username),
        )
    try:
        _code_file().unlink()
    except FileNotFoundError:
        pass
    return row
