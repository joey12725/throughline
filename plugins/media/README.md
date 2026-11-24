# Throughline media

Shared building blocks for plugins that enrich records with media: the
transcriber interface and its implementations, ffmpeg frame and audio
extraction, the yt-dlp runner and its error classification, YouTube video
processing, and the content-addressed asset store with its outcome cache. It
is a library, not a plugin: it declares no channels and runs nothing on its
own. The [YouTube connector](../youtube/README.md) and the
[Beeper plugin](../beeper/README.md) use it; every enrichment stays off until
a plugin run switches it on.

| Module | Contents |
|---|---|
| `transcribe` | `Transcriber`, `OpenAICompatible` (primary), `FasterWhisper`, `NullTranscriber`, `endpoint_from(settings, secrets)`, the shared `THROUGHLINE_TRANSCRIBE_*` setting declarations |
| `ffmpeg` | `frames(video, outdir, every, limit)`, `audio(video, outdir)`, `duration`, `silences`, `cut` |
| `ytdlp` | `YtDlp` runner (fixed options, no shell, no user config, optional cookies), `classify` and `classify_youtube` |
| `youtube` | link detection, `YouTubeFetcher` (metadata, captions, audio-only and <=480p downloads), WebVTT parsing, caption choice, `windows`, and the per-video `Processor` |
| `assets` | `LocalAssetStore(directory, base_url=None, namespace=...)`, `Cache`, `StoreError`, `default_state_dir(name)` |
| `errors` | `MediaUnavailable(status, reason)` (permanent), `MediaTransient` (retry later), `BudgetExhausted` |
| `settings` | `truthy`, `parse_flags`, `apply_aliases` |
| `pacing` | `Pacing` (rolling 24-hour limits and per-kind enable times in the cache file), `Allowance` (daily transcription seconds), `parse_limit`, `parse_since` |

## Transcription with an OpenAI-compatible endpoint

The documented primary path is an OpenAI-compatible endpoint: any server that
implements `POST <base>/audio/transcriptions` with a multipart `file` and
`model` the way OpenAI does, whether OpenAI itself or a self-hosted Whisper
server. Configure it once for every plugin that uses this package (placeholder
values; no real hosts or keys belong in the repository):

```powershell
$env:THROUGHLINE_TRANSCRIBE_URL = 'https://stt.example.invalid/v1'
$env:THROUGHLINE_TRANSCRIBE_MODEL = 'whisper-1'
$env:THROUGHLINE_TRANSCRIBE_API_KEY = '<bearer key, if the server needs one>'
uv run throughline-youtube backfill --transcripts
```

On Linux use `export NAME=value`. Without an API key no `Authorization`
header is sent, which suits a local server.

| Variable | Default | Meaning |
|---|---|---|
| `THROUGHLINE_TRANSCRIBE_URL` | none | API base ending in `/v1`; unset means no endpoint |
| `THROUGHLINE_TRANSCRIBE_MODEL` | `whisper-1` | Model name sent with every request |
| `THROUGHLINE_TRANSCRIBE_API_KEY` | none | Declared secret: sent as `Authorization: Bearer <key>` |
| `THROUGHLINE_TRANSCRIBE_MAX_BYTES` | 25165824 (24 MiB) | Upload limit per request; larger audio is split |
| `THROUGHLINE_TRANSCRIBE_CHUNK_SECONDS` | 1200 | Longest audio per request (some models limit duration) |
| `THROUGHLINE_TRANSCRIBE_TIMEOUT` | 600 | Seconds to wait for one request |
| `THROUGHLINE_TRANSCRIBE_RETRIES` | 4 | Retries per request after HTTP 429, 5xx, timeouts or connection errors |
| `THROUGHLINE_TRANSCRIBE_MAX_MINUTES` | 600 | Audio minutes sent per run; 0 means no cap |
| `THROUGHLINE_TRANSCRIBE_MINUTES_PER_DAY` | 120 | Audio minutes sent in any 24 hours, across runs and plugins (one pool: Beeper and the YouTube connector draw on the same minutes); 0 switches the endpoint off, `unlimited` lifts the cap |
| `THROUGHLINE_TRANSCRIBE_STATE_DIR` | `%LOCALAPPDATA%\throughline\media` | Where that shared count is kept (`transcribe.sqlite3`) |

Plugins declare these settings (and the key as a secret) in their own
manifests; Beeper also accepts its older `BEEPER_TRANSCRIBE_URL`, `_MODEL` and
`_API_KEY` names as aliases.

How a recording is transcribed (`OpenAICompatible.transcript`):

1. Callers extract mono 16 kHz, 32 kb/s Opus audio first (`ffmpeg.audio`).
2. Audio that fits the upload limit and the chunk length is sent as is.
   Longer audio is split: the chunk length is the smaller of
   `_CHUNK_SECONDS` and what fits 90 % of `_MAX_BYTES` at the file's bitrate.
   Each cut goes to the middle of the latest silence (ffmpeg `silencedetect`,
   -35 dB for 0.4 s) within the last fifth of the chunk (at most 60 s) before
   the nominal boundary, with no overlap; without a silence the cut falls on
   the boundary and both neighbouring chunks extend 2 s across it. Chunks are
   re-encoded with `ffmpeg.cut`.
3. Each request asks for `response_format=verbose_json` to get segment
   timestamps. A server that answers 400, 415 or 422 is asked again with
   `json` (text only), and that choice is remembered for the rest of the run.
4. Segments are moved onto the recording's timeline (chunk start + segment
   time). A segment belongs to the chunk that holds its midpoint. At a fixed
   cut, where both chunks heard the 2 s overlap, the later chunk's first
   segment is also kept when the earlier chunk's kept segments stop before it
   ends (so no speech is lost), its start moves to where the earlier one
   ended, and leading words that repeat the end of the text so far (two or
   more words) are dropped, so the overlap is not transcribed twice.
   Plain-text chunks become one segment spanning the chunk, with the same
   repeated-word trim at fixed cuts, and the result reports
   `timestamps: false`.
5. HTTP 429 and 5xx, timeouts and connection errors are retried with
   exponential backoff (1, 2, 4 ... s, at most 30 s; a numeric `Retry-After`
   up to 60 s wins). After the last retry `MediaTransient` is raised, which
   callers record as a retryable failure. Other HTTP errors (401, 403, 404)
   raise `RuntimeError`.
6. Before sending, the audio's duration counts against
   `_MAX_MINUTES` and, when the plugin attached its daily `Allowance`
   (`client.daily`), against `_MINUTES_PER_DAY`; the daily minutes count when
   attempted, before anything is sent. Once an allowance is used up
   `BudgetExhausted` is raised and callers leave the remaining work for a
   later run without recording a failure. `capacity_s()` is the most one
   recording may ever take; callers record longer audio as `over_budget`
   (final until the allowance is raised) instead of deferring it forever.

The result is `{text, segments: [{start, end, text}], timestamps, language?,
chunks?}`. No cost is estimated for paid endpoints.

`FasterWhisper` (local, needs the `faster-whisper` package, which Beeper's
`whisper` extra installs) and `NullTranscriber` (records nothing) implement
the same interface.

## YouTube processing

`youtube.Processor(fetcher, store, transcriber, transcripts=..., frames=...,
frame_every=60, source="auto", max_seconds=7200, caption_langs=["en"],
max_bytes=2 GiB)` turns a video ID into one cacheable outcome:

- one `yt-dlp --dump-single-json --skip-download` call for metadata (title,
  duration, upload time, caption tracks), cached with the outcome;
- the transcript from the endpoint (`source=endpoint`, or `auto` with a
  transcriber) after an audio-only download (`-f ba`), or from YouTube's
  captions (`source=captions`, or `auto` without one) via `yt-dlp
  --write-subs`/`--write-auto-subs --sub-format vtt`: manual captions in a
  preferred language (the video's own language counts), then automatic
  captions of the original speech, then any manual track, then automatic
  translations; rolling repeats in automatic captions are removed;
- frames from a video stream of at most 480p (`bv[height<=480]/...`), one
  every `frame_every` seconds from 0:00 (ffmpeg `fps=1/N`, at most 960 px
  wide), stored as JPEGs in the asset store. The video is deleted right after.

Downloads use a temporary directory that is removed after each step; audio
and video are never kept. Final outcomes: `private`, `removed`,
`members_only`, `age_restricted`, `premium_only`, `too_long` (redone when
`max_seconds` is raised) and `unavailable` (three transient failures). The
three sign-in gated ones are retried once when cookies are configured.
Transient failures (`error`, with `attempts`) are retried by later runs and
are not meant to be rendered (`youtube.pending`). The transcript and frames
parts keep their own status, so a later run can add frames, change the frame
interval or switch the transcript source without redoing the other part.
`windows(cues, frames, duration, length)` cuts an outcome into fixed-length
parts for rendering.

A video whose transcript waits for the allowance gets the outcome
`deferred` (with its metadata, so the next attempt fetches nothing again);
like transient failures it is not rendered, and a video is done completely or
not at all, so its frames wait too. Once metadata is known, `todo` reports
nothing to do while the video does not fit the minutes left.

## Pacing

`pacing.Pacing(conn)` keeps two tables in a plugin's outcome cache (the same
SQLite file), so they hold across runs and processes:

- `usage`: one row per attempted fetch (or transcribed second) with its time.
  `take(kind, limit, amount=1)` records an attempt only if the last 24 hours
  leave room, in an immediate transaction, so two processes never both take
  the last unit; `remaining` and `frees_at` (ISO time the window frees up)
  report on it. The window rolls; it is not a calendar day. A limit of `0`
  never fits and `None` (`unlimited`) always fits.
- `enabled`: per enrichment kind, the moment it was first switched on;
  `since(kind, explicit=None)` stores it on first use, or replaces it with an
  explicit `--enrich-since` time. Plugins enrich only items that arrived at or
  after it unless the backlog is requested.

The transcription endpoint's daily minutes are the exception: they are one
pool for every plugin on the machine. `pacing.endpoint_allowance(settings,
minutes)` opens the `usage` table of a separate `transcribe.sqlite3` in
`THROUGHLINE_TRANSCRIBE_STATE_DIR` (default `default_state_dir("media")`), so
Beeper and the YouTube connector share the same minutes in any 24 hours.

The enable time is stored once: when two runs enable a kind at the same
moment, the first stored time holds. Dates without a time or offset (such as
`--enrich-since 2026-01-31`) are read as UTC.

Read-only runs (dry runs, enrichment off) write nothing and read what exists.

## Assets and cache

`LocalAssetStore` writes `<sha256>.<ext>` files atomically and links them as
`throughline-asset://<namespace>/<sha256>.<ext>` (or under an explicit
`base_url`), so a later core asset endpoint can serve the same names. Beeper
uses the namespace `beeper` and the YouTube connector `youtube`. A failing
write raises `StoreError`, which stops the run instead of caching a failure.
`Cache` is one SQLite table of JSON outcomes; opened read-only it never
creates the file. `default_state_dir(name)` is `%LOCALAPPDATA%\throughline\<name>`
on Windows and `$XDG_STATE_HOME/throughline/<name>` (or
`~/.local/state/throughline/<name>`) elsewhere.

## Tests

```powershell
uv run pytest -q plugins/media
uv run ruff check plugins/media
```

Tests use synthetic media only: sine tones (with silent gaps) and test
patterns generated by ffmpeg, invented captions, httpx mock transports for the
endpoint, and a monkeypatched subprocess for yt-dlp. They cover chunk planning
at silences and fixed cuts with overlap, the merged absolute timeline, the
`verbose_json` fallback, retries and backoff, the per-run minute cap, frame
intervals, caption parsing and choice, error classification and the
processor's caching rules; `test_pacing.py` covers the rolling window with a
fake clock, two connections sharing one file, enable times (including two
runs storing one at once) and the daily transcription minutes shared by
every plugin. Tests needing ffmpeg are skipped without it. No
real YouTube, Instagram or Discord content is downloaded.
