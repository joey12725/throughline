"""Onboarding configuration for external sign-in (Authentik / OpenID Connect).

Reads and writes the standard THROUGHLINE_AUTH_* environment variables through
onboarding/store.py (settings.json + secrets.json), so configuring Authentik in
the browser takes effect immediately without editing .env files or restarting.
Variables set in the server's real environment remain locked (read-only in UI).
"""

from __future__ import annotations

import os
import re

from ..auth import accounts, config, oidc
from ..db import connect
from . import owner, store
from .errors import SetupError

_PROVIDER_KEYS = (
    "KIND",
    "LABEL",
    "ISSUER",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "SCOPES",
    "ALLOWED_GROUPS",
    "LINK_BY_EMAIL",
    "REDIRECT_URI",
)


def _prefix(pid: str) -> str:
    return f"THROUGHLINE_AUTH_{pid.upper()}_"


def _provider_locked(pid: str) -> bool:
    p = _prefix(pid)
    return store.locked("THROUGHLINE_AUTH_PROVIDERS") or any(
        store.locked(p + k) for k in _PROVIDER_KEYS
    )


def _base_url(request) -> str:
    public = (os.getenv("THROUGHLINE_PUBLIC_URL") or "").strip().rstrip("/")
    if public:
        return public
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://127.0.0.1:8000"


def public_callback_url(request, provider_id: str = "authentik") -> str:
    pid = (provider_id or "authentik").strip().lower()
    if not config.ID.match(pid):
        pid = "authentik"
    return f"{_base_url(request)}/v1/auth/oidc/{pid}/return"


def _configured_ids() -> list[str]:
    seen = []
    for pid in re.split(r"[,\s]+", (os.getenv("THROUGHLINE_AUTH_PROVIDERS") or "").lower()):
        if pid and config.ID.match(pid) and pid not in seen:
            seen.append(pid)
    return seen


def _account_summary(account_id: int | None) -> dict | None:
    if not account_id:
        return None
    with connect("app") as conn:
        row = conn.execute(
            "SELECT id,handle,display_name,email,status,"
            " (SELECT count(*) FROM tl.account_passwords p WHERE p.account_id=a.id)>0 AS has_password"
            " FROM tl.accounts a WHERE a.id=%s",
            (account_id,),
        ).fetchone()
        if not row:
            return None
        links = conn.execute(
            "SELECT provider,subject,issuer,email,username,linked_at,last_login_at"
            " FROM tl.account_links WHERE account_id=%s ORDER BY provider,id",
            (account_id,),
        ).fetchall()
    return {
        "id": row["id"],
        "handle": row["handle"],
        "display_name": row["display_name"],
        "email": row["email"],
        "status": row["status"],
        "has_password": bool(row["has_password"]),
        "links": [
            {
                "provider": link["provider"],
                "subject": link["subject"],
                "issuer": link["issuer"],
                "email": link["email"],
                "username": link["username"],
                "linked_at": link["linked_at"].isoformat() if link["linked_at"] else None,
                "last_login_at": link["last_login_at"].isoformat() if link["last_login_at"] else None,
            }
            for link in links
        ],
    }


def view(request=None, account_id: int | None = None) -> dict:
    """Owner-facing view of external sign-in configuration and linked identities."""
    settings = config.settings()
    ready, hint = owner.owner_ready()
    providers = []
    for pid, p in settings.providers.items():
        raw_secret = os.getenv(_prefix(pid) + "CLIENT_SECRET") or ""
        providers.append(
            {
                "id": p.id,
                "kind": p.kind,
                "label": p.label,
                "issuer": p.issuer,
                "client_id": p.client_id,
                "client_secret_set": bool(raw_secret.strip()),
                "client_secret_hint": store.mask(raw_secret.strip()) if raw_secret.strip() else None,
                "scopes": p.scopes,
                "allowed_groups": list(p.allowed_groups),
                "link_by_email": p.link_by_email,
                "redirect_uri": p.redirect_uri,
                "callback_url": p.redirect_uri or public_callback_url(request, p.id),
                "locked": _provider_locked(pid),
            }
        )
    return {
        "public_url": settings.public_url or _base_url(request),
        "public_url_locked": store.locked("THROUGHLINE_PUBLIC_URL"),
        "default_redirect_uri": public_callback_url(request, "authentik"),
        "password_enabled": settings.password,
        "password_locked": store.locked("THROUGHLINE_AUTH_PASSWORD"),
        "token_enabled": settings.token,
        "token_locked": store.locked("THROUGHLINE_AUTH_TOKEN"),
        "providers_locked": store.locked("THROUGHLINE_AUTH_PROVIDERS"),
        "can_configure_oidc": not store.locked("THROUGHLINE_AUTH_PROVIDERS"),
        "providers": providers,
        "problems": list(settings.problems),
        "current_account": _account_summary(account_id),
        "owner_ready": ready,
        "owner_hint": hint,
    }


def save_provider(data: dict, *, verify_discovery: bool = True) -> config.Provider:
    """Validate, optionally test discovery, and persist an OIDC/Authentik provider."""
    pid = str((data or {}).get("id") or "authentik").strip().lower()
    if not config.ID.match(pid):
        raise SetupError(
            "Invalid provider ID",
            "Use lowercase letters, digits, or underscores starting with a letter (e.g. authentik).",
            status=422,
            problems=[{"where": "id", "message": "must start with a letter and use a-z, 0-9, _"}],
        )
    if _provider_locked(pid):
        raise SetupError(
            "Sign-in provider is locked by server environment",
            f"The {pid} sign-in provider is configured in the server's environment variables.",
            "Edit the environment variables on the server to change it.",
            status=409,
        )

    kind = str((data or {}).get("kind") or (pid if pid in config.PRESETS else "oidc")).strip().lower()
    if kind not in config.PRESETS:
        raise SetupError(
            "Unsupported sign-in provider type",
            f"Kind {kind!r} isn't supported (use 'authentik' or 'oidc').",
            status=422,
            problems=[{"where": "kind", "message": "use authentik or oidc"}],
        )
    preset = config.PRESETS[kind]

    issuer = str((data or {}).get("issuer") or "").strip()
    client_id = str((data or {}).get("client_id") or "").strip()
    problems = []
    if not issuer or not re.match(r"^https?://", issuer):
        problems.append({"where": "issuer", "message": "Enter the full http(s):// issuer URL from Authentik"})
    if not client_id:
        problems.append({"where": "client_id", "message": "Enter the OAuth2 Client ID"})
    if problems:
        raise SetupError(
            "Missing sign-in provider settings",
            "Fill in both the Issuer URL and Client ID from your OIDC provider.",
            "In Authentik, open your OAuth2/OpenID Provider and copy OpenID Configuration Issuer and Client ID.",
            status=422,
            problems=problems,
        )

    prefix = _prefix(pid)
    raw_secret = (data or {}).get("client_secret")
    existing_secret = os.getenv(prefix + "CLIENT_SECRET") or ""
    if raw_secret is None or (isinstance(raw_secret, str) and raw_secret.startswith("••••")):
        client_secret_store = existing_secret or None
    else:
        client_secret_store = str(raw_secret).strip() or None

    label = str((data or {}).get("label") or "").strip() or preset["label"]
    scopes = str((data or {}).get("scopes") or "").strip() or "openid email profile"
    raw_groups = (data or {}).get("allowed_groups")
    if isinstance(raw_groups, list):
        groups = tuple(str(g).strip() for g in raw_groups if str(g).strip())
    elif isinstance(raw_groups, str):
        groups = tuple(g for g in re.split(r"[,\s]+", raw_groups.strip()) if g)
    else:
        groups = ()
    link_by_email = bool((data or {}).get("link_by_email", True))
    redirect_uri = str((data or {}).get("redirect_uri") or "").strip() or None

    resolved_secret = client_secret_store
    if resolved_secret:
        from .. import secrets as tl_secrets

        if tl_secrets.is_ref(resolved_secret):
            try:
                resolved_secret = tl_secrets.resolve(resolved_secret)
            except Exception as exc:
                raise SetupError(
                    "Couldn't resolve Client Secret reference",
                    str(exc),
                    "Check your Infisical configuration or paste the secret directly.",
                    status=422,
                    problems=[{"where": "client_secret", "message": str(exc)}],
                ) from None

    candidate = config.Provider(
        id=pid,
        kind=kind,
        protocol=preset["protocol"],
        label=label,
        issuer=issuer,
        client_id=client_id,
        client_secret=resolved_secret,
        scopes=scopes,
        allowed_groups=groups,
        link_by_email=link_by_email,
        redirect_uri=redirect_uri,
    )

    if verify_discovery:
        try:
            oidc.discover(candidate)
        except oidc.SignInError as exc:
            raise SetupError(
                f"Couldn't reach {label}",
                exc.detail or "OpenID Connect discovery failed.",
                "Verify the Issuer URL ends with the application slug (e.g. https://auth.example.com/application/o/throughline/) and is reachable from the Throughline server.",
                status=400,
                problems=[{"where": "issuer", "message": exc.detail or "OIDC discovery failed"}],
            ) from None

    ids = _configured_ids()
    if pid not in ids:
        ids.append(pid)

    updates: dict[str, str | None] = {
        "THROUGHLINE_AUTH_PROVIDERS": ",".join(ids),
        prefix + "KIND": kind,
        prefix + "LABEL": label,
        prefix + "ISSUER": issuer,
        prefix + "CLIENT_ID": client_id,
        prefix + "CLIENT_SECRET": client_secret_store,
        prefix + "SCOPES": scopes,
        prefix + "ALLOWED_GROUPS": ",".join(groups) if groups else None,
        prefix + "LINK_BY_EMAIL": "1" if link_by_email else "0",
        prefix + "REDIRECT_URI": redirect_uri,
    }
    public_url = (data or {}).get("public_url")
    if public_url is not None and not store.locked("THROUGHLINE_PUBLIC_URL"):
        updates["THROUGHLINE_PUBLIC_URL"] = str(public_url).strip().rstrip("/") or None

    try:
        store.set_env(updates, secret_keys={prefix + "CLIENT_SECRET"})
    except PermissionError as exc:
        raise SetupError("Locked by server environment", str(exc), status=409) from None

    return config.settings().providers[pid]


def remove_provider(provider_id: str) -> None:
    pid = (provider_id or "").strip().lower()
    if not config.ID.match(pid):
        raise SetupError("Invalid provider ID", f"{provider_id!r} is not a valid provider id.", status=422)
    if _provider_locked(pid):
        raise SetupError(
            "Sign-in provider is locked by server environment",
            f"The {pid} provider is set in the server environment and can't be removed from the browser.",
            status=409,
        )
    remaining = [p for p in _configured_ids() if p != pid]
    settings = config.settings()
    if not remaining and not settings.password:
        raise SetupError(
            "Can't remove your only sign-in method",
            "Re-enable password sign-in before removing the last external sign-in provider.",
            status=409,
        )
    prefix = _prefix(pid)
    updates: dict[str, str | None] = {
        "THROUGHLINE_AUTH_PROVIDERS": ",".join(remaining) if remaining else None,
        **{prefix + k: None for k in _PROVIDER_KEYS},
    }
    store.set_env(updates, secret_keys={prefix + "CLIENT_SECRET"})


def update_methods(*, password: bool | None = None, token: bool | None = None, public_url: str | None = None) -> None:
    updates: dict[str, str | None] = {}
    if password is not None:
        if store.locked("THROUGHLINE_AUTH_PASSWORD"):
            raise SetupError("Locked by server environment", "THROUGHLINE_AUTH_PASSWORD is set on the server.", status=409)
        if not password:
            settings = config.settings()
            if not settings.providers:
                raise SetupError(
                    "Connect an SSO provider first",
                    "Configure and link Authentik or another OIDC provider before turning off password sign-in.",
                    status=409,
                )
            with connect("app") as conn:
                linked = conn.execute(
                    "SELECT 1 FROM tl.account_links i JOIN tl.accounts a ON a.id=i.account_id"
                    " WHERE a.status='active' AND i.provider=ANY(%s) LIMIT 1",
                    (list(settings.providers),),
                ).fetchone()
            if not linked:
                raise SetupError(
                    "Link your account first",
                    "Sign in or link your owner account with Authentik before disabling password sign-in so you don't lock yourself out.",
                    status=409,
                )
        updates["THROUGHLINE_AUTH_PASSWORD"] = "1" if password else "0"
    if token is not None:
        if store.locked("THROUGHLINE_AUTH_TOKEN"):
            raise SetupError("Locked by server environment", "THROUGHLINE_AUTH_TOKEN is set on the server.", status=409)
        updates["THROUGHLINE_AUTH_TOKEN"] = "1" if token else "0"
    if public_url is not None:
        if store.locked("THROUGHLINE_PUBLIC_URL"):
            raise SetupError("Locked by server environment", "THROUGHLINE_PUBLIC_URL is set on the server.", status=409)
        updates["THROUGHLINE_PUBLIC_URL"] = str(public_url).strip().rstrip("/") or None
    if updates:
        store.set_env(updates)


def update_account_email(account_id: int, email: str | None) -> dict:
    owner.require_owner("throughline lib account update <handle> --email <address>")
    with connect() as conn:
        row = conn.execute("SELECT handle FROM tl.accounts WHERE id=%s", (account_id,)).fetchone()
    if not row:
        raise SetupError("Account not found", "Sign in with an owner account first.", status=404)
    try:
        updated = accounts.update(row["handle"], email=email or "")
    except ValueError as exc:
        raise SetupError("Invalid email address", str(exc).capitalize() + ".", status=422,
                         problems=[{"where": "email", "message": str(exc)}]) from None
    return dict(updated)
