"""Which connectors belong on a desktop, and how to run them there.

Every connector under plugins/ was classified (docs/companion.md, "What runs
where"). The ones below read something that only exists on the computer the
owner uses: coding-agent logs, browser profiles, local app databases, the
ActivityWatch server. Everything else talks to cloud APIs, LAN services or
push receivers and runs in Docker (the `connectors` service).

Each entry names the plugin (its manifest name), the Python entry point, the
operating systems it supports, the paths that show it is worth enabling on
this computer, and defaults for its settings. The settings a plugin accepts
come from its own manifest (`<entry> manifest`), so they are not repeated
here.
"""

import importlib.util
import os
from pathlib import Path

from . import paths


def _home(*parts):
    return paths.home().joinpath(*parts)


def _appdata(*parts):
    return Path(os.getenv("APPDATA") or _home("AppData", "Roaming")).joinpath(*parts)


def _localappdata(*parts):
    return Path(os.getenv("LOCALAPPDATA") or _home("AppData", "Local")).joinpath(*parts)


def _mac_support(*parts):
    return _home("Library", "Application Support", *parts)


def _xdg_config(*parts):
    return Path(os.getenv("XDG_CONFIG_HOME") or _home(".config")).joinpath(*parts)


ANY = ("windows", "macos", "linux")


def _browser_profiles():
    kind = paths.platform()
    if kind == "windows":
        return [
            _localappdata("Google", "Chrome", "User Data"),
            _localappdata("Microsoft", "Edge", "User Data"),
            _localappdata("BraveSoftware", "Brave-Browser", "User Data"),
            _appdata("Mozilla", "Firefox", "Profiles"),
        ]
    if kind == "macos":
        return [
            _mac_support("Google", "Chrome"),
            _mac_support("Firefox", "Profiles"),
            _mac_support("BraveSoftware", "Brave-Browser"),
            _mac_support("Microsoft Edge"),
            _home("Library", "Safari", "History.db"),
        ]
    return [
        _xdg_config("google-chrome"),
        _xdg_config("chromium"),
        _xdg_config("BraveSoftware", "Brave-Browser"),
        _xdg_config("microsoft-edge"),
        _home(".mozilla", "firefox"),
        _home(".librewolf"),
        _home("snap", "firefox"),
        _home(".var", "app", "org.mozilla.firefox"),
    ]


def _cursor_user():
    kind = paths.platform()
    if kind == "windows":
        return [_appdata("Cursor", "User")]
    if kind == "macos":
        return [_mac_support("Cursor", "User")]
    return [_xdg_config("Cursor", "User")]


CATALOG = [
    dict(
        name="claude_code",
        title="Claude Code",
        summary="Your Claude Code conversations and tool calls (~/.claude/projects).",
        entry="throughline_claude_code:main",
        os=ANY,
        detect=lambda: [Path(os.getenv("CLAUDE_CONFIG_DIR") or _home(".claude")) / "projects"],
    ),
    dict(
        name="codex",
        title="Codex CLI",
        summary="OpenAI Codex CLI sessions (~/.codex/sessions).",
        entry="throughline_codex:main",
        os=ANY,
        detect=lambda: [Path(os.getenv("CODEX_HOME") or _home(".codex")) / "sessions"],
    ),
    dict(
        name="cursor",
        title="Cursor",
        summary="Cursor chat and composer history (state.vscdb).",
        entry="throughline_cursor:main",
        os=ANY,
        detect=_cursor_user,
    ),
    dict(
        name="aider",
        title="Aider",
        summary="Aider chat histories found under folders you choose.",
        entry="throughline_aider:main",
        os=ANY,
        detect=lambda: [],
        required=["AIDER_ROOTS"],
        defaults=lambda: {"AIDER_ROOTS": str(_home("code"))},
    ),
    dict(
        name="t3code",
        title="T3 Code",
        summary="T3 Code threads, projects and AI subscription usage (~/.t3).",
        entry="throughline_t3code:main",
        os=ANY,
        detect=lambda: [_home(".t3", "userdata")],
    ),
    dict(
        name="lmstudio",
        title="LM Studio",
        summary="LM Studio conversations (~/.lmstudio/conversations).",
        entry="throughline_lmstudio:main",
        os=ANY,
        detect=lambda: [
            _home(".lmstudio", "conversations"),
            _home(".cache", "lm-studio", "conversations"),
        ],
    ),
    dict(
        name="browser_history",
        title="Browser history",
        summary="Visits from Chrome, Edge, Brave, Firefox and Safari profiles.",
        entry="throughline_browser_history:main",
        os=ANY,
        detect=_browser_profiles,
    ),
    dict(
        name="activitywatch",
        title="ActivityWatch",
        summary="App, window and web activity from ActivityWatch (bundled or your own).",
        entry="throughline_activitywatch:main",
        os=ANY,
        detect=lambda: [],
        activitywatch=True,
    ),
    dict(
        name="capture",
        title="Screen and audio capture",
        summary=(
            "Uploads what the companion's capture records: screenshots to Immich, "
            "transcripts of speech to Throughline. Switch capture on under Capture."
        ),
        entry="throughline_capture:main",
        os=ANY,
        detect=lambda: [],
        defaults=lambda: {"CAPTURE_SPOOL_DIR": str(paths.data_dir() / "capture" / "spool")},
        secrets_hint=["IMMICH_API_KEY"],
    ),
    dict(
        name="markdown_notes",
        title="Markdown notes",
        summary="A folder of Markdown notes (an Obsidian vault, a notes repo).",
        entry="throughline_markdown_notes:main",
        os=ANY,
        detect=lambda: [],
        required=["MARKDOWN_NOTES_ROOTS"],
    ),
    dict(
        name="apple_notes",
        title="Apple Notes",
        summary="Apple Notes (NoteStore.sqlite). Needs Full Disk Access.",
        entry="throughline_apple_notes:main",
        os=("macos",),
        detect=lambda: [
            _home("Library", "Group Containers", "group.com.apple.notes", "NoteStore.sqlite")
        ],
    ),
    dict(
        name="imessage",
        title="iMessage",
        summary="Messages from ~/Library/Messages/chat.db. Needs Full Disk Access.",
        entry="throughline_imessage:main",
        os=("macos",),
        detect=lambda: [_home("Library", "Messages", "chat.db")],
    ),
    dict(
        name="beeper",
        title="Beeper (desktop API)",
        summary="Chats through the Beeper Desktop API on localhost:23373.",
        entry="throughline_beeper:main",
        os=ANY,
        detect=lambda: [],
        defaults=lambda: {"BEEPER_SOURCE": "live", "BEEPER_BASE_URL": "http://localhost:23373"},
        secrets_hint=["BEEPER_ACCESS_TOKEN"],
    ),
    dict(
        name="youtube",
        title="YouTube (signed-in history)",
        summary=(
            "Watch history, Watch Later and subscriptions read with your browser's "
            "YouTube cookies. On a server, the cookies bridge serves the same cookies."
        ),
        entry="throughline_youtube:main",
        os=ANY,
        detect=lambda: [],
        defaults=lambda: {"YOUTUBE_COOKIES_BROWSER": "chrome"},
        # fetch.py polls into an export directory, then the plugin maps it.
        prepare=dict(
            module="throughline_youtube.fetch",
            args=["poll", "--state", "{state}/state.json", "--out", "{exports}/yt-{stamp}.jsonl.gz"],
            env={"YOUTUBE_WATCHER": "{exports}"},
        ),
    ),
]

# Connectors that can run either place; the companion offers them because
# their files often live on a desktop.
EITHER = {"markdown_notes", "beeper", "youtube"}

BY_NAME = {entry["name"]: entry for entry in CATALOG}


def module_of(entry):
    return entry["entry"].split(":", 1)[0]


def installed(entry):
    """Whether the plugin's package is importable in this environment."""
    try:
        return importlib.util.find_spec(module_of(entry)) is not None
    except (ImportError, ValueError):
        return False


def supported(entry):
    return paths.platform() in entry["os"]


def detected(entry):
    """Paths that exist and suggest this connector has data here."""
    try:
        return [str(p) for p in entry["detect"]() if Path(p).exists()]
    except OSError:
        return []


def describe(entry, config=None):
    """What the dashboard shows for one catalog entry."""
    state = (config.data["plugins"].get(entry["name"]) if config else None) or {}
    return dict(
        name=entry["name"],
        title=entry["title"],
        summary=entry["summary"],
        supported=supported(entry),
        installed=installed(entry),
        detected=detected(entry),
        required=entry.get("required", []),
        enabled=bool(state.get("enabled")),
        has_token=bool(state.get("token")),
        interval_s=state.get("interval_s"),
        env=state.get("env") or {},
    )


def defaults(entry):
    make = entry.get("defaults")
    return dict(make()) if make else {}
