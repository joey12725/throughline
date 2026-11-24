"""Background jobs for onboarding: installs, backfills, syncs, people.

Jobs are rows of tl.setup_jobs so the browser can follow them across reloads
and the agent setup flow can poll them. They run in this server process: a
plugin run is a subprocess with its own environment (onboarding.catalog's
base environment, the plugin's settings and secrets, its scoped token and
THROUGHLINE_PROGRESS=stderr). Progress lines (`TL-PROGRESS {json}`) and the
last lines of output are written back to the row about once a second.

A small scheduler runs `sync --once` for every enabled install at its
interval (THROUGHLINE_SETUP_SCHEDULER=0 turns it off), so a source set up in
the browser keeps itself current without systemd timers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from ..db import connect
from . import store
from .errors import SetupError

log = logging.getLogger("throughline.setup")

MAX_PARALLEL = int(os.getenv("THROUGHLINE_SETUP_PARALLEL") or 3)
TAIL = 40
FLUSH_S = 1.0

_pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix="setup-job")
_procs: dict[int, subprocess.Popen] = {}
_cancelled: set[int] = set()
_lock = threading.Lock()
_api_url: str | None = None


def _conn():
    return connect("app")


def set_api_url(url: str):
    global _api_url
    if url and not _api_url:
        _api_url = url.rstrip("/")


def api_url():
    return (
        os.getenv("THROUGHLINE_PLUGIN_API_URL")
        or _api_url
        or os.getenv("THROUGHLINE_PUBLIC_URL")
        or "http://127.0.0.1:8000"
    ).rstrip("/")


# ───────────── rows ─────────────

EMPTY_PROGRESS = dict(delivered=0, tracks=0, oldest=None, newest=None, done=None, total=None, unit=None,
                      fraction=None, rate_per_s=None, eta_s=None)


def create(kind, title, plugin_id=None, args=None):
    with _conn() as conn:
        row = conn.execute(
            "INSERT INTO tl.setup_jobs(kind,plugin_id,title,args,progress) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (kind, plugin_id, title, Jsonb(args or {}), Jsonb(EMPTY_PROGRESS)),
        ).fetchone()
    return row["id"]


def _update(job_id, **fields):
    sets, values = [], []
    for key, value in fields.items():
        sets.append(f"{key}=%s")
        values.append(Jsonb(value) if isinstance(value, (dict, list)) else value)
    with _conn() as conn:
        conn.execute(f"UPDATE tl.setup_jobs SET {','.join(sets)} WHERE id=%s", (*values, job_id))


def get(job_id):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM tl.setup_jobs WHERE id=%s", (job_id,)).fetchone()
    if not row:
        raise SetupError("Job not found", f"There is no job {job_id}.", status=404)
    return public(row)


def public(row):
    return dict(
        id=row["id"], kind=row["kind"], plugin_id=row["plugin_id"], title=row["title"],
        status=row["status"], phase=row["phase"], progress={**EMPTY_PROGRESS, **(row["progress"] or {})},
        error=row["error"], log_tail=row["log_tail"] or [], started_at=row["started_at"],
        finished_at=row["finished_at"], created_at=row["created_at"],
    )


def listing(kinds=("install", "backfill", "people", "check", "lens", "pages"), limit=200):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (coalesce(plugin_id, kind), kind) * FROM tl.setup_jobs WHERE kind = ANY(%s)"
            " ORDER BY coalesce(plugin_id, kind), kind, id DESC",
            (list(kinds),),
        ).fetchall()
    jobs = sorted((public(r) for r in rows), key=lambda j: j["id"])[-limit:]
    summary = dict(running=0, queued=0, failed=0, done=0, delivered=0)
    fractions = []
    for job in jobs:
        if job["kind"] != "backfill":
            continue
        status = job["status"]
        if status in ("running", "queued"):
            summary[status] += 1
        elif status in ("failed", "interrupted"):
            summary["failed"] += 1
        elif status == "done":
            summary["done"] += 1
        summary["delivered"] += (job["progress"]["delivered"] or 0) + (job["progress"]["tracks"] or 0)
        if status == "done":
            fractions.append(1.0)
        elif job["progress"]["fraction"] is not None:
            fractions.append(job["progress"]["fraction"])
        else:
            fractions.append(None)
    known = [f for f in fractions if f is not None]
    summary["fraction"] = (sum(known) / len(fractions)) if fractions and len(known) == len(fractions) else (
        (sum(known) / len(fractions)) if known and len(known) >= len(fractions) / 2 else None
    )
    return dict(jobs=jobs, summary=summary)


def interrupt_stale():
    """At startup: jobs that were running when the server stopped."""
    try:
        with _conn() as conn:
            conn.execute(
                "UPDATE tl.setup_jobs SET status='interrupted',finished_at=now(),"
                "error=%s WHERE status IN ('queued','running')",
                (Jsonb(dict(title="Interrupted", message="The server restarted while this was running.",
                            hint="Press Retry: imports resume where they stopped.")),),
            )
    except Exception:
        pass


# ───────────── running ─────────────


def submit(job_id, function, *args):
    def run():
        if job_id in _cancelled:
            return
        _update(job_id, status="running", started_at=datetime.now(timezone.utc))
        try:
            function(job_id, *args)
        except SetupError as error:
            _update(job_id, status="failed", finished_at=datetime.now(timezone.utc), error=error.body())
        except Exception as error:  # a bug: still tell the person something useful
            log.error("setup job %s failed: %s", job_id, traceback.format_exc())
            _update(
                job_id, status="failed", finished_at=datetime.now(timezone.utc),
                error=dict(title="Unexpected error", message=store.redact(f"{type(error).__name__}: {error}"),
                           hint="See the server log for details, then press Retry."),
            )
        else:
            if job_id in _cancelled:
                _update(job_id, status="cancelled", finished_at=datetime.now(timezone.utc))
            else:
                _update(job_id, status="done", finished_at=datetime.now(timezone.utc))

    _pool.submit(run)
    return job_id


def cancel(job_id):
    _cancelled.add(job_id)
    with _lock:
        proc = _procs.get(job_id)
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
    with _conn() as conn:
        conn.execute(
            "UPDATE tl.setup_jobs SET status='cancelled',finished_at=now() WHERE id=%s AND status IN ('queued','running')",
            (job_id,),
        )
    return get(job_id)


class Tracker:
    """Collects progress and output of one job and writes it out throttled."""

    def __init__(self, job_id):
        self.job_id = job_id
        self.progress = dict(EMPTY_PROGRESS)
        self.tail = deque(maxlen=TAIL)
        self.phase = None
        self.started = time.monotonic()
        self.last = 0.0
        self.extra_secrets: list[str] = []

    def line(self, text):
        text = text.rstrip("\n")
        if text.startswith("TL-PROGRESS "):
            try:
                self.update(**json.loads(text[len("TL-PROGRESS "):]))
            except ValueError:
                pass
            return
        if text.strip():
            self.tail.append(store.redact(text, self.extra_secrets)[:500])
        self.flush()

    def update(self, **fields):
        for key in ("delivered", "tracks", "oldest", "newest", "done", "total", "unit"):
            if fields.get(key) is not None:
                self.progress[key] = fields[key]
        if fields.get("note"):
            self.phase = str(fields["note"])[:200]
        elif fields.get("phase"):
            self.phase = str(fields["phase"])[:200]
        self.flush()

    def computed(self):
        p = dict(self.progress)
        elapsed = max(time.monotonic() - self.started, 0.001)
        count = (p["delivered"] or 0) + (p["tracks"] or 0)
        if p.get("done") is not None and p.get("total"):
            p["fraction"] = max(0.0, min(1.0, p["done"] / p["total"]))
            rate = p["done"] / elapsed
            p["rate_per_s"] = round(count / elapsed, 2) if count else round(rate, 2)
            p["eta_s"] = int((p["total"] - p["done"]) / rate) if rate > 0 and p["done"] < p["total"] else 0
        else:
            p["fraction"] = None
            p["rate_per_s"] = round(count / elapsed, 2) if count else None
            p["eta_s"] = None
        if self.phase in (None, "Starting") and p.get("oldest"):
            when = str(p["oldest"])[:7]
            p_phase = f"Reading back to {_month(when)}"
        else:
            p_phase = self.phase
        return p, p_phase

    def flush(self, force=False):
        now = time.monotonic()
        if not force and now - self.last < FLUSH_S:
            return
        self.last = now
        progress, phase = self.computed()
        _update(self.job_id, progress=progress, phase=phase, log_tail=list(self.tail))


def _month(yyyy_mm):
    try:
        return datetime.strptime(yyyy_mm, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return yyyy_mm


def run_process(job_id, argv, env, cwd=None, tracker=None, timeout=None):
    """Run argv streaming output into the tracker. Returns (code, tracker)."""
    tracker = tracker or Tracker(job_id)
    if job_id in _cancelled:
        return -1, tracker
    try:
        proc = subprocess.Popen(
            argv, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
        )
    except FileNotFoundError:
        raise SetupError(
            "The plugin program wasn't found",
            f"{argv[0]} doesn't exist.",
            "Install the plugin again from its setup card.",
        ) from None
    with _lock:
        _procs[job_id] = proc
    deadline = time.monotonic() + timeout if timeout else None
    try:
        assert proc.stdout is not None
        for text in proc.stdout:
            tracker.line(text)
            if deadline and time.monotonic() > deadline:
                os.killpg(proc.pid, signal.SIGTERM)
                break
        code = proc.wait(timeout=30)
    finally:
        with _lock:
            _procs.pop(job_id, None)
    tracker.flush(force=True)
    return code, tracker


MISSING = re.compile(r"missing environment variable ([A-Z0-9_]+)")


def explain_failure(spec, tracker, code):
    """A SetupError for a plugin run that exited non-zero."""
    lines = list(tracker.tail)
    text = "\n".join(lines)
    match = MISSING.search(text)
    if match:
        key = match.group(1)
        label = next((f.label for f in spec.setup.fields if f.key == key), key)
        return SetupError(
            "A required setting is missing",
            f"{spec.name} needs {label} ({key}).",
            f"Open {spec.name}'s setup, fill in {label}, save and retry.",
            code="missing_setting",
        )
    last = next((line for line in reversed(lines) if line.strip() and not line.startswith("{")), "")
    for needle, title, hint in (
        ("401", "The service refused the credentials", "Check the key or token in this source's setup, then retry."),
        ("403", "The service refused access", "The credentials work but lack permission; check their scopes."),
        ("429", "The service is rate limiting", "Wait a few minutes and retry; imports resume where they stopped."),
        ("Connection refused", "Couldn't connect", "Check the address in this source's setup and that the service is running."),
        ("Name or service not known", "Couldn't find the server", "Check the address for typos and this server's internet access."),
        ("FileNotFoundError", "A file or folder wasn't found", "Check the paths in this source's setup exist on the Throughline server."),
        ("No such file", "A file or folder wasn't found", "Check the paths in this source's setup exist on the Throughline server."),
        ("PermissionError", "Permission denied", "The server can't read that file or folder; check its permissions."),
    ):
        if needle in text:
            return SetupError(title, last or f"{spec.name} stopped (exit {code}).", hint, code="plugin_failed")
    return SetupError(
        f"{spec.name} stopped with an error",
        last or f"The plugin exited with status {code}.",
        "Open the log below for details. Fix the setting it names, then press Retry; it resumes where it stopped.",
        code="plugin_failed",
    )


# ───────────── scheduler ─────────────

_scheduler_started = False


def start_scheduler():
    global _scheduler_started
    if _scheduler_started or os.getenv("THROUGHLINE_SETUP_SCHEDULER", "1") == "0":
        return
    _scheduler_started = True
    thread = threading.Thread(target=_scheduler_loop, name="setup-scheduler", daemon=True)
    thread.start()


def _scheduler_loop():
    from . import runs

    while True:
        time.sleep(30)
        try:
            runs.tick()
        except Exception:
            log.warning("setup scheduler tick failed: %s", traceback.format_exc(limit=3))
