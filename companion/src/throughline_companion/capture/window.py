"""The focused window: app, title, and for browsers the page URL when the
title gives it away. Best effort; every field may be None."""

import ctypes
import json
import os
import subprocess
import sys


def _run(cmd, timeout=3):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def active_window():
    """{app, title, pid, monitor, geometry} of the focused window."""
    try:
        if sys.platform.startswith("win"):
            return _windows()
        if sys.platform == "darwin":
            return _mac()
        if os.getenv("HYPRLAND_INSTANCE_SIGNATURE"):
            return _hyprland()
        if os.getenv("SWAYSOCK"):
            return _sway()
        if os.getenv("DISPLAY") and not os.getenv("WAYLAND_DISPLAY"):
            return _x11()
    except Exception:  # noqa: BLE001
        pass
    return {"app": None, "title": None}


def _hyprland():
    data = json.loads(_run(["hyprctl", "activewindow", "-j"]) or "{}")
    if not data:
        return {"app": None, "title": None}
    return {
        "app": data.get("class") or data.get("initialClass"),
        "title": data.get("title"),
        "pid": data.get("pid"),
        "monitor": data.get("monitor"),
    }


def _sway():
    tree = json.loads(_run(["swaymsg", "-t", "get_tree"]) or "{}")
    stack = [tree]
    while stack:
        node = stack.pop()
        if node.get("focused"):
            return {"app": node.get("app_id") or (node.get("window_properties") or {}).get("class"),
                    "title": node.get("name"), "pid": node.get("pid")}
        stack += node.get("nodes", []) + node.get("floating_nodes", [])
    return {"app": None, "title": None}


def _x11():
    out = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"]) or ""
    window = out.strip().split()[-1] if out.strip() else None
    if not window or window == "0x0":
        return {"app": None, "title": None}
    props = _run(["xprop", "-id", window, "WM_CLASS", "_NET_WM_NAME"]) or ""
    app = title = None
    for line in props.splitlines():
        if line.startswith("WM_CLASS"):
            parts = line.split("=", 1)[1].replace('"', "").split(",")
            app = parts[-1].strip() if parts else None
        elif line.startswith("_NET_WM_NAME"):
            title = line.split("=", 1)[1].strip().strip('"')
    return {"app": app, "title": title}


def _windows():
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if handle:
        size = ctypes.c_ulong(1024)
        path = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            app = os.path.basename(path.value)
        kernel32.CloseHandle(handle)
    return {"app": app, "title": buffer.value, "pid": pid.value}


def _mac():
    script = (
        'tell application "System Events" to set p to first application process whose frontmost is true\n'
        'set n to name of p\nset t to ""\n'
        'try\nset t to name of front window of p\nend try\nreturn n & "\\n" & t'
    )
    out = _run(["osascript", "-e", script]) or ""
    app, _, title = out.rstrip("\n").partition("\n")
    return {"app": app or None, "title": title or None}


BROWSERS = ("firefox", "chrome", "chromium", "brave", "edge", "msedge", "safari", "vivaldi", "opera", "librewolf", "zen", "arc")


def is_browser(app):
    app = (app or "").lower()
    return any(name in app for name in BROWSERS)
