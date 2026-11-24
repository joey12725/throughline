"""ActivityWatch, bundled: install, run and watch it.

Three modes (companion.json "activitywatch.mode"):

  external  use an ActivityWatch the owner runs already (default; the
            connector reads http://localhost:5600)
  managed   the companion downloads the official ActivityWatch release for
            this OS (pinned version, SHA-256 checked), starts aw-server-rust
            and the watchers, and restarts them if they exit
  off       nothing

Managed mode never runs aw-qt (ActivityWatch's own tray app), so there is one
tray icon. On Linux under Wayland it starts aw-awatcher instead of the X11
window and AFK watchers. On macOS the window watcher needs the Accessibility
permission (System Settings > Privacy & Security > Accessibility).
"""

import hashlib
import logging
import os
import platform as _platform
import shutil
import stat
import subprocess
import threading
import time
import urllib.request
import zipfile

import httpx

from . import paths

log = logging.getLogger("throughline_companion.activitywatch")

VERSION = "v0.14.3"
BASE = f"https://github.com/ActivityWatch/activitywatch/releases/download/{VERSION}/"
# Official release archives and their SHA-256 (from the GitHub release).
RELEASES = {
    ("linux", "x86_64"): (
        f"activitywatch-{VERSION}-linux-x86_64.zip",
        "bbe6604aa3db4b18af546b3ec5f57314d193fa4ecc3f05404351f05d6dd2c351",
    ),
    ("windows", "x86_64"): (
        f"activitywatch-{VERSION}-windows-x86_64.zip",
        "f86fb8b226103d412d64c71a781817775180e71cd27a79481a0e4653d2da18c3",
    ),
    ("macos", "arm64"): (
        f"activitywatch-{VERSION}-macos-arm64.zip",
        "39094082adeefec1eca06b7acc711d2eed5e10130ee4679a2fa26c0f540739af",
    ),
    ("macos", "x86_64"): (
        f"activitywatch-{VERSION}-macos-x86_64.zip",
        "a2dc2f8e4fe6ad4c402fefe72a12ec87e834d261dc8d3661e05067a515c1732a",
    ),
}
DEFAULT_URL = "http://localhost:5600"


def arch():
    machine = _platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return machine


def release():
    return RELEASES.get((paths.platform(), arch()))


def install_dir():
    return paths.data_dir() / "activitywatch" / VERSION


def _exe(name):
    return name + (".exe" if paths.platform() == "windows" else "")


def binary(module):
    """Path of one ActivityWatch module in the managed install."""
    root = install_dir() / "activitywatch"
    single = root / _exe(module)
    if single.exists():
        return single
    return root / module / _exe(module)


def installed():
    return binary("aw-server-rust").exists() or binary("aw-server").exists()


def reachable(url=DEFAULT_URL):
    try:
        return httpx.get(url.rstrip("/") + "/api/0/info", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def default_watchers():
    if paths.platform() == "linux" and os.getenv("WAYLAND_DISPLAY"):
        return ["aw-awatcher"]
    return ["aw-watcher-afk", "aw-watcher-window"]


def install(progress=None):
    """Download, verify and unpack the pinned release. Returns the install dir."""
    picked = release()
    if not picked:
        raise RuntimeError(
            f"no bundled ActivityWatch for {paths.platform()} {arch()}; install it "
            "from activitywatch.net and use external mode"
        )
    name, digest = picked
    target = install_dir()
    if installed():
        return target
    downloads = paths.data_dir() / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / name
    if not archive.exists() or _sha256(archive) != digest:
        partial = archive.with_suffix(".part")
        request = urllib.request.Request(BASE + name, headers={"User-Agent": "throughline-companion"})
        with urllib.request.urlopen(request, timeout=60) as response, open(partial, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if _sha256(partial) != digest:
            partial.unlink()
            raise RuntimeError("ActivityWatch download failed its checksum; not installed")
        partial.replace(archive)
    staging = target.with_name(target.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            path = (staging / member.filename).resolve()
            if staging.resolve() not in path.parents and path != staging.resolve():
                raise RuntimeError("unsafe path in ActivityWatch archive")
            bundle.extract(member, staging)
            mode = (member.external_attr >> 16) & 0o777
            if mode and not member.is_dir():
                os.chmod(path, mode | stat.S_IRUSR)
    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)
    archive.unlink(missing_ok=True)
    return target


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Supervisor:
    """Keeps the managed ActivityWatch modules running."""

    def __init__(self, config):
        self.config = config
        self.processes = {}
        self.stopping = threading.Event()
        self.thread = None
        self.last_error = None
        self.restarts = 0

    @property
    def options(self):
        return self.config.data["activitywatch"]

    def modules(self):
        server = "aw-server-rust" if binary("aw-server-rust").exists() else "aw-server"
        return [server] + list(self.options.get("watchers") or default_watchers())

    def snapshot(self):
        mode = self.options.get("mode", "external")
        url = self.options.get("url") or DEFAULT_URL
        return dict(
            mode=mode,
            url=url,
            version=VERSION,
            available=release() is not None,
            installed=installed(),
            reachable=reachable(url),
            running=sorted(n for n, p in self.processes.items() if p.poll() is None),
            last_error=self.last_error,
            restarts=self.restarts,
        )

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="activitywatch", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self._stop_all()

    def _stop_all(self):
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
        for process in self.processes.values():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        self.processes = {}

    def _loop(self):
        while not self.stopping.is_set():
            try:
                if self.options.get("mode") == "managed":
                    self._tick()
                elif self.processes:
                    self._stop_all()
            except Exception as error:  # noqa: BLE001
                self.last_error = str(error)[:300]
                log.warning("activitywatch: %s", self.last_error)
            self.stopping.wait(10)

    def _tick(self):
        if not installed():
            install()
        logs = paths.logs_dir()
        for module in self.modules():
            process = self.processes.get(module)
            if process and process.poll() is None:
                continue
            if not module.startswith("aw-server") and not reachable(self.options.get("url") or DEFAULT_URL):
                # Watchers need the server up first.
                continue
            path = binary(module)
            if not path.exists():
                self.last_error = f"{module} is not part of this ActivityWatch build"
                continue
            if process is not None:
                self.restarts += 1
            kwargs = {}
            if paths.platform() == "windows":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            handle = open(logs / f"activitywatch-{module}.log", "a", encoding="utf-8")
            self.processes[module] = subprocess.Popen(
                [str(path)],
                cwd=str(path.parent),
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                **kwargs,
            )
            handle.close()
            if module.startswith("aw-server"):
                deadline = time.time() + 20
                while time.time() < deadline and not reachable(self.options.get("url") or DEFAULT_URL):
                    time.sleep(0.5)
