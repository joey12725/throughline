# PyInstaller spec: one folder with a console build (throughline-companion,
# also used to run connectors) and a windowed build (the tray app); on macOS
# the windowed build is wrapped in "Throughline Companion.app" (menu bar only).
#
#   uv run --extra tray --extra cookies --extra desktop-plugins \
#       --with pyinstaller pyinstaller companion/packaging/companion.spec
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PLUGINS = [
    "throughline_activitywatch",
    "throughline_aider",
    "throughline_apple_notes",
    "throughline_browser_history",
    "throughline_claude_code",
    "throughline_codex",
    "throughline_cursor",
    "throughline_imessage",
    "throughline_lmstudio",
    "throughline_markdown_notes",
    "throughline_t3code",
    "throughline_youtube",
    "throughline_capture",
]

# Test-time SDK modules (the contract kit starts a core and a database).
TEST_ONLY = ("throughline_sdk.contract", "throughline_sdk._server", "throughline_sdk._checks", "throughline_sdk._capture")

hidden = ["throughline_sdk", "yt_dlp.cookies", "pystray", "PIL.Image", "mss", "soundcard", "numpy"]
for package in ["throughline_companion", "throughline_sdk"] + PLUGINS:
    hidden += [m for m in collect_submodules(package) if not m.startswith(TEST_ONLY)]
datas = collect_data_files("throughline_companion")
for package in PLUGINS:
    datas += collect_data_files(package)

block = dict(
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "psycopg", "psycopg_binary", "fastapi", "uvicorn", "throughline", *TEST_ONLY],
)
console_a = Analysis(["entry.py"], **block)
gui_a = Analysis(["entry_gui.py"], **block)
console_pyz = PYZ(console_a.pure)
gui_pyz = PYZ(gui_a.pure)

console_exe = EXE(
    console_pyz,
    console_a.scripts,
    [],
    exclude_binaries=True,
    name="throughline-companion",
    console=True,
)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="Throughline Companion",
    console=False,
)
folder = COLLECT(
    console_exe,
    gui_exe,
    console_a.binaries,
    console_a.datas,
    gui_a.binaries,
    gui_a.datas,
    name="throughline-companion",
)
if sys.platform == "darwin":
    app = BUNDLE(
        folder,
        name="Throughline Companion.app",
        bundle_identifier="net.throughline.companion",
        info_plist={
            "LSUIElement": True,
            "CFBundleShortVersionString": "0.1.0",
            "NSHumanReadableCopyright": "Throughline contributors",
        },
    )
