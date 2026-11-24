"""Settings the web onboarding writes, kept on the box beside backup.json.

Throughline reads every model credential and setting from environment
variables. Onboarding keeps that model: what the owner enters in the browser
is saved to two owner-only (0600) files under the setup directory and applied
to this process's environment, and handed to plugin processes as their
environment:

* ``settings.json`` – ``{"env": {NAME: value}}`` for server-wide variables
  (provider choices and keys, egress hosts), and
  ``{"plugins": {id: {NAME: value}}}`` for plugin settings;
* ``secrets.json`` – the same shape for secret values only.

A variable already present in the server's real environment when it started
always wins and is reported as *locked*: the browser can see that it is set,
never its value, and cannot change it. Secret values are never returned by
any API; only a masked hint ("••••3f9a").

The directory is ``THROUGHLINE_SETUP_DIR`` or ``~/.throughline/config``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

_lock = threading.RLock()
# The environment as the server started, before any managed value applied.
_ORIGINAL = dict(os.environ)
_applied: set[str] = set()


def setup_dir() -> Path:
    path = Path(os.getenv("THROUGHLINE_SETUP_DIR") or "~/.throughline/config").expanduser()
    return path


def data_dir() -> Path:
    """Where onboarding keeps plugin checkouts, environments and uploads."""
    path = os.getenv("THROUGHLINE_SETUP_DATA")
    if path:
        return Path(path).expanduser()
    return setup_dir().parent / "setup"


def _file(name):
    return setup_dir() / name


def _read(name):
    path = _file(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"env": {}, "plugins": {}}
    except (OSError, ValueError) as error:
        raise RuntimeError(f"{path} is not readable JSON: {error}") from None
    data.setdefault("env", {})
    data.setdefault("plugins", {})
    return data


def _write(name, data):
    directory = setup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + name, suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, directory / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def locked(name: str) -> bool:
    """Set in the server's own environment (not by onboarding)."""
    return name in _ORIGINAL and name not in _applied


def mask(value: str | None) -> str | None:
    if not value:
        return None
    tail = value[-4:] if len(value) >= 12 else ""
    return "••••" + tail


# ───────────── server-wide environment ─────────────


def env_values() -> dict:
    with _lock:
        merged = dict(_read("settings.json")["env"])
        merged.update(_read("secrets.json")["env"])
        return merged


def get_env(name: str) -> str | None:
    """The effective value: the server environment first, then onboarding."""
    if locked(name):
        return _ORIGINAL.get(name)
    return env_values().get(name)


def set_env(values: dict[str, str | None], secret_keys=()):
    """Save server-wide variables (None or "" removes one) and apply them to
    this process. Locked names are refused."""
    refused = [k for k in values if locked(k)]
    if refused:
        raise PermissionError(
            "set in the server environment, change them there: " + ", ".join(sorted(refused))
        )
    secret_keys = set(secret_keys)
    with _lock:
        plain, hidden = _read("settings.json"), _read("secrets.json")
        for key, value in values.items():
            target = hidden if key in secret_keys else plain
            other = plain if key in secret_keys else hidden
            other["env"].pop(key, None)
            if value is None or value == "":
                target["env"].pop(key, None)
            else:
                target["env"][key] = str(value)
        _write("settings.json", plain)
        _write("secrets.json", hidden)
        apply()


def apply():
    """Apply saved server-wide values to os.environ (startup and after each
    save). Values from the real environment are never overwritten."""
    with _lock:
        try:
            values = env_values()
        except RuntimeError:
            return
        for key in list(_applied):
            if key not in values:
                os.environ.pop(key, None)
                _applied.discard(key)
        for key, value in values.items():
            if key in _ORIGINAL and key not in _applied:
                continue
            os.environ[key] = value
            _applied.add(key)


# ───────────── per-plugin values ─────────────


def plugin_values(plugin_id: str, secrets=True) -> dict:
    with _lock:
        values = dict(_read("settings.json")["plugins"].get(plugin_id) or {})
        if secrets:
            values.update(_read("secrets.json")["plugins"].get(plugin_id) or {})
        return values


def set_plugin_values(plugin_id: str, values: dict, secret_keys=()):
    """Merge values for one plugin; None or "" clears a key. A secret sent
    back masked ("••••…") is left unchanged, so forms can round-trip."""
    secret_keys = set(secret_keys)
    with _lock:
        plain, hidden = _read("settings.json"), _read("secrets.json")
        p = plain["plugins"].setdefault(plugin_id, {})
        h = hidden["plugins"].setdefault(plugin_id, {})
        for key, value in values.items():
            if isinstance(value, str) and value.startswith("••••"):
                continue
            target, other = (h, p) if key in secret_keys else (p, h)
            other.pop(key, None)
            if value is None or value == "":
                target.pop(key, None)
            else:
                target[key] = str(value)
        _write("settings.json", plain)
        _write("secrets.json", hidden)


def drop_plugin(plugin_id: str):
    with _lock:
        for name in ("settings.json", "secrets.json"):
            data = _read(name)
            if data["plugins"].pop(plugin_id, None) is not None:
                _write(name, data)


def public_plugin_values(plugin_id: str, secret_keys) -> dict:
    values = plugin_values(plugin_id)
    return {
        key: (mask(value) if key in secret_keys else value) for key, value in values.items()
    }


def secret_values() -> list[str]:
    """Every saved secret, for log redaction."""
    with _lock:
        data = _read("secrets.json")
        out = list(data["env"].values())
        for values in data["plugins"].values():
            out.extend(values.values())
        return [v for v in out if v and len(v) >= 4]


def redact(text: str, extra=()) -> str:
    if not text:
        return text
    for value in sorted({*secret_values(), *[e for e in extra if e]}, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    return text
