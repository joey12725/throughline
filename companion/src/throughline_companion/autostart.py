"""Start the companion at login: Windows Run key, macOS LaunchAgent, Linux
XDG autostart entry."""

import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from . import paths

LABEL = "net.throughline.companion"


def command():
    if getattr(sys, "frozen", False):
        return [sys.executable, "run"]
    return [sys.executable, "-m", "throughline_companion", "run"]


def _linux_file():
    import os

    base = Path(os.getenv("XDG_CONFIG_HOME") or paths.home() / ".config")
    return base / "autostart" / "throughline-companion.desktop"


def _mac_file():
    return paths.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def enabled():
    kind = paths.platform()
    if kind == "linux":
        return _linux_file().exists()
    if kind == "macos":
        return _mac_file().exists()
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.QueryValueEx(key, "ThroughlineCompanion")
        return True
    except OSError:
        return False


def enable():
    kind = paths.platform()
    cmd = command()
    if kind == "linux":
        path = _linux_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=Throughline Companion\n"
            f"Exec={shlex.join(cmd)}\nX-GNOME-Autostart-enabled=true\nTerminal=false\n",
            encoding="utf-8",
        )
    elif kind == "macos":
        path = _mac_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            plistlib.dump(
                {
                    "Label": LABEL,
                    "ProgramArguments": cmd,
                    "RunAtLoad": True,
                    "KeepAlive": False,
                    "ProcessType": "Interactive",
                },
                handle,
            )
        subprocess.run(["launchctl", "load", "-w", str(path)], check=False, capture_output=True)
    else:
        import winreg

        value = subprocess.list2cmdline(cmd)
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "ThroughlineCompanion", 0, winreg.REG_SZ, value)
    return True


def disable():
    kind = paths.platform()
    if kind == "linux":
        _linux_file().unlink(missing_ok=True)
    elif kind == "macos":
        path = _mac_file()
        if path.exists():
            subprocess.run(["launchctl", "unload", "-w", str(path)], check=False, capture_output=True)
            path.unlink()
    else:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.DeleteValue(key, "ThroughlineCompanion")
        except OSError:
            pass
    return False
