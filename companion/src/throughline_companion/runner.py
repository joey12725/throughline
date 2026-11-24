"""Runs connectors and other commands on their schedules.

The same scheduler runs on a desktop (the companion app) and on a server (the
`connectors` service in docker-compose.yml, `throughline-companion run
--server`). A job is one connector run (`<entry> sync --once`) or any
command (core maintenance such as `throughline lib backup tick`).

Each job runs as its own process with only the environment it needs: the
Throughline URL, its own plugin token, its settings, and values resolved from
a desktop through the bridge just before it starts (never stored). Output goes
to one log file per job; tokens are redacted from the last error shown.
Failures retry after 1, 2, 4 ... minutes, capped at the job's interval, like
the SDK's own sync loop.
"""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from throughline_sdk import infisical

from . import catalog, paths

log = logging.getLogger("throughline_companion.runner")

MAX_LOG = 5_000_000
DEFAULT_TIMEOUT_S = 3600
# Environment variables a plugin job never inherits from the companion's own
# environment (each job gets exactly its own token, URL and resolved secrets).
STRIP_PREFIXES = ("THROUGHLINE_", "INFISICAL_")
# What an isolated (server) job inherits.
BASE_ENV = {
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "USER",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
}


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat(timespec="seconds") if value else None


def self_command():
    """How to start this companion again as a subprocess.

    A packaged app ships a console build (throughline-companion) next to the
    windowed one; connectors run through the console build so their output
    reaches the log even on Windows, where a windowed program has no stdout.
    """
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).parent
        name = "throughline-companion" + (".exe" if sys.platform.startswith("win") else "")
        console = here / name
        return [str(console if console.exists() else sys.executable)]
    return [sys.executable, "-m", "throughline_companion"]


class Job:
    def __init__(self, name, command, interval_s, env, token=None, kind="plugin",
                 timeout_s=DEFAULT_TIMEOUT_S, prepare=None, entry=None):
        self.name = name
        self.command = list(command)
        self.interval_s = max(60, int(interval_s or 900))
        self.env = dict(env or {})
        self.token = token
        self.kind = kind
        self.timeout_s = timeout_s
        self.prepare = prepare
        self.entry = entry
        self.state = "idle"
        self.last_run_at = None
        self.last_ok_at = None
        self.last_error = None
        self.last_output = None
        self.next_run_at = time.time() + 5
        self.failures = 0
        self.process = None
        self.lock = threading.Lock()

    def snapshot(self):
        return dict(
            name=self.name,
            kind=self.kind,
            state=self.state,
            interval_s=self.interval_s,
            last_run_at=iso(self.last_run_at),
            last_ok_at=iso(self.last_ok_at),
            last_error=self.last_error,
            last_output=self.last_output,
            next_run_at=iso(datetime.fromtimestamp(self.next_run_at, timezone.utc))
            if self.next_run_at
            else None,
        )


def _redact(text, secrets):
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def _rotate(path):
    try:
        if path.exists() and path.stat().st_size > MAX_LOG:
            shutil.move(path, path.with_suffix(".log.1"))
    except OSError:
        pass


def plugin_job(config, name):
    """The Job for an enabled plugin entry of the config, or None."""
    entry = config.data["plugins"].get(name) or {}
    if not entry.get("enabled"):
        return None
    spec = catalog.BY_NAME.get(name)
    command = entry.get("command")
    target = entry.get("entry") or (spec or {}).get("entry")
    if not command:
        if not target:
            return None
        command = self_command() + ["exec", target, "sync", "--once"]
    env = catalog.defaults(spec) if spec else {}
    env.update(entry.get("env") or {})
    interval = entry.get("interval_s") or 900
    return Job(
        name,
        command,
        interval,
        env,
        token=token_value(entry.get("token")),
        prepare=(spec or {}).get("prepare"),
        entry=target,
    )


def token_value(value):
    """A token, or "env:NAME" to read it from the runner's environment (the
    Docker service keeps tokens in its env file, not in the config)."""
    if isinstance(value, str) and value.startswith("env:"):
        return os.getenv(value[4:]) or None
    return value or None


def command_job(spec):
    return Job(
        spec["name"],
        spec["command"],
        spec.get("interval_s") or 3600,
        spec.get("env") or {},
        token=token_value(spec.get("token")),
        kind="command",
        timeout_s=spec.get("timeout_s") or DEFAULT_TIMEOUT_S,
    )


def jobs_from(config):
    jobs = []
    for name in sorted(config.data["plugins"]):
        job = plugin_job(config, name)
        if job:
            jobs.append(job)
    for spec in config.data.get("jobs") or []:
        if spec.get("enabled", True):
            jobs.append(command_job(spec))
    return jobs


def _write_temp_secret(key, text, temp_dir):
    fd, path = tempfile.mkstemp(dir=temp_dir, prefix=key.lower() + "-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    paths.ensure_private(path)
    return path


def resolve_env(
    env,
    url,
    token,
    temp_dir,
    call=None,
    *,
    infisical_client=None,
    infisical_settings=None,
    resolved_secrets=None,
):
    """Replace desktop and Infisical references in env with live values fetched
    just before a job runs (never stored on disk).

    Supported values:
      "env:NAME"           runner's own environment variable (may itself be an infisical: ref)
      "infisical:KEY"      fetched from Infisical (also "infisical:/path/KEY?env=...&as=file")
      {"infisical": KEY, "path": ..., "environment": ..., "project_id": ..., "as": "value"|"file"}
      {"desktop": capability, "args": {...}, "as": "header" | "file" | "value", "key": ...}

    "file" writes the secret text to an owner-only file in temp_dir (deleted
    when the job finishes) and passes its path. Returns the plain env dict.
    """
    client_holder = [infisical_client]

    def get_infisical():
        if client_holder[0] is None:
            client_holder[0] = infisical.InfisicalClient(
                infisical.InfisicalConfig.from_settings(infisical_settings)
            )
        return client_holder[0]

    def resolve_infisical(key, ref_obj):
        parsed = infisical.parse_ref(ref_obj)
        secret_value = get_infisical().get_secret(parsed)
        if resolved_secrets is not None and secret_value:
            resolved_secrets.append(secret_value)
        if parsed.mode == "file":
            return _write_temp_secret(key, secret_value, temp_dir)
        return secret_value

    out = {}
    for key, value in env.items():
        if not isinstance(value, dict):
            text = "" if value is None else str(value)
            if text.startswith("env:"):
                # A secret kept in the runner's own environment, by name.
                text = os.getenv(text[4:], "")
            if infisical.is_ref(text):
                out[key] = resolve_infisical(key, text)
            else:
                out[key] = text
            continue
        if "infisical" in value:
            out[key] = resolve_infisical(key, value)
            continue
        if "desktop" not in value:
            raise ValueError(f"{key}: an env object must name a desktop capability or infisical secret")
        call = call or _bridge_call
        answer = call(url, token, value["desktop"], value.get("args") or {})
        mode = value.get("as", "value")
        if mode == "header":
            out[key] = answer["header"]
            if resolved_secrets is not None and answer.get("header"):
                resolved_secrets.append(str(answer["header"]))
        elif mode == "file":
            out[key] = _write_temp_secret(key, answer["text"], temp_dir)
        else:
            val = str(answer[value.get("key", "value")])
            out[key] = val
            if resolved_secrets is not None and val:
                resolved_secrets.append(val)
    return out


def _bridge_call(url, token, capability, args):
    response = httpx.post(
        url.rstrip("/") + "/v1/companion/call",
        headers={"Authorization": "Bearer " + token},
        json={"capability": capability, "args": args, "timeout_s": 60},
        timeout=90,
    )
    if response.status_code != 200:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:200]
        raise RuntimeError(f"desktop bridge refused {capability}: {detail}")
    return response.json()["result"]


class Runner:
    """Schedules jobs; at most `parallel` run at once."""

    def __init__(self, config, *, parallel=3, on_change=None, isolate=False, infisical_client=None):
        self.config = config
        self.isolate = isolate
        self.parallel = threading.Semaphore(parallel)
        self.jobs = {}
        self.paused = False
        self.stopping = threading.Event()
        self.on_change = on_change
        self.thread = None
        self._infisical_override = infisical_client
        self._infisical_lock = threading.Lock()
        self._infisical_key = None
        self._infisical_client = None
        self.reload()

    def _get_infisical_client(self):
        if self._infisical_override is not None:
            return self._infisical_override
        cfg = infisical.InfisicalConfig.from_settings(
            self.config.data.get("infisical"), default_cache_ttl_s=0.0
        )
        key = cfg.cache_key()
        with self._infisical_lock:
            if self._infisical_client is None or self._infisical_key != key:
                self._infisical_client = infisical.InfisicalClient(cfg)
                self._infisical_key = key
            return self._infisical_client

    def reload(self):
        """Pick up config changes, keeping the state of unchanged jobs."""
        fresh = {job.name: job for job in jobs_from(self.config)}
        for name, job in fresh.items():
            old = self.jobs.get(name)
            if old:
                for attr in ("state", "last_run_at", "last_ok_at", "last_error",
                             "last_output", "next_run_at", "failures", "process"):
                    setattr(job, attr, getattr(old, attr))
                job.lock = old.lock
        self.jobs = fresh

    def snapshot(self):
        return [job.snapshot() for job in sorted(self.jobs.values(), key=lambda j: j.name)]

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="runner", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        for job in list(self.jobs.values()):
            process = job.process
            if process and process.poll() is None:
                process.terminate()

    def run_now(self, name):
        job = self.jobs.get(name)
        if not job:
            raise KeyError(name)
        job.next_run_at = time.time()
        return job.snapshot()

    def _loop(self):
        while not self.stopping.is_set():
            if not self.paused:
                for job in list(self.jobs.values()):
                    if job.next_run_at and time.time() >= job.next_run_at and job.lock.acquire(blocking=False):
                        job.next_run_at = None
                        threading.Thread(
                            target=self._run_locked, args=(job,), name=f"job-{job.name}", daemon=True
                        ).start()
            self.stopping.wait(1)

    def _run_locked(self, job):
        try:
            with self.parallel:
                self.run(job)
        finally:
            job.lock.release()

    def run(self, job):
        """Run one job to completion and schedule its next run."""
        url = self.config.server.get("url") or ""
        job.state = "running"
        job.last_run_at = now()
        self._changed()
        secrets = [job.token, self.config.server.get("device_token")]
        ok = False
        try:
            self._attempt(job, url, secrets)
            ok = True
        except Exception as error:  # noqa: BLE001 - every failure is reported
            job.last_error = _redact(str(error), secrets)[-500:]
            log.warning("%s failed: %s", job.name, job.last_error)
        if ok:
            job.state = "ok"
            job.failures = 0
            job.last_ok_at = now()
            job.last_error = None
            job.next_run_at = time.time() + job.interval_s
        else:
            job.state = "failed"
            job.failures += 1
            delay = min(60 * 2 ** (job.failures - 1), job.interval_s)
            job.next_run_at = time.time() + delay
        self._changed()
        return ok

    def _attempt(self, job, url, secrets):
        log_path = paths.logs_dir() / f"{job.name}.log"
        _rotate(log_path)
        with tempfile.TemporaryDirectory(prefix=f"tlc-{job.name}-") as temp_dir:
            paths.ensure_private(temp_dir)
            resolved_secrets = []
            env = self._environment(job, url, temp_dir, resolved_secrets=resolved_secrets)
            secrets += resolved_secrets
            secrets += [
                v for k, v in env.items() if any(m in k for m in ("TOKEN", "PASSWORD", "SECRET", "COOKIE", "API_KEY"))
            ]
            steps = []
            if job.prepare:
                steps.append(self._prepare_command(job, env))
            steps.append(job.command)
            with open(log_path, "a", encoding="utf-8", errors="replace") as handle:
                for command in steps:
                    handle.write(f"\n=== {iso(now())} {job.name}: {' '.join(command[-3:])}\n")
                    handle.flush()
                    code, tail = self._spawn(job, command, env, handle)
                    if code != 0:
                        raise RuntimeError(tail or f"exit status {code}")
                    job.last_output = _redact(tail, secrets)[-500:] if tail else None
        if job.prepare:
            _archive_exports(job)

    def _environment(self, job, url, temp_dir, *, resolved_secrets=None):
        if self.isolate and job.kind == "plugin":
            # Server mode: a job sees a minimal base environment plus its own
            # settings; secrets reach it only by name ("env:NAME" or "infisical:KEY").
            env = {k: v for k, v in os.environ.items() if k in BASE_ENV}
        else:
            env = {
                k: v for k, v in os.environ.items() if not k.startswith(STRIP_PREFIXES)
            }
        infisical_client = None
        needs_infisical = infisical.is_ref(job.token) or any(
            infisical.is_ref(v)
            or (isinstance(v, str) and v.startswith("env:") and infisical.is_ref(os.getenv(v[4:], "")))
            for v in job.env.values()
        )
        if needs_infisical:
            infisical_client = self._get_infisical_client()
        job_token = job.token
        if infisical.is_ref(job_token):
            job_token = infisical_client.get_secret(job_token)
            if resolved_secrets is not None and job_token:
                resolved_secrets.append(job_token)
        if job.kind == "plugin":
            if not url or not job_token:
                raise RuntimeError("not paired: no Throughline URL or plugin token")
            env["THROUGHLINE_URL"] = url
            env["THROUGHLINE_TOKEN"] = job_token
            env["THROUGHLINE_INTERVAL_S"] = str(job.interval_s)
        else:
            # Commands (core maintenance) keep the settings the runner itself
            # was given: the database URL and PGPASSWORD in Docker, Infisical
            # coordinates, and the model and embedding keys that lenses,
            # curation and the embedding worker read. Only plugins are isolated.
            env.update({k: v for k, v in os.environ.items() if k.startswith(STRIP_PREFIXES)})
            if job_token:
                env["THROUGHLINE_TOKEN"] = job_token
        env.update(
            resolve_env(
                job.env,
                url,
                job_token,
                temp_dir,
                infisical_client=infisical_client,
                infisical_settings=self.config.data.get("infisical"),
                resolved_secrets=resolved_secrets,
            )
        )
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _prepare_command(self, job, env):
        prepare = job.prepare
        state = paths.state_dir() / job.name
        exports = state / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        paths.ensure_private(state)
        values = dict(
            state=str(state),
            exports=str(exports),
            stamp=now().strftime("%Y%m%dT%H%M%SZ"),
        )
        for key, value in (prepare.get("env") or {}).items():
            env[key] = value.format(**values)
        return self_command() + ["exec-module", prepare["module"]] + [
            a.format(**values) for a in prepare["args"]
        ]

    def _spawn(self, job, command, env, handle):
        kwargs = {}
        if paths.platform() == "windows":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        job.process = process
        lines = []
        timer = threading.Timer(job.timeout_s, process.kill)
        timer.start()
        try:
            for line in process.stdout:
                handle.write(line)
                lines.append(line.rstrip())
                del lines[:-20]
            code = process.wait()
        finally:
            timer.cancel()
            job.process = None
        handle.flush()
        return code, "\n".join(lines[-3:])

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass


def _archive_exports(job):
    """Move exports a successful run has mapped out of the way."""
    exports = paths.state_dir() / job.name / "exports"
    archive = exports / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    for item in exports.iterdir():
        if item.is_file():
            item.replace(archive / item.name)
    # Keep the newest 200 archived exports.
    archived = sorted(archive.iterdir(), key=lambda p: p.name)
    for old in archived[:-200]:
        try:
            old.unlink()
        except OSError:
            pass


def exec_entry(entry, argv):
    """Run a plugin entry point (module:function) with argv, in this process."""
    import importlib

    module_name, _, function = entry.partition(":")
    module = importlib.import_module(module_name)
    sys.argv = [module_name] + list(argv)
    return getattr(module, function or "main")()


def exec_module(module_name, argv):
    import runpy

    sys.argv = [module_name] + list(argv)
    runpy.run_module(module_name, run_name="__main__", alter_sys=True)


def wait_for(path, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            return True
        time.sleep(0.1)
    return False
