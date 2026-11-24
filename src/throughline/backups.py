"""Whole-install backups: the Throughline database and, when the owner opts
in, the other databases, Docker databases and files an install needs, on a
schedule the owner sets.

`throughline.ops.backup` dumps the one Throughline database with counts and
hashes taken in the dump's own snapshot (the restore drill compares against
them). A *bundle* wraps it. By default that dump is all a bundle holds; the
settings can add:

  * other databases of the same cluster (include.databases: a list of names,
    or "all" for every database discovered at run time), minus exclude
    patterns (test and drill databases by default), one
    `pg_dump --format=custom` each;
  * the cluster's roles and grants (include.roles, `pg_dumpall
    --globals-only`): the app and reader roles a restored Throughline
    database expects;
  * databases inside Docker containers (for example a photo library's),
    through `docker exec <container> pg_dumpall`;
  * file trees (include.paths: state cursors, configuration, secrets files,
    exports, a source checkout, service units, ...) as one tar.zst.
    ~/.throughline is the usual first entry.

Every part is encrypted (gpg symmetric with a passphrase file by default, or
the owner's own encrypt/decrypt commands) before it is kept. A run writes
`<dir>/throughline-<stamp>-<hex>.partial/`, renames it when every part is in,
writes MANIFEST.json (names, sizes and hashes only), keeps the newest `keep`
bundles and, when an off-site target is set, copies the bundle there (rsync
over ssh to `host:path`, a local path, or a command template) and prunes the
remote to `offsite.keep`.

Settings live in a JSON file (THROUGHLINE_BACKUP_CONFIG, default
~/.throughline/config/backup.json) so the scheduler can read them while the
database is down; `throughline lib backup settings` and
GET/PUT /v1/backup/settings edit the same file. `throughline lib backup tick`
(run by a systemd timer every 15 minutes) runs a bundle when the schedule
says one is due and the restore drill when the verify interval has passed.
Commands run without a shell, from argument lists, with a timeout.
"""

import copy
import fcntl
import fnmatch
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from . import ops
from .db import connect, database_url, direct_connection
from .ops import OpsError, redact, run_command, sha256_file

BUNDLE_RE = re.compile(r"^throughline-\d{8}T\d{6,12}Z-[0-9a-f]{6}$")
DUMP_MAGIC = ops.DUMP_MAGIC
STATE_HOME = "~/.throughline"
RESTORE_SCOPES = ("database", "config", "lenses", "pages")

DEFAULTS = {
    "enabled": True,
    # Bundles start at `at` (local time) and every `every_hours` after it.
    "schedule": {"every_hours": 24, "at": "03:30"},
    # Restore drill of the Throughline database plus a decrypt-and-list check
    # of every other part. 0 turns it off.
    "verify": {"every_hours": 168},
    # Where bundles are written and how many are kept there.
    "destination": {"dir": f"{STATE_HOME}/backups", "keep": 3},
    # kind: none | rsync (target host:path or a local path) | command
    # (template with {dir} and {name}, no shell).
    "offsite": {
        "kind": "none",
        "target": None,
        "ssh_key": None,
        "command": None,
        "keep": 14,
    },
    # kind: gpg (symmetric AES256, passphrase from passphrase_file) | command
    # (encrypt_cmd/decrypt_cmd templates with {in} and {out}).
    "encryption": {
        "kind": "gpg",
        "passphrase_file": f"{STATE_HOME}/backup.passphrase",
        "encrypt_cmd": None,
        "decrypt_cmd": None,
    },
    "include": {
        # Other databases of the cluster: a list of names, or "all" (every
        # database the owner URL's server holds). Opt-in: by default a bundle
        # holds the Throughline database only.
        "databases": [],
        "exclude_databases": ["postgres", "template*", "tl_test_*", "tl_verify_*"],
        # Cluster roles and grants (pg_dumpall --globals-only); opt-in.
        "roles": False,
        # [{"container": "immich_postgres", "user": "postgres"}]
        "containers": [],
        "docker": {"command": "docker", "context": None, "config": None},
        # File trees to archive; opt-in (for example ["~/.throughline"]).
        "paths": [],
        # tar --exclude patterns; a bare name matches at any depth.
        "exclude_paths": [
            f"{STATE_HOME}/pg",
            f"{STATE_HOME}/pg-linux",
            f"{STATE_HOME}/dumps",
            f"{STATE_HOME}/locks",
            # Pre-upgrade snapshots are rolled back from locally; bundles hold
            # the same database already.
            f"{STATE_HOME}/upgrade-snapshots",
            f"{STATE_HOME}/immich/postgres",
            ".venv",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
        ],
    },
    # Age past which status calls the last bundle / drill stale; null derives
    # 1.5 x the interval (at least 36 h for bundles).
    "stale_hours": None,
    "verify_stale_hours": None,
    # A failed run is retried after this many minutes, not on every tick.
    "retry_minutes": 60,
}

_TYPES = {
    "enabled": bool,
    "schedule.every_hours": (int, float),
    "schedule.at": str,
    "verify.every_hours": (int, float),
    "destination.dir": str,
    "destination.keep": int,
    "offsite.kind": str,
    "offsite.target": (str, type(None)),
    "offsite.ssh_key": (str, type(None)),
    "offsite.command": (str, list, type(None)),
    "offsite.keep": int,
    "encryption.kind": str,
    "encryption.passphrase_file": (str, type(None)),
    "encryption.encrypt_cmd": (str, list, type(None)),
    "encryption.decrypt_cmd": (str, list, type(None)),
    "include.databases": (str, list),
    "include.exclude_databases": list,
    "include.roles": bool,
    "include.containers": list,
    "include.docker.command": str,
    "include.docker.context": (str, type(None)),
    "include.docker.config": (str, type(None)),
    "include.paths": list,
    "include.exclude_paths": list,
    "stale_hours": (int, float, type(None)),
    "verify_stale_hours": (int, float, type(None)),
    "retry_minutes": (int, float),
}


# --- settings ------------------------------------------------------------------------


def config_path():
    return Path(
        os.path.expanduser(
            os.getenv("THROUGHLINE_BACKUP_CONFIG") or f"{STATE_HOME}/config/backup.json"
        )
    )


def _expand(path):
    return None if path is None else os.path.expanduser(str(path))


def _merge(base, overlay):
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _env_defaults():
    """The older THROUGHLINE_BACKUP_* variables seed the defaults, so an
    install configured through the environment keeps working."""
    base = copy.deepcopy(DEFAULTS)
    if os.getenv("THROUGHLINE_BACKUP_DIR"):
        base["destination"]["dir"] = os.environ["THROUGHLINE_BACKUP_DIR"]
    if os.getenv("THROUGHLINE_BACKUP_KEEP"):
        try:
            base["destination"]["keep"] = int(os.environ["THROUGHLINE_BACKUP_KEEP"])
        except ValueError:
            pass
    if os.getenv("THROUGHLINE_BACKUP_ENCRYPT_CMD"):
        base["encryption"].update(
            kind="command",
            encrypt_cmd=os.environ["THROUGHLINE_BACKUP_ENCRYPT_CMD"],
            decrypt_cmd=os.getenv("THROUGHLINE_BACKUP_DECRYPT_CMD"),
        )
    if os.getenv("THROUGHLINE_BACKUP_OFFSITE_CMD"):
        base["offsite"].update(
            kind="command", command=os.environ["THROUGHLINE_BACKUP_OFFSITE_CMD"]
        )
    if os.getenv("THROUGHLINE_BACKUP_STALE_HOURS"):
        try:
            base["stale_hours"] = float(os.environ["THROUGHLINE_BACKUP_STALE_HOURS"])
        except ValueError:
            pass
    return base


def _flat(settings, prefix=""):
    for key, value in settings.items():
        name = prefix + key
        if isinstance(value, dict) and any(t.startswith(name + ".") for t in _TYPES):
            yield from _flat(value, name + ".")
        else:
            yield name, value


def validate(settings):
    """Raise ValueError naming the first bad setting; return the settings."""
    known = set(_TYPES)
    for name, value in _flat(settings):
        if name not in known:
            raise ValueError(f"unknown setting {name}")
        expected = _TYPES[name]
        if isinstance(value, bool) and expected in (int, (int, float)):
            raise ValueError(f"{name} must be a number")
        if not isinstance(value, expected):
            raise ValueError(f"{name} has the wrong type")
    if not 0.25 <= float(settings["schedule"]["every_hours"]) <= 24 * 31:
        raise ValueError("schedule.every_hours must be between 0.25 and 744")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", settings["schedule"]["at"]):
        raise ValueError("schedule.at must be HH:MM (24-hour, local time)")
    if float(settings["verify"]["every_hours"]) < 0:
        raise ValueError("verify.every_hours must be 0 (off) or more")
    if settings["destination"]["keep"] < 1 or settings["offsite"]["keep"] < 1:
        raise ValueError("keep must be at least 1")
    if not str(settings["destination"]["dir"]).strip():
        raise ValueError("destination.dir is required")
    off = settings["offsite"]
    if off["kind"] not in ("none", "rsync", "command"):
        raise ValueError("offsite.kind must be none, rsync or command")
    if off["kind"] == "rsync" and not off["target"]:
        raise ValueError("offsite.target is required for rsync")
    if off["kind"] == "command" and not off["command"]:
        raise ValueError("offsite.command is required for kind command")
    enc = settings["encryption"]
    if enc["kind"] not in ("gpg", "command"):
        raise ValueError("encryption.kind must be gpg or command")
    if enc["kind"] == "gpg" and not enc["passphrase_file"]:
        raise ValueError("encryption.passphrase_file is required for gpg")
    if enc["kind"] == "command" and not (enc["encrypt_cmd"] and enc["decrypt_cmd"]):
        raise ValueError("encryption.encrypt_cmd and decrypt_cmd are required")
    dbs = settings["include"]["databases"]
    if isinstance(dbs, str) and dbs != "all":
        raise ValueError('include.databases must be "all" or a list of names')
    for entry in settings["include"]["containers"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("container"), str):
            raise ValueError("include.containers entries need a container name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", entry["container"]):
            raise ValueError("include.containers: invalid container name")
    if float(settings["retry_minutes"]) < 1:
        raise ValueError("retry_minutes must be at least 1")
    return settings


def load():
    """Settings: defaults (seeded from THROUGHLINE_BACKUP_*) overlaid with the
    settings file. A broken file raises ValueError rather than silently
    backing up less than the owner asked for."""
    path = config_path()
    stored = {}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"backup settings file unreadable: {exc}") from None
        if not isinstance(stored, dict):
            raise ValueError("backup settings file must hold a JSON object")
    return validate(_merge(_env_defaults(), stored))


def save(changes):
    """Deep-merge `changes` into the stored settings, validate and write the
    file atomically (0600). Returns the effective settings."""
    path = config_path()
    stored = {}
    if path.is_file():
        stored = json.loads(path.read_text(encoding="utf-8") or "{}")
    stored = _merge(stored, changes)
    validate(_merge(_env_defaults(), stored))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".backup-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(stored, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return load()


def set_value(changes, dotted, raw):
    """Apply `dotted.key=value` (value parsed as JSON, else a string)."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    node = changes
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    return changes


def public(settings):
    """Settings as shown over HTTP: the passphrase file's path and whether it
    exists, never its content."""
    out = copy.deepcopy(settings)
    key = _expand(settings["encryption"].get("passphrase_file"))
    out["encryption"]["passphrase_present"] = bool(key and Path(key).is_file())
    out["config_file"] = str(config_path())
    return out


# --- schedule ------------------------------------------------------------------------


def _local(dt):
    return dt.astimezone()


def last_slot(settings, now=None):
    """The latest scheduled start at or before now (aware datetime)."""
    now = _local(now or datetime.now(timezone.utc))
    hour, minute = (int(x) for x in settings["schedule"]["at"].split(":"))
    anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    step = timedelta(hours=float(settings["schedule"]["every_hours"]))
    if anchor > now:
        # Step back from today's anchor into the past, keeping the phase.
        while anchor > now:
            anchor -= step
        return anchor
    while anchor + step <= now:
        anchor += step
    return anchor


def next_slot(settings, now=None):
    return last_slot(settings, now) + timedelta(
        hours=float(settings["schedule"]["every_hours"])
    )


def stale_limits(settings):
    every = float(settings["schedule"]["every_hours"])
    dump = settings.get("stale_hours") or max(36.0, every * 1.5)
    verify_every = float(settings["verify"]["every_hours"])
    verify = settings.get("verify_stale_hours") or (
        None if verify_every == 0 else max(36.0, verify_every * 1.5)
    )
    return float(dump), None if verify is None else float(verify)


def _attempts(conn):
    """Last bundle attempt, last complete bundle and last ok drill."""
    row = conn.execute(
        """SELECT max(started_at) FILTER (WHERE kind='dump') AS last_attempt_at,
                  (array_agg(status ORDER BY id DESC) FILTER (WHERE kind='dump'))[1] AS last_status,
                  max(started_at) FILTER (WHERE kind='dump' AND status='ok'
                       AND coalesce((detail->'bundle'->>'ok')::boolean, true)) AS last_ok_at,
                  max(finished_at) FILTER (WHERE kind='verify' AND status='ok') AS last_verified_at,
                  max(finished_at) FILTER (WHERE kind='verify') AS last_verify_attempt_at
           FROM tl.backup_runs"""
    ).fetchone()
    return dict(row)


def due(settings, conn, now=None):
    """{backup: bool, verify: bool, reason} for this tick."""
    now = now or datetime.now(timezone.utc)
    seen = _attempts(conn)
    retry = timedelta(minutes=float(settings["retry_minutes"]))
    slot = last_slot(settings, now)
    backup = False
    reason = "not_due"
    if not settings["enabled"]:
        reason = "disabled"
    elif seen["last_ok_at"] is None or seen["last_ok_at"] < slot:
        attempted = seen["last_attempt_at"]
        if attempted is None or attempted < slot:
            backup, reason = True, "scheduled"
        elif now - attempted >= retry:
            backup, reason = True, "retry"
        else:
            reason = "retry_wait"
    verify = False
    every = float(settings["verify"]["every_hours"])
    if settings["enabled"] and every > 0:
        last = seen["last_verified_at"]
        tried = seen["last_verify_attempt_at"]
        if last is None or now - last >= timedelta(hours=every):
            # A failed drill waits retry_minutes before the next attempt.
            verify = tried is None or tried == last or now - tried >= retry
    return dict(backup=backup, verify=verify, reason=reason, **seen)


def status(conn, settings=None):
    """What /v1/backup/status and `lib backup status` answer."""
    settings = settings or load()
    seen = _attempts(conn)
    dump_limit, verify_limit = stale_limits(settings)
    now = datetime.now(timezone.utc)

    def age(at):
        return None if at is None else round((now - at).total_seconds() / 3600, 3)

    reasons = []
    if seen["last_ok_at"] is None:
        reasons.append("no_backup")
    elif age(seen["last_ok_at"]) > dump_limit:
        reasons.append("backup_stale")
    if seen["last_status"] == "failed" or (
        seen["last_attempt_at"] is not None
        and seen["last_ok_at"] != seen["last_attempt_at"]
    ):
        reasons.append("last_run_failed")
    if verify_limit is not None:
        if seen["last_verified_at"] is None:
            reasons.append("no_verify")
        elif age(seen["last_verified_at"]) > verify_limit:
            reasons.append("verify_stale")
    if not settings["enabled"]:
        reasons.append("disabled")
    latest = conn.execute(
        """SELECT id,started_at,finished_at,status,bytes,detail->'bundle' AS bundle,detail->>'error' AS error
           FROM tl.backup_runs WHERE kind='dump' ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    return dict(
        ok=not reasons,
        reasons=reasons,
        enabled=settings["enabled"],
        next_run_at=next_slot(settings, now).isoformat(),
        last_attempt_at=seen["last_attempt_at"],
        last_ok_at=seen["last_ok_at"],
        backup_age_hours=age(seen["last_ok_at"]),
        last_verified_at=seen["last_verified_at"],
        verify_age_hours=age(seen["last_verified_at"]),
        stale_hours=dump_limit,
        verify_stale_hours=verify_limit,
        destination=settings["destination"]["dir"],
        offsite=None
        if settings["offsite"]["kind"] == "none"
        else settings["offsite"]["target"] or "command",
        latest=None if latest is None else dict(latest),
    )


# --- commands ------------------------------------------------------------------------


def _encrypt_template(settings):
    enc = settings["encryption"]
    if enc["kind"] == "command":
        return enc["encrypt_cmd"]
    key = _passphrase(settings)
    # --compress-algo none: every part is already compressed.
    return [
        "gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback",
        "--passphrase-file", str(key), "--symmetric", "--cipher-algo", "AES256",
        "--compress-algo", "none", "--output", "{out}", "{in}",
    ]  # fmt: skip


def _decrypt_template(settings):
    enc = settings["encryption"]
    if enc["kind"] == "command":
        return enc["decrypt_cmd"]
    key = _passphrase(settings)
    return [
        "gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback",
        "--passphrase-file", str(key), "--decrypt", "--output", "{out}", "{in}",
    ]  # fmt: skip


def _passphrase(settings):
    key = Path(_expand(settings["encryption"]["passphrase_file"]))
    if not key.is_file() or key.stat().st_size == 0:
        raise OpsError(
            f"backup passphrase file {key} is missing; run"
            " `throughline lib backup init-key` (and keep a copy off this machine)"
        )
    return key


def init_key(settings=None):
    """Create the gpg passphrase file (0600) if it does not exist."""
    settings = settings or load()
    key = Path(_expand(settings["encryption"]["passphrase_file"]))
    if key.exists():
        return dict(created=False, file=str(key))
    key.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secrets.token_urlsafe(48) + "\n")
    return dict(created=True, file=str(key))


def _run_to_file(args, path, *, step, env=None):
    """Run a program without a shell, streaming its stdout into `path`."""
    with open(path, "wb") as out:
        try:
            result = subprocess.run(
                args,
                shell=False,
                stdout=out,
                stderr=subprocess.PIPE,
                timeout=ops._timeout(),
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpsError(f"{step} timed out after {exc.timeout:g} s") from None
        except OSError as exc:
            raise OpsError(f"{step} could not start: {redact(str(exc))}") from None
    if result.returncode != 0:
        raise OpsError(
            f"{step} failed with exit code {result.returncode}: {redact(result.stderr)}"
        )


def _tar(args):
    """GNU tar exits 1 when a file changed while it was read (logs, cursors):
    the archive is complete, so only exit codes above 1 fail."""
    try:
        result = subprocess.run(
            args, shell=False, capture_output=True, timeout=ops._timeout()
        )
    except subprocess.TimeoutExpired as exc:
        raise OpsError(f"tar timed out after {exc.timeout:g} s") from None
    except OSError as exc:
        raise OpsError(f"tar could not start: {redact(str(exc))}") from None
    if result.returncode > 1:
        raise OpsError(
            f"tar failed with exit code {result.returncode}: {redact(result.stderr)}"
        )


def _encrypt(settings, plain, final):
    ops._private_file(final.parent, final.name)
    run_command(
        ops.command_args(_encrypt_template(settings), **{"in": plain, "out": final}),
        step=f"encrypt {plain.name}",
    )
    if not final.is_file() or final.stat().st_size == 0:
        raise OpsError(f"encrypt produced no output for {plain.name}")
    with open(final, "rb") as handle:
        head = handle.read(8)
    if head.startswith(DUMP_MAGIC) or head.startswith(b"\x28\xb5\x2f\xfd"):
        raise OpsError(f"encrypt output for {plain.name} is not encrypted")


def _docker_env(settings):
    docker = settings["include"]["docker"]
    env = dict(os.environ)
    if docker.get("config"):
        env["DOCKER_CONFIG"] = _expand(docker["config"])
    base = [docker.get("command") or "docker"]
    if docker.get("context"):
        base += ["--context", docker["context"]]
    return base, env


# --- what to include -----------------------------------------------------------------


def databases(settings, conn):
    """The cluster's databases this bundle dumps (the live Throughline one
    excluded: ops.backup dumps it with its snapshot values)."""
    live = conninfo_to_dict(database_url()).get("dbname")
    names = [
        r["datname"]
        for r in conn.execute(
            "SELECT datname FROM pg_database WHERE NOT datistemplate AND datallowconn ORDER BY datname"
        )
    ]
    wanted = settings["include"]["databases"]
    if wanted != "all":
        missing = sorted(set(wanted) - set(names))
        if missing:
            raise OpsError(f"databases not found: {', '.join(missing)}")
        names = [n for n in names if n in wanted]
    patterns = settings["include"]["exclude_databases"]
    return [
        n
        for n in names
        if n != live and not any(fnmatch.fnmatchcase(n, p) for p in patterns)
    ]


def _tar_args(settings, archive, destination):
    paths = []
    for raw in settings["include"]["paths"]:
        path = Path(_expand(raw)).resolve()
        if path.exists():
            paths.append(path)
    if not paths:
        return None, []
    excludes = []
    for raw in settings["include"]["exclude_paths"]:
        expanded = _expand(raw)
        if os.path.isabs(expanded):
            excludes.append(str(Path(expanded).resolve()).lstrip("/"))
        else:
            excludes.append(expanded)
    # Never archive the backups themselves.
    excludes.append(str(Path(destination).resolve()).lstrip("/"))
    args = [
        "tar", "--create", "--zstd", "--file", str(archive), "--directory", "/",
        "--warning=no-file-changed", "--warning=no-file-removed",
        "--ignore-failed-read", "--anchored", "--no-wildcards-match-slash",
    ]  # fmt: skip
    for pattern in excludes:
        if "/" in pattern:
            args.append(f"--exclude={pattern}")
        else:
            args += ["--no-anchored", f"--exclude={pattern}", "--anchored"]
    args.append("--")
    args += [str(p).lstrip("/") or "." for p in paths]
    return args, [str(p) for p in paths]


# --- the bundle ----------------------------------------------------------------------


@contextmanager
def _lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    handle = open(directory / ".backup.lock", "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OpsError("another backup is running") from None
        yield
    finally:
        handle.close()


def bundles(directory):
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir() if p.is_dir() and BUNDLE_RE.match(p.name)
    )


def _retain(directory, keep):
    found = bundles(directory)
    removed = []
    for path in found[: max(0, len(found) - keep)]:
        shutil.rmtree(path)
        removed.append(path.name)
    for stale in Path(directory).glob("throughline-*.partial"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
    return removed


def _part(name, path, kind, **extra):
    return dict(
        name=name,
        file=path.name,
        kind=kind,
        bytes=path.stat().st_size,
        sha256=sha256_file(path),
        **extra,
    )


def run(settings=None, *, on_part=None):
    """Write one bundle; returns its manifest. Raises OpsError when the
    Throughline database itself could not be dumped; a failure in any other
    part is recorded (manifest ok false) and the remaining parts still run."""
    settings = settings or load()
    destination = Path(_expand(settings["destination"]["dir"]))
    with _lock(destination):
        return _run_locked(settings, destination, on_part)


def _run_locked(settings, destination, on_part):
    encrypt = _encrypt_template(settings)  # fails early without a key
    started = datetime.now(timezone.utc)
    name = f"throughline-{started.strftime('%Y%m%dT%H%M%S%f')}Z-{secrets.token_hex(3)}"
    work = destination / (name + ".partial")
    work.mkdir(parents=True)
    os.chmod(work, 0o700)
    parts, errors = [], []

    def note(part):
        parts.append(part)
        if on_part:
            on_part(part)

    def failed(label, exc):
        message = str(exc) if isinstance(exc, OpsError) else redact(f"{exc!r}")
        errors.append(dict(part=label, error=message))
        if on_part:
            on_part(dict(name=label, error=message))

    # 1. The Throughline database, with counts and hashes (restore drill).
    try:
        result = ops.backup(
            work, encrypt_cmd=encrypt, offsite_cmd=False, keep=1, bundle=True
        )
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    main = Path(result["file"])
    note(
        dict(
            name=conninfo_to_dict(database_url()).get("dbname"),
            file=main.name,
            kind="throughline",
            bytes=result["bytes"],
            sha256=result["sha256"],
            dump_id=result["dump_id"],
        )
    )
    dump_id = result["dump_id"]
    dsn = conninfo_to_dict(database_url())
    pg_dump = ops._pg_program("pg_dump")

    # 2. Roles and grants.
    if settings["include"]["roles"]:
        try:
            plain = ops._private_file(work, "globals.sql")
            conninfo, env = ops._libpq(database_url())
            run_command(
                [
                    ops._pg_program("pg_dumpall"),
                    "--dbname",
                    conninfo,
                    "--globals-only",
                    "--file",
                    str(plain),
                ],
                step="pg_dumpall globals",
                env=env,
            )
            final = work / "globals.sql.gpg"
            _encrypt(settings, plain, final)
            note(_part("globals", final, "globals"))
        except Exception as exc:  # noqa: BLE001 - recorded, run continues
            failed("globals", exc)
        finally:
            (work / "globals.sql").unlink(missing_ok=True)

    # 3. Every other database in the cluster.
    try:
        with direct_connection(database_url(), row_factory=dict_row) as conn:
            names = databases(settings, conn)
    except Exception as exc:  # noqa: BLE001
        names = []
        failed("databases", exc)
    for db in names:
        plain = work / f"db-{db}.dump"
        try:
            ops._private_file(work, plain.name)
            conninfo, env = ops._libpq(make_conninfo(**{**dsn, "dbname": db}))
            run_command(
                [
                    pg_dump,
                    "--dbname",
                    conninfo,
                    "--format=custom",
                    "--file",
                    str(plain),
                ],
                step=f"pg_dump {db}",
                env=env,
            )
            final = work / (plain.name + ".gpg")
            _encrypt(settings, plain, final)
            note(_part(db, final, "database"))
        except Exception as exc:  # noqa: BLE001
            failed(f"db:{db}", exc)
        finally:
            plain.unlink(missing_ok=True)

    # 4. Databases inside containers.
    base, docker_env = _docker_env(settings)
    for entry in settings["include"]["containers"]:
        label = entry["container"]
        plain = work / f"container-{label}.sql"
        try:
            ops._private_file(work, plain.name)
            _run_to_file(
                base
                + [
                    "exec",
                    label,
                    "pg_dumpall",
                    "--clean",
                    "--if-exists",
                    "-U",
                    entry.get("user") or "postgres",
                ],
                plain,
                step=f"docker pg_dumpall {label}",
                env=docker_env,
            )
            if plain.stat().st_size == 0:
                raise OpsError(f"docker pg_dumpall {label} wrote nothing")
            packed = work / (plain.name + ".zst")
            run_command(
                ["zstd", "-q", "-T0", "--rm", str(plain), "-o", str(packed)],
                step=f"zstd {label}",
            )
            final = work / (packed.name + ".gpg")
            _encrypt(settings, packed, final)
            note(_part(label, final, "container"))
        except Exception as exc:  # noqa: BLE001
            failed(f"container:{label}", exc)
        finally:
            plain.unlink(missing_ok=True)
            (work / f"container-{label}.sql.zst").unlink(missing_ok=True)

    # 5. Files.
    archive = work / "files.tar.zst"
    try:
        args, roots = _tar_args(settings, archive, destination)
        if args:
            ops._private_file(work, archive.name)
            _tar(args)
            final = work / (archive.name + ".gpg")
            _encrypt(settings, archive, final)
            note(_part("files", final, "files", paths=roots))
    except Exception as exc:  # noqa: BLE001
        failed("files", exc)
    finally:
        archive.unlink(missing_ok=True)

    manifest = dict(
        format=1,
        name=name,
        started_at=started.isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
        encryption=settings["encryption"]["kind"],
        ok=not errors,
        parts=parts,
        errors=errors,
        bytes=sum(p["bytes"] for p in parts),
    )
    (work / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    final_dir = destination / name
    work.rename(final_dir)
    manifest["dir"] = str(final_dir)

    # 6. Off-site copy and retention.
    manifest["offsite"] = None
    if settings["offsite"]["kind"] != "none":
        manifest["offsite"] = offsite(settings, final_dir, dump_id)
        if manifest["offsite"]["status"] != "ok":
            manifest["ok"] = False
    manifest["removed"] = _retain(destination, int(settings["destination"]["keep"]))
    _annotate(dump_id, manifest)
    return manifest


def _annotate(dump_id, manifest):
    """Store the bundle summary on the Throughline dump row: status and the
    staleness check count a bundle only when every part made it."""
    summary = dict(
        ok=manifest["ok"],
        name=manifest["name"],
        bytes=manifest["bytes"],
        parts=[
            {k: p.get(k) for k in ("name", "kind", "bytes", "sha256")}
            for p in manifest["parts"]
        ],
        errors=manifest["errors"],
        offsite=(manifest.get("offsite") or {}).get("status"),
    )
    with connect() as conn:
        conn.execute(
            "UPDATE tl.backup_runs SET detail=detail||jsonb_build_object('bundle',%s::jsonb) WHERE id=%s",
            (json.dumps(summary), dump_id),
        )


# --- off-site ------------------------------------------------------------------------


def _remote(target):
    """(host, path) for host:path, or (None, path) for a local path."""
    if re.match(r"^[^/:]+:", target):
        host, path = target.split(":", 1)
        return host, path or "."
    return None, _expand(target)


def _ssh(settings):
    args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    key = settings["offsite"].get("ssh_key")
    if key:
        args += ["-i", _expand(key), "-o", "IdentitiesOnly=yes"]
    return args


def offsite(settings, bundle_dir, dump_id=None):
    off = settings["offsite"]
    started = datetime.now(timezone.utc)
    detail = dict(bundle=bundle_dir.name, kind=off["kind"])
    try:
        if off["kind"] == "command":
            run_command(
                ops.command_args(off["command"], dir=bundle_dir, name=bundle_dir.name),
                step="offsite",
            )
        else:
            host, path = _remote(off["target"])
            dest = f"{host}:{path.rstrip('/')}/" if host else path.rstrip("/") + "/"
            if not host:
                Path(path).mkdir(parents=True, exist_ok=True)
            args = ["rsync", "-a", "--partial", "--mkpath", "--chmod=D700,F600"]
            if host:
                args += ["-e", shlex.join(_ssh(settings))]
            # The manifest goes last: a remote bundle without one is incomplete.
            run_command(
                args + ["--exclude=MANIFEST.json", str(bundle_dir), dest],
                step="offsite rsync",
            )
            run_command(
                args
                + [str(bundle_dir / "MANIFEST.json"), dest + bundle_dir.name + "/"],
                step="offsite rsync manifest",
            )
            detail["removed"] = _prune_remote(settings, host, path)
        status = "ok"
    except OpsError as exc:
        detail["error"] = str(exc)
        status = "failed"
    with connect() as conn:
        run_id = conn.execute(
            "INSERT INTO tl.backup_runs(kind,started_at,finished_at,status,detail) VALUES ('offsite',%s,now(),%s,%s::jsonb) RETURNING id",
            (started, status, json.dumps(dict(detail, dump_id=dump_id))),
        ).fetchone()["id"]
    return dict(id=run_id, status=status, **detail)


def _prune_remote(settings, host, path):
    keep = int(settings["offsite"]["keep"])
    if host is None:
        found = bundles(path)
        removed = [p.name for p in found[: max(0, len(found) - keep)]]
        for name in removed:
            shutil.rmtree(Path(path) / name)
        return removed
    listing = run_command(
        _ssh(settings) + [host, "ls", "-1", "--", shlex.quote(path)],
        step="offsite list",
    ).stdout.decode("utf-8", "replace")
    names = sorted(n for n in listing.split() if BUNDLE_RE.match(n))
    removed = names[: max(0, len(names) - keep)]
    if removed:
        targets = [shlex.quote(f"{path.rstrip('/')}/{n}") for n in removed]
        run_command(
            _ssh(settings) + [host, "rm", "-rf", "--", *targets], step="offsite prune"
        )
    return removed


# --- verify --------------------------------------------------------------------------


def latest(settings=None):
    settings = settings or load()
    found = bundles(_expand(settings["destination"]["dir"]))
    return found[-1] if found else None


def verify(bundle_dir=None, settings=None, *, scratch_url=None):
    """Restore drill of the bundle's Throughline dump (ops.verify_restore,
    recorded as kind verify) plus, for every other part, decrypt it and check
    it reads: `pg_restore --list` for dumps, `tar --list` for files, `zstd
    --test` for container dumps, sha256 against the manifest."""
    settings = settings or load()
    bundle_dir = Path(bundle_dir) if bundle_dir else latest(settings)
    if bundle_dir is None or not (bundle_dir / "MANIFEST.json").is_file():
        raise OpsError("no complete backup bundle to verify")
    manifest = json.loads((bundle_dir / "MANIFEST.json").read_text(encoding="utf-8"))
    decrypt = _decrypt_template(settings)
    # Decrypted parts are as large as the databases: keep them beside the
    # bundles on disk, not in the (often tmpfs) system temporary directory.
    work = Path(_expand(settings["destination"]["dir"])) / ".verify-tmp"
    work.mkdir(parents=True, exist_ok=True)
    os.chmod(work, 0o700)
    checks = []
    drill = None
    for part in manifest["parts"]:
        path = bundle_dir / part["file"]
        if not path.is_file() or sha256_file(path) != part["sha256"]:
            checks.append(dict(name=part["name"], ok=False, error="hash mismatch"))
            continue
        if part["kind"] == "throughline":
            drill = ops.verify_restore(
                path, scratch_url=scratch_url, decrypt_cmd=decrypt, work_dir=work
            )
            checks.append(
                dict(
                    name=part["name"],
                    ok=drill["ok"],
                    error=drill.get("error"),
                    probes_failed=[
                        p["name"]
                        for p in drill["probes"]
                        if not p["ok"] and not p.get("live_fails")
                    ],
                    probes_failed_live_too=[
                        p["name"] for p in drill["probes"] if p.get("live_fails")
                    ],
                )
            )
            continue
        temp = Path(tempfile.mkdtemp(prefix="tl-bundle-verify-", dir=work))
        try:
            plain = ops._private_file(temp, "part")
            run_command(
                ops.command_args(decrypt, **{"in": path, "out": plain}),
                step=f"decrypt {part['file']}",
            )
            if part["kind"] == "database":
                run_command(
                    [ops._pg_program("pg_restore"), "--list", str(plain)],
                    step=f"pg_restore --list {part['name']}",
                )
            elif part["kind"] == "files":
                run_command(
                    ["tar", "--list", "--zstd", "--file", str(plain)],
                    step="tar --list",
                )
            elif part["kind"] == "container":
                run_command(["zstd", "-q", "--test", str(plain)], step="zstd --test")
            elif part["kind"] == "globals":
                if b"CREATE ROLE" not in plain.read_bytes()[: 1 << 20]:
                    raise OpsError("globals.sql has no roles")
            checks.append(dict(name=part["name"], ok=True))
        except OpsError as exc:
            checks.append(dict(name=part["name"], ok=False, error=str(exc)))
        finally:
            shutil.rmtree(temp, ignore_errors=True)
    ok = bool(checks) and all(c["ok"] for c in checks) and manifest.get("ok", False)
    return dict(
        ok=ok,
        bundle=bundle_dir.name,
        bundle_ok=manifest.get("ok", False),
        parts=checks,
        drill=None
        if drill is None
        else {k: drill.get(k) for k in ("ok", "run_id", "duration_s", "error")},
    )


# --- inspect & partial restore -------------------------------------------------------


def inspect_bundle(bundle_dir):
    """Read a bundle's MANIFEST.json and summarize which partial-restore scopes
    are present (database, config, lenses, pages) without decrypting."""
    bundle_dir = Path(bundle_dir).expanduser()
    manifest_path = bundle_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        raise OpsError(f"not a complete backup bundle (missing MANIFEST.json): {bundle_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OpsError(f"could not read bundle manifest: {exc}") from None
    parts = manifest.get("parts") or []
    kinds = {p.get("kind") for p in parts}
    available = []
    if "throughline" in kinds:
        available.append("database")
    if "files" in kinds:
        available.extend(["config", "lenses", "pages"])
    return dict(
        name=manifest.get("name") or bundle_dir.name,
        dir=str(bundle_dir),
        started_at=manifest.get("started_at"),
        finished_at=manifest.get("finished_at"),
        ok=bool(manifest.get("ok", False)),
        encryption=manifest.get("encryption"),
        bytes=manifest.get("bytes") or 0,
        available_scopes=available,
        parts=[
            {
                "name": p.get("name"),
                "kind": p.get("kind"),
                "file": p.get("file"),
                "bytes": p.get("bytes"),
                "present": (bundle_dir / str(p.get("file") or "")).is_file(),
            }
            for p in parts
        ],
    )


def list_bundles(settings=None):
    """Inspect all bundles in the configured backup directory, newest first."""
    settings = settings or load()
    found = []
    for path in reversed(bundles(_expand(settings["destination"]["dir"]))):
        if not (path / "MANIFEST.json").is_file():
            continue
        try:
            found.append(inspect_bundle(path))
        except OpsError:
            continue
    return found


def _decrypt_for_restore(settings, src, dst, passphrase_file=None):
    if passphrase_file:
        key = Path(passphrase_file).expanduser()
        if not key.is_file() or key.stat().st_size == 0:
            raise OpsError(f"passphrase file is missing or empty: {key}")
        template = [
            "gpg", "--batch", "--yes", "--quiet", "--pinentry-mode", "loopback",
            "--passphrase-file", str(key), "--decrypt", "--output", "{out}", "{in}",
        ]
    else:
        template = _decrypt_template(settings)
    run_command(
        ops.command_args(template, **{"in": src, "out": dst}),
        step=f"decrypt {src.name}",
    )


def _throughline_relpath(member_name):
    """Map an archived path (e.g. home/user/.throughline/config/settings.json)
    to its path relative to ~/.throughline, or None if outside."""
    parts = [p for p in Path(member_name).parts if p not in ("", "/", ".")]
    if ".." in parts:
        return None
    for idx, part in enumerate(parts):
        if part == ".throughline":
            rel = parts[idx + 1 :]
            return Path(*rel) if rel else None
    return None


def _merge_onboarding_json(dest_path, incoming_bytes, *, merge, overwrite):
    try:
        incoming = json.loads(incoming_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OpsError(f"backup file {dest_path.name} is not valid JSON: {exc}") from None
    if not isinstance(incoming, dict):
        raise OpsError(f"backup file {dest_path.name} must be a JSON object")
    existing = {"env": {}, "plugins": {}}
    if dest_path.is_file() and merge and not overwrite:
        try:
            loaded = json.loads(dest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            pass
    merged = {
        "env": dict(existing.get("env") or {}),
        "plugins": {
            k: dict(v) for k, v in (existing.get("plugins") or {}).items() if isinstance(v, dict)
        },
    }
    added_env, added_plugins = 0, 0
    for k, v in (incoming.get("env") or {}).items():
        if overwrite or k not in merged["env"]:
            merged["env"][k] = v
            added_env += 1
    for pid, values in (incoming.get("plugins") or {}).items():
        if not isinstance(values, dict):
            continue
        target = merged["plugins"].setdefault(pid, {})
        for k, v in values.items():
            if overwrite or k not in target:
                target[k] = v
                added_plugins += 1
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest_path.parent, prefix="." + dest_path.name, suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, indent=2, sort_keys=True)
        os.replace(tmp, dest_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dict(env_keys=added_env, plugin_keys=added_plugins)


def _restore_files_part(archive_path, scopes, *, home_dir, setup_dir, merge, overwrite):
    """Extract only the selected scopes (config, lenses, pages) from a
    decrypted files.tar.zst archive without overwriting unrelated host files."""
    listing = run_command(
        ["tar", "--list", "--zstd", "--file", str(archive_path)],
        step="tar --list",
    ).stdout.decode("utf-8", "replace")
    want = []
    for raw in listing.splitlines():
        name = raw.strip()
        if not name or name.endswith("/"):
            continue
        rel = _throughline_relpath(name)
        if rel is None:
            continue
        rel_str = rel.as_posix()
        if "config" in scopes and rel.parts[0] == "config" and len(rel.parts) == 2:
            if rel.name not in ("setup-code", "restored") and rel.suffix == ".json":
                want.append(("config", name, rel))
        elif "lenses" in scopes and rel.parts[0] == "lenses" and rel.suffix == ".toml":
            want.append(("lenses", name, rel))
        elif "pages" in scopes and (
            rel_str == "pages.toml" or (rel.parts[0] == "pages" and rel.suffix == ".toml")
        ):
            want.append(("pages", name, rel))
    restored = {s: [] for s in scopes if s != "database"}
    skipped = []
    if not want:
        return restored, skipped
    with tempfile.TemporaryDirectory(prefix="tl-files-restore-") as extract_dir:
        os.chmod(extract_dir, 0o700)
        run_command(
            [
                "tar", "--extract", "--zstd", "--file", str(archive_path),
                "--directory", extract_dir, "--no-same-owner", "--no-same-permissions",
                "--", *[name for _, name, _ in want],
            ],
            step="tar --extract",
        )
        root_resolved = Path(extract_dir).resolve()
        for scope, name, rel in want:
            src = (root_resolved / name.lstrip("/")).resolve()
            if not src.is_relative_to(root_resolved) or not src.is_file() or src.is_symlink():
                skipped.append(rel.as_posix())
                continue
            if scope == "config":
                dst = setup_dir / rel.name
                if rel.name in ("settings.json", "secrets.json"):
                    stats = _merge_onboarding_json(
                        dst, src.read_bytes(), merge=merge, overwrite=overwrite
                    )
                    restored["config"].append({"file": rel.name, **stats})
                    continue
                if dst.exists() and not overwrite:
                    skipped.append(rel.as_posix())
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                data = src.read_bytes()
                fd = os.open(dst, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                restored["config"].append({"file": rel.name, "bytes": len(data)})
            else:
                dst = home_dir / rel
                if dst.exists() and not overwrite:
                    skipped.append(rel.as_posix())
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                restored[scope].append(rel.as_posix())
    if "config" in scopes:
        from .onboarding import store

        store.apply()
    return restored, skipped


def _restore_database_part(plain_dump, *, clean=False):
    from .cli import refused_transaction_timeout, restore_through_script
    from .db import cluster_lock, migrate as migrate_db

    with direct_connection(database_url(), row_factory=dict_row) as conn:
        ops._owner(conn)
        used = conn.execute(
            "SELECT to_regnamespace('tl') IS NOT NULL AS used"
        ).fetchone()["used"]
        if used and not clean:
            raise OpsError(
                "database already contains Throughline data; pass clean=True to replace it "
                "or omit 'database' for a partial config/lenses/pages restore"
            )
    restore_dsn, restore_env = ops._libpq(database_url())
    restore_env = {
        **restore_env,
        "PGOPTIONS": (
            os.environ.get("PGOPTIONS", "")
            + " -c throughline.bulk_restore=on"
            + " -c max_parallel_maintenance_workers=0"
        ).strip(),
    }
    args = [ops._pg_program("pg_restore"), "--dbname", restore_dsn, "--no-owner", "--exit-on-error"]
    if clean:
        args += ["--clean", "--if-exists"]
    args.append(str(plain_dump))
    try:
        run_command(args, step="pg_restore", env=restore_env)
    except OpsError as exc:
        if not refused_transaction_timeout(getattr(exc, "stderr", None)):
            raise
        with tempfile.TemporaryDirectory(prefix="tl-restore-sql-") as folder:
            restore_through_script(
                plain_dump,
                restore_dsn,
                lambda cmd: run_command(cmd, step=Path(cmd[0]).stem, env=restore_env),
                ops._private_file(folder, "restore.sql"),
                ops._private_file(folder, "restore.filtered.sql"),
            )
    with cluster_lock(), direct_connection(database_url(), row_factory=dict_row) as conn:
        ready = conn.execute(
            "SELECT to_regprocedure('tl.harden_reader()') IS NOT NULL AS ready"
        ).fetchone()["ready"]
        if ready:
            conn.execute("SELECT tl.harden_reader()")
        conn.commit()
    if not ready:
        migrate_db()
    return dict(restored=True)


def restore_bundle(
    bundle_dir=None,
    *,
    only=None,
    settings=None,
    passphrase_file=None,
    merge=True,
    overwrite=False,
    clean=False,
    continue_onboarding=True,
    home_dir=None,
    setup_dir=None,
):
    """Restore all or selected scopes from a backup bundle.

    `only` may be any subset of ('database', 'config', 'lenses', 'pages').
    By default existing onboarding config/secrets are merged (preserving keys
    already set on the server) and `continue_onboarding=True` keeps onboarding
    open in restore mode so additional data sources can be added afterwards.
    """
    settings = settings or load()
    bundle_path = Path(bundle_dir).expanduser() if bundle_dir else latest(settings)
    if bundle_path is None or not (bundle_path / "MANIFEST.json").is_file():
        raise OpsError("no complete backup bundle found to restore")
    manifest = json.loads((bundle_path / "MANIFEST.json").read_text(encoding="utf-8"))
    info = inspect_bundle(bundle_path)
    if only is None:
        scopes = list(info["available_scopes"])
    else:
        scopes = []
        for item in only:
            for token in str(item).split(","):
                name = token.strip().lower()
                if not name:
                    continue
                if name not in RESTORE_SCOPES:
                    raise OpsError(
                        f"unknown restore scope {name!r}; choose from {', '.join(RESTORE_SCOPES)}"
                    )
                if name not in scopes:
                    scopes.append(name)
    parts_by_kind = {p["kind"]: p for p in manifest.get("parts") or []}
    missing_parts = []
    if "database" in scopes and "throughline" not in parts_by_kind:
        missing_parts.append("database")
    file_scopes = [s for s in scopes if s in ("config", "lenses", "pages")]
    if file_scopes and "files" not in parts_by_kind:
        missing_parts.extend(file_scopes)
    if missing_parts:
        raise OpsError(
            f"bundle {bundle_path.name} does not contain: {', '.join(missing_parts)}"
        )

    from .onboarding import state as onboarding_state, store

    resolved_setup = Path(setup_dir).expanduser() if setup_dir else store.setup_dir()
    resolved_home = Path(home_dir).expanduser() if home_dir else Path(_expand(STATE_HOME))
    work = Path(_expand(settings["destination"]["dir"])) / ".restore-tmp"
    work.mkdir(parents=True, exist_ok=True)
    os.chmod(work, 0o700)
    temp = Path(tempfile.mkdtemp(prefix="tl-restore-", dir=work))
    os.chmod(temp, 0o700)
    restored = {}
    skipped = []
    try:
        if "database" in scopes:
            part = parts_by_kind["throughline"]
            src = bundle_path / part["file"]
            if not src.is_file() or sha256_file(src) != part["sha256"]:
                raise OpsError(f"hash mismatch on {part['file']}")
            with open(src, "rb") as handle:
                is_plain = handle.read(len(DUMP_MAGIC)) == DUMP_MAGIC
            plain = src
            if not is_plain:
                plain = ops._private_file(temp, "throughline.dump")
                _decrypt_for_restore(settings, src, plain, passphrase_file=passphrase_file)
                with open(plain, "rb") as handle:
                    if handle.read(len(DUMP_MAGIC)) != DUMP_MAGIC:
                        raise OpsError("decrypt did not produce a PostgreSQL dump")
            restored["database"] = _restore_database_part(plain, clean=clean)

        if file_scopes:
            part = parts_by_kind["files"]
            src = bundle_path / part["file"]
            if not src.is_file() or sha256_file(src) != part["sha256"]:
                raise OpsError(f"hash mismatch on {part['file']}")
            with open(src, "rb") as handle:
                is_plain = handle.read(4) == b"\x28\xb5\x2f\xfd"
            plain = src
            if not is_plain:
                plain = ops._private_file(temp, "files.tar.zst")
                _decrypt_for_restore(settings, src, plain, passphrase_file=passphrase_file)
            file_res, skipped = _restore_files_part(
                plain,
                file_scopes,
                home_dir=resolved_home,
                setup_dir=resolved_setup,
                merge=merge,
                overwrite=overwrite,
            )
            restored.update(file_res)
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    onboarding_open = False
    if continue_onboarding:
        marker = resolved_setup / "restored"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("restored\n", encoding="utf-8")
        try:
            onboarding_state.mark_restored(keep_steps=True)
            onboarding_open = True
        except Exception:
            # DB may not be migrated yet if restoring config before database.
            onboarding_open = True
    return dict(
        ok=True,
        bundle=bundle_path.name,
        scopes=scopes,
        restored=restored,
        skipped=skipped,
        onboarding_open=onboarding_open,
    )


# --- scheduler -----------------------------------------------------------------------


def tick(now=None, settings=None):
    """One scheduler tick: run the bundle and/or the drill when due."""
    settings = settings or load()
    with connect() as conn:
        plan = due(settings, conn, now)
    out = dict(due=plan["reason"], backup=None, verify=None)
    if plan["backup"]:
        try:
            manifest = run(settings)
            out["backup"] = {
                k: manifest.get(k) for k in ("ok", "name", "bytes", "errors", "removed")
            } | {"offsite": (manifest.get("offsite") or {}).get("status")}
        except OpsError as exc:
            out["backup"] = dict(ok=False, error=str(exc))
    if plan["verify"] and latest(settings) is not None:
        try:
            out["verify"] = verify(settings=settings)
        except OpsError as exc:
            out["verify"] = dict(ok=False, error=str(exc))
    return out
