"""What onboarding jobs actually do with a plugin: install, check, backfill,
and the scheduled sync."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..db import connect
from . import catalog, jobs, store
from .errors import SetupError


def _conn():
    return connect("app")


# ───────────── install ─────────────


def _sdk_source():
    """The local throughline-sdk project, so plugins resolve it offline."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "sdk"
        if (candidate / "pyproject.toml").exists():
            return candidate
    return None


def install_job(job_id, plugin_id):
    repo_id, root, spec = catalog.find(plugin_id)
    can, why = catalog.installable(spec, root)
    if not can:
        raise SetupError(f"{spec.name} can't be installed here", why or "Not installable.", status=409)
    tracker = jobs.Tracker(job_id)
    existing = catalog.resolve_command(spec, root)
    if existing:
        tracker.update(phase="Already available on this server")
        catalog.record_install(plugin_id, repo_id, root, spec, existing)
        return
    if spec.install.kind == "command":
        raise SetupError(
            "Missing program",
            f"{spec.name} runs `{' '.join(spec.install.run)}`, which isn't available on this server.",
            "Install the program it needs (see the plugin's README) and retry.",
        )
    env_path = catalog.env_dir()
    uv = shutil.which("uv")
    base = catalog.plugin_base_env()
    plugin_dir = root / spec.path
    if not (plugin_dir / "pyproject.toml").exists():
        raise SetupError(
            "Not a Python package",
            f"{spec.path}/pyproject.toml is missing in the repository.",
            "The plugin entry's path must point at its package directory.",
        )
    sources = [str(plugin_dir)]
    sdk = _sdk_source()
    if sdk:
        sources.insert(0, str(sdk))
    tracker.update(phase="Creating the plugin environment")
    if not (env_path / "bin" / "python").exists():
        argv = [uv, "venv", "--python", sys.executable, str(env_path)] if uv else [sys.executable, "-m", "venv", str(env_path)]
        code, _ = jobs.run_process(job_id, argv, base, tracker=tracker, timeout=600)
        if code != 0:
            raise SetupError("Couldn't create the plugin environment", tracker.tail[-1] if tracker.tail else "venv failed.",
                             "Check disk space and that Python 3.12+ is available.")
    tracker.update(phase=f"Installing {spec.name}")
    python = str(env_path / "bin" / "python")
    argv = [uv, "pip", "install", "--python", python, *sources] if uv else [python, "-m", "pip", "install", *sources]
    code, _ = jobs.run_process(job_id, argv, base, tracker=tracker, timeout=1800)
    if code != 0:
        raise SetupError(
            f"Installing {spec.name} failed",
            (tracker.tail[-1] if tracker.tail else "The package manager reported an error."),
            "Open the log for the exact error. Often a dependency needs a system library or newer Python.",
        )
    command = catalog.resolve_command(spec, root)
    if not command:
        raise SetupError(
            "Installed, but the program wasn't found",
            f"The package installed but has no `{spec.install.entry}` command.",
            "The plugin entry's install.entry must match a [project.scripts] name.",
        )
    catalog.record_install(plugin_id, repo_id, root, spec, command)
    tracker.update(phase="Installed")


# ───────────── environment for a run ─────────────


def plugin_env(plugin_id, spec, values_override=None, token=True):
    env = catalog.plugin_base_env()
    for field in spec.setup.fields:
        if field.default is not None:
            env[field.key] = field.default
    values = store.plugin_values(plugin_id)
    if values_override:
        for key, value in values_override.items():
            if isinstance(value, str) and value.startswith("••••"):
                continue
            if value not in (None, ""):
                values[key] = str(value)
    env.update({k: v for k, v in values.items() if k != catalog.TOKEN_KEY})
    env["THROUGHLINE_URL"] = jobs.api_url()
    env["THROUGHLINE_PROGRESS"] = "stderr"
    env["THROUGHLINE_INTERVAL_S"] = str(spec.schedule.interval_s)
    env["PYTHONUNBUFFERED"] = "1"
    if token:
        env["THROUGHLINE_TOKEN"] = catalog.ensure_token(plugin_id, spec)
    return env


def _command(plugin_id):
    repo_id, root, spec = catalog.find(plugin_id)
    command = catalog.resolve_command(spec, root)
    if not command:
        raise SetupError(
            f"{spec.name} isn't installed",
            "Install it first from its setup card.",
            None,
            status=409,
            code="not_installed",
        )
    return repo_id, root, spec, command


# ───────────── check ─────────────


def check(plugin_id, values=None, timeout=45):
    """A CheckResult: the plugin's own `check` when it has one, else a short
    dry-run read (nothing is written)."""
    import time

    started = time.monotonic()
    repo_id, root, spec, command = _command(plugin_id)
    missing = []
    merged = store.plugin_values(plugin_id)
    merged.update({k: v for k, v in (values or {}).items() if v not in (None, "") and not str(v).startswith("••••")})
    for field in spec.setup.fields:
        if field.required and not merged.get(field.key) and not field.default and catalog._visible(field, merged, spec):
            missing.append(field.label)
    if missing:
        return dict(ok=False, title="Missing details", message="Fill in: " + ", ".join(missing) + ".", hint=None)
    env = plugin_env(plugin_id, spec, values, token=True)
    cwd = str(root / spec.path) if root else None
    secrets = [v for k, v in merged.items() if k in spec.secret_keys()]
    if spec.check:
        try:
            result = subprocess.run([*command, "check"], env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return dict(ok=False, title="The check timed out", message=f"No answer within {timeout} s.",
                        hint="The service may be slow or unreachable from this server.")
        line = next((x for x in reversed(result.stdout.splitlines()) if x.strip().startswith("{")), None)
        try:
            body = json.loads(line) if line else {}
        except ValueError:
            body = {}
        ok = result.returncode == 0 and body.get("ok", True)
        message = store.redact(body.get("message") or (result.stderr.strip().splitlines() or ["No details."])[-1], secrets)
        return dict(ok=bool(ok), title="Connected" if ok else "The check failed", message=message,
                    hint=body.get("hint"), duration_ms=int((time.monotonic() - started) * 1000))
    # Dry run: reads from the source, writes nothing. Stop at the first sign of
    # data so a large history doesn't make the check slow.
    try:
        proc = subprocess.Popen([*command, "backfill", "--dry-run"], env=env, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, start_new_session=True)
    except FileNotFoundError:
        return dict(ok=False, title="Program not found", message=f"{command[0]} is missing.", hint="Reinstall the plugin.")
    tail, seen = [], 0
    deadline = time.monotonic() + timeout
    import signal as _signal

    try:
        assert proc.stdout is not None
        for text in proc.stdout:
            if text.startswith("TL-PROGRESS "):
                try:
                    data = json.loads(text[12:])
                    seen = max(seen, int(data.get("delivered") or 0) + int(data.get("tracks") or 0))
                except ValueError:
                    pass
                if seen:
                    break
            elif text.strip():
                tail.append(store.redact(text.rstrip(), secrets))
            if time.monotonic() > deadline:
                break
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, _signal.SIGTERM)
            except ProcessLookupError:
                pass
        code = proc.wait(timeout=10)
    ms = int((time.monotonic() - started) * 1000)
    report = None
    for line in reversed(tail):
        if line.startswith("{"):
            try:
                report = json.loads(line)
                break
            except ValueError:
                continue
    if seen or (report and sum((report.get("counts") or {}).values())):
        n = seen or sum((report.get("counts") or {}).values())
        return dict(ok=True, title="Connected", message=f"{spec.name} can read your data (found {n:,}+ items in a test read; nothing was saved).", duration_ms=ms)
    if code == 0 or code < 0 and not tail:
        return dict(ok=True, title="Connected", message=f"{spec.name} ran without errors but found nothing to import yet.",
                    hint="If you expected data, check the paths and account settings.", duration_ms=ms)
    error = jobs.explain_failure(spec, _TailTracker(tail), code)
    return dict(ok=False, title=error.title, message=error.message, hint=error.hint, duration_ms=ms,
                detail={"log": tail[-12:]})


class _TailTracker:
    def __init__(self, tail):
        self.tail = tail


# ───────────── backfill & sync ─────────────


def backfill_job(job_id, plugin_id, mode="backfill"):
    repo_id, root, spec, command = _command(plugin_id)
    env = plugin_env(plugin_id, spec)
    tracker = jobs.Tracker(job_id)
    tracker.extra_secrets = list(store.plugin_values(plugin_id).values())
    tracker.started_at = datetime.now(timezone.utc)
    with _conn() as conn:
        tracker.track_floor = conn.execute("SELECT coalesce(max(id),0) AS n FROM tl.location_track").fetchone()["n"]
    tracker.update(phase="Starting" if mode == "backfill" else "Checking for new items")
    argv = [*command, "backfill"] if mode == "backfill" else [*command, "sync", "--once"]
    stop = threading.Event()
    counter = threading.Thread(target=_count_rows, args=(tracker, spec, stop), daemon=True)
    counter.start()
    try:
        code, tracker = jobs.run_process(job_id, argv, env, cwd=str(root / spec.path) if root else None, tracker=tracker)
    finally:
        stop.set()
        counter.join(timeout=5)
        _count_once(tracker, spec)
    with _conn() as conn:
        conn.execute(
            "UPDATE tl.plugin_installs SET last_run_at=now(),last_status=%s,last_error=%s,"
            "next_run_at=now()+make_interval(secs=>interval_s) WHERE plugin_id=%s",
            ("ok" if code == 0 else "failed", None if code == 0 else (tracker.tail[-1] if tracker.tail else f"exit {code}"), plugin_id),
        )
    if job_id in jobs._cancelled:
        return
    if code != 0:
        raise jobs.explain_failure(spec, tracker, code)
    tracker.update(phase="Up to date")
    tracker.progress["done"] = tracker.progress.get("total") or tracker.progress.get("done")
    tracker.flush(force=True)
    if mode == "backfill":
        with _conn() as conn:
            conn.execute(
                "UPDATE tl.plugin_installs SET enabled=true,backfilled_at=now() WHERE plugin_id=%s", (plugin_id,)
            )


def _count_once(tracker, spec):
    """Rows this run has written, counted in the database: plugins that
    don't print progress lines (or deliver through their own client) still
    get a live count. Never lowers what the plugin itself reported."""
    since = tracker.started_at
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT count(*) AS n,min(m.happened_at) AS oldest,max(m.happened_at) AS newest FROM tl.messages m"
                " JOIN tl.channels c ON c.id=m.channel_id WHERE c.plugin=%s AND m.ingested_at>=%s",
                (spec.id, since),
            ).fetchone()
            tracks = 0
            if spec.track_sources:
                tracks = conn.execute(
                    "SELECT count(*) AS n FROM tl.location_track WHERE source=ANY(%s) AND id>%s",
                    (spec.track_sources, tracker.track_floor),
                ).fetchone()["n"]
    except Exception:
        return
    fields = {}
    if row["n"] > (tracker.progress.get("delivered") or 0):
        fields["delivered"] = row["n"]
        if row["oldest"] is not None and not tracker.progress.get("oldest"):
            fields["oldest"] = row["oldest"].isoformat()
            fields["newest"] = row["newest"].isoformat()
    if tracks > (tracker.progress.get("tracks") or 0):
        fields["tracks"] = tracks
    if fields:
        tracker.update(**fields)


def _count_rows(tracker, spec, stop):
    while not stop.wait(2.0):
        _count_once(tracker, spec)


def start_backfill(plugin_id):
    _, _, spec, _ = _command(plugin_id)
    catalog.ensure_token(plugin_id, spec)
    job_id = jobs.create("backfill", f"Importing {spec.name}", plugin_id)
    return jobs.submit(job_id, backfill_job, plugin_id)


def start_install(plugin_id):
    _, _, spec = catalog.find(plugin_id)
    job_id = jobs.create("install", f"Installing {spec.name}", plugin_id)
    return jobs.submit(job_id, install_job, plugin_id)


def retry(job_id):
    job = jobs.get(job_id)
    if job["status"] in ("running", "queued"):
        raise SetupError("Still running", "This job hasn't finished.", status=409)
    if job["kind"] == "backfill":
        return start_backfill(job["plugin_id"])
    if job["kind"] == "install":
        return start_install(job["plugin_id"])
    if job["kind"] == "people":
        from . import people

        return people.start()
    raise SetupError("Can't retry this", f"{job['kind']} jobs can't be retried.", status=409)


def tick():
    """Run a sync for every enabled install that is due."""
    now = datetime.now(timezone.utc)
    with _conn() as conn:
        due = conn.execute(
            "SELECT i.plugin_id FROM tl.plugin_installs i WHERE i.enabled AND i.command IS NOT NULL"
            " AND (i.next_run_at IS NULL OR i.next_run_at<=%s)"
            " AND NOT EXISTS (SELECT 1 FROM tl.setup_jobs j WHERE j.plugin_id=i.plugin_id AND j.status IN ('queued','running'))",
            (now,),
        ).fetchall()
        for row in due:
            conn.execute(
                "UPDATE tl.plugin_installs SET next_run_at=%s+make_interval(secs=>interval_s) WHERE plugin_id=%s",
                (now, row["plugin_id"]),
            )
    for row in due:
        try:
            _, _, spec = catalog.find(row["plugin_id"])
        except SetupError:
            continue
        job_id = jobs.create("sync", f"Syncing {spec.name}", row["plugin_id"])
        jobs.submit(job_id, backfill_job, row["plugin_id"], "sync")
    with _conn() as conn:
        conn.execute("DELETE FROM tl.setup_jobs WHERE kind='sync' AND created_at < now()-interval '3 days'")
