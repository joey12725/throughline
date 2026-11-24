"""Shared media package: synthetic media from ffmpeg and fake HTTP only.

Nothing here downloads real YouTube, Instagram or Discord content: yt-dlp is
replaced by fakes, transcription endpoints by httpx mock transports.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest

from throughline_media import ffmpeg as av
from throughline_media import transcribe as stt
from throughline_media import youtube
from throughline_media.assets import Cache, LocalAssetStore
from throughline_media.errors import BudgetExhausted, MediaTransient, MediaUnavailable
from throughline_media.settings import apply_aliases, parse_flags, truthy
from throughline_media.ytdlp import YtDlp, classify, classify_youtube

needs_ffmpeg = pytest.mark.skipif(
    not shutil.which("ffmpeg"), reason="ffmpeg not on PATH"
)


def tone(path, seconds, quiet=()):
    """A synthetic sine tone, silent during each (start, end): never real speech."""
    volume = "".join(f",volume=enable='between(t,{a},{b})':volume=0" for a, b in quiet)
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}" + volume,
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def pattern(path, seconds):
    """A synthetic test-pattern video without audio."""
    subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=2:duration={seconds}",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


# Assets, cache, settings ----------------------------------------------------------


def test_asset_links_use_the_namespace(tmp_path):
    youtube_store = LocalAssetStore(tmp_path / "a", namespace="youtube")
    stored = youtube_store.put(b"synthetic", "JPG")
    assert stored["link"] == f"throughline-asset://youtube/{stored['sha256']}.jpg"
    assert youtube_store.path(stored["link"]).read_bytes() == b"synthetic"
    assert youtube_store.path("throughline-asset://youtube/../x.jpg") is None
    beeper = LocalAssetStore(tmp_path / "b", namespace="beeper")
    assert beeper.put(b"synthetic", "jpg")["link"].startswith(
        "throughline-asset://beeper/"
    )
    custom = LocalAssetStore(tmp_path / "c", "https://assets.example.invalid/x/")
    assert custom.put(b"z", "png")["link"].startswith(
        "https://assets.example.invalid/x/"
    )


def test_readonly_cache_is_never_created(tmp_path):
    path = tmp_path / "state" / "cache.sqlite3"
    reader = Cache(path, readonly=True)
    assert reader.conn is None and reader.get("k") is None and not path.exists()
    writer = Cache(path)
    writer.put("k", {"status": "ok", "n": [1]})
    writer.close()
    reader = Cache(path, readonly=True)
    assert reader.get("k") == {"n": [1], "status": "ok"}
    reader.close()


def test_flags_truthy_and_aliases():
    flags = {"--transcripts": ("X_TRANSCRIPTS", None), "--limit": ("X_LIMIT", int)}
    assert parse_flags(
        ["backfill", "--transcripts", "--limit", "3", "--once"], flags
    ) == (
        {"X_TRANSCRIPTS": "1", "X_LIMIT": "3"},
        ["backfill", "--once"],
    )
    assert truthy("true") and truthy("1") and not truthy("false") and not truthy(None)
    values = {"url": None, "alias": "https://stt.example.invalid/v1", "model": "m"}
    apply_aliases(values, [("url", "alias"), ("model", "alias")])
    assert values["url"] == "https://stt.example.invalid/v1" and values["model"] == "m"


# Transcription endpoint -----------------------------------------------------------


def test_chunk_plan_prefers_silence_and_overlaps_fixed_cuts():
    assert stt.plan(50, 60) == [(0.0, 50.0, 0.0, 50.0)]
    fixed = stt.plan(150, 60, overlap=2)
    assert [p[2:] for p in fixed] == [(0.0, 60.0), (60.0, 120.0), (120.0, 150.0)]
    assert fixed[0][:2] == (0.0, 62.0) and fixed[1][:2] == (58.0, 122.0)
    quiet = stt.plan(150, 60, quiet=[(55.0, 58.0), (110.0, 112.0)], overlap=2)
    assert [round(p[2], 1) for p in quiet] == [0.0, 56.5, 111.0]
    # Cut at a silence: no overlap needed on either side of it.
    assert quiet[0][1] == 56.5 and quiet[1][0] == 56.5


def endpoint(handler, **options):
    options.setdefault("sleep", lambda s: None)
    return stt.OpenAICompatible(
        "https://stt.example.invalid/v1/",
        "fake-key",
        "fake-model",
        transport=httpx.MockTransport(handler),
        **options,
    )


def form(request):
    body = request.content
    return {
        name: value
        for name in ("model", "response_format")
        for value in [
            body.split(f'name="{name}"'.encode(), 1)[1]
            .split(b"\r\n\r\n", 1)[1]
            .split(b"\r\n", 1)[0]
            .decode()
        ]
    }


def test_endpoint_verbose_json_segments_and_auth(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        assert form(request) == {
            "model": "fake-model",
            "response_format": "verbose_json",
        }
        return httpx.Response(
            200,
            json={
                "text": "invented one invented two",
                "language": "english",
                "segments": [
                    {"start": 0.0, "end": 2.5, "text": " invented one"},
                    {"start": 2.5, "end": 4.0, "text": "invented two "},
                ],
            },
        )

    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake-audio")
    result = endpoint(handler).transcript(audio)
    assert result["segments"] == [
        {"start": 0.0, "end": 2.5, "text": "invented one"},
        {"start": 2.5, "end": 4.0, "text": "invented two"},
    ]
    assert result["timestamps"] and result["language"] == "english"
    assert seen[0].url.path == "/v1/audio/transcriptions"
    assert seen[0].headers["Authorization"] == "Bearer fake-key"


def test_endpoint_falls_back_to_plain_json_once(tmp_path):
    formats = []

    def handler(request):
        formats.append(form(request)["response_format"])
        if formats[-1] == "verbose_json":
            return httpx.Response(400, json={"error": "unsupported response_format"})
        return httpx.Response(200, json={"text": " plain words "})

    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake-audio")
    client = endpoint(handler)
    assert client.transcribe(audio) == "plain words"
    assert client.transcript(audio)["timestamps"] is False
    assert formats == ["verbose_json", "json", "json"]  # Remembered per server.


def test_endpoint_retries_with_backoff_then_gives_up(tmp_path):
    calls, delays = [], []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if len(calls) == 2:
            return httpx.Response(503)
        if len(calls) == 3:
            raise httpx.ReadTimeout("invented timeout", request=request)
        return httpx.Response(200, json={"text": "finally"})

    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"fake-audio")
    client = endpoint(handler, sleep=delays.append)
    assert client.transcribe(audio) == "finally"
    assert delays == [7.0, 2.0, 4.0]

    def busy(request):
        return httpx.Response(502)

    with pytest.raises(MediaTransient):
        endpoint(busy, retries=2).transcribe(audio)

    def refused(request):
        return httpx.Response(401, json={"error": "invented"})

    with pytest.raises(RuntimeError, match="401"):
        endpoint(refused).transcribe(audio)


def test_endpoint_from_settings():
    assert stt.endpoint_from({}, {}) is None
    client = stt.endpoint_from(
        {
            "transcribe_url": "https://stt.example.invalid/v1",
            "transcribe_max_bytes": "1000",
            "transcribe_max_minutes": "0",
        },
        {"transcribe_api_key": "fake-key"},
    )
    assert client.model == "whisper-1" and client.max_bytes == 1000
    assert client.remaining_s() is None
    defaults = {name: default for name, _, default, _ in stt.SETTINGS}
    stt.validate(defaults)
    with pytest.raises(ValueError, match="THROUGHLINE_TRANSCRIBE_RETRIES"):
        stt.validate(dict(defaults, transcribe_retries="-1"))


def probe_segments(request, step=10.0):
    """A fake server: ffprobe the uploaded chunk, one segment per `step` s."""
    _, blob = request.content.split(b'name="file"', 1)
    blob = blob.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "upload.ogg"
        path.write_bytes(blob)
        seconds = av.duration(path)
    segments, at, index = [], 0.0, 0
    while at < seconds - 0.05:
        end = min(at + step, seconds)
        segments.append({"start": at, "end": end, "text": f"invented part {index}"})
        at, index = end, index + 1
    return httpx.Response(
        200,
        json={"text": " ".join(s["text"] for s in segments), "segments": segments},
    )


@needs_ffmpeg
def test_long_audio_split_under_the_upload_limit_at_silences(tmp_path):
    audio = tone(tmp_path / "long.ogg", 150, quiet=[(55, 58), (112, 115)])
    sizes = []

    def handler(request):
        sizes.append(len(request.content))
        return probe_segments(request)

    # An upload limit that holds about 60 s of this audio.
    limit = int(audio.stat().st_size / av.duration(audio) * 60 / 0.9)
    client = endpoint(handler, max_bytes=limit, chunk_seconds=None)
    assert audio.stat().st_size > limit
    result = client.transcript(audio)
    assert result["chunks"] == 3 and len(sizes) == 3
    assert all(size < limit + 2_000 for size in sizes)
    starts = [s["start"] for s in result["segments"]]
    assert starts == sorted(starts) and starts[0] == 0.0
    # Absolute timeline: the second chunk begins at the first silence (~56.5 s).
    second = next(s for s in result["segments"] if s["start"] > 50)
    assert 55 <= second["start"] <= 58
    assert result["segments"][-1]["end"] == pytest.approx(150, abs=0.5)
    for a, b in zip(result["segments"], result["segments"][1:]):
        assert a["end"] <= b["start"] + 0.01
    assert client.used_s == pytest.approx(150, abs=0.5)


@needs_ffmpeg
def test_fixed_length_chunks_overlap_and_merge_without_duplicates(tmp_path):
    audio = tone(tmp_path / "steady.ogg", 130)

    def handler(request):
        return probe_segments(request, step=5.0)

    client = endpoint(handler, chunk_seconds=60, overlap_s=2.0)
    result = client.transcript(audio)
    assert result["chunks"] == 3
    middles = [(s["start"] + s["end"]) / 2 for s in result["segments"]]
    assert middles == sorted(middles)
    # Overlapping chunk edges are kept once: no two segments cover one moment.
    for a, b in zip(result["segments"], result["segments"][1:]):
        assert b["start"] >= a["end"] - 2.5
    assert len(middles) == len({round(m) for m in middles})


@needs_ffmpeg
@pytest.mark.parametrize("step,verbose", [(3.0, True), (5.0, True), (5.0, False)])
def test_fixed_cuts_keep_every_word_once(tmp_path, step, verbose):
    """A fake server that "hears" one invented word per absolute second.

    At a fixed cut both chunks hear the 2 s overlap; the merged transcript
    must hold every word exactly once, in order, with or without segments.
    """
    audio = tone(tmp_path / "steady.ogg", 130)
    pieces = stt.plan(130, 60, overlap=2.0)
    calls = []

    def handler(request):
        fmt = form(request)["response_format"]
        if fmt == "verbose_json" and not verbose:
            return httpx.Response(400, json={"error": "unsupported"})
        begin, end = pieces[len(calls)][:2]
        calls.append(fmt)
        words = [(s, f"w{s}") for s in range(int(begin), int(round(end)))]
        segments, at = [], begin
        while at < end - 0.05:
            stop = min(at + step, end)
            said = [w for s, w in words if at <= s < stop]
            if said:
                segments.append(
                    {"start": at - begin, "end": stop - begin, "text": " ".join(said)}
                )
            at = stop
        body = {"text": " ".join(w for _, w in words)}
        if verbose:
            body["segments"] = segments
        return httpx.Response(200, json=body)

    result = endpoint(handler, chunk_seconds=60, overlap_s=2.0).transcript(audio)
    assert len(calls) == 3
    assert result["text"].split() == [f"w{s}" for s in range(130)]
    starts = [s["start"] for s in result["segments"]]
    assert starts == sorted(starts)
    for a, b in zip(result["segments"], result["segments"][1:]):
        assert a["end"] <= b["start"] + 0.01
    if verbose:
        # Absolute times: each segment starts at (or after) its first word.
        for segment in result["segments"]:
            first = int(segment["text"].split()[0][1:])
            assert segment["start"] <= first + 0.01 <= segment["end"] + 1


def test_trim_repeat():
    assert stt.trim_repeat("so we went. Then we", "then we left") == "left"
    assert stt.trim_repeat("a b c", "c d") == "c d"  # One word is a coincidence.
    assert stt.trim_repeat("", "x y") == "x y"
    assert stt.trim_repeat("x y", "x y") == ""


@needs_ffmpeg
def test_budget_caps_audio_minutes_per_run(tmp_path):
    audio = tone(tmp_path / "a.ogg", 90)

    def handler(request):
        return httpx.Response(200, json={"text": "invented"})

    client = endpoint(handler, max_minutes=2)
    client.transcript(audio)
    assert client.remaining_s() == pytest.approx(30, abs=0.5)
    with pytest.raises(BudgetExhausted):
        client.transcript(audio)


# ffmpeg ---------------------------------------------------------------------------


@needs_ffmpeg
def test_frames_at_a_parameter_interval_and_audio(tmp_path):
    video = pattern(tmp_path / "v.mp4", 150)
    (tmp_path / "f").mkdir()
    found = av.frames(video, tmp_path / "f", 60, limit=10)
    assert [at for at, _ in found] == [0, 60, 120]
    assert all(p.read_bytes()[:2] == b"\xff\xd8" for _, p in found)
    assert av.audio(video, tmp_path) is None  # No audio stream.
    assert av.duration(video) == pytest.approx(150, abs=0.6)
    sound = tone(tmp_path / "t.ogg", 10, quiet=[(4, 6)])
    assert [(round(a), round(b)) for a, b in av.silences(sound)] == [(4, 6)]


# yt-dlp ---------------------------------------------------------------------------


def test_classifiers():
    for stderr, status in [
        (
            "ERROR: [youtube] x: Private video. Sign in if you've been granted access",
            "private",
        ),
        (
            "ERROR: Video unavailable. This video has been removed by the uploader",
            "removed",
        ),
        (
            "ERROR: Join this channel to get access to members-only content",
            "members_only",
        ),
        (
            "ERROR: Sign in to confirm your age. This video may be inappropriate for some users.",
            "age_restricted",
        ),
    ]:
        with pytest.raises(MediaUnavailable) as caught:
            classify_youtube(stderr)
        assert caught.value.status == status
    for stderr in [
        "ERROR: Sign in to confirm you're not a bot",
        "ERROR: HTTP Error 429: Too Many Requests",
        "ERROR: something new",
    ]:
        with pytest.raises(MediaTransient):
            classify_youtube(stderr)
    with pytest.raises(MediaUnavailable):
        classify("ERROR: This content is private")


def test_runner_command_and_fetcher_arguments(monkeypatch, tmp_path):
    calls = []
    info = {
        "id": "FAKEVIDEO01",
        "title": "Invented",
        "duration": 61.5,
        "formats": [{"url": "https://media.example.invalid/x"}],
        "subtitles": {"en": [{}]},
        "automatic_captions": {"en-orig": [{}], "fr": [{}]},
        "timestamp": 1577880000,
    }

    def fake_run(command, capture_output, timeout):
        calls.append(command)
        if "--dump-single-json" in command:
            return subprocess.CompletedProcess(
                command, 0, json.dumps(info).encode(), b""
            )
        template = Path(command[command.index("-o") + 1])
        if "--write-subs" in command:
            Path(str(template).replace("%(ext)s", "en.vtt")).write_text(
                "WEBVTT\n\n00:00.000 --> 00:01.000\ninvented\n"
            )
        else:
            Path(str(template).replace("%(ext)s", "webm")).write_bytes(b"fake")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = YtDlp(
        cookies="cookies.txt",
        command=["python", "fake-yt-dlp"],
        classify=classify_youtube,
    )
    fetcher = youtube.YouTubeFetcher(runner)
    meta = fetcher.info("FAKEVIDEO01")
    assert meta == {
        "id": "FAKEVIDEO01",
        "title": "Invented",
        "duration": 61.5,
        "timestamp": 1577880000,
        "subtitles": ["en"],
        "automatic_captions": ["en-orig", "fr"],
    }
    assert calls[0][:7] == [
        "python",
        "fake-yt-dlp",
        "--ignore-config",
        "--no-warnings",
        "--no-progress",
        "--cookies",
        "cookies.txt",
    ]
    assert calls[0][-1] == "https://www.youtube.com/watch?v=FAKEVIDEO01"
    assert "invented" in fetcher.captions("FAKEVIDEO01", "manual", "en", tmp_path)
    assert fetcher.audio("FAKEVIDEO01", tmp_path).name == "audio.webm"
    assert calls[-1][calls[-1].index("-f") + 1].startswith("ba/")
    assert fetcher.video("FAKEVIDEO01", tmp_path).name == "video.webm"
    assert "bv[height<=480]" in calls[-1][calls[-1].index("-f") + 1]
    assert fetcher.authenticated


# YouTube pieces -------------------------------------------------------------------


def test_youtube_link_detection():
    text = (
        "a https://www.youtube.com/watch?v=FAKEVIDEO01&t=30s b youtu.be/FAKEVIDEO02 "
        "c https://m.youtube.com/watch?feature=share&v=FAKEVIDEO03 "
        "d youtube.com/shorts/FAKEVIDEO04 e https://www.youtube.com/live/FAKEVIDEO05?si=x "
        "f https://notyoutube.com/watch?v=FAKEVIDEO06 g https://youtu.be/FAKEVIDEO01 "
        "h https://www.youtube.com/watch?v=TOOSHORT i https://www.youtube.com/@invented"
    )
    assert youtube.youtube_links(text) == [
        ("https://www.youtube.com/watch?v=FAKEVIDEO01", "watch", "FAKEVIDEO01"),
        ("https://www.youtube.com/watch?v=FAKEVIDEO02", "watch", "FAKEVIDEO02"),
        ("https://www.youtube.com/watch?v=FAKEVIDEO03", "watch", "FAKEVIDEO03"),
        ("https://www.youtube.com/watch?v=FAKEVIDEO04", "short", "FAKEVIDEO04"),
        ("https://www.youtube.com/watch?v=FAKEVIDEO05", "live", "FAKEVIDEO05"),
    ]
    assert youtube.watch_url("FAKEVIDEO01", 300.0) == (
        "https://www.youtube.com/watch?v=FAKEVIDEO01&t=300s"
    )
    assert youtube.clock(4500) == "75:00" and youtube.clock(61) == "01:01"


def test_vtt_manual_and_rolling_automatic_captions():
    manual = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n1\n00:00:01.000 --> 00:00:03.500\n"
        "Invented &amp; <i>spoken</i>\nline\n\n00:00:04.000 --> 00:00:05.000\nnext\n"
    )
    assert youtube.parse_vtt(manual) == [
        {"start": 1.0, "end": 3.5, "text": "Invented & spoken line"},
        {"start": 4.0, "end": 5.0, "text": "next"},
    ]
    rolling = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000 align:start position:0%\n"
        "invented<00:00:00.500><c> words</c>\n\n"
        "00:00:02.000 --> 00:00:02.010\ninvented words\n\n"
        "00:00:02.010 --> 00:00:04.000\ninvented words\nmore<00:00:03.000><c> speech</c>\n\n"
        "00:00:04.000 --> 00:00:04.010\nmore speech\n\n"
        "00:00:04.010 --> 00:00:06.000\nmore speech\nthe end\n"
    )
    assert [c["text"] for c in youtube.parse_vtt(rolling)] == [
        "invented words",
        "more speech",
        "the end",
    ]


def test_caption_choice_order():
    meta = {
        "subtitles": ["de"],
        "automatic_captions": ["de-orig", "en"],
        "language": "de",
    }
    assert youtube.pick_captions(meta, ["en"]) == ("manual", "de")
    meta = {"subtitles": ["en-GB", "live_chat"], "automatic_captions": ["en"]}
    assert youtube.pick_captions(meta, ["en"]) == ("manual", "en-GB")
    meta = {
        "subtitles": ["es"],
        "automatic_captions": ["en-orig", "es"],
        "language": "en",
    }
    assert youtube.pick_captions(meta, ["fr"]) == ("automatic", "en-orig")
    assert youtube.pick_captions({"subtitles": ["live_chat"]}, ["en"]) is None


def test_windows_split_cues_and_frames():
    cues = [
        {"start": 1.0, "end": 4.0, "text": "a"},
        {"start": 59.0, "end": 62.0, "text": "b"},
        {"start": 61.0, "end": 63.0, "text": "c"},
    ]
    frames = [
        {"at_s": 0, "link": "x0"},
        {"at_s": 60, "link": "x1"},
        {"at_s": 120, "link": "x2"},
    ]
    out = youtube.windows(cues, frames, 150.0, 60)
    assert [(w["start"], w["end"], w["text"]) for w in out] == [
        (0, 60, "a b"),
        (60, 120, "c"),
        (120, 150.0, ""),
    ]
    assert [[f["link"] for f in w["frames"]] for w in out] == [["x0"], ["x1"], ["x2"]]


# Processor ------------------------------------------------------------------------


class FakeFetcher:
    """Stands in for YouTubeFetcher: synthetic media made with ffmpeg."""

    def __init__(self, videos, authenticated=False):
        self.videos, self.calls, self.authenticated = videos, [], authenticated
        self.paths = []

    def outcome(self, video):
        value = self.videos[video]
        if isinstance(value, Exception):
            raise value
        return value

    def info(self, video):
        self.calls.append(("info", video))
        return dict(self.outcome(video)["meta"])

    def captions(self, video, kind, lang, workdir):
        self.calls.append(("captions", video, kind, lang))
        return self.outcome(video)["vtt"]

    def audio(self, video, workdir, max_bytes):
        self.calls.append(("audio", video))
        target = Path(workdir) / "dl"
        target.mkdir()
        path = tone(target / "audio.ogg", self.outcome(video)["meta"]["duration"])
        self.paths.append(path)
        return path

    def video(self, video, workdir, max_bytes):
        self.calls.append(("video", video))
        target = Path(workdir) / "dl"
        target.mkdir()
        path = pattern(target / "video.mp4", self.outcome(video)["meta"]["duration"])
        self.paths.append(path)
        return path


VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\ninvented caption\n\n01:05.000 --> 01:07.000\nlater words\n"


def meta(**extra):
    return dict(
        {
            "id": "FAKEVIDEO01",
            "title": "Invented",
            "duration": 130,
            "subtitles": ["en"],
            "timestamp": 1577880000,
        },
        **extra,
    )


@needs_ffmpeg
def test_processor_captions_frames_cached_and_video_deleted(tmp_path):
    fetcher = FakeFetcher({"FAKEVIDEO01": {"meta": meta(), "vtt": VTT}})
    store = LocalAssetStore(tmp_path / "assets", namespace="youtube")
    processor = youtube.Processor(fetcher, store, transcripts=True, frames=True)
    result = processor.process("FAKEVIDEO01")
    assert result["status"] == "ok"
    assert result["transcript"]["source"] == "captions"
    assert result["transcript"]["caption_kind"] == "manual"
    assert [c["text"] for c in youtube.cues_of(result)] == [
        "invented caption",
        "later words",
    ]
    assert [f["at_s"] for f in youtube.frames_of(result)] == [0, 60, 120]
    assert all(
        f["link"].startswith("throughline-asset://youtube/")
        for f in youtube.frames_of(result)
    )
    assert not any(p.exists() for p in fetcher.paths)  # Never keep the video.
    assert processor.todo(result) == []
    assert processor.process("FAKEVIDEO01", result) is result
    assert [c[0] for c in fetcher.calls] == ["info", "captions", "video"]
    # A new interval redoes only the frames; the cached metadata is reused.
    faster = youtube.Processor(fetcher, store, frames=True, frame_every=30)
    again = faster.process("FAKEVIDEO01", result)
    assert [f["at_s"] for f in youtube.frames_of(again)] == [0, 30, 60, 90, 120]
    assert again["transcript"] == result["transcript"]
    assert [c[0] for c in fetcher.calls][-1] == "video" and len(fetcher.calls) == 4


@needs_ffmpeg
def test_processor_endpoint_source_and_budget(tmp_path):
    class Fake(stt.Transcriber):
        name = "fake-endpoint"
        model = "fake-model"

        def __init__(self, remaining=None):
            self.calls, self.remaining = 0, remaining

        def remaining_s(self):
            return self.remaining

        def transcript(self, path):
            self.calls += 1
            assert av.duration(path) == pytest.approx(130, abs=0.5)
            return {
                "text": "spoken",
                "segments": [{"start": 70.0, "end": 72.0, "text": " spoken\x00 "}],
                "timestamps": True,
            }

    fetcher = FakeFetcher({"FAKEVIDEO01": {"meta": meta(), "vtt": VTT}})
    store = LocalAssetStore(tmp_path / "assets", namespace="youtube")
    transcriber = Fake()
    result = youtube.Processor(fetcher, store, transcriber, transcripts=True).process(
        "FAKEVIDEO01"
    )
    assert result["transcript"]["source"] == "endpoint"
    assert result["transcript"]["model"] == "fake-model"
    assert result["transcript"]["cues"] == [
        {"start": 70.0, "end": 72.0, "text": "spoken"}
    ]
    assert ("audio", "FAKEVIDEO01") in fetcher.calls and (
        "video",
        "FAKEVIDEO01",
    ) not in fetcher.calls
    # Auto keeps the cached endpoint transcript; explicit captions replace it.
    captions = youtube.Processor(
        fetcher, store, transcriber, transcripts=True, source="captions"
    )
    assert captions.todo(result) == ["transcript"]
    # The per-run allowance defers work without recording a failure; a
    # deferred outcome is not rendered and is redone once minutes free up.
    short = youtube.Processor(fetcher, store, Fake(remaining=60), transcripts=True)
    deferred = short.process("FAKEVIDEO01")
    assert deferred == {
        "video_id": "FAKEVIDEO01",
        "status": "deferred",
        "meta": meta(),
    }
    assert youtube.pending(deferred)
    assert short.deferred and short.todo(None) == []
    roomy = youtube.Processor(fetcher, store, Fake(), transcripts=True)
    assert roomy.todo(deferred) == ["transcript"]
    calls = len(fetcher.calls)
    done = roomy.process("FAKEVIDEO01", deferred)
    assert done["status"] == "ok" and done["transcript"]["source"] == "endpoint"
    assert ("info", "FAKEVIDEO01") not in fetcher.calls[calls:]  # Meta reused.
    # A video longer than the most the allowance can ever hold is final.
    tiny = Fake(remaining=60)
    tiny.capacity_s = lambda: 60
    capped = youtube.Processor(fetcher, store, tiny, transcripts=True)
    over = capped.process("FAKEVIDEO01")
    assert over["status"] == "ok" and over["transcript"] == {
        "status": "over_budget",
        "source": "endpoint",
        "duration_s": 130,
    }
    assert capped.todo(over) == []
    tiny.capacity_s = lambda: 600  # Raised, but only 60 s are left today:
    assert capped.todo(over) == []  # it waits without fetching anything.
    tiny.remaining = None
    assert capped.todo(over) == ["transcript"]


def test_processor_gates_limits_and_transient_attempts(tmp_path):
    store = LocalAssetStore(tmp_path / "assets", namespace="youtube")
    fetcher = FakeFetcher(
        {
            "FAKEPRIVATE": MediaUnavailable("private", "private video"),
            "FAKELONGONE": {"meta": meta(duration=99999)},
            "FAKEMEMBERS": {"meta": meta(availability="subscriber_only")},
            "FAKEFLAKY01": MediaTransient("bot check"),
            "FAKENOCAPS1": {"meta": meta(subtitles=[])},
        }
    )
    processor = youtube.Processor(fetcher, store, transcripts=True, max_seconds=3600)
    private = processor.process("FAKEPRIVATE")
    assert private["status"] == "private" and processor.todo(private) == []
    signed_in = youtube.Processor(
        FakeFetcher({}, authenticated=True), store, transcripts=True
    )
    assert signed_in.todo(private) == ["transcript"]  # Cookies may get past it.
    long = processor.process("FAKELONGONE")
    assert long["status"] == "too_long" and processor.todo(long) == []
    raised = youtube.Processor(fetcher, store, transcripts=True, max_seconds=100000)
    assert raised.todo(long) == ["transcript"]
    assert processor.process("FAKEMEMBERS")["status"] == "members_only"
    outcome = None
    for attempt in range(1, 4):
        outcome = processor.process("FAKEFLAKY01", outcome)
        assert outcome["attempts"] == attempt
    assert outcome["status"] == "unavailable" and processor.todo(outcome) == []
    assert youtube.pending({"status": "error"}) and not youtube.pending(outcome)
    nocaps = processor.process("FAKENOCAPS1")
    assert nocaps["transcript"] == {"status": "no_captions", "source": "captions"}
    assert processor.transient_streak == 0
