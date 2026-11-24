"""Run the companion as a background service, no GUI needed.

  Linux    a systemd user unit (throughline-companion.service). It joins the
           graphical session when there is one (so screen capture and the
           idle detector can reach Wayland/X11 and D-Bus) and runs without
           one too. `--linger` keeps it running with nobody logged in.
  macOS    a LaunchAgent. Loaded into the console user's GUI session when
           someone is logged in at the screen, else into the background user
           domain (connectors run; screen capture needs a GUI session and the
           Screen Recording permission, which only the person at the Mac can
           grant).
  Windows  a scheduled task at logon (interactive, least privilege), started
           right away when the user is logged on. Over SSH it still starts in
           the user's desktop session, not the SSH session.

Every service runs `throughline-companion run --headless`: no tray icon and
no browser; the dashboard is still served on 127.0.0.1 for an SSH tunnel.
"""

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import paths

UNIT = "throughline-companion.service"
LABEL = "net.throughline.companion"
TASK = "Throughline Companion"


def command(extra=()):
    if getattr(sys, "frozen", False):
        base = [sys.executable]
    else:
        exe = shutil.which("throughline-companion")
        base = [exe] if exe else [sys.executable, "-m", "throughline_companion"]
    return base + ["run", "--headless", *extra]


def _run(cmd, check=False):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=check)
    except FileNotFoundError:
        if check:
            raise RuntimeError(f"{cmd[0]} is not available on this computer") from None
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")


# -- Linux ------------------------------------------------------------------


def _unit_path():
    base = Path(os.getenv("XDG_CONFIG_HOME") or paths.home() / ".config")
    return base / "systemd" / "user" / UNIT


def _unit(cmd):
    log = paths.logs_dir() / "companion.log"
    return (
        "[Unit]\n"
        "Description=Throughline Companion (desktop connectors, capture, bridge)\n"
        "After=network-online.target graphical-session.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={shlex.join(cmd)}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n"
        f"StandardOutput=append:{log}\n"
        f"StandardError=append:{log}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemd_running():
    return Path("/run/systemd/system").is_dir()


def _linux_install(cmd, linger):
    if not shutil.which("systemctl") or not _systemd_running():
        raise RuntimeError(
            "no systemd here: run `throughline-companion run --headless` from your own init "
            "system (or a container's), or use autostart on a desktop"
        )
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_unit(cmd), encoding="utf-8")
    notes = []
    if linger:
        result = _run(["loginctl", "enable-linger", os.getenv("USER") or ""])
        notes.append("linger on" if result.returncode == 0 else "could not enable linger: " + result.stderr.strip())
    _run(["systemctl", "--user", "daemon-reload"])
    result = _run(["systemctl", "--user", "enable", UNIT])
    if result.returncode == 0:
        # restart, not start: after an update the old version may be running.
        result = _run(["systemctl", "--user", "restart", UNIT])
    if result.returncode != 0:
        raise RuntimeError(
            "systemctl --user failed: " + result.stderr.strip()
            + " (over SSH, log in once with a user session or use --linger)"
        )
    env = _run(["systemctl", "--user", "show-environment"]).stdout
    if "WAYLAND_DISPLAY" not in env and "DISPLAY=" not in env:
        notes.append(
            "no graphical session is visible to systemd yet: connectors run now; screen capture "
            "starts when the desktop session imports its environment (most desktops do at login)"
        )
    return notes


def _linux_uninstall():
    _run(["systemctl", "--user", "disable", "--now", UNIT])
    _unit_path().unlink(missing_ok=True)
    _run(["systemctl", "--user", "daemon-reload"])


def _linux_status():
    if not shutil.which("systemctl") or not _systemd_running():
        return dict(installed=_unit_path().exists(), state="no systemd")
    out = _run(["systemctl", "--user", "is-active", UNIT]).stdout.strip()
    return dict(installed=_unit_path().exists(), state=out or "unknown")


# -- macOS ------------------------------------------------------------------


def _plist_path():
    return paths.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _mac_domain():
    uid = os.getuid()
    console = _run(["stat", "-f", "%Su", "/dev/console"]).stdout.strip()
    me = _run(["id", "-un"]).stdout.strip()
    return f"gui/{uid}" if console == me else f"user/{uid}"


def _mac_install(cmd):
    path = _plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log = str(paths.logs_dir() / "companion.log")
    with open(path, "wb") as handle:
        plistlib.dump(
            {
                "Label": LABEL,
                "ProgramArguments": cmd,
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ProcessType": "Background",
                "StandardOutPath": log,
                "StandardErrorPath": log,
            },
            handle,
        )
    domain = _mac_domain()
    _run(["launchctl", "bootout", f"{domain}/{LABEL}"])
    result = _run(["launchctl", "bootstrap", domain, str(path)])
    if result.returncode != 0:
        raise RuntimeError("launchctl bootstrap failed: " + result.stderr.strip())
    notes = [f"loaded into {domain}"]
    if domain.startswith("user/"):
        notes.append("nobody is logged in at the screen: connectors run, screen capture waits for a GUI login")
    notes.append(
        "screen capture and iMessage/Notes need permissions only the person at the Mac can grant "
        "(System Settings > Privacy & Security)"
    )
    return notes


def _mac_uninstall():
    _run(["launchctl", "bootout", f"{_mac_domain()}/{LABEL}"])
    _plist_path().unlink(missing_ok=True)


def _mac_status():
    out = _run(["launchctl", "print", f"{_mac_domain()}/{LABEL}"])
    state = "running" if "state = running" in out.stdout else ("loaded" if out.returncode == 0 else "not loaded")
    return dict(installed=_plist_path().exists(), state=state)


# -- Windows ----------------------------------------------------------------


def _win_install(cmd):
    user = os.getenv("USERDOMAIN", "") + "\\" + os.getenv("USERNAME", "")
    value = subprocess.list2cmdline(cmd)
    result = _run(["schtasks", "/Create", "/F", "/TN", TASK, "/SC", "ONLOGON", "/RU", user,
                   "/IT", "/RL", "LIMITED", "/TR", value])
    if result.returncode != 0:
        raise RuntimeError("schtasks /Create failed: " + (result.stderr or result.stdout).strip())
    # An update replaces the program: end the running copy so the new one starts.
    _run(["schtasks", "/End", "/TN", TASK])
    started = _run(["schtasks", "/Run", "/TN", TASK])
    notes = []
    if started.returncode != 0:
        notes.append("installed; it starts at the next logon (no interactive session now)")
    return notes


def _win_uninstall():
    _run(["schtasks", "/End", "/TN", TASK])
    _run(["schtasks", "/Delete", "/F", "/TN", TASK])


def _win_status():
    out = _run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"])
    state = "unknown"
    for line in out.stdout.splitlines():
        if line.strip().lower().startswith("status:"):
            state = line.split(":", 1)[1].strip().lower()
    return dict(installed=out.returncode == 0, state=state if out.returncode == 0 else "not installed")


# -- public -----------------------------------------------------------------


def install(*, linger=False, extra=()):
    """Install and start the service; returns notes for the person installing."""
    cmd = command(extra)
    kind = paths.platform()
    if kind == "linux":
        return _linux_install(cmd, linger)
    if kind == "macos":
        return _mac_install(cmd)
    return _win_install(cmd)


def uninstall():
    kind = paths.platform()
    if kind == "linux":
        _linux_uninstall()
    elif kind == "macos":
        _mac_uninstall()
    else:
        _win_uninstall()


def status():
    kind = paths.platform()
    if kind == "linux":
        return _linux_status()
    if kind == "macos":
        return _mac_status()
    return _win_status()


def restart():
    kind = paths.platform()
    if kind == "linux":
        _run(["systemctl", "--user", "restart", UNIT], check=True)
    elif kind == "macos":
        _run(["launchctl", "kickstart", "-k", f"{_mac_domain()}/{LABEL}"], check=True)
    else:
        _run(["schtasks", "/End", "/TN", TASK])
        _run(["schtasks", "/Run", "/TN", TASK], check=True)
