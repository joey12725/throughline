"""The web interface's static shell, served under /ui/.

The shell is public bytes: an HTML page, scripts, styles and fonts built by
web/ (vite) into the `static` directory next to this file. It holds no data;
everything it shows is fetched from /v1 as whoever signed in (session cookie
or a pasted bearer token; docs/web/auth.md). The routes are plain Starlette routes added after the API routers, so
they are not part of the OpenAPI schema, carry no authentication dependency,
open no database connection and leave no audit row.

Environment:
  THROUGHLINE_WEB=0                 do not serve the shell at all
  THROUGHLINE_WEB_TILE_ORIGIN=...   one https origin the page may load map
                                    tiles from (opt-in; default none; plain
                                    http only for a loopback host)
"""

import ipaddress
import logging
import os
import re
from pathlib import Path

from starlette.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

# Never "dist" or "build": both are git-ignored at any depth.
STATIC = Path(__file__).parent / "static"

_ORIGIN = re.compile(r"^(https?)://([A-Za-z0-9.-]+)(:[0-9]{1,5})?$")
log = logging.getLogger(__name__)

NOT_BUILT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Throughline</title>
<style>
body{font:16px/1.5 system-ui,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;background:#f6f5f1;color:#1d1c1a}
main{max-width:34rem;padding:2rem}
h1{font-size:1.25rem;margin:0 0 .5rem}
code{font:0.9em ui-monospace,monospace;background:#e9e7e0;padding:.1em .35em;border-radius:3px}
@media (prefers-color-scheme: dark){body{background:#161614;color:#ecebe6}code{background:#2a2925}}
</style>
</head>
<body>
<main>
<h1>UI not built</h1>
<p>The Throughline API is running, but the web interface has not been built
into this installation. Run <code>pnpm install</code> and
<code>pnpm build</code> in <code>web/</code>, then reload this page.</p>
<p>The API itself is unaffected: <code>/v1</code> answers as usual.</p>
</main>
</body>
</html>
"""


def enabled():
    value = (os.getenv("THROUGHLINE_WEB") or "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def _loopback(host):
    host = host.lower().rstrip(".")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tile_origin():
    """The opt-in map tile origin, or None. Anything that is not exactly one
    https origin (or an http origin on a loopback host) is ignored, so the
    value can never extend the policy nor let tiles (and the page's view of
    the map) travel in the clear."""
    value = (os.getenv("THROUGHLINE_WEB_TILE_ORIGIN") or "").strip().rstrip("/")
    match = _ORIGIN.match(value)
    if not match:
        return None
    if match.group(1) == "http" and not _loopback(match.group(2)):
        return None
    return value


def csp():
    """Content security policy of every shell response.

    Scripts come only from this origin: no inline script, no eval, no remote
    code. Styles allow inline ones because the editor and chart libraries
    inject style elements at run time. The interface starts no workers
    (worker-src 'none'; frames fall back to default-src 'none').
    connect-src 'self' is what lets the page call /v1 and nothing else.
    """
    extra = tile_origin()
    img = "img-src 'self' data: blob:" + (" " + extra if extra else "")
    connect = "connect-src 'self'" + (" " + extra if extra else "")
    return "; ".join(
        (
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self' 'unsafe-inline'",
            img,
            "font-src 'self'",
            connect,
            "worker-src 'none'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        )
    )


def headers():
    return {
        "Content-Security-Policy": csp(),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Cross-Origin-Opener-Policy": "same-origin",
    }


class Assets(StaticFiles):
    """Hashed build output: the file name changes with the content."""

    async def check_config(self):
        # No build yet is not a configuration error: assets are then 404 and
        # the shell says how to build.
        return None

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(headers())
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _top_level_file(static, path):
    """A regular file directly in the static directory (favicon, manifest),
    never index.html, never anything below or outside it."""
    if not path or "/" in path or "\\" in path or path.startswith("."):
        return None
    if path == "index.html":
        return None
    candidate = static / path
    try:
        if candidate.is_file() and candidate.resolve().parent == static.resolve():
            return candidate
    except OSError:
        return None
    return None


def install(app, static=None):
    """Add the shell routes to the application; False when disabled.

    The build is looked up per request, so building the front end does not
    need a restart; without one the shell is a page that says how to build it.
    """
    if not enabled():
        return False
    configured = (os.getenv("THROUGHLINE_WEB_TILE_ORIGIN") or "").strip()
    if configured and tile_origin() is None:
        log.warning(
            "THROUGHLINE_WEB_TILE_ORIGIN is ignored: it must be exactly one https origin "
            "(http only for a loopback host)"
        )
    static = Path(static) if static is not None else STATIC

    async def shell(request):
        own = {**headers(), "Cache-Control": "no-store"}
        extra = _top_level_file(static, request.path_params.get("path", ""))
        if extra is not None:
            return FileResponse(extra, headers=own)
        index = static / "index.html"
        if index.is_file():
            return FileResponse(index, media_type="text/html", headers=own)
        return HTMLResponse(NOT_BUILT, headers=own)

    async def root(request):
        return RedirectResponse("/ui/", status_code=307)

    app.router.routes.extend(
        [
            Mount(
                "/ui/assets",
                Assets(directory=static / "assets", check_dir=False),
                name="ui-assets",
            ),
            Route("/", root, methods=["GET"], include_in_schema=False),
            Route("/ui", shell, methods=["GET"], include_in_schema=False),
            Route("/ui/{path:path}", shell, methods=["GET"], include_in_schema=False),
        ]
    )
    return True
