"""Sign-in configuration, read from the environment on every call.

Nothing here is required: with no variable set, the interface offers a
username and password (for accounts created on the box) and pasting an access
token, and no external provider.

  THROUGHLINE_AUTH_PASSWORD=1          username and password sign-in (0 hides it)
  THROUGHLINE_AUTH_TOKEN=1             "use an access token" in the interface
                                       (bearer tokens always work on /v1)
  THROUGHLINE_AUTH_PROVIDERS=authentik external providers, comma separated ids
  THROUGHLINE_AUTH_SESSION_DAYS=30     lifetime of a remembered session
  THROUGHLINE_AUTH_SESSION_HOURS=12    lifetime of a session that is not
  THROUGHLINE_AUTH_COOKIE_SECURE=auto  auto (https public URL or request), 1, 0
  THROUGHLINE_PUBLIC_URL=              https://throughline.example.com, the
                                       address browsers use (redirect URIs)

Per provider <ID> (upper case of the id):
  THROUGHLINE_AUTH_<ID>_ISSUER         OIDC issuer URL (required)
  THROUGHLINE_AUTH_<ID>_CLIENT_ID      (required)
  THROUGHLINE_AUTH_<ID>_CLIENT_SECRET  confidential clients (recommended)
  THROUGHLINE_AUTH_<ID>_KIND           preset: authentik or oidc (default: the
                                       id when it names a preset, else oidc)
  THROUGHLINE_AUTH_<ID>_LABEL          button text (default from the preset)
  THROUGHLINE_AUTH_<ID>_SCOPES         default "openid email profile"
  THROUGHLINE_AUTH_<ID>_ALLOWED_GROUPS only members of one of these groups
  THROUGHLINE_AUTH_<ID>_LINK_BY_EMAIL  1 (default): a first sign-in with a
                                       verified e-mail equal to an account's
                                       links that account; 0 needs
                                       `throughline account link`
  THROUGHLINE_AUTH_<ID>_REDIRECT_URI   override the derived callback URL
"""

import os
import re
from dataclasses import dataclass, field

ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

# Presets fill in what a provider family always looks like. A new family is a
# new entry here (and, for a non-OIDC protocol, a new module).
PRESETS = {
    "authentik": {"protocol": "oidc", "label": "Authentik"},
    "oidc": {"protocol": "oidc", "label": "Single sign-on"},
}


@dataclass(frozen=True)
class Provider:
    id: str
    kind: str
    protocol: str
    label: str
    issuer: str
    client_id: str
    client_secret: str | None
    scopes: str
    allowed_groups: tuple[str, ...] = ()
    link_by_email: bool = True
    redirect_uri: str | None = None

    def public(self):
        """What the sign-in screen may know: no issuer, client or secret."""
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "start": f"/v1/auth/{self.protocol}/{self.id}/start",
        }


@dataclass(frozen=True)
class Settings:
    password: bool
    token: bool
    providers: dict = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    session_days: float = 30
    session_hours: float = 12
    cookie_secure: str = "auto"
    public_url: str | None = None


def _flag(name, default):
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


def _number(name, default, low, high):
    try:
        value = float(os.getenv(name) or default)
    except ValueError:
        return default
    return min(max(value, low), high)


def _provider(pid):
    prefix = f"THROUGHLINE_AUTH_{pid.upper()}_"

    def env(key, default=None):
        value = os.getenv(prefix + key)
        return value.strip() if value and value.strip() else default

    kind = (env("KIND") or (pid if pid in PRESETS else "oidc")).lower()
    if kind not in PRESETS:
        raise ValueError(f"{pid}: unknown kind {kind!r} (known: {', '.join(PRESETS)})")
    preset = PRESETS[kind]
    issuer, client_id = env("ISSUER"), env("CLIENT_ID")
    missing = [k for k, v in (("ISSUER", issuer), ("CLIENT_ID", client_id)) if not v]
    if missing:
        raise ValueError(f"{pid}: set " + " and ".join(prefix + k for k in missing))
    if not re.match(r"^https?://", issuer):
        raise ValueError(f"{pid}: {prefix}ISSUER must be an http(s) URL")
    groups = tuple(
        g for g in re.split(r"[,\s]+", env("ALLOWED_GROUPS", "")) if g
    )
    client_secret = env("CLIENT_SECRET")
    if client_secret:
        from .. import secrets as tl_secrets

        if tl_secrets.is_ref(client_secret):
            client_secret = tl_secrets.resolve(client_secret)
    return Provider(
        id=pid,
        kind=kind,
        protocol=preset["protocol"],
        label=env("LABEL", preset["label"]),
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        scopes=env("SCOPES", "openid email profile"),
        allowed_groups=groups,
        link_by_email=_flag(prefix + "LINK_BY_EMAIL", True),
        redirect_uri=env("REDIRECT_URI"),
    )


def settings():
    """The current settings. A misconfigured provider is left out and named
    in `problems` (logged at startup) rather than failing every request."""
    providers, problems = {}, []
    for pid in re.split(r"[,\s]+", (os.getenv("THROUGHLINE_AUTH_PROVIDERS") or "").lower()):
        if not pid:
            continue
        if not ID.match(pid) or pid in providers:
            problems.append(f"{pid}: not a valid provider id")
            continue
        try:
            providers[pid] = _provider(pid)
        except ValueError as exc:
            problems.append(str(exc))
    secure = (os.getenv("THROUGHLINE_AUTH_COOKIE_SECURE") or "auto").strip().lower()
    if secure not in ("auto", "1", "0", "true", "false"):
        secure = "auto"
    public = (os.getenv("THROUGHLINE_PUBLIC_URL") or "").strip().rstrip("/") or None
    return Settings(
        password=_flag("THROUGHLINE_AUTH_PASSWORD", True),
        token=_flag("THROUGHLINE_AUTH_TOKEN", True),
        providers=providers,
        problems=tuple(problems),
        session_days=_number("THROUGHLINE_AUTH_SESSION_DAYS", 30, 1 / 24, 400),
        session_hours=_number("THROUGHLINE_AUTH_SESSION_HOURS", 12, 0.25, 24 * 30),
        cookie_secure=secure,
        public_url=public,
    )
