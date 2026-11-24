"""YouTube videos: links, metadata, captions, endpoint transcripts and frames.

Shared by the YouTube connector and the Beeper plugin. ``Processor.process``
turns a video ID into one JSON-serializable outcome that callers cache by
video ID, so a rerun repeats no downloads. Audio and video files are only
ever written to a temporary directory and deleted when a step ends; only
frame JPEGs go to the asset store.
"""

import copy
import html
import json
import math
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import ffmpeg as av
from .assets import StoreError
from .errors import BudgetExhausted, MediaTransient, MediaUnavailable
from .ytdlp import YtDlp, classify_youtube

MAX_ATTEMPTS = 3
FRAME_EVERY_S = 60
SEGMENT_S = 300
MAX_SECONDS = 7200
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
SOURCES = ("auto", "endpoint", "captions")
# Final outcomes a signed-in run may get past: retried once cookies appear.
AUTH_GATED = {"private", "members_only", "age_restricted"}
UNAVAILABLE = {
    "private": "unavailable (private)",
    "removed": "unavailable (removed or not found)",
    "members_only": "unavailable (members-only)",
    "age_restricted": "unavailable (age-restricted)",
    "premium_only": "unavailable (YouTube Premium only)",
    "unavailable": "unavailable (failed repeatedly)",
}
TRANSCRIPT_STATUS = {
    "no_captions": "no captions available",
    "no_audio": "no audio",
    "empty": "no speech",
    "too_large": "audio larger than the download cap",
    "over_budget": "longer than the transcription minutes allowed per run or day",
    "unavailable": "transcript unavailable",
    "failed": "transcription failed",
}

# Loose on purpose for IDs the connector already holds; links need exactly 11.
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{6,64}")
LINK = re.compile(
    # With a scheme, or bare when not glued to a longer host or path.
    r"(?:\bhttps?://|(?<![\w.@/:-]))"
    r"(?:(?:www\.|m\.|music\.)?youtube\.com/(?:watch/?\?(?:[^\s#]*?&)?v=|(shorts|live)/)"
    r"|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


def youtube_links(text):
    """[(canonical URL, kind, video ID)]: kind is watch, short or live."""
    seen, result = set(), []
    for match in LINK.finditer(text or ""):
        kind = {"shorts": "short", "live": "live"}.get((match.group(1) or "").lower())
        video = match.group(2)
        if video not in seen:
            seen.add(video)
            result.append((watch_url(video), kind or "watch", video))
    return result


def watch_url(video_id, at=None):
    url = "https://www.youtube.com/watch?v=" + video_id
    return url if at is None else f"{url}&t={int(at)}s"


def clock(seconds):
    """mm:ss with unbounded minutes (75:00 is an hour and a quarter in)."""
    seconds = int(seconds or 0)
    return f"{seconds // 60:02}:{seconds % 60:02}"


def clean_text(text):
    text = "".join(ch if ord(ch) >= 32 and ch != "\x7f" else " " for ch in text or "")
    return re.sub(r"\s+", " ", text).strip()


# Metadata ----------------------------------------------------------------------


def summary(info):
    """The few metadata fields kept in the cache; never format lists or URLs."""
    keep = {
        key: info.get(key)
        for key in (
            "id",
            "title",
            "duration",
            "availability",
            "live_status",
            "timestamp",
            "upload_date",
            "language",
            "channel",
            "channel_id",
        )
    }
    keep["subtitles"] = sorted(info.get("subtitles") or {})
    keep["automatic_captions"] = sorted(info.get("automatic_captions") or {})
    if isinstance(keep.get("duration"), (int, float)):
        keep["duration"] = round(float(keep["duration"]), 3)
    return {k: v for k, v in keep.items() if v not in (None, "", [])}


def published(meta):
    """(ISO time, precision) of publication, or (None, None)."""
    stamp = (meta or {}).get("timestamp")
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
        return datetime.fromtimestamp(int(stamp), UTC).isoformat(), "published"
    day = str((meta or {}).get("upload_date") or "")
    if re.fullmatch(r"\d{8}", day):
        return f"{day[:4]}-{day[4:6]}-{day[6:]}T00:00:00+00:00", "published_day"
    return None, None


def gate(meta):
    """The exception a video's availability implies, or None."""
    availability = meta.get("availability")
    if availability == "private":
        return MediaUnavailable("private", "private video")
    if availability == "subscriber_only":
        return MediaUnavailable("members_only", "members-only video")
    if availability == "premium_only":
        return MediaUnavailable("premium_only", "YouTube Premium only")
    if availability == "needs_auth":
        return MediaUnavailable("age_restricted", "sign-in required")
    if meta.get("live_status") in ("is_live", "is_upcoming", "post_live"):
        return MediaTransient("live, upcoming or still processing")
    return None


def pick_captions(meta, langs):
    """("manual" | "automatic", language) or None.

    Order: manual captions in a preferred language (the video's own language
    counts as preferred); YouTube's automatic captions of the original speech;
    any manual track; automatic captions in a preferred language (those are
    machine translations).
    """
    manual = [t for t in meta.get("subtitles") or [] if t != "live_chat"]
    auto = list(meta.get("automatic_captions") or [])
    original = meta.get("language")
    wanted = [lang for lang in [*langs, original] if lang]

    def find(tracks, languages):
        for lang in languages:
            if lang in tracks:
                return lang
        for lang in languages:
            for track in tracks:
                if track.split("-")[0].lower() == lang.split("-")[0].lower():
                    return track
        return None

    found = find(manual, wanted)
    if found:
        return "manual", found
    if original:
        for track in (original + "-orig", original):
            if track in auto:
                return "automatic", track
    origs = sorted(t for t in auto if t.endswith("-orig"))
    if origs:
        return "automatic", origs[0]
    if manual:
        return "manual", sorted(manual)[0]
    found = find(auto, list(langs))
    return ("automatic", found) if found else None


# Captions ----------------------------------------------------------------------

TIMING = re.compile(
    r"((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
)


def seconds_of(value):
    parts = value.replace(",", ".").split(":")
    total = 0.0
    for part in parts:
        total = total * 60 + float(part)
    return round(total, 3)


def parse_vtt(text):
    """[{start, end, text}] from WebVTT; YouTube's rolling repeats removed.

    Automatic captions repeat the previous line at the top of every cue (and
    add near-zero-length cues holding only that line); only new lines count.
    """
    cues, previous = [], []
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n[ \t]*\n", normalized):
        lines = block.split("\n")
        for index, line in enumerate(lines):
            match = TIMING.search(line)
            if match:
                break
        else:
            continue
        payload = []
        for line in lines[index + 1 :]:
            line = clean_text(html.unescape(re.sub(r"<[^>]*>", "", line)))
            if line:
                payload.append(line)
        fresh = [line for line in payload if line not in previous]
        if payload:
            previous = payload
        if fresh:
            start, end = seconds_of(match.group(1)), seconds_of(match.group(2))
            cues.append(
                {"start": start, "end": max(start, end), "text": " ".join(fresh)}
            )
    return cues


def windows(cues, frames, duration, length=SEGMENT_S):
    """Fixed windows of `length` seconds over the video, each with its cues
    (by start time) and frames: [{index, start, end, text, cues, frames}]."""
    length = max(int(length), 1)
    total = max(
        [float(duration or 0)]
        + [float(c["end"]) for c in cues]
        + [float(f["at_s"]) for f in frames]
    )
    count = max(1, math.ceil(total / length))
    out = [
        {
            "index": k,
            "start": k * length,
            "end": round(min((k + 1) * length, total), 3),
            "cues": [],
            "frames": [],
        }
        for k in range(count)
    ]
    for cue in cues:
        out[min(int(cue["start"] // length), count - 1)]["cues"].append(cue)
    for frame in frames:
        out[min(int(frame["at_s"] // length), count - 1)]["frames"].append(frame)
    for window in out:
        window["text"] = " ".join(c["text"] for c in window["cues"]).strip()
    return out


def cues_of(result):
    part = (result or {}).get("transcript") or {}
    return part.get("cues") or [] if part.get("status") == "ok" else []


def frames_of(result):
    part = (result or {}).get("frames") or {}
    return part.get("items") or [] if part.get("status") == "ok" else []


def pending(result):
    """A transient failure that will be retried is not rendered yet.

    Its attempt count changes on every retry; rendering it would give the
    record a new version per attempt. Only final outcomes are rendered. A
    video deferred by the transcription allowance is not final either.
    """
    if not result:
        return False
    if result.get("status") in ("error", "deferred"):
        return True
    return any(
        (result.get(part) or {}).get("status") == "error"
        for part in ("transcript", "frames")
    )


# yt-dlp ------------------------------------------------------------------------


class YouTubeFetcher:
    """Metadata, captions, audio-only and <=480p video downloads via yt-dlp."""

    def __init__(self, runner=None, height=480):
        self.runner = runner or YtDlp(classify=classify_youtube)
        self.height = height

    @property
    def authenticated(self):
        return self.runner.authenticated

    def info(self, video_id):
        out = self.runner.run(
            [
                "--dump-single-json",
                "--no-playlist",
                "--skip-download",
                watch_url(video_id),
            ],
            300,
        )
        try:
            info = json.loads(out)
        except ValueError:
            raise MediaTransient("yt-dlp returned no metadata") from None
        if not isinstance(info, dict):
            raise MediaTransient("yt-dlp returned no metadata")
        return summary(info)

    def captions(self, video_id, kind, lang, workdir):
        workdir = Path(workdir)
        self.runner.run(
            [
                "--skip-download",
                "--no-playlist",
                "--write-subs" if kind == "manual" else "--write-auto-subs",
                "--sub-langs",
                re.escape(lang),
                "--sub-format",
                "vtt",
                "--convert-subs",
                "vtt",
                "-o",
                str(workdir / "captions.%(ext)s"),
                watch_url(video_id),
            ],
            300,
        )
        found = sorted(workdir.glob("captions*.vtt"))
        if not found:
            raise MediaTransient("yt-dlp wrote no caption file")
        return found[0].read_text(encoding="utf-8", errors="replace")

    def download(self, video_id, workdir, stem, selector, max_bytes):
        target = Path(workdir) / stem
        target.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [
                "--no-playlist",
                "-f",
                selector,
                "--max-filesize",
                str(max_bytes),
                "-o",
                str(target / (stem + ".%(ext)s")),
                watch_url(video_id),
            ],
            3600,
        )
        found = sorted(
            p
            for p in target.glob(stem + ".*")
            if p.suffix not in (".part", ".ytdl", ".json") and p.is_file()
        )
        return found[0] if found else None  # None: over --max-filesize.

    def audio(self, video_id, workdir, max_bytes=MAX_DOWNLOAD_BYTES):
        """Audio-only stream (a low-resolution file only when none exists)."""
        return self.download(
            video_id, workdir, "audio", "ba/b[height<=480]/w", max_bytes
        )

    def video(self, video_id, workdir, max_bytes=MAX_DOWNLOAD_BYTES):
        h = self.height
        return self.download(
            video_id,
            workdir,
            "video",
            f"bv[height<={h}]/bv*[height<={h}]/b[height<={h}]/wv/w",
            max_bytes,
        )


# Processing --------------------------------------------------------------------

DEFERRED = object()  # A step waits for the transcription allowance.


class Processor:
    """One video ID in, one cacheable outcome out.

    Outcome: {video_id, status, meta?, transcript?, frames?, error?,
    attempts?, authenticated?}. status is ok, too_long, error (retried on
    later runs), deferred (the transcription allowance ran out before the
    video was done; nothing is rendered and it is redone later), or a final
    unavailable reason (private, removed, members_only, age_restricted,
    premium_only, unavailable). The transcript and frames parts carry their
    own status and attempts.

    A video is enriched completely or not at all: when its transcript must
    wait for the allowance, its frames wait too, so its records change once.
    """

    def __init__(
        self,
        fetcher,
        store,
        transcriber=None,
        *,
        transcripts=False,
        frames=False,
        frame_every=FRAME_EVERY_S,
        source="auto",
        max_seconds=MAX_SECONDS,
        caption_langs=("en",),
        max_bytes=MAX_DOWNLOAD_BYTES,
    ):
        if source not in SOURCES:
            raise ValueError("transcript source must be auto, endpoint or captions")
        self.fetcher, self.store = fetcher, store
        named = transcriber is not None and transcriber.name != "none"
        self.endpoint = transcriber if named else None
        self.transcripts, self.frames = bool(transcripts), bool(frames)
        self.frame_every = max(int(frame_every), 1)
        self.source = source
        self.max_seconds = int(max_seconds)
        self.langs = list(caption_langs)
        self.max_bytes = int(max_bytes)
        self.transient_streak = 0
        self.deferred = False  # The endpoint's minutes (run or day) are used up.

    @property
    def source_now(self):
        if self.source == "auto":
            return "endpoint" if self.endpoint else "captions"
        return self.source

    # What is left to do ----------------------------------------------------------

    def blocked(self, result=None):
        """The endpoint cannot take this audio now (its allowance is used up).

        With a known duration (an earlier outcome's metadata), a video that
        does not fit what is left waits without fetching anything; one that
        could never fit is not blocked, so it reaches its final over_budget.
        """
        if self.source_now != "endpoint" or self.endpoint is None:
            return False
        if self.deferred:
            return True
        remaining = self.endpoint.remaining_s()
        if remaining is None:
            return False
        if remaining <= 0:
            return True
        duration = ((result or {}).get("meta") or {}).get("duration") or 0
        capacity = self.endpoint.capacity_s()
        fits_ever = capacity is None or duration <= capacity
        return fits_ever and duration > remaining

    def todo(self, result, only=None):
        """Parts still to do; `only` restricts which parts may be done now."""
        parts = self.wanted_parts(result, only)
        if "transcript" in parts and self.blocked(result):
            return []  # The whole video waits for the allowance.
        return parts

    def wanted_parts(self, result, only=None):
        wanted = [
            part
            for part, on in (("transcript", self.transcripts), ("frames", self.frames))
            if on and (only is None or part in only)
        ]
        if not wanted or result is None:
            return wanted
        status = result.get("status")
        if status == "deferred":
            return wanted
        if status == "error":
            return wanted if result.get("attempts", 0) < MAX_ATTEMPTS else []
        if status == "too_long":
            duration = (result.get("meta") or {}).get("duration") or 0
            return wanted if duration <= self.max_seconds else []
        if status in AUTH_GATED:
            fresh = self.fetcher.authenticated and not result.get("authenticated")
            return wanted if fresh else []
        if status != "ok":
            return []
        parts = []
        if "transcript" in wanted and self.transcript_needed(result.get("transcript")):
            parts.append("transcript")
        if "frames" in wanted and self.frames_needed(result.get("frames")):
            parts.append("frames")
        return parts

    def transcript_needed(self, part):
        if part is None:
            return True
        status = part.get("status")
        if status == "error":
            return part.get("attempts", 0) < MAX_ATTEMPTS
        source = self.source_now
        if source == "endpoint" and self.endpoint is None:
            return False
        if status == "over_budget" and part.get("source") == source:
            capacity = self.endpoint.capacity_s() if self.endpoint else None
            return capacity is None or capacity >= part.get("duration_s", 0)
        if status in ("ok", "empty"):
            return self.source != "auto" and part.get("source") != source
        return part.get("source") != source  # Another source may succeed.

    def frames_needed(self, part):
        if part is None:
            return True
        if part.get("status") == "error":
            return part.get("attempts", 0) < MAX_ATTEMPTS
        return part.get("status") == "ok" and part.get("every_s") != self.frame_every

    # Work --------------------------------------------------------------------------

    def process(self, video_id, previous=None, only=None):
        """The new outcome, or `previous` unchanged when nothing was due."""
        parts = self.todo(previous, only)
        if not parts:
            return previous
        original = previous
        previous = previous or {}
        status = previous.get("status")
        result = copy.deepcopy(previous) if status == "ok" else {}
        result["video_id"] = video_id
        attempts = previous.get("attempts", 0) + 1 if status == "error" else 1
        try:
            meta = result.get("meta")
            if meta is None and status in ("too_long", "deferred"):
                meta = previous.get("meta")
            if meta is None:
                meta = self.fetcher.info(video_id)
            blocked = gate(meta)
            if blocked is not None:
                raise blocked
        except MediaUnavailable as error:
            self.transient_streak = 0
            return {
                "video_id": video_id,
                "status": error.status,
                "error": str(error),
                "authenticated": self.fetcher.authenticated,
            }
        except MediaTransient as error:
            self.transient_streak += 1
            return {
                "video_id": video_id,
                "status": "unavailable" if attempts >= MAX_ATTEMPTS else "error",
                "error": str(error),
                "attempts": attempts,
            }
        self.transient_streak = 0
        if (meta.get("duration") or 0) > self.max_seconds:
            return {"video_id": video_id, "status": "too_long", "meta": meta}
        result.update(status="ok", meta=meta)
        for key in ("error", "attempts", "authenticated"):
            result.pop(key, None)
        with tempfile.TemporaryDirectory(prefix="throughline-yt-") as tmp:
            tmp = Path(tmp)
            if "transcript" in parts:
                part = self.guard(
                    self.transcript, result.get("transcript"), video_id, meta, tmp / "t"
                )
                if part is DEFERRED:
                    # Nothing changes until the allowance frees up: an earlier
                    # outcome stays as it was, a new video is marked deferred.
                    if original is not None and status == "ok":
                        return original
                    return {"video_id": video_id, "status": "deferred", "meta": meta}
                if part is not None:
                    result["transcript"] = part
            if "frames" in parts:
                part = self.guard(
                    self.capture, result.get("frames"), video_id, meta, tmp / "f"
                )
                if part is not None:
                    result["frames"] = part
        return result

    def guard(self, step, previous, video_id, meta, workdir):
        """Run one part; failures become that part's status, never the video's."""
        source = self.source_now if step == self.transcript else None
        again = previous and previous.get("status") == "error"
        if source and again and previous.get("source") != source:
            again = False
        attempts = previous.get("attempts", 0) + 1 if again else 1
        base = {"source": source} if source else {"every_s": self.frame_every}
        workdir.mkdir(parents=True, exist_ok=True)
        try:
            part = step(video_id, meta, workdir)
        except BudgetExhausted:
            self.deferred = True
            return DEFERRED
        except StoreError:
            raise  # Out of disk or permissions: stop the run, cache nothing.
        except MediaUnavailable as error:
            self.transient_streak = 0
            return dict(base, status="unavailable", error=str(error))
        except MediaTransient as error:
            self.transient_streak += 1
            final = attempts >= MAX_ATTEMPTS
            return dict(
                base,
                status="unavailable" if final else "error",
                error=str(error),
                attempts=attempts,
            )
        except Exception as error:  # endpoint refusal, broken media, a bug
            final = attempts >= MAX_ATTEMPTS
            return dict(
                base,
                status="failed" if final else "error",
                error=type(error).__name__,
                attempts=attempts,
            )
        self.transient_streak = 0
        return part

    def transcript(self, video_id, meta, workdir):
        if self.source_now == "captions":
            choice = pick_captions(meta, self.langs)
            if choice is None:
                return {"status": "no_captions", "source": "captions"}
            kind, lang = choice
            cues = parse_vtt(self.fetcher.captions(video_id, kind, lang, workdir))
            return {
                "status": "ok" if cues else "empty",
                "source": "captions",
                "caption_kind": kind,
                "language": lang,
                "timestamps": True,
                "cues": cues,
            }
        endpoint = self.endpoint
        if endpoint is None:
            raise MediaTransient("no transcription endpoint configured")
        duration = meta.get("duration") or 0
        capacity = endpoint.capacity_s()
        if capacity is not None and duration > capacity:
            # Could never fit: final until the allowance is raised.
            return {
                "status": "over_budget",
                "source": "endpoint",
                "duration_s": duration,
            }
        remaining = endpoint.remaining_s()
        if remaining is not None and duration > remaining:
            raise BudgetExhausted("transcription minutes used up")
        download = self.fetcher.audio(video_id, workdir, self.max_bytes)
        if download is None:
            return {"status": "too_large", "source": "endpoint"}
        sound = av.audio(download, workdir)
        download.unlink(missing_ok=True)
        if sound is None:
            return {"status": "no_audio", "source": "endpoint"}
        data = endpoint.transcript(sound)
        sound.unlink(missing_ok=True)
        if data is None:
            raise MediaTransient("transcriber returned nothing")
        cues = []
        for segment in data.get("segments") or []:
            text = clean_text(segment.get("text"))
            if text:
                start = round(float(segment["start"]), 3)
                end = round(max(float(segment["end"]), start), 3)
                cues.append({"start": start, "end": end, "text": text})
        if not cues and clean_text(data.get("text")):
            end = round(float(meta.get("duration") or 0), 3)
            cues = [{"start": 0.0, "end": end, "text": clean_text(data["text"])}]
        part = {
            "status": "ok" if cues else "empty",
            "source": "endpoint",
            "transcriber": endpoint.name,
            "timestamps": bool(data.get("timestamps")),
            "cues": cues,
        }
        model = getattr(endpoint, "model_name", None) or getattr(
            endpoint, "model", None
        )
        if isinstance(model, str) and model:
            part["model"] = model
        if isinstance(data.get("language"), str) and data["language"]:
            part["language"] = data["language"]
        return part

    def capture(self, video_id, meta, workdir):
        every = self.frame_every
        video = self.fetcher.video(video_id, workdir, self.max_bytes)
        if video is None:
            return {"status": "too_large", "every_s": every}
        framedir = workdir / "frames"
        framedir.mkdir()
        span = meta.get("duration") or self.max_seconds
        limit = math.ceil(span / every) + 1
        items = []
        try:
            for at, path in av.frames(video, framedir, every, limit):
                asset = self.store.put(path.read_bytes(), "jpg")
                items.append(
                    {"at_s": at, "link": asset["link"], "sha256": asset["sha256"]}
                )
        finally:
            video.unlink(missing_ok=True)  # Never keep the video itself.
        return {"status": "ok", "every_s": every, "items": items}
