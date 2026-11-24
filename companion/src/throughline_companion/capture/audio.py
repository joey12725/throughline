"""Audio capture: the microphone and, where the OS allows, what the computer
plays (Windows WASAPI loopback, Linux PulseAudio/PipeWire monitor; macOS
needs a loopback device such as BlackHole selected as a microphone).

Needs the audio extra (soundcard + numpy). Speech is cut into segments by a
simple energy voice-activity detector with an adaptive noise floor: a
segment starts on speech, ends after `silence_s` of quiet or at `max_s`,
and is dropped under `min_s`. Segments are 16 kHz mono 16-bit WAV files in
the spool, deleted after transcription. Silence is never written.
"""

import logging
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("throughline_companion.capture.audio")

RATE = 16000
BLOCK_S = 0.5


def available():
    try:
        import numpy  # noqa: F401
        import soundcard  # noqa: F401
    except Exception:  # noqa: BLE001 - missing extra or no audio server
        return False
    return True


def devices():
    import soundcard as sc

    mics = [dict(id=m.id, name=m.name, loopback=False) for m in sc.all_microphones(include_loopback=False)]
    loops = []
    try:
        loops = [dict(id=m.id, name=m.name, loopback=True) for m in sc.all_microphones(include_loopback=True) if m.isloopback]
    except Exception:  # noqa: BLE001
        pass
    return mics + loops


def _open(source, device_id=None):
    import soundcard as sc

    if source == "system":
        speaker = sc.default_speaker()
        return sc.get_microphone(device_id or speaker.id, include_loopback=True)
    return sc.get_microphone(device_id) if device_id else sc.default_microphone()


class Recorder:
    """Records one source in a thread and hands finished segments to
    on_segment(path, started_at, ended_at, source). `paused()` is checked for
    every block: while it returns a reason nothing is recorded."""

    def __init__(self, source, spool, on_segment, paused, *, device_id=None,
                 min_s=1.0, max_s=30.0, silence_s=1.2, sensitivity=3.0):
        self.source = source
        self.spool = Path(spool)
        self.on_segment = on_segment
        self.paused = paused
        self.device_id = device_id
        self.min_s, self.max_s, self.silence_s = min_s, max_s, silence_s
        self.sensitivity = sensitivity
        self.stopping = threading.Event()
        self.thread = None
        self.state = "stopped"
        self.pause_reason = None
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name=f"audio-{self.source}", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()

    def _run(self):
        import numpy as np

        backoff = 5
        while not self.stopping.is_set():
            try:
                mic = _open(self.source, self.device_id)
                with mic.recorder(samplerate=RATE, channels=1, blocksize=int(RATE * BLOCK_S)) as rec:
                    self.state, self.error, backoff = "listening", None, 5
                    self._loop(rec, np)
            except Exception as error:  # noqa: BLE001
                self.state, self.error = "error", f"{type(error).__name__}: {error}"[:300]
                log.warning("audio %s: %s", self.source, self.error)
                self.stopping.wait(backoff)
                backoff = min(backoff * 2, 300)
        self.state = "stopped"

    def _loop(self, rec, np):
        floor = None
        frames, started, quiet = [], None, 0.0
        while not self.stopping.is_set():
            block = rec.record(numframes=int(RATE * BLOCK_S))
            reason = self.paused()
            if reason:
                self.state, self.pause_reason = "paused", reason
                frames, started, quiet = [], None, 0.0
                continue
            self.state, self.pause_reason = "listening", None
            mono = block.reshape(-1).astype("float32")
            rms = float(np.sqrt(np.mean(mono * mono)) + 1e-9)
            floor = rms if floor is None else (min(floor * 1.02, rms) if rms < floor * 4 else floor * 1.001)
            speaking = rms > max(floor * self.sensitivity, 0.004)
            now = datetime.now(timezone.utc)
            if speaking:
                if started is None:
                    started = now
                frames.append(mono)
                quiet = 0.0
            elif started is not None:
                frames.append(mono)
                quiet += BLOCK_S
            length = len(frames) * BLOCK_S
            if started is not None and (quiet >= self.silence_s or length >= self.max_s):
                if length - quiet >= self.min_s:
                    self._write(np.concatenate(frames), started, now)
                frames, started, quiet = [], None, 0.0

    def _write(self, samples, started, ended):
        import numpy as np

        self.spool.mkdir(parents=True, exist_ok=True)
        stamp = started.strftime("%Y%m%dT%H%M%S%fZ")
        path = self.spool / f"audio-{self.source}-{stamp}.wav"
        pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(RATE)
            out.writeframes(pcm)
        try:
            self.on_segment(path, started, ended, self.source)
        except Exception as error:  # noqa: BLE001
            log.warning("segment handler failed: %s", error)


def wav_seconds(path):
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def now_ms():
    return int(time.time() * 1000)
