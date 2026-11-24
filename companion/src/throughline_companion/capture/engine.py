"""The capture engine: screenshots on a timer while you are at the computer,
speech segments transcribed as they end, all written to a local spool that
the `capture` connector ships to the server and then empties.

Spool layout (<data>/capture/spool):
  screens/<stamp>.jpg + <stamp>.json      one screenshot and its context
  transcripts/<stamp>.json                one transcribed speech segment
  audio/<...>.wav                         untranscribed speech (deleted after)
A .json file is written last and atomically, so the connector never reads a
half-written item.
"""

import hashlib
import json
import logging
import os
import queue
import re
import socket
import threading
import time
from datetime import datetime, timezone

from .. import paths
from . import audio, media, screen, transcribe
from .idle import IdleMonitor
from .window import active_window

log = logging.getLogger("throughline_companion.capture")

DEFAULTS = {
    "enabled": False,
    "device": "",
    "spool_max_mb": 2048,
    "screens": {
        "enabled": False,
        "interval_s": 60,
        "only_when_active": True,
        "idle_after_s": 60,
        "monitors": "active",
        "max_px": 1600,
        "format": "jpeg",
        "quality": 70,
        "skip_unchanged": True,
        "exclude_apps": ["1password", "bitwarden", "keepassxc", "keychain access", "lastpass", "dashlane"],
        "exclude_titles": ["incognito", "inprivate", "private browsing", "private window"],
    },
    "audio": {
        "enabled": False,
        "sources": ["mic"],
        "mic_device": "",
        "only_when_active": False,
        "pause_on": ["spotify", "youtube", "streaming"],
        "min_s": 1.0,
        "max_s": 30.0,
        "silence_s": 1.2,
        "sensitivity": 3.0,
    },
    "transcription": {
        "engine": "groq",
        "base_url": "",
        "model": "",
        "api_key": "",
        "language": "",
        "prompt": "",
        "local_model": "small",
        "local_device": "auto",
    },
}


def spool_root():
    return paths.data_dir() / "capture" / "spool"


def _stamp(when):
    return when.strftime("%Y%m%dT%H%M%S%fZ")


def _write_json(path, value):
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def spool_bytes():
    total = 0
    for item in spool_root().rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def _matches(patterns, text):
    text = (text or "").lower()
    return any(p and (p.lower() in text or _regex(p, text)) for p in patterns or [])


def _regex(pattern, text):
    if not (pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 2):
        return False
    try:
        return re.search(pattern[1:-1], text, re.I) is not None
    except re.error:
        return False


class CaptureEngine:
    def __init__(self, config):
        self.config = config
        self.stopping = threading.Event()
        self.threads = []
        self.idle = None
        self.recorders = []
        self.segments = queue.Queue()
        self.local = None
        self.last_fp = None
        self.media_cache = (0.0, False, None)
        self.stats = dict(screens=0, skipped_unchanged=0, skipped_idle=0, skipped_excluded=0,
                          skipped_display_off=0, segments=0, transcribed=0, transcribe_failed=0,
                          paused_for_media=0)
        self.last_screen_at = None
        self.last_error = None
        self.audio_error = None
        self.state = "stopped"

    @property
    def options(self):
        return self.config.data["capture"]

    def device(self):
        return self.options.get("device") or self.config.server.get("device") or socket.gethostname().split(".")[0].lower()

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        thread = threading.Thread(target=self._supervise, name="capture", daemon=True)
        thread.start()
        self.threads.append(thread)
        worker = threading.Thread(target=self._transcriber, name="transcriber", daemon=True)
        worker.start()
        self.threads.append(worker)

    def stop(self):
        self.stopping.set()
        for recorder in self.recorders:
            recorder.stop()
        if self.local:
            self.local.close()

    def _supervise(self):
        next_shot = time.monotonic() + 5
        while not self.stopping.is_set():
            opts = self.options
            try:
                if not opts.get("enabled"):
                    self.state = "off"
                    self._sync_recorders(False)
                else:
                    self.state = "running"
                    self._sync_recorders(opts["audio"].get("enabled"))
                    if opts["screens"].get("enabled") and time.monotonic() >= next_shot:
                        next_shot = time.monotonic() + max(10, int(opts["screens"].get("interval_s") or 60))
                        self.capture_screen()
            except Exception as error:  # noqa: BLE001
                self.last_error = f"{type(error).__name__}: {error}"[:300]
                log.warning("capture: %s", self.last_error)
            self.stopping.wait(1)
        self._sync_recorders(False)

    # -- screens -----------------------------------------------------------

    def _idle_monitor(self):
        threshold = int(self.options["screens"].get("idle_after_s") or 60)
        if self.idle is None or self.idle.threshold_s != threshold:
            self.idle = IdleMonitor(threshold)
        return self.idle

    def capture_screen(self):
        """Take one screenshot if the rules allow; returns the spool path or None."""
        opts = self.options["screens"]
        if self._spool_full():
            self.last_error = "spool full: the capture connector has not uploaded for a while"
            return None
        if opts.get("only_when_active", True):
            active = self._idle_monitor().active()
            if active is False:
                self.stats["skipped_idle"] += 1
                return None
        if screen.displays_on() is False:
            self.stats["skipped_display_off"] += 1
            return None
        window = active_window()
        if _matches(opts.get("exclude_apps"), window.get("app")) or _matches(opts.get("exclude_titles"), window.get("title")):
            self.stats["skipped_excluded"] += 1
            return None
        image, monitor = screen.grab(opts.get("monitors") or "active")
        fp = screen.fingerprint(image)
        if opts.get("skip_unchanged", True) and not screen.changed(self.last_fp, fp):
            self.stats["skipped_unchanged"] += 1
            return None
        self.last_fp = fp
        data, mime, width, height = screen.encode(
            image, int(opts.get("max_px") or 1600), opts.get("format") or "jpeg", int(opts.get("quality") or 70)
        )
        now = datetime.now(timezone.utc)
        folder = spool_root() / "screens"
        folder.mkdir(parents=True, exist_ok=True)
        paths.ensure_private(folder)
        stamp = _stamp(now)
        ext = "webp" if mime == "image/webp" else "jpg"
        image_path = folder / f"{stamp}.{ext}"
        image_path.write_bytes(data)
        _write_json(
            folder / f"{stamp}.json",
            dict(
                kind="screen",
                device=self.device(),
                captured_at=now.isoformat(),
                file=image_path.name,
                mime=mime,
                width=width,
                height=height,
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                app=window.get("app"),
                title=window.get("title"),
                monitor=str(monitor) if monitor is not None else None,
                idle_backend=self.idle.backend if self.idle else None,
            ),
        )
        self.stats["screens"] += 1
        self.last_screen_at = now.isoformat()
        self.last_error = None
        return image_path

    def _spool_full(self):
        value = self.options.get("spool_max_mb")
        limit = int(2048 if value is None else value) * 1024 * 1024
        return spool_bytes() > limit

    # -- audio -------------------------------------------------------------

    def _sync_recorders(self, wanted):
        opts = self.options["audio"]
        sources = list(opts.get("sources") or []) if wanted else []
        running = {r.source: r for r in self.recorders}
        if wanted and not audio.available():
            self.audio_error = "audio capture needs the audio extra (soundcard, numpy)"
            sources = []
        for source, recorder in running.items():
            if source not in sources:
                recorder.stop()
                self.recorders.remove(recorder)
        for source in sources:
            if source in running:
                continue
            recorder = audio.Recorder(
                source,
                spool_root() / "audio",
                self._segment_ready,
                self._audio_paused,
                device_id=(opts.get("mic_device") or None) if source == "mic" else None,
                min_s=float(opts.get("min_s") or 1.0),
                max_s=float(opts.get("max_s") or 30.0),
                silence_s=float(opts.get("silence_s") or 1.2),
                sensitivity=float(opts.get("sensitivity") or 3.0),
            )
            recorder.start()
            self.recorders.append(recorder)

    def _audio_paused(self):
        opts = self.options["audio"]
        if opts.get("only_when_active"):
            if self._idle_monitor().active() is False:
                return "away from the computer"
        checked, pause, reason = self.media_cache
        if time.monotonic() - checked > 5:
            try:
                pause, reason = media.should_pause(opts.get("pause_on"), window=active_window())
            except Exception:  # noqa: BLE001
                pause, reason = False, None
            if pause and not self.media_cache[1]:
                self.stats["paused_for_media"] += 1
            self.media_cache = (time.monotonic(), pause, reason)
        return reason if pause else None

    def _segment_ready(self, path, started, ended, source):
        self.stats["segments"] += 1
        self.segments.put((path, started, ended, source, 0))

    def _engine(self):
        from throughline_sdk import infisical

        options = dict(self.options["transcription"])
        raw_key = options.get("api_key") or ""
        if isinstance(raw_key, str) and raw_key.startswith("env:"):
            raw_key = os.getenv(raw_key[4:], "")
        if infisical.is_ref(raw_key):
            cfg = infisical.InfisicalConfig.from_settings(self.config.data.get("infisical"))
            options["api_key"] = infisical.InfisicalClient(cfg).get_secret(raw_key)
        else:
            options["api_key"] = raw_key
        if options.get("engine") == "local":
            from .local_whisper import LocalWhisper

            model = options.get("local_model") or "small"
            if self.local is None or self.local.model != model:
                if self.local:
                    self.local.close()
                self.local = LocalWhisper(model, options.get("local_device") or "auto")
        return options

    def _transcriber(self):
        # Leftover segments from a previous run are transcribed first.
        for leftover in sorted((spool_root() / "audio").glob("audio-*.wav")) if (spool_root() / "audio").exists() else []:
            match = re.match(r"audio-(\w+)-(\d{8}T\d{12}Z)\.wav", leftover.name)
            if match:
                started = datetime.strptime(match.group(2), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
                self.segments.put((leftover, started, None, match.group(1), 0))
        while not self.stopping.is_set():
            try:
                path, started, ended, source, tries = self.segments.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.transcribe_segment(path, started, ended, source)
            except Exception as error:  # noqa: BLE001
                self.stats["transcribe_failed"] += 1
                self.audio_error = f"transcription: {error}"[:300]
                if tries < 3 and path.exists():
                    self.stopping.wait(min(60, 5 * 2 ** tries))
                    self.segments.put((path, started, ended, source, tries + 1))
                else:
                    path.unlink(missing_ok=True)

    def transcribe_segment(self, path, started, ended, source):
        options = self._engine()
        duration = audio.wav_seconds(path)
        try:
            result = transcribe.transcribe(path, options, local=self.local)
        except Exception as error:
            key = options.get("api_key") or ""
            msg = str(error)
            if key and key in msg:
                msg = msg.replace(key, "[REDACTED]")
                raise transcribe.TranscriptionError(msg) from None
            raise
        text = (result.get("text") or "").strip()
        if text and not _hallucination(text):
            folder = spool_root() / "transcripts"
            folder.mkdir(parents=True, exist_ok=True)
            paths.ensure_private(folder)
            ended = ended or datetime.fromtimestamp(started.timestamp() + duration, timezone.utc)
            _write_json(
                folder / f"{_stamp(started)}-{source}.json",
                dict(
                    kind="transcript",
                    device=self.device(),
                    source=source,
                    started_at=started.isoformat(),
                    ended_at=ended.isoformat(),
                    duration_s=round(duration, 2),
                    text=text,
                    language=result.get("language"),
                    segments=result.get("segments") or [],
                    engine=result.get("engine"),
                    model=result.get("model"),
                ),
            )
            self.stats["transcribed"] += 1
        path.unlink(missing_ok=True)
        self.audio_error = None

    # -- status ------------------------------------------------------------

    def snapshot(self):
        from . import local_whisper

        opts = self.options
        transcription = dict(opts["transcription"])
        raw_key = transcription.get("api_key") or ""
        if isinstance(raw_key, str) and (
            raw_key.startswith("infisical:") or raw_key.startswith("env:")
        ):
            transcription["api_key"] = raw_key
        else:
            transcription["api_key"] = "set" if raw_key else ""
        return dict(
            state=self.state,
            settings=dict(opts, transcription=transcription),
            device=self.device(),
            screen_backend=screen.backend(),
            idle_backend=self.idle.backend if self.idle else None,
            audio_available=audio.available(),
            recorders=[dict(source=r.source, state=r.state, paused=r.pause_reason, error=r.error) for r in self.recorders],
            stats=dict(self.stats),
            last_screen_at=self.last_screen_at,
            last_error=self.last_error,
            audio_error=self.audio_error,
            spool_bytes=spool_bytes(),
            spool=str(spool_root()),
            local=local_whisper.status(),
            engines=sorted(transcribe.PRESETS),
        )


# Phrases speech models invent over silence or music.
_HALLUCINATIONS = {
    "thank you.", "thanks for watching!", "thank you for watching.", "you", ".", "bye.",
    "subtitles by the amara.org community", "please subscribe.",
}


def _hallucination(text):
    return text.strip().lower() in _HALLUCINATIONS
