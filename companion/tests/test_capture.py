"""Capture engine: screenshots, idle rules, media pauses, transcription."""

import json
import struct
import wave
from datetime import datetime, timezone

import httpx
import pytest

from throughline_companion.capture import engine as engine_mod
from throughline_companion.capture import local_whisper, media, screen, transcribe
from throughline_companion.capture.engine import CaptureEngine, spool_root
from throughline_companion.config import Config

PIL = pytest.importorskip("PIL.Image")


@pytest.fixture()
def capture(tmp_path, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_COMPANION_HOME", str(tmp_path / "home"))
    config = Config()
    config.data["capture"]["enabled"] = True
    config.data["capture"]["screens"]["enabled"] = True
    config.data["capture"]["device"] = "synthbox"
    eng = CaptureEngine(config)
    state = {"active": True, "color": (10, 20, 30), "window": {"app": "code", "title": "orchard.py"}}

    class Idle:
        backend = "fake"
        threshold_s = 60

        def active(self):
            return state["active"]

    eng.idle = Idle()
    monkeypatch.setattr(eng, "_idle_monitor", lambda: eng.idle)
    monkeypatch.setattr(screen, "displays_on", lambda: True)
    monkeypatch.setattr(screen, "grab", lambda monitors: (PIL.new("RGB", (3200, 1800), state["color"]), "HDMI-A-1"))
    monkeypatch.setattr(engine_mod, "active_window", lambda: state["window"])
    return eng, state, config


def test_screenshot_written_with_context_and_downscaled(capture):
    eng, state, _ = capture
    path = eng.capture_screen()
    assert path and path.suffix == ".jpg"
    meta = json.loads(path.with_suffix(".json").read_text())
    assert meta["app"] == "code" and meta["title"] == "orchard.py" and meta["device"] == "synthbox"
    assert max(meta["width"], meta["height"]) == 1600 and meta["bytes"] == path.stat().st_size


def test_skips_idle_unchanged_excluded_and_display_off(capture, monkeypatch):
    eng, state, _ = capture
    assert eng.capture_screen()
    assert eng.capture_screen() is None and eng.stats["skipped_unchanged"] == 1
    state["active"] = False
    state["color"] = (200, 0, 0)
    assert eng.capture_screen() is None and eng.stats["skipped_idle"] == 1
    state["active"] = True
    state["window"] = {"app": "1Password", "title": "Vault"}
    assert eng.capture_screen() is None and eng.stats["skipped_excluded"] == 1
    state["window"] = {"app": "firefox", "title": "Mail — Private Browsing"}
    assert eng.capture_screen() is None and eng.stats["skipped_excluded"] == 2
    state["window"] = {"app": "firefox", "title": "News"}
    monkeypatch.setattr(screen, "displays_on", lambda: False)
    assert eng.capture_screen() is None and eng.stats["skipped_display_off"] == 1
    monkeypatch.setattr(screen, "displays_on", lambda: True)
    assert eng.capture_screen()
    assert eng.stats["screens"] == 2


def test_spool_limit_stops_capture(capture):
    eng, _, config = capture
    config.data["capture"]["spool_max_mb"] = 0
    (spool_root() / "screens").mkdir(parents=True)
    (spool_root() / "screens" / "x.jpg").write_bytes(b"x" * 10)
    assert eng.capture_screen() is None and "spool full" in eng.last_error


def _wav(path, seconds=1.5):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(struct.pack("<h", 0) * int(16000 * seconds))


def test_transcribed_segment_becomes_a_transcript_and_audio_is_deleted(capture, monkeypatch):
    eng, _, config = capture
    config.data["capture"]["transcription"].update(engine="groq", api_key="gsk-synthetic")
    seen = {}

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        seen["model"] = kwargs["data"]["model"]
        return httpx.Response(200, json={"text": " Synthetic orchard note. ", "language": "en",
                                         "segments": [{"start": 0, "end": 1.4, "text": "Synthetic orchard note."}]})

    monkeypatch.setattr(transcribe.httpx, "post", fake_post)
    folder = spool_root() / "audio"
    folder.mkdir(parents=True)
    wav = folder / "audio-mic-20200101T100000000000Z.wav"
    _wav(wav)
    started = datetime(2020, 1, 1, 10, tzinfo=timezone.utc)
    eng.transcribe_segment(wav, started, None, "mic")
    assert not wav.exists()
    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert seen["model"] == "whisper-large-v3-turbo" and seen["headers"]["Authorization"] == "Bearer gsk-synthetic"
    [out] = list((spool_root() / "transcripts").glob("*.json"))
    item = json.loads(out.read_text())
    assert item["text"] == "Synthetic orchard note." and item["source"] == "mic" and item["engine"] == "groq"
    assert item["ended_at"].startswith("2020-01-01T10:00:01.5")


def test_silence_hallucinations_are_dropped(capture, monkeypatch):
    eng, _, config = capture
    config.data["capture"]["transcription"].update(engine="custom", base_url="http://127.0.0.1:9/v1")
    monkeypatch.setattr(transcribe.httpx, "post", lambda url, **k: httpx.Response(200, json={"text": "Thank you."}))
    folder = spool_root() / "audio"
    folder.mkdir(parents=True)
    wav = folder / "audio-mic-20200101T100000000000Z.wav"
    _wav(wav)
    eng.transcribe_segment(wav, datetime(2020, 1, 1, tzinfo=timezone.utc), None, "mic")
    assert not wav.exists() and not (spool_root() / "transcripts").exists()


def test_deepgram_and_missing_keys():
    with pytest.raises(transcribe.TranscriptionError, match="API key"):
        transcribe.transcribe("x.wav", {"engine": "openai"})
    with pytest.raises(transcribe.TranscriptionError, match="API key"):
        transcribe.transcribe("x.wav", {"engine": "deepgram"})
    with pytest.raises(transcribe.TranscriptionError, match="not installed"):
        transcribe.transcribe("x.wav", {"engine": "local"})


def test_media_policies():
    spotify = [dict(player="spotify", identity="Spotify", title="Song", url="", services=["spotify"])]
    youtube = [dict(player="chromium.instance2", identity="Chromium", title="Talk", url="https://www.youtube.com/watch?v=x", services=["youtube"])]
    mpv = [dict(player="mpv", identity="mpv", title="home video.mp4", url="file:///v.mp4", services=[])]
    assert media.classify("Netflix — Chromium") == ["netflix"]
    assert media.should_pause(["spotify"], spotify)[0]
    assert not media.should_pause(["spotify"], youtube)[0]
    assert media.should_pause(["youtube"], youtube) == (True, "YouTube is playing")
    assert media.should_pause(["streaming"], youtube)[0]
    assert not media.should_pause(["streaming"], mpv)[0]
    assert media.should_pause(["any_media"], mpv)[0]
    assert not media.should_pause([], spotify)[0]
    assert not media.should_pause(["any_media"], [])[0]


def test_local_model_names_from_cache_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_COMPANION_HOME", str(tmp_path))
    models = local_whisper.root() / "models"
    for folder in ("models--Systran--faster-whisper-small", "models--Systran--faster-distil-whisper-large-v3",
                   "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo", "models--Systran--faster-whisper-tiny.en"):
        (models / folder / "snapshots" / "x").mkdir(parents=True)
        (models / folder / "snapshots" / "x" / "model.bin").write_bytes(b"0")
    assert local_whisper.installed_models() == ["distil-large-v3", "large-v3-turbo", "small", "tiny.en"]
    local_whisper.remove("small")
    assert "small" not in local_whisper.installed_models()
    local_whisper.remove()
    assert not local_whisper.root().exists()


def test_capture_settings_via_the_app(tmp_path, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_COMPANION_HOME", str(tmp_path))
    from throughline_companion.app import Companion

    companion = Companion(Config(), isolate=False)
    snap = companion.update_capture({"enabled": True, "screens": {"enabled": True, "interval_s": 60},
                                     "transcription": {"api_key": "secret-key"}})
    assert snap["settings"]["screens"]["interval_s"] == 60
    assert snap["settings"]["transcription"]["api_key"] == "set"
    # The uploader connector is switched on with capture.
    assert companion.config.data["plugins"]["capture"]["enabled"] is True
    assert "secret-key" not in json.dumps(companion.config.redacted())
    # "set" (the dashboard's placeholder) never overwrites the stored key.
    companion.update_capture({"transcription": {"api_key": "set"}})
    assert companion.config.data["capture"]["transcription"]["api_key"] == "secret-key"
    with pytest.raises(ValueError):
        companion.update_capture({"screens": {"bogus": 1}})
