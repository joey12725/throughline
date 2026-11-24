"""Run classifier code in a separate process.

The child is `python -I runtime/throughline_classify.py`, speaking JSON lines
on stdin/stdout. It never gets a database URL, a key or a token: its
environment is emptied. Under bubblewrap (THROUGHLINE_CLASSIFIER_SANDBOX=auto,
the default, when /usr/bin/bwrap works) it also has no network and sees only
/usr, the Python installation and its own read-only directory. Without
bubblewrap (containers without user namespaces) it runs as a plain process
with resource limits and the runtime's import allow-list; `mode` says which.

THROUGHLINE_CLASSIFIER_SANDBOX: auto (default) | bwrap (refuse without it) |
process.
"""

import json
import os
import resource
import select
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RUNTIME = Path(__file__).parent / "runtime" / "throughline_classify.py"
MEMORY_BYTES = 1024 * 1024 * 1024
CPU_SECONDS = 600
_bwrap_ok = None


class SandboxError(Exception):
    """The classifier process failed to start, broke the protocol or timed out."""


def _limits():
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
    os.setsid()


def _python():
    return os.path.realpath(sys.executable)


def _bwrap_args(workdir):
    py = _python()
    base = os.path.realpath(sys.base_prefix)
    args = [
        shutil.which("bwrap") or "/usr/bin/bwrap",
        "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/bin", "/bin",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--ro-bind", workdir, "/work",
        "--chdir", "/work",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PYTHONIOENCODING", "utf-8",
    ]
    if not base.startswith("/usr/"):
        args += ["--ro-bind", base, base]
    if not py.startswith(base + "/") and not py.startswith("/usr/"):
        args += ["--ro-bind", os.path.dirname(py), os.path.dirname(py)]
    return args + [py, "-I", "/work/throughline_classify.py"]


def bwrap_available():
    global _bwrap_ok
    if _bwrap_ok is None:
        exe = shutil.which("bwrap")
        if not exe:
            _bwrap_ok = False
        else:
            with tempfile.TemporaryDirectory() as d:
                try:
                    out = subprocess.run(
                        _bwrap_args(d)[:-2] + ["-I", "-c", "print('ok')"],
                        capture_output=True, text=True, timeout=20, env={},
                    )
                    _bwrap_ok = out.returncode == 0 and out.stdout.strip() == "ok"
                except (OSError, subprocess.SubprocessError):
                    _bwrap_ok = False
    return _bwrap_ok


def mode():
    want = (os.getenv("THROUGHLINE_CLASSIFIER_SANDBOX") or "auto").strip().lower()
    if want == "process":
        return "process"
    if bwrap_available():
        return "bwrap"
    if want == "bwrap":
        raise SandboxError("THROUGHLINE_CLASSIFIER_SANDBOX=bwrap but bubblewrap does not work here")
    return "process"


class Session:
    """One child process with one classifier loaded; run() it per thread."""

    def __init__(self, code, *, timeout_s=300):
        self.timeout_s = timeout_s
        self.dir = tempfile.mkdtemp(prefix="tl-classifier-")
        shutil.copy(RUNTIME, os.path.join(self.dir, "throughline_classify.py"))
        os.chmod(self.dir, 0o755)
        self.mode = mode()
        if self.mode == "bwrap":
            argv = _bwrap_args(self.dir)
            cwd = None
        else:
            argv = [_python(), "-I", os.path.join(self.dir, "throughline_classify.py")]
            cwd = self.dir
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, env={"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"} if self.mode == "process" else {},
            cwd=cwd, preexec_fn=_limits,
        )
        self.logs = []
        self._send({"type": "load", "code": code})
        reply = self._read(time.monotonic() + 30)
        if reply.get("type") == "error":
            self.close()
            raise SandboxError(reply.get("error") or "the classifier did not load")
        if reply.get("type") != "loaded":
            self.close()
            raise SandboxError("the classifier process did not start")
        self.grain = reply.get("grain") or "thread"

    def _send(self, msg):
        try:
            self.proc.stdin.write(json.dumps(msg, default=str) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise SandboxError(self._died()) from None

    def _died(self):
        err = ""
        try:
            self.proc.wait(timeout=2)
            err = (self.proc.stderr.read() or "")[-800:]
        except Exception:
            pass
        return "the classifier process exited" + (f": {err.strip()}" if err.strip() else "")

    def _read(self, deadline):
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.close()
                raise SandboxError("the classifier timed out")
            ready, _, _ = select.select([self.proc.stdout], [], [], min(left, 5))
            if not ready:
                if self.proc.poll() is not None:
                    raise SandboxError(self._died())
                continue
            line = self.proc.stdout.readline()
            if not line:
                raise SandboxError(self._died())
            try:
                return json.loads(line)
            except ValueError:
                continue

    def run(self, thread, handle_call, *, owner_name=None):
        """Classify one thread. handle_call(op, args) -> value, raising
        bridge.Stopped to stop or ValueError for a bad call. Returns
        ('done', marks) | ('stopped', reason) | ('error', message)."""
        from .bridge import Stopped

        self._send({"type": "run", "thread": thread, "owner_name": owner_name})
        deadline = time.monotonic() + self.timeout_s
        while True:
            msg = self._read(deadline)
            kind = msg.get("type")
            if kind == "log":
                if len(self.logs) < 200:
                    self.logs.append(str(msg.get("text"))[:2000])
            elif kind == "call":
                reply = {"type": "result", "id": msg.get("id")}
                try:
                    reply["value"] = handle_call(msg.get("op"), msg.get("args") or {})
                except Stopped as exc:
                    reply.update(stop=True, error=str(exc)[:500])
                except (ValueError, TypeError) as exc:
                    reply["error"] = str(exc)[:500]
                self._send(reply)
            elif kind == "done":
                return "done", list(msg.get("marks") or [])
            elif kind == "stopped":
                return "stopped", msg.get("error")
            elif kind == "error":
                return "error", msg.get("error")

    def close(self):
        try:
            if self.proc.poll() is None:
                try:
                    self.proc.stdin.write(json.dumps({"type": "exit"}) + "\n")
                    self.proc.stdin.flush()
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
