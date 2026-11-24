# Screen and audio capture

The [Companion](companion.md) can keep a record of your screen and of speech
around you, like Screenpipe, and feed it into Throughline's timeline and
search. It is entirely optional: nothing is captured, installed or
downloaded until you switch it on, and each part (screenshots, audio,
transcription engine, local model) is switched on separately.

Where things are stored:

| What | Where | Kept on the desktop? |
|---|---|---|
| Screenshots | your **Immich** library (album *Source · Screen capture*, kind `screenshot`), linked to a `capture.screens` record | no: the spool is emptied after upload |
| Transcripts | `capture.transcripts` records in Throughline, **embedded** like messages | no |
| Audio | nowhere: each speech segment is deleted once transcribed | only until transcribed |

## Screenshots

- One screenshot every `interval_s` (default 60 s), **only while you are at
  the keyboard or mouse** (idle after `idle_after_s`, default 60 s), never
  while every display is asleep.
- Unchanged screens are skipped (a tiny grayscale fingerprint is compared
  with the last kept frame), images are downscaled to 1600 px and saved as
  JPEG quality 70: about 100 to 250 KB each, so roughly 50 to 120 MB per
  8-hour day at one a minute, less when the screen is still.
- The focused app and window title go with each screenshot. Windows of
  password managers and private/incognito browser windows are never captured
  (`exclude_apps`, `exclude_titles`; plain text or `/regex/`).
- `monitors`: `active` (the monitor with the focused window), `primary` or
  `all`.

| OS | Screenshot | "At the keyboard" | Focused window |
|---|---|---|---|
| Windows | mss | GetLastInputInfo | Win32 |
| macOS | mss (Screen Recording permission) | HIDIdleTime | System Events (Accessibility permission) |
| Linux, Hyprland / Sway / wlroots | grim | ext-idle-notify (built in, no dependency) | hyprctl / swaymsg |
| Linux, KDE Wayland | spectacle | ext-idle-notify | - |
| Linux, GNOME Wayland | gnome-screenshot | Mutter IdleMonitor | - |
| Linux X11 | mss | XScreenSaver | xprop |

Without any idle source the Companion falls back to ActivityWatch's AFK
watcher.

In Throughline, `capture.screens` is a life event per screenshot (content
`Screen: <app> — <window title>`, fields `app`, `window_title`, `device`,
`immich_asset_id`, ...). It takes your location from the track, threads are
one per device per day, and the timeline groups screenshots less than 5
minutes apart into one episode. The channel is **not embedded** by default:
a window title a minute is noise for text search. The images themselves are
searchable through Immich's OCR and the image vectors (`images[]` on each
record, `/v1/assets/search`). The image pipeline's `capture` source adopts
each upload as a `screenshot` linked to its record, and skips these files
when it scans Immich for camera-roll photos. Turn embedding of the records
on with `throughline embed channels` if you want it.

## Audio and transcription

Off by default. Sources: the microphone, and what the computer plays
(Windows and Linux loopback; on macOS pick a loopback device such as
BlackHole as the microphone). Needs the `audio` extra (`soundcard`,
`numpy`).

A simple voice-activity detector cuts speech into segments (1 to 30 s,
ending after 1.2 s of quiet). Silence is never written or sent. Each segment
is transcribed and deleted.

**Pause while media plays** (`audio.pause_on`): `spotify`, `youtube`,
`streaming` (Netflix, Prime Video, Disney+, Max, Hulu, Apple TV+, Peacock,
Paramount+, Crunchyroll, Twitch, Plex, Jellyfin, Apple Music, Tidal, Deezer,
SoundCloud, Pandora, Amazon Music, YouTube, Spotify) or `any_media`. The
default is the first three. Playback is read from MPRIS on Linux (Spotify,
browsers' media sessions with page URLs, mpv, VLC), Windows media sessions
(optional `winrt-Windows.Media.Control` packages), and on macOS from Spotify
and Music via AppleScript plus `nowplaying-cli` when installed.

**Engines.** Only real speech-to-text engines (meeting-note services such as
Wave or Granola are separate data sources):

| Engine | Default model | Rough cost |
|---|---|---|
| `groq` (default) | whisper-large-v3-turbo | ~$0.04 per hour of speech |
| `openai` | gpt-4o-mini-transcribe | ~$0.18 per hour |
| `deepgram` | nova-3 | ~$0.26 per hour |
| `custom` | any OpenAI-compatible `/audio/transcriptions` server (speaches, a whisper.cpp server, Together, Fireworks) | yours |
| `local` | faster-whisper on this computer | free, private |

**Local model, downloaded on request.** The dashboard's *Download* (or
`throughline-companion capture install-local small`) creates a private
Python environment in the Companion's data folder (with `uv`, downloaded
and checksum-checked if it is not installed), installs faster-whisper, and
downloads the model you picked: tiny 75 MB, base 145 MB, small 485 MB,
medium 1.5 GB, large-v3-turbo 1.6 GB, distil-large-v3 1.5 GB (`.en`
variants too). The runtime takes about 0.9 GB on top of the model. *Remove*
deletes a model or everything. It uses the NVIDIA GPU when one is present,
else the CPU (int8).

Transcripts become `capture.transcripts` records: one per segment, with
`source` (mic or system), `duration_s`, `language`, `engine`, `model` and
timed `segments`, threaded per device per day, grouped by the timeline into
conversations (10 minutes of quiet ends one), and embedded for meaning
search.

## Setting it up

1. On the server, enroll the desktop with the uploader connector:
   `throughline lib companion enroll laptop --url ... --plugin capture`
   (or `add-plugin laptop capture`).
2. In Immich, create an API key with only the **asset.upload** permission.
   In the Companion, open *Connectors → Screen and audio capture* and set
   `IMMICH_URL` and `IMMICH_API_KEY`.
3. Open *Capture*, switch it on, then switch on screenshots and/or audio.
   *Take a test screenshot* shows what would be captured or why not.

CLI:

```sh
throughline-companion capture set enabled=true screens.enabled=true screens.interval_s=60
throughline-companion capture set audio.enabled=true 'audio.pause_on=["spotify","youtube","streaming"]' transcription.engine=groq transcription.api_key=...
throughline-companion capture install-local small
throughline-companion capture status
```

The local spool is capped (`spool_max_mb`, default 2 GB): if the uploader
cannot reach the server for that long, capture pauses rather than filling
the disk.
