"""System tray icon (optional: needs the tray extra, pystray + Pillow).

Without a tray (no extra installed, no display, a Linux desktop without
AppIndicator) the companion keeps running and the dashboard stays reachable
with `throughline-companion open`.
"""

import logging
import webbrowser

log = logging.getLogger("throughline_companion.tray")


def _icon_image():
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, size - 4, size - 4), fill=(37, 99, 235, 255))
    # A thread running through: one line across the circle.
    draw.line((14, 40, 30, 26, 50, 30), fill=(255, 255, 255, 255), width=6, joint="curve")
    return image


def available():
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception:  # noqa: BLE001 - missing extra or no display backend
        return False
    return True


def run(companion, dashboard, on_quit):
    """Show the tray icon; blocks until Quit. Returns False when no tray could
    be shown (the caller then waits headless)."""
    try:
        import pystray
    except Exception:  # noqa: BLE001
        return False

    def open_dashboard(icon=None, item=None):
        webbrowser.open(dashboard.url)

    def sync_now(icon, item):
        for job in companion.runner.snapshot():
            companion.runner.run_now(job["name"])

    def toggle_pause(icon, item):
        companion.runner.paused = not companion.runner.paused

    def quit_app(icon, item):
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", open_dashboard, default=True),
        pystray.MenuItem("Sync everything now", sync_now),
        pystray.MenuItem("Pause", toggle_pause, checked=lambda item: companion.runner.paused),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    )
    try:
        icon = pystray.Icon("throughline-companion", _icon_image(), "Throughline Companion", menu)
        icon.run()
    except Exception as error:  # noqa: BLE001
        log.warning("no tray icon: %s", error)
        return False
    return True
