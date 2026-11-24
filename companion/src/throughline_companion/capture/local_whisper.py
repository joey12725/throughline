"""On-device transcription with faster-whisper, installed only on request.

Nothing is bundled: `install(model)` (the dashboard's "Download" button or
`throughline-companion capture install-local MODEL`) creates a private Python
environment in the companion's data folder with uv (the one on PATH, else a
pinned, checksum-verified uv download), installs faster-whisper there and
downloads the chosen model. `remove()` deletes it all again. Transcription
then runs in a long-lived worker process of that environment, so the
companion itself stays small and works the same when packaged.
"""

import hashlib
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
import zipfile
from pathlib import Path

from .. import paths

log = logging.getLogger("throughline_companion.capture.local")

FASTER_WHISPER = "faster-whisper==1.2.1"
# PyAV 19 dropped the metadata_errors argument faster-whisper 1.2.1 passes.
PYAV = "av<19"
UV_VERSION = "0.13.9"
UV_ASSETS = {
    ("linux", "x86_64"): ("uv-x86_64-unknown-linux-gnu.tar.gz", "a7fbaff2b9a271d85c1bdb540941090fd1ce011797b4abeb48e4dcd39c2471d5"),
    ("linux", "arm64"): ("uv-aarch64-unknown-linux-gnu.tar.gz", "0797542018e541a9288855df555c8077eed06f7a47d206362030480e8ecc9f3d"),
    ("macos", "x86_64"): ("uv-x86_64-apple-darwin.tar.gz", "617a3211261b0fc739562f0071aa34e9739797c510208ede8e2977c30903d041"),
    ("macos", "arm64"): ("uv-aarch64-apple-darwin.tar.gz", "5aeca67f2283f75235de40d0bd9c0a5bbc5fce23b6d48b3bf9fa1b2db08382ca"),
    ("windows", "x86_64"): ("uv-x86_64-pc-windows-msvc.zip", "edd61f7f5728a56cc62455386661860bdfc7bab65872f39b0c25bdcc1d8e1beb"),
    ("windows", "arm64"): ("uv-aarch64-pc-windows-msvc.zip", "72eed595983a4d7d22d79f21065b0f4479cc7e5c7a89104bb410c8f47559a878"),
}
# Download sizes of the CTranslate2 models faster-whisper fetches.
MODELS = {
    "tiny": "75 MB",
    "base": "145 MB",
    "small": "485 MB",
    "medium": "1.5 GB",
    "large-v3-turbo": "1.6 GB",
    "distil-large-v3": "1.5 GB",
    "tiny.en": "75 MB",
    "base.en": "145 MB",
    "small.en": "485 MB",
}

WORKER = r'''
import json, sys
from faster_whisper import WhisperModel, download_model

def main():
    root, model, device = sys.argv[1], sys.argv[2], sys.argv[3]
    if len(sys.argv) > 4 and sys.argv[4] == "--download":
        download_model(model, cache_dir=root)
        print(json.dumps({"ok": True}), flush=True)
        return
    compute = "int8" if device == "cpu" else "default"
    whisper = WhisperModel(model, device=device, compute_type=compute, download_root=root, local_files_only=True)
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        job = json.loads(line)
        try:
            segments, info = whisper.transcribe(job["path"], language=job.get("language"), vad_filter=True, beam_size=1)
            items = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
            print(json.dumps({"ok": True, "text": " ".join(i["text"] for i in items).strip(),
                              "language": info.language, "segments": items}), flush=True)
        except Exception as error:
            print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"[:300]}), flush=True)

main()
'''


def root():
    return paths.data_dir() / "local-whisper"


def _arch():
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "x86_64"


def _python():
    venv = root() / "venv"
    return venv / ("Scripts/python.exe" if paths.platform() == "windows" else "bin/python")


def _model_of(directory):
    """The model name of a faster-whisper cache folder, or None."""
    name = directory.name.replace("distil-whisper-", "whisper-distil-")
    for model in sorted(MODELS, key=len, reverse=True):
        if name.endswith("whisper-" + model):
            return model
    return None


def installed_models():
    models = root() / "models"
    if not models.exists():
        return []
    found = set()
    for item in models.iterdir():
        model = _model_of(item) if item.is_dir() else None
        if model and any(item.rglob("model.bin")):
            found.add(model)
    return sorted(found)


def status():
    return dict(
        runtime=_python().exists(),
        models=installed_models(),
        available=MODELS,
        folder=str(root()),
        bytes=_size(root()),
    )


def _size(path):
    total = 0
    if path.exists():
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    pass
    return total


def _uv():
    found = shutil.which("uv")
    if found:
        return found
    target = root() / "bin" / ("uv.exe" if paths.platform() == "windows" else "uv")
    if target.exists():
        return str(target)
    asset = UV_ASSETS.get((paths.platform(), _arch()))
    if not asset:
        raise RuntimeError("no uv build for this computer; install uv and try again")
    name, digest = asset
    url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{name}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "throughline-companion"}), timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError("uv download failed its checksum")
    target.parent.mkdir(parents=True, exist_ok=True)
    binary = "uv.exe" if paths.platform() == "windows" else "uv"
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            member = next(m for m in bundle.namelist() if m.endswith(binary))
            target.write_bytes(bundle.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as bundle:
            member = next(m for m in bundle.getmembers() if m.name.endswith("/" + binary) or m.name == binary)
            target.write_bytes(bundle.extractfile(member).read())
    os.chmod(target, 0o755)
    return str(target)


def install(model="small", progress=None):
    """Install the runtime (once) and download one model."""
    if model not in MODELS:
        raise ValueError("unknown model; one of " + ", ".join(MODELS))
    base = root()
    base.mkdir(parents=True, exist_ok=True)
    say = progress or (lambda text: None)
    if not _python().exists():
        uv = _uv()
        say("creating the transcription environment")
        env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(base / "python"))
        subprocess.run([uv, "venv", "--python", "3.12", str(base / "venv")], check=True, env=env, capture_output=True)
        say("installing faster-whisper")
        subprocess.run([uv, "pip", "install", "--python", str(_python()), FASTER_WHISPER, PYAV],
                       check=True, env=env, capture_output=True)
    (base / "worker.py").write_text(WORKER, encoding="utf-8")
    say(f"downloading the {model} model ({MODELS[model]})")
    result = subprocess.run([str(_python()), str(base / "worker.py"), str(base / "models"), model, "cpu", "--download"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("model download failed: " + result.stderr.strip()[-300:])
    return status()


def remove(model=None):
    """Delete one model, or everything (model None)."""
    if model is None:
        shutil.rmtree(root(), ignore_errors=True)
    else:
        folder = root() / "models"
        for item in folder.iterdir() if folder.exists() else []:
            if item.is_dir() and _model_of(item) == model:
                shutil.rmtree(item, ignore_errors=True)
    return status()


class LocalWhisper:
    """A long-lived worker process; restarted when it dies."""

    def __init__(self, model="small", device="auto"):
        self.model = model
        self.device = device
        self.process = None
        self.lock = threading.Lock()

    def _device(self):
        if self.device != "auto":
            return self.device
        return "cuda" if shutil.which("nvidia-smi") else "cpu"

    def _start(self):
        if self.model not in installed_models():
            raise RuntimeError(f"the {self.model} model is not downloaded (Capture > Local model)")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0
        for device in dict.fromkeys([self._device(), "cpu"]):
            process = subprocess.Popen(
                [str(_python()), str(root() / "worker.py"), str(root() / "models"), self.model, device],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, creationflags=flags,
            )
            line = process.stdout.readline()
            if line and json.loads(line).get("ready"):
                self.process = process
                return
            process.kill()
        raise RuntimeError("the local transcription worker did not start")

    def transcribe(self, path, language=None):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
            self.process.stdin.write(json.dumps({"path": str(path), "language": language}) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
        if not line:
            self.process = None
            raise RuntimeError("the local transcription worker stopped")
        answer = json.loads(line)
        if not answer.get("ok"):
            raise RuntimeError(answer.get("error") or "local transcription failed")
        return dict(text=answer["text"], language=answer.get("language"), segments=answer.get("segments") or [],
                    engine="local", model=self.model)

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
        self.process = None


def path_exists(value):
    return Path(value).exists()
