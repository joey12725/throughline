"""The dashboard: a local web page served on 127.0.0.1 only.

Every request needs the session key the companion generated at start (in the
URL the tray opens, then a cookie), the Host header must be localhost (no DNS
rebinding) and writes must come from the dashboard's own origin. The key is
also written to <state>/ui-url (owner-only) so `throughline-companion open`
can find the page.
"""

import json
import logging
import re
import secrets
import threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import paths

log = logging.getLogger("throughline_companion.ui")

COOKIE = "tlc_session"
ROUTE_PLUGIN = re.compile(r"^/api/plugins/([a-z][a-z0-9_]*)(?:/(run|manifest|log))?$")
ROUTE_BRIDGE = re.compile(r"^/api/bridge(?:/([a-z][a-z0-9_.]*))?$")


def page():
    return resources.files("throughline_companion").joinpath("static/index.html").read_bytes()


class Dashboard:
    def __init__(self, companion, port=8767, host="127.0.0.1"):
        self.companion = companion
        self.key = secrets.token_urlsafe(24)
        self.host = host
        self.server = self._bind(port)
        self.port = self.server.server_address[1]
        self.thread = None

    def _bind(self, port):
        handler = _handler(self)
        for candidate in (port, 0):
            try:
                return ThreadingHTTPServer((self.host, candidate), handler)
            except OSError:
                continue
        raise RuntimeError("could not open the dashboard port")

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/?key={self.key}"

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, name="ui", daemon=True)
        self.thread.start()
        path = paths.state_dir() / "ui-url"
        path.write_text(self.url, encoding="utf-8")
        paths.ensure_private(path)

    def stop(self):
        self.server.shutdown()
        (paths.state_dir() / "ui-url").unlink(missing_ok=True)


def _handler(dashboard):
    companion = dashboard.companion

    class Handler(BaseHTTPRequestHandler):
        server_version = "ThroughlineCompanion"

        def log_message(self, fmt, *args):  # quiet: no request lines with keys
            log.debug(fmt, *args)

        # -- plumbing ------------------------------------------------------

        def _host_ok(self):
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost", "[::1]")

        def _authorized(self):
            jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
            given = jar[COOKIE].value if COOKIE in jar else self.headers.get("X-Companion-Key")
            return bool(given) and secrets.compare_digest(given, dashboard.key)

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            parsed = urlparse(origin)
            return parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == dashboard.port

        def _send(self, status, body, content_type="application/json", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:",
            )
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("request too large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        # -- routes --------------------------------------------------------

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "bad host"})
            url = urlparse(self.path)
            if url.path == "/":
                key = parse_qs(url.query).get("key", [None])[0]
                if key and secrets.compare_digest(key, dashboard.key):
                    # Trade the key in the URL for a cookie, then drop it from the address bar.
                    return self._send(
                        303,
                        b"",
                        "text/plain",
                        {
                            "Location": "/",
                            "Set-Cookie": f"{COOKIE}={dashboard.key}; HttpOnly; SameSite=Strict; Path=/",
                        },
                    )
                if not self._authorized():
                    return self._send(
                        401,
                        b"Open the dashboard from the Throughline Companion tray icon "
                        b"or run: throughline-companion open",
                        "text/plain; charset=utf-8",
                    )
                return self._send(200, page(), "text/html; charset=utf-8")
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            try:
                if url.path == "/api/state":
                    return self._send(200, companion.state())
                if url.path == "/api/infisical/secrets":
                    qs = parse_qs(url.query)
                    secret_path = qs.get("path", [None])[0]
                    environment = qs.get("environment", [None])[0]
                    recursive = (qs.get("recursive", ["false"])[0] or "").lower() in ("1", "true", "yes")
                    return self._send(
                        200,
                        {"secrets": companion.list_infisical_secrets(secret_path, environment, recursive)},
                    )
                match = ROUTE_PLUGIN.match(url.path)
                if match and match.group(2) == "manifest":
                    return self._send(200, companion.manifest(match.group(1)))
                if match and match.group(2) == "log":
                    return self._send(200, {"log": companion.log_tail(match.group(1))})
            except ValueError as error:
                return self._send(400, {"error": str(error)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not (self._host_ok() and self._origin_ok()):
                return self._send(403, {"error": "bad origin"})
            if not self._authorized():
                return self._send(401, {"error": "unauthorized"})
            path = urlparse(self.path).path
            try:
                data = self._body()
                if path == "/api/pair":
                    return self._send(200, companion.pair(data.get("code") or ""))
                if path == "/api/unpair":
                    companion.unpair()
                    return self._send(200, {"ok": True})
                if path == "/api/pause":
                    companion.runner.paused = bool(data.get("paused"))
                    return self._send(200, {"paused": companion.runner.paused})
                if path == "/api/autostart":
                    return self._send(200, {"autostart": companion.set_autostart(bool(data.get("enabled")))})
                if path == "/api/activitywatch":
                    return self._send(200, companion.update_activitywatch(data))
                if path == "/api/capture":
                    return self._send(200, companion.update_capture(data))
                if path == "/api/capture/local-model":
                    return self._send(200, companion.install_local_model(str(data.get("model") or "small")))
                if path == "/api/capture/local-model/remove":
                    return self._send(200, companion.remove_local_model(data.get("model")))
                if path == "/api/capture/test-screen":
                    shot = companion.capture.capture_screen()
                    return self._send(200, {"captured": bool(shot), "stats": companion.capture.stats})
                if path == "/api/activitywatch/install":
                    return self._send(200, companion.install_activitywatch())
                if path == "/api/infisical":
                    return self._send(200, companion.update_infisical(data))
                if path == "/api/infisical/test":
                    return self._send(200, companion.test_infisical())
                match = ROUTE_PLUGIN.match(path)
                if match and match.group(2) == "run":
                    return self._send(200, companion.runner.run_now(match.group(1)))
                if match and match.group(2) is None:
                    return self._send(200, companion.update_plugin(match.group(1), data))
                match = ROUTE_BRIDGE.match(path)
                if match:
                    return self._send(200, companion.update_bridge(match.group(1), data))
            except KeyError as error:
                return self._send(404, {"error": f"{error} is not running here"})
            except (ValueError, TypeError) as error:
                return self._send(400, {"error": str(error)})
            return self._send(404, {"error": "not found"})

    return Handler
