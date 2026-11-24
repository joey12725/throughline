"""Transcription behind one small interface, so tests use fakes.

``Transcriber.transcribe(path)`` returns plain text (or None when no
transcriber is configured); ``Transcriber.transcript(path)`` returns
``{text, segments: [{start, end, text}], timestamps, language?}`` with
absolute timestamps in seconds.

``OpenAICompatible`` is the primary implementation: any server exposing
``POST {base}/audio/transcriptions`` the way OpenAI does. Long audio is split
into chunks under the upload limit, preferably at silences, each chunk is
transcribed with ``response_format=verbose_json`` (falling back to ``json``
when the server rejects it) and the pieces are merged into one timeline.
"""

import mimetypes
import tempfile
import time
from pathlib import Path

import httpx

from . import ffmpeg as av
from . import pacing
from .errors import BudgetExhausted, MediaTransient

DEFAULT_MODEL = "whisper-1"
DEFAULT_MAX_BYTES = 24 * 1024 * 1024
DEFAULT_CHUNK_SECONDS = 1200
DEFAULT_OVERLAP_S = 2.0
# Opus at 32 kb/s, the bitrate ffmpeg.audio and ffmpeg.cut produce.
SPEECH_BYTES_PER_S = 32_000 / 8

# (setting name, environment variable, default, description). Plugins turn
# these into manifest declarations; the API key is declared as a secret.
SETTINGS = [
    (
        "transcribe_url",
        "THROUGHLINE_TRANSCRIBE_URL",
        None,
        "OpenAI-compatible API base ending in /v1, e.g. https://stt.example.invalid/v1",
    ),
    (
        "transcribe_model",
        "THROUGHLINE_TRANSCRIBE_MODEL",
        None,
        "Model name sent to the endpoint, default whisper-1",
    ),
    (
        "transcribe_max_bytes",
        "THROUGHLINE_TRANSCRIBE_MAX_BYTES",
        str(DEFAULT_MAX_BYTES),
        "Upload limit per request; longer audio is split into chunks below it",
    ),
    (
        "transcribe_chunk_seconds",
        "THROUGHLINE_TRANSCRIBE_CHUNK_SECONDS",
        str(DEFAULT_CHUNK_SECONDS),
        "Longest audio chunk per request, in seconds",
    ),
    (
        "transcribe_timeout",
        "THROUGHLINE_TRANSCRIBE_TIMEOUT",
        "600",
        "Seconds to wait for one transcription request",
    ),
    (
        "transcribe_retries",
        "THROUGHLINE_TRANSCRIBE_RETRIES",
        "4",
        "Retries per request after HTTP 429, 5xx, timeouts or connection errors",
    ),
    (
        "transcribe_max_minutes",
        "THROUGHLINE_TRANSCRIBE_MAX_MINUTES",
        "600",
        "Audio minutes sent to the endpoint per run; 0 means no cap",
    ),
    (
        "transcribe_minutes_per_day",
        "THROUGHLINE_TRANSCRIBE_MINUTES_PER_DAY",
        "120",
        "Audio minutes sent to the endpoint in any 24 hours, across runs and plugins; 0 disables the endpoint, unlimited lifts the cap",
    ),
    (
        pacing.STATE_DIR_SETTING,
        pacing.STATE_DIR_ENV,
        None,
        "Directory of the daily-minutes count shared by every plugin, default %LOCALAPPDATA%/throughline/media",
    ),
]
SECRET = (
    "transcribe_api_key",
    "THROUGHLINE_TRANSCRIBE_API_KEY",
    "Bearer key for the OpenAI-compatible transcription endpoint",
)
DAILY_SETTING = "transcribe_minutes_per_day"
INTEGER_SETTINGS = [s for s in SETTINGS if s[2] is not None and s[0] != DAILY_SETTING]


def validate(settings):
    for name, env, _, _ in INTEGER_SETTINGS:
        if not str(settings.get(name) or "").isdigit():
            raise ValueError(env + " must be a non-negative integer")
    daily_minutes(settings)


def daily_minutes(settings):
    """Daily endpoint minutes: an int (0 disables the endpoint) or None."""
    value = settings.get(DAILY_SETTING)
    return pacing.parse_limit(
        "120" if value is None else value, "THROUGHLINE_TRANSCRIBE_MINUTES_PER_DAY"
    )


def endpoint_from(settings, secrets, transport=None):
    """OpenAICompatible from THROUGHLINE_TRANSCRIBE_* settings, or None."""
    url = settings.get("transcribe_url")
    if not url or daily_minutes(settings) == 0:
        return None  # Not configured, or switched off by a daily cap of 0.

    def number(name, default):
        value = settings.get(name)
        return int(value) if str(value or "").isdigit() else default

    return OpenAICompatible(
        url,
        secrets.get("transcribe_api_key"),
        settings.get("transcribe_model") or DEFAULT_MODEL,
        transport=transport,
        max_bytes=number("transcribe_max_bytes", DEFAULT_MAX_BYTES),
        chunk_seconds=number("transcribe_chunk_seconds", DEFAULT_CHUNK_SECONDS),
        timeout=number("transcribe_timeout", 600),
        retries=number("transcribe_retries", 4),
        max_minutes=number("transcribe_max_minutes", 0) or None,
    )


class Transcriber:
    name = "none"
    model = None

    def transcribe(self, audio_path):
        """Transcript text, or None when unavailable."""
        return None

    def transcript(self, audio_path):
        """{text, segments, timestamps}, or None when unavailable."""
        text = self.transcribe(audio_path)
        if text is None:
            return None
        return {"text": text, "segments": [], "timestamps": False}

    def remaining_s(self):
        """Audio seconds this run may still send, or None for no cap."""
        return None

    def capacity_s(self):
        """Most audio seconds one item may ever take (per run or per day)."""
        return None


class NullTranscriber(Transcriber):
    """Records "transcript unavailable"; callers may keep audio for later."""


class FasterWhisper(Transcriber):
    name = "faster-whisper"

    def __init__(self, model="small"):
        self.model_name = model or "small"
        self.model = None

    @staticmethod
    def available():
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def segments(self, audio_path):
        from faster_whisper import WhisperModel

        if self.model is None:
            self.model = WhisperModel(
                self.model_name, device="auto", compute_type="int8"
            )
        segments, _ = self.model.transcribe(str(audio_path), vad_filter=True)
        return list(segments)

    def transcribe(self, audio_path):
        return " ".join(s.text.strip() for s in self.segments(audio_path)).strip()

    def transcript(self, audio_path):
        found = self.segments(audio_path)
        return {
            "text": " ".join(s.text.strip() for s in found).strip(),
            "segments": [
                {"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
                for s in found
            ],
            "timestamps": True,
        }


def plan(duration, chunk_s, quiet=(), overlap=DEFAULT_OVERLAP_S, search=None):
    """[(start, end, keep_from, keep_to)]: extraction span and owned span.

    A cut lands at the middle of the latest silence in the `search` window
    before each nominal boundary, with no overlap; without one it falls on
    the boundary and both neighbours extend `overlap` seconds across it. Each
    chunk owns [keep_from, keep_to): merged segments are kept by midpoint.
    """
    chunk_s = max(float(chunk_s), 1.0)
    if duration <= chunk_s:
        return [(0.0, float(duration), 0.0, float(duration))]
    search = min(60.0, chunk_s * 0.2) if search is None else search
    cuts = [(0.0, 0.0)]  # (position, overlap at this cut)
    while duration - cuts[-1][0] > chunk_s:
        target = cuts[-1][0] + chunk_s
        middles = [
            (a + b) / 2
            for a, b in quiet
            if target - search <= (a + b) / 2 <= target and (a + b) / 2 > cuts[-1][0]
        ]
        cuts.append((max(middles), 0.0) if middles else (target, overlap))
    cuts.append((float(duration), 0.0))
    pieces = []
    for (begin, before), (end, after) in zip(cuts, cuts[1:]):
        pieces.append(
            (max(0.0, begin - before), min(float(duration), end + after), begin, end)
        )
    return pieces


def _word(token):
    return "".join(ch for ch in token.lower() if ch.isalnum())


def trim_repeat(tail, text, most=40, least=2):
    """`text` without leading words that repeat the end of `tail`.

    Used at fixed (overlapping) cuts, where both chunks transcribe the same
    couple of seconds. The longest repeat of at least `least` words wins;
    a shorter coincidence is left alone.
    """
    words = text.split()
    before = [w for w in (_word(t) for t in tail.split()) if w][-most:]
    if not words or not before:
        return text
    head = [_word(t) for t in words[:most]]
    for k in range(min(len(before), len(head)), least - 1, -1):
        if before[-k:] == head[:k] and all(head[:k]):
            return " ".join(words[k:])
    return text


class OpenAICompatible(Transcriber):
    """POST {base}/audio/transcriptions, as OpenAI and compatible servers do."""

    name = "openai-compatible"

    def __init__(
        self,
        base_url,
        api_key=None,
        model=DEFAULT_MODEL,
        transport=None,
        *,
        max_bytes=DEFAULT_MAX_BYTES,
        chunk_seconds=DEFAULT_CHUNK_SECONDS,
        overlap_s=DEFAULT_OVERLAP_S,
        timeout=600,
        retries=4,
        max_minutes=None,
        sleep=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model or DEFAULT_MODEL
        headers = {"Authorization": "Bearer " + api_key} if api_key else {}
        self.http = httpx.Client(timeout=timeout, headers=headers, transport=transport)
        self.max_bytes = int(max_bytes)
        self.chunk_seconds = int(chunk_seconds) if chunk_seconds else None
        self.overlap_s = overlap_s
        self.retries = int(retries)
        self.budget_s = max_minutes * 60 if max_minutes else None
        self.used_s = 0.0
        self.sleep = sleep
        self.verbose = None  # Unknown until the server accepts or refuses it.
        self.requests = 0
        # A pacing.Allowance: audio seconds in any 24 hours, across runs.
        self.daily = None

    def remaining_s(self):
        left = [] if self.budget_s is None else [max(0.0, self.budget_s - self.used_s)]
        if self.daily is not None and self.daily.remaining_s() is not None:
            left.append(self.daily.remaining_s())
        return min(left) if left else None

    def capacity_s(self):
        caps = [] if self.budget_s is None else [self.budget_s]
        if self.daily is not None and self.daily.capacity_s() is not None:
            caps.append(self.daily.capacity_s())
        return min(caps) if caps else None

    def close(self):
        self.http.close()

    def transcribe(self, audio_path):
        return self.transcript(audio_path)["text"]

    def transcript(self, audio_path):
        path = Path(audio_path)
        size = path.stat().st_size
        seconds = av.duration(path)
        if self.budget_s is not None and seconds:
            if self.used_s + seconds > self.budget_s:
                raise BudgetExhausted("per-run transcription minutes used up")
        # Counted when attempted, before anything is sent.
        if self.daily is not None and not self.daily.take(seconds or 0.0):
            raise BudgetExhausted("daily transcription minutes used up")
        limit = self.chunk_seconds
        fits = size <= self.max_bytes and (
            seconds is None or limit is None or seconds <= limit + self.overlap_s
        )
        if fits:
            result = self.request(path)
            self.used_s += seconds or 0.0
            if seconds and not result["segments"] and result["text"]:
                result["segments"] = [
                    {"start": 0.0, "end": round(seconds, 3), "text": result["text"]}
                ]
            return result
        if not seconds:
            raise MediaTransient("cannot measure the audio to split it")
        # Bytes per second of the chunks actually uploaded (re-encoded speech).
        rate = max(size / seconds, SPEECH_BYTES_PER_S)
        chunk_s = max(30.0, 0.9 * self.max_bytes / rate)
        if limit:
            chunk_s = min(chunk_s, float(limit))
        quiet = av.silences(path)
        pieces = plan(seconds, chunk_s, quiet, self.overlap_s)
        segments, texts, timestamps, language = [], [], True, None
        with tempfile.TemporaryDirectory(prefix="throughline-stt-") as tmp:
            for index, (start, end, keep_from, keep_to) in enumerate(pieces):
                chunk = av.cut(
                    path, start, end - start, Path(tmp) / f"chunk{index}.ogg"
                )
                if chunk.stat().st_size > self.max_bytes:
                    raise MediaTransient("audio chunk exceeds the upload limit")
                part = self.request(chunk)
                chunk.unlink(missing_ok=True)
                language = language or part.get("language")
                last = index == len(pieces) - 1
                # A fixed cut: both neighbours heard the overlap, so the words
                # spoken in it may arrive twice (or a segment may straddle it).
                overlapped = index > 0 and start < keep_from
                tail = " ".join(texts[-40:]) if overlapped else ""
                if not part["segments"]:
                    timestamps = False
                    text = (
                        trim_repeat(tail, part["text"]) if overlapped else part["text"]
                    )
                    if text:
                        texts.append(text)
                        segments.append(
                            {
                                "start": round(keep_from, 3),
                                "end": round(keep_to, 3),
                                "text": text,
                            }
                        )
                    continue
                kept = []
                previous_end = segments[-1]["end"] if segments else 0.0
                for segment in part["segments"]:
                    begin = start + segment["start"]
                    finish = start + segment["end"]
                    middle = (begin + finish) / 2
                    if middle >= keep_to and not last:
                        continue
                    if middle < keep_from:
                        # Owned by the previous chunk, unless that chunk's
                        # kept segments stop before this one ends: then keep
                        # it, minus the words already kept, so nothing is lost.
                        if not overlapped or finish <= previous_end + 0.25:
                            continue
                    text = segment["text"]
                    if overlapped and not kept:
                        text = trim_repeat(tail, text)
                        begin = max(begin, previous_end)
                        if not text:
                            continue
                    kept.append(
                        {
                            "start": round(max(begin, 0.0), 3),
                            "end": round(min(max(finish, begin), seconds), 3),
                            "text": text,
                        }
                    )
                segments.extend(kept)
                texts.extend(s["text"] for s in kept)
        self.used_s += seconds
        result = {
            "text": " ".join(t for t in texts if t).strip(),
            "segments": segments,
            "timestamps": timestamps,
            "chunks": len(pieces),
        }
        if language:
            result["language"] = language
        return result

    # HTTP ----------------------------------------------------------------------

    def request(self, path):
        """One upload: {text, segments (relative), timestamps, language?}."""
        formats = ["json"] if self.verbose is False else ["verbose_json", "json"]
        for fmt in formats:
            response = self.post(path, fmt)
            if fmt == "verbose_json" and response.status_code in (400, 415, 422):
                self.verbose = False  # Plain text from now on, for this server.
                continue
            if response.is_error:
                raise RuntimeError(f"transcriber HTTP {response.status_code}")
            if fmt == "verbose_json":
                self.verbose = True
            return self.parse(response)
        raise RuntimeError("transcriber refused every response format")

    @staticmethod
    def parse(response):
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text}
        if not isinstance(body, dict):
            body = {"text": str(body)}
        segments = []
        for segment in body.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            try:
                start, end = float(segment["start"]), float(segment["end"])
            except (KeyError, TypeError, ValueError):
                continue
            text = str(segment.get("text") or "").strip()
            if text:
                segments.append({"start": start, "end": max(end, start), "text": text})
        result = {
            "text": str(body.get("text") or "").strip(),
            "segments": segments,
            "timestamps": bool(segments),
        }
        if isinstance(body.get("language"), str) and body["language"]:
            result["language"] = body["language"]
        return result

    def backoff(self, attempt, response=None):
        if response is not None:
            after = response.headers.get("retry-after", "")
            if after.strip().isdigit():
                return min(float(after.strip()), 60.0)
        return min(1.0 * 2**attempt, 30.0)

    def post(self, path, fmt):
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        for attempt in range(self.retries + 1):
            self.requests += 1
            try:
                with path.open("rb") as handle:
                    response = self.http.post(
                        self.base_url + "/audio/transcriptions",
                        data={"model": self.model, "response_format": fmt},
                        files={"file": (path.name, handle, mime)},
                    )
            except httpx.TimeoutException:
                if attempt == self.retries:
                    raise MediaTransient("transcriber timed out") from None
                self.sleep(self.backoff(attempt))
                continue
            except httpx.TransportError:
                if attempt == self.retries:
                    raise MediaTransient("transcriber unreachable") from None
                self.sleep(self.backoff(attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == self.retries:
                    raise MediaTransient(f"transcriber HTTP {response.status_code}")
                self.sleep(self.backoff(attempt, response))
                continue
            return response
        raise MediaTransient("transcriber failed")
