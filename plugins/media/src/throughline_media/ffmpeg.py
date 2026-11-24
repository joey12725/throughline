"""ffmpeg steps: frames at a fixed interval, speech-rate audio, duration, silence.

Every function takes and returns local paths; nothing here downloads.
"""

import re
import shutil
import subprocess
from pathlib import Path

from .errors import MediaTransient


def ffmpeg():
    binary = shutil.which("ffmpeg")
    if not binary:
        raise MediaTransient("ffmpeg not found on PATH")
    return binary


def frames(video, outdir, every, limit=200):
    """[(at_seconds, path)]: one JPEG every `every` seconds from t=0."""
    outdir = Path(outdir)
    pattern = outdir / "frame_%05d.jpg"
    subprocess.run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(video),
            "-vf",
            f"fps=1/{every}:round=up,scale=w='min(960,iw)':h=-2",
            "-frames:v",
            str(limit),
            "-q:v",
            "4",
            str(pattern),
        ],
        check=True,
        capture_output=True,
        timeout=max(900, int(limit) * every // 4),
    )
    found = sorted(outdir.glob("frame_*.jpg"))
    return [(index * every, path) for index, path in enumerate(found)]


def audio(video, outdir):
    """Mono 16 kHz, 32 kb/s speech audio file, or None without an audio stream."""
    for codec, ext in (("libopus", "ogg"), ("aac", "m4a")):
        target = Path(outdir) / ("audio." + ext)
        result = subprocess.run(
            [
                ffmpeg(),
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(video),
                "-map",
                "0:a:0?",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                codec,
                "-b:a",
                "32k",
                str(target),
            ],
            capture_output=True,
            timeout=3600,
        )
        if result.returncode == 0 and target.exists() and target.stat().st_size > 0:
            return target
        stderr = result.stderr.decode(errors="replace").lower()
        if "does not contain any stream" in stderr or "output file is empty" in stderr:
            return None
        if result.returncode == 0:
            return None  # "0:a:0?" mapped nothing: no audio stream.
    return None


def duration(path):
    """Media duration in seconds, or None when it cannot be measured."""
    probe = shutil.which("ffprobe")
    try:
        if probe:
            result = subprocess.run(
                [
                    probe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                timeout=120,
            )
            value = result.stdout.decode(errors="replace").strip()
            if result.returncode == 0 and value and value != "N/A":
                return float(value)
            return None
        result = subprocess.run(
            [ffmpeg(), "-nostdin", "-hide_banner", "-i", str(path)],
            capture_output=True,
            timeout=120,
        )
    except (MediaTransient, OSError, ValueError, subprocess.TimeoutExpired):
        return None
    match = re.search(
        r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)",
        result.stderr.decode(errors="replace"),
    )
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def silences(path, noise="-35dB", minimum=0.4):
    """[(start, end)] of silent stretches, from ffmpeg silencedetect."""
    result = subprocess.run(
        [
            ffmpeg(),
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise}:d={minimum}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=3600,
    )
    text = result.stderr.decode(errors="replace")
    found, start = [], None
    for line in text.splitlines():
        begin = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if begin:
            start = max(0.0, float(begin.group(1)))
            continue
        end = re.search(r"silence_end:\s*([\d.]+)", line)
        if end and start is not None:
            found.append((start, float(end.group(1))))
            start = None
    return found


def cut(path, start, length, target):
    """Re-encode [start, start+length) of `path` as mono speech audio."""
    target = Path(target)
    subprocess.run(
        [
            ffmpeg(),
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{length:.3f}",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            str(target),
        ],
        check=True,
        capture_output=True,
        timeout=3600,
    )
    return target
