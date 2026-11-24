"""Signing in to the web interface (docs/web/auth.md).

Three ways in, all optional except the bearer token on /v1:
  - an access token pasted into the interface (as before),
  - a username and password of an account made on the box,
  - an external provider (OpenID Connect; Authentik is a preset).

The last two open a browser session (an HttpOnly cookie). A session resolves
to its account's principal token, so it is authorised, scoped and audited
exactly like that token. See accounts.py for how accounts relate to tokens.
"""

from . import config, sessions


def describe(settings=None):
    """GET /v1/auth/config: what the sign-in screen offers. Public."""
    settings = settings or config.settings()
    try:
        exist = sessions.accounts_exist()
    except Exception:
        exist = None
    password = settings.password and exist is not None
    return {
        "methods": {"password": password, "token": settings.token},
        "providers": [p.public() for p in settings.providers.values()],
        # False tells the screen to explain `throughline account create`.
        "accounts": bool(exist),
    }
