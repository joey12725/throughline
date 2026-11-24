"""The companion's settings file (companion.json, owner-only permissions).

{
  "server":   {"url", "device", "device_token"},        from the pairing code
  "plugins":  {name: {"enabled", "token", "interval_s", "env", "command"}},
  "jobs":     [{"name", "command", "interval_s", "env", "token"}],   extra commands
  "bridge":   {"enabled", "capabilities": {capability: {"enabled",
               "allowed_plugins", ...capability options}}},
  "activitywatch": {"mode": "off" | "external" | "managed", "url", "watchers"},
  "ui":       {"port"}
}

A plugin env value may be a desktop reference instead of a string:
{"desktop": "cookies", "args": {"domain": "youtube.com"}, "as": "header"}.
It is resolved through the bridge right before each run and never stored
(used by the headless runner on a server; see runner.resolve_env).
"""

import copy
import json
import os
import threading
from pathlib import Path

from . import paths
from .capture.engine import DEFAULTS as CAPTURE_DEFAULTS

DEFAULTS = {
    "version": 1,
    "server": {"url": "", "device": "", "device_token": ""},
    "plugins": {},
    "jobs": [],
    "bridge": {
        "enabled": True,
        "capabilities": {
            "cookies": {
                "enabled": False,
                "allowed_plugins": [],
                "browsers": ["chrome", "firefox", "edge", "brave", "chromium", "safari"],
                "domains": [],
            },
            "file.read": {"enabled": False, "allowed_plugins": [], "roots": []},
            "http.local": {"enabled": False, "allowed_plugins": [], "targets": {}},
            "plugin.run": {"enabled": True, "allowed_plugins": []},
            "status": {"enabled": True, "allowed_plugins": ["*"]},
        },
    },
    "activitywatch": {
        "mode": "external",
        "url": "http://localhost:5600",
        "watchers": [],
    },
    "ui": {"port": 8767},
    # Infisical secret references: optional connection settings (may also come
    # from INFISICAL_* environment variables or "env:NAME" values here).
    "infisical": {
        "site_url": "https://app.infisical.com",
        "client_id": "",
        "client_secret": "",
        "token": "",
        "project_id": "",
        "project_slug": "",
        "environment": "prod",
        "secret_path": "/",
    },
    # Screen and audio capture: off unless switched on (capture/engine.py).
    "capture": CAPTURE_DEFAULTS,
}


def _merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key != "env":
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """companion.json, loaded with defaults and saved atomically."""

    def __init__(self, path=None):
        self.path = Path(path) if path else paths.config_dir() / "companion.json"
        self.lock = threading.RLock()
        self.data = self._load()

    def _load(self):
        if self.path.is_dir():
            # A bind mount of a settings file that does not exist yet shows
            # up as an empty directory: run with defaults (no jobs).
            stored = {}
        elif self.path.exists():
            with open(self.path, encoding="utf-8") as handle:
                stored = json.load(handle)
        else:
            stored = {}
        return _merge(DEFAULTS, stored)

    def reload(self):
        with self.lock:
            self.data = self._load()

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            paths.ensure_private(self.path.parent)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            paths.ensure_private(self.path)

    # -- accessors ---------------------------------------------------------

    @property
    def server(self):
        return self.data["server"]

    @property
    def paired(self):
        server = self.server
        return bool(server.get("url") and server.get("device_token"))

    def plugin(self, name):
        with self.lock:
            return self.data["plugins"].setdefault(
                name, {"enabled": False, "token": "", "interval_s": None, "env": {}}
            )

    def capability(self, name):
        return self.data["bridge"]["capabilities"].get(name)

    def apply_pairing(self, bundle, save=True):
        """Store a decoded pairing bundle: server, device token and the plugin
        tokens (each enrolled plugin is switched on)."""
        if bundle.get("v") != 1 or not bundle.get("device_token"):
            raise ValueError("unsupported pairing code")
        with self.lock:
            self.data["server"] = {
                "url": bundle["url"].rstrip("/"),
                "device": bundle["device"],
                "device_token": bundle["device_token"],
            }
            for name, token in (bundle.get("plugin_tokens") or {}).items():
                entry = self.plugin(name)
                entry["token"] = token
                entry["enabled"] = True
            if save:
                self.save()

    def redacted(self):
        """The settings without token values (for the dashboard)."""
        def _mask(value):
            if not value:
                return ""
            if isinstance(value, str) and (
                value.startswith("infisical:") or value.startswith("env:")
            ):
                return value
            return "set"

        with self.lock:
            data = copy.deepcopy(self.data)
        if data["server"].get("device_token"):
            data["server"]["device_token"] = "set"
        for entry in data["plugins"].values():
            entry["token"] = _mask(entry.get("token"))
        tr = data.get("capture", {}).get("transcription", {})
        if tr.get("api_key"):
            tr["api_key"] = _mask(tr["api_key"])
        inf = data.get("infisical") or {}
        if inf.get("client_secret"):
            inf["client_secret"] = _mask(inf["client_secret"])
        if inf.get("token"):
            inf["token"] = _mask(inf["token"])
        for job in data["jobs"]:
            if job.get("token"):
                job["token"] = _mask(job["token"])
        return data
