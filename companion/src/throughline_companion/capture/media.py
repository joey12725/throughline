"""Is media playing? Used to pause audio capture and transcription.

Policies (capture.audio.pause_on, any combination):
  spotify     Spotify is playing
  youtube     YouTube is playing (in a browser or an app)
  streaming   a major streaming or music service is playing (Netflix, Prime
              Video, Disney+, Max, Hulu, Apple TV+, Peacock, Paramount+,
              Crunchyroll, Twitch, Plex, Jellyfin, Apple Music, Tidal,
              Deezer, SoundCloud, Pandora, Amazon Music, YouTube, Spotify)
  any_media   anything reports playback

Sources: Linux MPRIS over D-Bus (Spotify, browsers' media sessions with page
URL and title, mpv, VLC, ...); Windows media sessions (SMTC, optional winrt
packages) with the window-title fallback; macOS Spotify and Music through
AppleScript, browsers' front tabs through their window titles, and
`nowplaying-cli` when installed. A source that cannot be read is skipped.
"""

import json
import re
import shutil
import subprocess
import sys

STREAMING = {
    "spotify": ["spotify"],
    "youtube": ["youtube", "youtu.be", "youtube music"],
    "netflix": ["netflix"],
    "prime_video": ["prime video", "primevideo", "amazon.com/gp/video"],
    "disney": ["disney+", "disneyplus"],
    "max": ["hbo max", "max.com", "play.max.com"],
    "hulu": ["hulu"],
    "apple_tv": ["apple tv", "tv.apple.com"],
    "peacock": ["peacock"],
    "paramount": ["paramount+", "paramountplus"],
    "crunchyroll": ["crunchyroll"],
    "twitch": ["twitch"],
    "plex": ["plex"],
    "jellyfin": ["jellyfin"],
    "apple_music": ["music.apple.com", "apple music"],
    "tidal": ["tidal"],
    "deezer": ["deezer"],
    "soundcloud": ["soundcloud"],
    "pandora": ["pandora"],
    "amazon_music": ["music.amazon"],
}


def classify(text):
    """The streaming services a player/title/url text names."""
    text = (text or "").lower()
    return sorted(name for name, needles in STREAMING.items() if any(n in text for n in needles))


def _run(cmd, timeout=3):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _mpris():
    if not shutil.which("busctl"):
        return []
    names = re.findall(r"^(org\.mpris\.MediaPlayer2\.\S+)", _run(["busctl", "--user", "list", "--no-pager", "--no-legend"]) or "", re.M)
    players = []
    for name in names:
        status = _run(["busctl", "--user", "get-property", name, "/org/mpris/MediaPlayer2",
                       "org.mpris.MediaPlayer2.Player", "PlaybackStatus"]) or ""
        if '"Playing"' not in status:
            continue
        identity = _run(["busctl", "--user", "get-property", name, "/org/mpris/MediaPlayer2",
                         "org.mpris.MediaPlayer2", "Identity"]) or ""
        meta = {}
        raw = _run(["busctl", "--user", "--json=short", "get-property", name, "/org/mpris/MediaPlayer2",
                    "org.mpris.MediaPlayer2.Player", "Metadata"])
        if raw:
            try:
                meta = {k: v.get("data") for k, v in json.loads(raw)["data"].items()}
            except (ValueError, KeyError, AttributeError):
                meta = {}
        players.append(
            dict(
                player=name.split("MediaPlayer2.", 1)[1],
                identity=identity.strip().partition(" ")[2].strip('"'),
                title=str(meta.get("xesam:title") or ""),
                url=str(meta.get("xesam:url") or ""),
            )
        )
    return players


def _windows_sessions():
    try:
        from winrt.windows.media.control import (  # type: ignore[import-not-found]
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )
    except ImportError:
        return []
    import asyncio

    async def read():
        manager = await Manager.request_async()
        out = []
        for session in manager.get_sessions():
            info = session.get_playback_info()
            if int(info.playback_status) != 4:  # PLAYING
                continue
            props = await session.try_get_media_properties_async()
            out.append(dict(player=session.source_app_user_model_id or "", identity="",
                            title=props.title or "", url=""))
        return out

    try:
        return asyncio.run(read())
    except Exception:  # noqa: BLE001
        return []


def _mac_players():
    players = []
    for app in ("Spotify", "Music"):
        running = _run(["osascript", "-e", f'application "{app}" is running']) or ""
        if running.strip() != "true":
            continue
        state = _run(["osascript", "-e", f'tell application "{app}" to player state as string']) or ""
        if state.strip() == "playing":
            players.append(dict(player=app.lower(), identity=app, title="", url=""))
    if shutil.which("nowplaying-cli"):
        rate = (_run(["nowplaying-cli", "get", "playbackRate"]) or "").strip()
        if rate not in ("", "0", "null"):
            title = (_run(["nowplaying-cli", "get", "title"]) or "").strip()
            players.append(dict(player="nowplaying", identity="", title=title, url=""))
    return players


def playing():
    """Players that report playback now: [{player, identity, title, url, services}]."""
    if sys.platform.startswith("linux"):
        found = _mpris()
    elif sys.platform.startswith("win"):
        found = _windows_sessions()
    elif sys.platform == "darwin":
        found = _mac_players()
    else:
        found = []
    for item in found:
        item["services"] = classify(" ".join([item["player"], item["identity"], item["title"], item["url"]]))
    return found


def should_pause(policy, players=None, window=None):
    """(pause?, reason) for a pause_on policy list.

    The focused window's title also counts for browsers (a YouTube tab in a
    window that does not publish a media session).
    """
    policy = set(policy or [])
    if not policy:
        return False, None
    players = playing() if players is None else players
    if "any_media" in policy and players:
        return True, "media playing: " + (players[0]["identity"] or players[0]["player"])
    names = set()
    for item in players:
        names.update(item.get("services") or [])
    if window and window.get("title"):
        from .window import is_browser

        title_services = classify(window["title"])
        # A focused browser tab is only a hint; trust it for video sites.
        if is_browser(window.get("app")) and players:
            names.update(title_services)
    if "spotify" in policy and "spotify" in names:
        return True, "Spotify is playing"
    if "youtube" in policy and "youtube" in names:
        return True, "YouTube is playing"
    if "streaming" in policy and names:
        return True, "streaming: " + ", ".join(sorted(names))
    return False, None
