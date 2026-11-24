"""Seconds since the last keyboard or mouse input, on every desktop.

Windows: GetLastInputInfo. macOS: IOHIDSystem HIDIdleTime (no permission
needed). Linux Wayland: ext-idle-notify (Hyprland, Sway, KDE, ...), GNOME's
Mutter IdleMonitor over D-Bus. Linux X11: XScreenSaverQueryInfo. If nothing
works, ActivityWatch's AFK watcher when it runs. `None` means unknown.
"""

import ctypes
import ctypes.util
import json
import os
import re
import subprocess
import sys
import time


class IdleMonitor:
    def __init__(self, threshold_s=60):
        self.threshold_s = threshold_s
        self.backend = None
        self._wayland = None
        self._x11 = None
        self._pick()

    def _pick(self):
        if sys.platform.startswith("win"):
            self.backend = "windows"
        elif sys.platform == "darwin":
            self.backend = "macos"
        elif os.getenv("WAYLAND_DISPLAY"):
            from .wayland_idle import WaylandIdle

            watcher = WaylandIdle(timeout_s=self.threshold_s)
            if watcher.start():
                self._wayland = watcher
                self.backend = "wayland"
            elif _gnome_idle_ms() is not None:
                self.backend = "gnome"
            else:
                self.backend = "activitywatch"
        elif os.getenv("DISPLAY") and _x11_idle_ms() is not None:
            self.backend = "x11"
        else:
            self.backend = "activitywatch"

    def idle_seconds(self):
        try:
            if self.backend == "windows":
                return _windows_idle_ms() / 1000
            if self.backend == "macos":
                return _mac_idle_s()
            if self.backend == "wayland":
                w = self._wayland
                if w.idle_since is None:
                    return 0.0
                return time.monotonic() - w.idle_since
            if self.backend == "gnome":
                value = _gnome_idle_ms()
                return None if value is None else value / 1000
            if self.backend == "x11":
                value = _x11_idle_ms()
                return None if value is None else value / 1000
            return _activitywatch_idle_s()
        except Exception:  # noqa: BLE001
            return None

    def active(self):
        """True at the keyboard/mouse, False away, None unknown."""
        seconds = self.idle_seconds()
        if seconds is None:
            return None
        return seconds < self.threshold_s


def _windows_idle_ms():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF


def _mac_idle_s():
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=5).stdout
    match = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(match.group(1)) / 1e9 if match else None


def _gnome_idle_ms():
    try:
        out = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.gnome.Mutter.IdleMonitor",
             "--object-path", "/org/gnome/Mutter/IdleMonitor/Core",
             "--method", "org.gnome.Mutter.IdleMonitor.GetIdletime"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"uint64 (\d+)", out.stdout)
    return int(match.group(1)) if match else None


class _XSSInfo(ctypes.Structure):
    _fields_ = [("window", ctypes.c_ulong), ("state", ctypes.c_int), ("kind", ctypes.c_int),
                ("til_or_since", ctypes.c_ulong), ("idle", ctypes.c_ulong), ("eventMask", ctypes.c_ulong)]


def _x11_idle_ms():
    try:
        xlib = ctypes.cdll.LoadLibrary(ctypes.util.find_library("X11") or "libX11.so.6")
        xss = ctypes.cdll.LoadLibrary(ctypes.util.find_library("Xss") or "libXss.so.1")
    except OSError:
        return None
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    xlib.XDefaultRootWindow.restype = ctypes.c_ulong
    xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XSSInfo)
    xss.XScreenSaverQueryInfo.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XSSInfo)]
    display = xlib.XOpenDisplay(None)
    if not display:
        return None
    try:
        info = xss.XScreenSaverAllocInfo()
        if not xss.XScreenSaverQueryInfo(display, xlib.XDefaultRootWindow(display), info):
            return None
        return int(info.contents.idle)
    finally:
        xlib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        xlib.XCloseDisplay(display)


def _activitywatch_idle_s(url="http://localhost:5600/api/0"):
    import urllib.request

    with urllib.request.urlopen(url + "/buckets/", timeout=2) as response:
        buckets = json.load(response)
    afk = [b for b in buckets if b.startswith("aw-watcher-afk")]
    if not afk:
        return None
    with urllib.request.urlopen(f"{url}/buckets/{afk[0]}/events?limit=1", timeout=2) as response:
        events = json.load(response)
    if not events:
        return None
    return 0.0 if events[0]["data"].get("status") == "not-afk" else 10**6
