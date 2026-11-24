# Throughline Companion: the desktop half of an install

A Throughline install has two halves:

- **The server**, all in Docker: PostgreSQL, the API, the connectors that
  talk to cloud APIs and LAN services, and the maintenance jobs
  (`docker-compose.yml`).
- **The Companion**, a desktop app for Windows, macOS and Linux, on every
  computer you use. It runs the connectors that must read that computer,
  can install and supervise ActivityWatch, and answers *bridge* requests
  from connectors on the server that need something only the desktop has
  (signed-in browser cookies, a file in a folder you share, a localhost
  app).

The Companion only makes outbound HTTPS calls to your server, so a laptop
behind NAT, a VPN or a hotel network works without opening any port.

```
 desktop (Companion)                          server (Docker)
 ┌──────────────────────────┐    HTTPS     ┌──────────────────────────────┐
 │ desktop connectors ──────┼── ingest ──▶ │ throughline API ── postgres  │
 │ ActivityWatch (bundled)  │              │   ▲          ▲               │
 │ bridge ◀── long-poll ────┼──────────────┼───┘          │               │
 │  cookies / files / local │              │ connectors ──┘ (bridge call) │
 └──────────────────────────┘              │ maintenance                  │
                                           └──────────────────────────────┘
```

## What runs where

Every connector under `plugins/` was checked for what it reads.

**Desktop: run them in the Companion** (they read files, databases or local
servers that exist only on the computer you use):

| Connector | Reads | OS |
|---|---|---|
| `claude_code` | `~/.claude/projects/**/*.jsonl` | any |
| `codex` | `~/.codex/sessions`, `archived_sessions` | any |
| `cursor` | Cursor `User/**/state.vscdb` | any |
| `aider` | `.aider.chat.history.md` below folders you choose | any |
| `t3code` | `~/.t3/userdata/statev2.sqlite`, provider logs | any |
| `lmstudio` | `~/.lmstudio/conversations` | any |
| `browser_history` | Chrome, Edge, Brave, Vivaldi, Firefox, LibreWolf, Safari profiles | any (Safari: macOS) |
| `activitywatch` | ActivityWatch at `localhost:5600` (bundled or your own) | any |
| `markdown_notes` | a notes folder (Obsidian vault, repo) | any |
| `apple_notes` | `NoteStore.sqlite` (Full Disk Access) | macOS |
| `imessage` | `~/Library/Messages/chat.db` (Full Disk Access) | macOS |
| `beeper` (live) | Beeper Desktop API at `localhost:23373` | any |
| `capture` | the Companion's own capture spool ([capture.md](capture.md)) | any |
| `youtube` (signed-in history) | your browser's YouTube cookies | any |

**Either place** (run them where the files or daemon are): `android_sms`
(a synced backup folder), `audio_transcripts` (watched folders + ffmpeg),
`signal` (a signal-cli daemon), `granola` (API key anywhere; the cache mode
is macOS).

**Server: run them in Docker** (`connectors` profile): bluesky, caldav,
calendar, consumption, email, gbrain, github, goodreads, health, hermes,
homeassistant, imap, instagram_feed, jellyfin, lastfm, letterboxd,
listenbrainz, mastodon, msgraph, notion, openwebui, plex, qbittorrent,
readwise, reddit, research, simplefin, spotify, telegram, trakt, wave,
youtube_feed, zoom, and the push receivers `gps_push` and `apple_health`
(behind your HTTPS proxy). A server connector that needs a desktop secret
(YouTube cookies, Instagram cookies, Beeper's `yt-dlp` cookies) asks the
Companion for it through the bridge instead of keeping a copy.

Optional **screen and audio capture** (screenshots to Immich, transcripts
of speech to Throughline, local or cloud transcription) is described in
[capture.md](capture.md).

## Install without a GUI (SSH, unattended)

The dashboard and tray are optional. Everything they do has a command, and
a service needs neither:

- `throughline-companion run --headless`: no tray, no browser; connectors,
  capture, ActivityWatch and the bridge all run. The dashboard is still
  served on 127.0.0.1: `throughline-companion open` prints its URL and the
  `ssh -L` line to reach it from another computer. `--no-dashboard` turns it
  off. Without a display, `run` behaves like `--headless` on its own.
- `throughline-companion service install [--linger]`: a systemd user unit on
  Linux (`--linger` keeps it running with nobody logged in), a LaunchAgent on
  macOS (in the GUI session when someone is at the screen, else the
  background domain), a logon task on Windows that runs in the user's desktop
  session even when installed over SSH. `service status|restart|logs|uninstall`.
- `throughline-companion setup ...`: everything in one non-interactive step:
  `--pair -` (code from stdin), `--enable a,b`, `--detected` (every
  connector that has a token and found data here), `--set
  plugin:KEY=VALUE`, `--activitywatch managed`, `--capture-screens 60`,
  `--service [--linger]`.

**Remote install over SSH**, from a computer with a Throughline checkout:

```sh
throughline lib companion enroll laptop --url https://tl.example --plugin claude_code --plugin capture --json \
  | jq -r .pairing \
  | throughline-companion deploy alex@laptop --pair - -- --detected --capture-screens 60 --service
```

`deploy` builds wheels of the companion, the SDK and the desktop
connectors, copies them with the installer to
`~/throughline-companion-install` (scp), and runs the installer there. The
installer needs only `sh` and curl or wget (PowerShell on Windows,
`--windows`): it installs uv if missing (pinned version, SHA-256 checked),
installs the companion as a uv tool and runs `setup`. The pairing code
travels on the SSH connection's stdin, never on a command line. The
installer also works alone: `sh install.sh --from <wheels|checkout|requirement>
-- <setup arguments>`.

What only the person at the computer can do: grant macOS privacy
permissions (Screen Recording, Accessibility, Full Disk Access) and, on
Linux, start the graphical session that screen capture needs (connectors run
without it; capture starts once the session's environment reaches systemd,
which most desktops do at login).

## Set up a desktop

1. **Enroll the desktop on the server.** Tokens are only created on the box:

   ```sh
   docker compose run --rm migrate throughline lib companion enroll laptop \
       --url https://throughline.example.com \
       --plugin claude_code --plugin codex --plugin browser_history \
       --plugin activitywatch
   ```

   It prints a pairing code (`tlc1.…`) once. It holds the device token and
   one plugin token per connector: treat it like a password. Add a connector
   later with `throughline lib companion add-plugin laptop cursor`.

2. **Install and start the Companion.** Download the build for your OS (the
   `Companion app` workflow builds Windows, macOS and Linux), or run it from
   the repository with `uv run throughline-companion run`. The dashboard
   opens in your browser on first start.

3. **Paste the pairing code** in the dashboard (or
   `throughline-companion pair tlc1.…`). The enrolled connectors switch on
   with their tokens. The dashboard shows which connectors found data on this
   computer (`throughline-companion detect` does the same), their settings
   (read from each connector's own manifest), last run, next run, errors and
   logs.

4. **Optional:** switch on *Start at login*, choose *Bundled* ActivityWatch,
   and switch on the bridge capabilities you want to share.

`throughline lib companion devices` on the server lists every device, whether
it is online, what it offers to whom, and its connectors' states.
`throughline lib companion revoke laptop` revokes the device and every token
issued for it.

## The bridge

A connector running on the server asks a desktop with
`POST /v1/companion/call` (SDK: `self.desktop("cookies", {"domain":
"youtube.com"})` inside `records()`, or `Client.desktop`). The core picks an
online device that offers that capability *to that plugin*, queues the
request, the device claims it on its long-poll, runs it locally and posts the
answer, and the core hands it to the caller and clears it.

Capabilities (all off until you switch them on in the Companion and name the
connectors allowed to use them; `*` means any connector):

| Capability | Answers | Limits |
|---|---|---|
| `cookies` | `{header, text, count, browser}`: the cookies of one site from a local browser, as a Cookie header and as Netscape `cookies.txt` | only sites you list (subdomains included); only browsers you tick; decrypted with yt-dlp's cookie reader (Chrome, Edge, Brave, Chromium, Opera, Vivaldi, Firefox, Safari) |
| `file.read` / `file.list` | one file (text, or base64) / one folder listing | only below folders you share; symlinks resolved first; 900 kB per file |
| `http.local` | one request to a named localhost service | only names you define (e.g. `beeper → http://localhost:23373`); localhost only; GET/POST/PUT |
| `plugin.run` | starts one of this desktop's connectors now | connectors enabled on this desktop |
| `status` | what the Companion runs and offers | no data |

Security properties:

- **Two allow-lists.** The device declares, every minute, each capability
  and the plugins allowed to call it; the core refuses any other caller
  (`403 not_offered`) before the request reaches the desktop, and the
  Companion checks its own settings again when it runs the request.
- **Narrow tokens.** The device token is a plugin token scoped to
  `companion_<device>`: it can only report its own state and serve its own
  queue. It cannot call the bridge, ingest, or read anything. Each desktop
  connector has its own plugin token.
- **Nothing kept.** Request arguments and answers live in
  `tl.companion_requests` only until collected (then cleared), unanswered
  requests expire, and finished rows are deleted after 7 days. The audit
  log records the capability, device and outcome, never arguments or
  answers.
- **Server-side secrets by reference.** In the Docker runner (and on desktops), a connector
  setting can reference a secret in [Infisical](infisical.md) (`"infisical:SECRET_NAME"`,
  `"infisical:/path/SECRET_NAME"`, or `{"infisical": "SECRET_NAME", "as": "file"}`) or a
  desktop bridge capability, resolved right before each run and passed only to that run
  (a value in its environment, or an owner-only temp file deleted afterwards):

  ```json
  "GITHUB_TOKEN": "infisical:/connectors/github/GITHUB_TOKEN",
  "YOUTUBE_COOKIE_HEADER": {"desktop": "cookies", "args": {"domain": "youtube.com"}, "as": "header"},
  "INSTAGRAM_FEED_COOKIES_FILE": {"desktop": "cookies", "args": {"domain": "instagram.com"}, "as": "file"}
  ```

  Infisical credentials (`INFISICAL_*`) are stripped from plugin environments so each connector
  receives only the secrets it links. Desktop bridge calls use that connector's own token, so
  the desktop's allow-list applies.

Errors a caller sees: `404 no_device`, `403 not_offered`, `503 offline` (no
poll for 90 s), `504 timeout`, `502` with the desktop's refusal
(`failed`).

## ActivityWatch

The *ActivityWatch* section has three modes:

- **Use my own** (default): the connector reads `http://localhost:5600`.
- **Bundled**: the Companion downloads the official ActivityWatch
  release for your OS (pinned version `v0.14.3`, SHA-256 checked against
  the release digests), starts `aw-server-rust` and the watchers
  (`aw-watcher-afk` + `aw-watcher-window`, or `aw-awatcher` under Linux
  Wayland), restarts them if they exit, and never starts ActivityWatch's own
  tray app. On macOS, allow the window watcher under System Settings →
  Privacy & Security → Accessibility. Linux on ARM has no classic build:
  install ActivityWatch yourself and use *Use my own*.
- **Off.**

CLI: `throughline-companion activitywatch install`, `… activitywatch mode
managed`.

## Server connectors and maintenance in Docker

The `connectors` and `maintenance` compose services use the Companion's
scheduler in server mode (`throughline-companion run --server`): no
tray, no dashboard, no capture. Each connector gets only a minimal
environment plus its own settings (secrets by name with `"env:NAME"`);
maintenance commands get the service's whole environment, because they need
the database and model settings it was given.

```sh
# 1. Settings: start from the example; secrets go in connectors.env.
cp docker/connectors.example.json docker/connectors.json
echo 'GITHUB_TOKEN=...' >> connectors.env

# 2. Turn the service on (adds "connectors" to COMPOSE_PROFILES in .env).
scripts/install.sh --connectors
```

There is no pairing code to copy: on every start the one-shot `migrate`
service runs `throughline lib companion provision docker`, which keeps a
device named `docker` enrolled with one token per enabled connector of
`docker/connectors.json`. It adds tokens for newly enabled connectors,
revokes those of connectors no longer enabled, and enrolls afresh when the
device was revoked. The pairing code is kept in the `connectors-pairing`
volume (0600), which only `migrate` writes and only `connectors` reads. After
editing `docker/connectors.json`, run `scripts/install.sh` again. Set `"location_source": true` on a connector that
sends track points (its token may then write the location track). A connector
given its own `"token"` in the file is left alone, and a
`THROUGHLINE_COMPANION_PAIRING` code in `.env` still takes precedence.

`maintenance` runs the core jobs on a schedule instead of host timers: the
embedding worker (`embed run --until-idle`, which exits quietly while no
embedding provider is configured), lenses, curation,
source alerts and backups. Classifier ticks are not among the defaults: user
classifier code would run beside the superuser password, and the image has
no bubblewrap sandbox; add `throughline lib classifiers tick --json` to
`docker/maintenance.json` only for classifiers you trust. `scripts/install.sh` turns it on. It reads
`docker/maintenance.json`, or the built-in `docker/maintenance.example.json`
while that file does not exist; copy the example to change schedules. Its
jobs read `throughline.env` (model and embedding keys) and, like `migrate`,
it holds the superuser password.

Desktops must reach the API: put port 8000 behind your HTTPS reverse proxy
or VPN, or publish it with `THROUGHLINE_BIND=0.0.0.0` on a trusted network.

## Building the app

`.github/workflows/companion.yml` builds and smoke-tests the app on Windows,
macOS (arm64, x86_64) and Linux with PyInstaller
(`companion/packaging/companion.spec`). Each build has a console program
(`throughline-companion`, also used to run connectors, so no Python is needed
on the desktop) and a windowed tray app; on macOS the tray app is
`Throughline Companion.app`, a menu-bar-only app. The builds are unsigned:
Windows SmartScreen and macOS Gatekeeper ask once (macOS: right-click →
Open). Code signing and notarization need your own certificates.

Run from source on any OS:

```sh
uv sync --all-packages --extra tray --extra cookies
uv run throughline-companion run
```

Updating keeps the pairing and settings: install the new build over the old
one, or rerun the installer (it restarts the service on the new version).
See [updating.md](updating.md#desktops-throughline-companion).

Settings live in `companion.json` (owner-only) in the per-user config folder:
`%APPDATA%\Throughline Companion` on Windows,
`~/Library/Application Support/Throughline Companion` on macOS and
`~/.config/throughline-companion` on Linux. Logs are one file per connector
under the data folder (`%LOCALAPPDATA%\Throughline Companion\logs`,
`~/.local/share/throughline-companion/logs`).
