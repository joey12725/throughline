"""Per-user directories on Windows, macOS and Linux (no third-party deps)."""

import os
import sys
from pathlib import Path

APP = "throughline-companion"


def platform():
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def home():
    return Path(os.path.expanduser("~"))


def config_dir():
    override = os.getenv("THROUGHLINE_COMPANION_HOME")
    if override:
        return Path(override)
    kind = platform()
    if kind == "windows":
        return Path(os.getenv("APPDATA") or home() / "AppData" / "Roaming") / "Throughline Companion"
    if kind == "macos":
        return home() / "Library" / "Application Support" / "Throughline Companion"
    return Path(os.getenv("XDG_CONFIG_HOME") or home() / ".config") / APP


def data_dir():
    override = os.getenv("THROUGHLINE_COMPANION_HOME")
    if override:
        return Path(override) / "data"
    kind = platform()
    if kind == "windows":
        return Path(os.getenv("LOCALAPPDATA") or home() / "AppData" / "Local") / "Throughline Companion"
    if kind == "macos":
        return config_dir()
    return Path(os.getenv("XDG_DATA_HOME") or home() / ".local" / "share") / APP


def logs_dir():
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_dir():
    path = data_dir() / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_private(path):
    """Owner-only permissions where the OS supports them (POSIX)."""
    try:
        os.chmod(path, 0o700 if Path(path).is_dir() else 0o600)
    except OSError:
        pass
