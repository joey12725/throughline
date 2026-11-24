"""Screenshots on Windows, macOS and Linux (X11 and Wayland).

Backends, picked in order: grim (Hyprland, Sway and other wlroots
compositors), gnome-screenshot / spectacle (GNOME / KDE Wayland), mss
(Windows, macOS, X11). macOS asks once for the Screen Recording permission.

`monitors`: "active" (the monitor with the focused window, else the primary
one), "primary" or "all" (one image of every screen side by side).
Images are downscaled to `max_px` on the long side and encoded as JPEG or
WebP; `fingerprint` gives a tiny grayscale thumbnail used to skip frames
that have not changed.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile


class CaptureError(RuntimeError):
    pass


def _run_bytes(cmd, timeout=10):
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise CaptureError(f"{cmd[0]}: {type(error).__name__}") from None
    if out.returncode != 0:
        raise CaptureError(f"{cmd[0]} failed: {out.stderr.decode(errors='replace').strip()[:200]}")
    return out.stdout


def backend():
    wayland = bool(os.getenv("WAYLAND_DISPLAY")) and sys.platform.startswith("linux")
    if wayland and shutil.which("grim") and (os.getenv("HYPRLAND_INSTANCE_SIGNATURE") or os.getenv("SWAYSOCK") or os.getenv("XDG_CURRENT_DESKTOP", "").lower() not in ("gnome", "kde")):
        return "grim"
    if wayland and shutil.which("gnome-screenshot"):
        return "gnome-screenshot"
    if wayland and shutil.which("spectacle"):
        return "spectacle"
    try:
        import mss  # noqa: F401
    except ImportError:
        return None
    return "mss"


def _hyprland_focused_output():
    try:
        monitors = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=3).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for monitor in monitors:
        if monitor.get("focused"):
            return monitor.get("name")
    return None


def _sway_focused_output():
    try:
        outputs = json.loads(subprocess.run(["swaymsg", "-t", "get_outputs"], capture_output=True, text=True, timeout=3).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for output in outputs:
        if output.get("focused"):
            return output.get("name")
    return None


def displays_on():
    """False when every display is powered off (DPMS), None when unknown.
    On Hyprland a screencopy then waits for a frame that never comes."""
    if os.getenv("HYPRLAND_INSTANCE_SIGNATURE"):
        try:
            monitors = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=3).stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        return any(m.get("dpmsStatus", True) for m in monitors)
    return None


def grab(monitors="active"):
    """A PIL image of the screen(s), and the monitor name when known."""
    from PIL import Image

    kind = backend()
    if kind is None:
        raise CaptureError("no screenshot backend: install the capture extra (mss, Pillow) or grim")
    if kind == "grim":
        cmd = ["grim", "-t", "png", "-l", "0"]
        name = None
        if monitors in ("active", "primary"):
            name = _hyprland_focused_output() if os.getenv("HYPRLAND_INSTANCE_SIGNATURE") else _sway_focused_output()
            if name:
                cmd += ["-o", name]
        data = _run_bytes(cmd + ["-"], timeout=5)
        return Image.open(io.BytesIO(data)).convert("RGB"), name
    if kind in ("gnome-screenshot", "spectacle"):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shot.png")
            if kind == "gnome-screenshot":
                _run_bytes(["gnome-screenshot", "-f", path])
            else:
                _run_bytes(["spectacle", "-b", "-n", "-f" if monitors == "all" else "-m", "-o", path])
            return Image.open(path).convert("RGB"), None
    import mss

    with mss.mss() as sct:
        index = 0 if monitors == "all" else 1
        if monitors == "active":
            index = _mss_active_monitor(sct) or 1
        shot = sct.grab(sct.monitors[index])
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX"), str(index)


def _mss_active_monitor(sct):
    """The monitor holding the focused window (Windows), else None."""
    if not sys.platform.startswith("win"):
        return None
    import ctypes
    from ctypes import wintypes

    hwnd = ctypes.windll.user32.GetForegroundWindow()
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    for i, mon in enumerate(sct.monitors[1:], start=1):
        if mon["left"] <= cx < mon["left"] + mon["width"] and mon["top"] <= cy < mon["top"] + mon["height"]:
            return i
    return None


def encode(image, max_px=1600, fmt="jpeg", quality=70):
    """(bytes, mime, width, height) of a downscaled image."""
    image = image.copy()
    image.thumbnail((max_px, max_px))
    buffer = io.BytesIO()
    if fmt == "webp":
        image.save(buffer, "WEBP", quality=quality, method=4)
        mime = "image/webp"
    else:
        image.save(buffer, "JPEG", quality=quality, optimize=True, progressive=True)
        mime = "image/jpeg"
    return buffer.getvalue(), mime, image.width, image.height


def fingerprint(image):
    """A 64x36 grayscale thumbnail as bytes."""
    small = image.convert("L").resize((64, 36))
    return small.tobytes()


def changed(previous, current, threshold=2.0):
    """Mean absolute difference (0-255) above threshold."""
    if previous is None or len(previous) != len(current):
        return True
    total = sum(abs(a - b) for a, b in zip(previous, current))
    return total / len(current) > threshold
