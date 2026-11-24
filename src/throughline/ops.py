"""Operational guarantees: snapshot-consistent backups, a restore drill and a
staleness check (ONB-2, ARC-5, PLG-7).

Everything here uses the owner database connection, like `throughline dump`,
`restore` and `purge`. Encryption and the off-site copy are external commands
the owner configures (for example age and rclone); no crypto library is used.
Commands run without a shell, from argument lists, with a timeout and the exit
code checked. tl.backup_runs holds counts and hashes only, never content.
"""

import hashlib
import os
import re
import secrets
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .db import cluster_lock, connect, database_url, direct_connection

PREFIX = "throughline-"
PLAIN_SUFFIX = ".dump"
ENCRYPTED_SUFFIX = ".dump.enc"
DUMP_MAGIC = b"PGDMP"
STDERR_LIMIT = 400
DEFAULT_KEEP = 7
DEFAULT_STALE_HOURS = 36.0
DEFAULT_TIMEOUT_S = 3600.0

# Content hashes: md5 over the ordered rows of key columns. Values that could
# carry content (message text, payloads, statements) enter only as md5, so the
# stored hash says nothing about them. The first column orders the rows.
HASHES = {
    "messages": "SELECT id,channel_id,external_id,version,payload_hash,md5(content) AS content_md5 FROM tl.messages",
    "raw_payloads": "SELECT message_id,md5(payload::text) AS payload_md5 FROM tl.raw_payloads",
    "links": "SELECT id,type,from_kind,from_id,to_kind,to_id,status,valid_from,valid_to,superseded_by FROM tl.links",
    "identifiers": "SELECT id,person_id,system,kind,md5(value_norm) AS value_md5,status FROM tl.identifiers",
    "people": "SELECT id,md5(coalesce(handle,'')||'|'||coalesce(display_name,'')) AS names_md5,is_owner,status,merged_into,kind FROM tl.people",
    "provenance": "SELECT derived_id,source_message_id,source_version,derived_kind,relation,origin,job,job_version FROM tl.provenance",
    "annotations": "SELECT id,target_channel_id,target_external_id,target_version,attribute,md5(value::text) AS value_md5,status,superseded_by FROM tl.annotations",
    "passages": "SELECT id,message_id,version,ordinal,char_start,char_end,text_hash FROM tl.passages",
}


# Output settings fixed (transaction-locally) while counts and hashes are read.
_HASH_SETTINGS = (
    ("TimeZone", "GMT"),  # built in: needs no time zone files
    ("DateStyle", "ISO, YMD"),
    ("IntervalStyle", "postgres"),
    ("extra_float_digits", "1"),
    ("bytea_output", "hex"),
)
PROBE_TIMEOUT_MS = 30000


class OpsError(RuntimeError):
    """A backup step failed; the message is safe to show (already redacted)."""


# --- helpers -----------------------------------------------------------------------


def _now():
    return datetime.now(timezone.utc)


def _env_float(name, default):
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return float(default)


def _timeout():
    return _env_float("THROUGHLINE_BACKUP_TIMEOUT_S", DEFAULT_TIMEOUT_S)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


_SECRET_PATTERNS = (
    # credentials inside URLs: scheme://user:password@host
    (re.compile(r"([a-z][a-z0-9+.-]*://[^:/@\s]*:)[^@\s]+@", re.I), r"\1[redacted]@"),
    # key=value or key: value for secret-looking keys
    (
        re.compile(
            r"((?:password|passwd|pwd|secret|token|api[_-]?key|key|passphrase|authorization)\s*[=:]\s*)\S+",
            re.I,
        ),
        r"\1[redacted]",
    ),
    (re.compile(r"(bearer\s+)\S+", re.I), r"\1[redacted]"),
    # age secret keys and long opaque strings (keys, tokens, hashes)
    (re.compile(r"AGE-SECRET-KEY-[0-9A-Z]+"), "[redacted]"),
    (re.compile(r"[A-Za-z0-9+/_=-]{32,}"), "[redacted]"),
)


def redact(text, limit=STDERR_LIMIT):
    """Truncate and redact command output before it is stored or shown."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = (text or "").strip()
    try:
        password = conninfo_to_dict(database_url()).get("password")
    except Exception:
        password = None
    # Docker passes the superuser password as PGPASSWORD, outside the URL.
    pgpass = os.environ.get("PGPASSWORD")
    resolved_pgpass = None
    if pgpass:
        try:
            from . import secrets

            if secrets.is_ref(pgpass):
                resolved_pgpass = secrets.resolve(pgpass)
        except Exception:
            resolved_pgpass = None
    for secret in (password, pgpass, resolved_pgpass):
        if secret:
            text = text.replace(secret, "[redacted]")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > limit:
        text = text[:limit] + "...[truncated]"
    return text


def command_args(template, **values):
    """Split a command template into an argument list (no shell) and fill
    {in}, {out} and {file} inside each argument. A value is never re-split, so
    a path with spaces or shell characters stays one argument."""
    if isinstance(template, (list, tuple)):
        parts = [str(part) for part in template]
    elif os.name == "nt":
        # Windows paths keep their backslashes; quotes only group.
        parts = [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'"
            else part
            for part in shlex.split(template, posix=False)
        ]
    else:
        parts = shlex.split(template)
    if not parts:
        raise OpsError("empty command template")
    filled = []
    for part in parts:
        for key, value in values.items():
            part = part.replace("{" + key + "}", str(value))
        filled.append(part)
    return filled


def run_command(args, *, step, env=None, timeout=None):
    """Run a program without a shell; raise OpsError with redacted, truncated
    stderr when it fails or times out."""
    try:
        result = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            timeout=timeout or _timeout(),
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpsError(f"{step} timed out after {exc.timeout:g} s") from None
    except OSError as exc:
        raise OpsError(f"{step} could not start: {redact(str(exc))}") from None
    if result.returncode != 0:
        error = OpsError(
            f"{step} failed with exit code {result.returncode}: {redact(result.stderr)}"
        )
        # Raw stderr stays on the exception for the caller's own checks; only
        # the redacted message is ever stored or shown.
        error.stderr = result.stderr
        raise error
    return result


def _pg_program(name):
    from .cli import pg_program

    try:
        return pg_program(name)
    except Exception as exc:  # typer.BadParameter outside a command
        raise OpsError(f"{name} not found; set THROUGHLINE_PG_BIN") from exc


def _libpq(url):
    """Split a connection string for a pg_* program: the password goes into the
    child's environment (PGPASSWORD), never onto its command line, where any
    local user could read it from the process list."""
    settings = conninfo_to_dict(url)
    env = dict(os.environ)
    password = settings.pop("password", None)
    if password:
        env["PGPASSWORD"] = password
    elif env.get("PGPASSWORD"):
        from . import secrets

        if secrets.is_ref(env["PGPASSWORD"]):
            env["PGPASSWORD"] = secrets.resolve(env["PGPASSWORD"])
    return make_conninfo(**settings), env


def _owner(conn):
    role = conn.execute("SELECT current_user AS role").fetchone()["role"]
    if role in ("throughline_app", "throughline_reader"):
        raise OpsError("owner database role required")


def _record(kind, status, *, started_at, file_sha256=None, size=None, detail=None):
    with connect() as conn:
        return conn.execute(
            "INSERT INTO tl.backup_runs(kind,started_at,finished_at,status,file_sha256,bytes,detail) VALUES (%s,%s,now(),%s,%s,%s,%s) RETURNING id",
            (kind, started_at, status, file_sha256, size, Jsonb(detail or {})),
        ).fetchone()["id"]


def _remove(path):
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def _private_file(directory, name):
    """Create an empty file only the owner can read (0600 where supported)."""
    path = Path(directory) / name
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


# --- counts and hashes ---------------------------------------------------------------


def snapshot_values(conn):
    """Row counts of every tl table and the content hashes, read in the
    caller's transaction (and therefore its snapshot).

    The hashes must not depend on the session or the server: values are
    rendered with fixed output settings (time zone, date, interval and float
    formats) and rows are ordered by their text in the "C" collation, so a
    restore drill on a server or session with other defaults compares equal.
    """
    for name, value in _HASH_SETTINGS:
        conn.execute("SELECT set_config(%s,%s,true)", (name, value))
    tables = [
        r["tablename"]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='tl' ORDER BY tablename"
        )
    ]
    counts = {}
    for name in tables:
        counts[name] = conn.execute(
            sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier("tl", name))
        ).fetchone()["n"]
    hashes = {}
    for name, statement in HASHES.items():
        if name not in counts:
            hashes[name] = None
            continue
        hashes[name] = conn.execute(
            "SELECT md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text COLLATE \"C\"),'')) AS h FROM ("
            + statement
            + ") t"
        ).fetchone()["h"]
    return counts, hashes


# --- backup --------------------------------------------------------------------------


def _retain(directory, keep):
    files = sorted(
        (
            p
            for p in Path(directory).iterdir()
            if p.is_file()
            and p.name.startswith(PREFIX)
            and (p.name.endswith(PLAIN_SUFFIX) or p.name.endswith(ENCRYPTED_SUFFIX))
        ),
        key=lambda p: p.name,
    )
    removed = []
    for path in files[: max(0, len(files) - keep)]:
        _remove(path)
        removed.append(path.name)
    return removed


def backup(
    path_dir,
    *,
    encrypt_cmd=None,
    offsite_cmd=None,
    keep=None,
    allow_plaintext_offsite=False,
    allow_plaintext=False,
    on_snapshot=None,
    bundle=False,
):
    """Dump the live database with counts and hashes taken in the dump's own
    snapshot; optionally encrypt and copy off-site; keep `keep` local files.

    encrypt_cmd ({in} {out}), offsite_cmd ({file}) and keep default to
    THROUGHLINE_BACKUP_ENCRYPT_CMD, THROUGHLINE_BACKUP_OFFSITE_CMD and
    THROUGHLINE_BACKUP_KEEP (7). on_snapshot(snapshot_id) is called right
    after the snapshot is exported (tests use it to write concurrently).
    Without an encrypt command nothing is dumped and the run fails
    (plaintext_refused), unless allow_plaintext is given: a dump holds the
    whole archive. Raises OpsError after recording a failed run.
    """
    encrypt_cmd = encrypt_cmd or os.getenv("THROUGHLINE_BACKUP_ENCRYPT_CMD") or None
    if not encrypt_cmd and not (allow_plaintext or allow_plaintext_offsite):
        _record(
            "dump",
            "failed",
            started_at=_now(),
            detail=dict(encrypted=False, error="plaintext_refused"),
        )
        raise OpsError(
            "no THROUGHLINE_BACKUP_ENCRYPT_CMD is set: a plaintext dump is refused"
            " (pass --allow-plaintext to write one into the backup directory)"
        )
    # False: no off-site step here (a backup bundle copies the whole bundle).
    if offsite_cmd is False:
        offsite_cmd = None
    else:
        offsite_cmd = offsite_cmd or os.getenv("THROUGHLINE_BACKUP_OFFSITE_CMD") or None
    if keep is None:
        keep = int(_env_float("THROUGHLINE_BACKUP_KEEP", DEFAULT_KEEP))
    keep = max(1, int(keep))
    directory = Path(path_dir)
    directory.mkdir(parents=True, exist_ok=True)
    started = _now()
    stamp = started.strftime("%Y%m%dT%H%M%S%fZ")
    base = f"{PREFIX}{stamp}-{secrets.token_hex(3)}"
    plain = directory / (base + PLAIN_SUFFIX)
    final = plain
    detail = {"encrypted": bool(encrypt_cmd)}
    if bundle:
        # Part of a whole-install bundle (throughline.backups): not counted
        # as a backup until the bundle marks itself complete.
        detail["bundle"] = {"ok": False, "pending": True}
    plain_sha = None
    try:
        pg_dump = _pg_program("pg_dump")
        _private_file(directory, plain.name)
        with direct_connection(database_url(), row_factory=dict_row) as conn:
            _owner(conn)
            conn.rollback()
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            conn.read_only = True
            snapshot = conn.execute("SELECT pg_export_snapshot() AS s").fetchone()["s"]
            if on_snapshot is not None:
                on_snapshot(snapshot)
            # The dump and the counts share the exported snapshot, so they
            # describe exactly the same rows even while plugins ingest.
            dump_dsn, dump_env = _libpq(database_url())
            run_command(
                [
                    pg_dump,
                    "--dbname",
                    dump_dsn,
                    "--format=custom",
                    "--no-owner",
                    "--snapshot",
                    snapshot,
                    "--file",
                    str(plain),
                ],
                step="pg_dump",
                env=dump_env,
            )
            counts, hashes = snapshot_values(conn)
            conn.rollback()
        plain_sha = sha256_file(plain)
        detail.update(
            snapshot=snapshot, counts=counts, hashes=hashes, plain_sha256=plain_sha
        )
        if encrypt_cmd:
            final = directory / (base + ENCRYPTED_SUFFIX)
            _private_file(directory, final.name)
            run_command(
                command_args(encrypt_cmd, **{"in": plain, "out": final}),
                step="encrypt",
            )
            if not final.is_file() or final.stat().st_size == 0:
                raise OpsError("encrypt produced no output")
            with open(final, "rb") as handle:
                if handle.read(len(DUMP_MAGIC)) == DUMP_MAGIC:
                    raise OpsError("encrypt output is a plaintext dump")
    except BaseException as exc:
        _remove(plain)
        if final != plain:
            _remove(final)
        detail["error"] = (
            str(exc)
            if isinstance(exc, OpsError)
            else redact(f"{type(exc).__name__}: {exc}")
        )
        _record(
            "dump", "failed", started_at=started, file_sha256=plain_sha, detail=detail
        )
        if isinstance(exc, (OpsError, KeyboardInterrupt)):
            raise
        raise OpsError(detail["error"]) from exc
    finally:
        # A plaintext dump never outlives the command once encryption is set.
        if encrypt_cmd:
            _remove(plain)
    file_sha = sha256_file(final)
    size = final.stat().st_size
    dump_id = _record(
        "dump", "ok", started_at=started, file_sha256=file_sha, size=size, detail=detail
    )
    result = dict(
        ok=True,
        dump_id=dump_id,
        file=str(final),
        encrypted=bool(encrypt_cmd),
        sha256=file_sha,
        bytes=size,
        snapshot=detail["snapshot"],
        counts=detail["counts"],
        hashes=detail["hashes"],
        offsite=None,
    )
    if offsite_cmd:
        result["offsite"] = _offsite(
            final,
            offsite_cmd,
            encrypted=bool(encrypt_cmd),
            allow_plaintext=allow_plaintext_offsite,
            dump_id=dump_id,
            file_sha=file_sha,
            size=size,
        )
        result["ok"] = result["offsite"]["status"] == "ok"
    result["removed"] = _retain(directory, keep)
    return result


def _offsite(path, template, *, encrypted, allow_plaintext, dump_id, file_sha, size):
    started = _now()
    detail = {"dump_id": dump_id, "encrypted": encrypted}
    if not encrypted and not allow_plaintext:
        detail["error"] = "plaintext_refused"
        run_id = _record(
            "offsite",
            "failed",
            started_at=started,
            file_sha256=file_sha,
            size=size,
            detail=detail,
        )
        return dict(id=run_id, status="failed", error="plaintext_refused")
    try:
        run_command(command_args(template, file=path), step="offsite")
    except OpsError as exc:
        detail["error"] = str(exc)
        run_id = _record(
            "offsite",
            "failed",
            started_at=started,
            file_sha256=file_sha,
            size=size,
            detail=detail,
        )
        return dict(id=run_id, status="failed", error=str(exc))
    run_id = _record(
        "offsite",
        "ok",
        started_at=started,
        file_sha256=file_sha,
        size=size,
        detail=detail,
    )
    return dict(id=run_id, status="ok", sha256=file_sha)


# --- restore drill -------------------------------------------------------------------


def _target_key(url):
    settings = conninfo_to_dict(url)
    host = (settings.get("host") or "localhost").lower()
    if host in ("localhost", "::1"):
        host = "127.0.0.1"
    return host, str(settings.get("port") or "5432"), settings.get("dbname")


def _identity(url):
    with direct_connection(url, row_factory=dict_row, connect_timeout=10) as conn:
        return conn.execute(
            "SELECT current_database() AS db,inet_server_port() AS port,(SELECT oid FROM pg_database WHERE datname=current_database()) AS oid"
        ).fetchone()


def _refuse_live(target):
    live = database_url()
    if _target_key(target) == _target_key(live):
        raise OpsError("the restore target is the live database; refused")
    try:
        same = _identity(target) == _identity(live)
    except psycopg.Error:
        same = False
    if same:
        raise OpsError("the restore target is the live database; refused")


def _stored_values(file_sha, plain_sha):
    with connect() as conn:
        return conn.execute(
            """SELECT id,detail FROM tl.backup_runs
               WHERE kind='dump' AND status='ok'
                 AND (file_sha256=%s OR detail->>'plain_sha256'=%s)
               ORDER BY id DESC LIMIT 1""",
            (file_sha, plain_sha),
        ).fetchone()


def probes(conn, only=None):
    """The fixed probe set: api.catalog() and every free SQL example of the
    catalogue. The examples come from the restored file, so each runs in its
    own READ ONLY transaction with a statement timeout and is rolled back."""
    statements = [("catalog()", "SELECT api.catalog()")]
    if conn.execute(
        "SELECT to_regclass('tl.api_functions') IS NOT NULL AS ok"
    ).fetchone()["ok"]:
        statements += [
            (r["name"], r["example"])
            for r in conn.execute(
                "SELECT name,example FROM tl.api_functions WHERE kind='sql' AND cost_class='free' ORDER BY name"
            )
        ]
    if only is not None:
        statements = [(n, q) for n, q in statements if n in only]
    conn.rollback()  # each probe below starts its own top-level transaction
    results = []
    for name, statement in statements:
        try:
            with conn.transaction(force_rollback=True):
                conn.execute("SET TRANSACTION READ ONLY")
                conn.execute(
                    "SELECT set_config('statement_timeout',%s,true)",
                    (str(PROBE_TIMEOUT_MS),),
                )
                conn.execute(statement).fetchall()
            results.append(dict(name=name, ok=True))
        except psycopg.Error as exc:
            conn.rollback()
            results.append(dict(name=name, ok=False, error=type(exc).__name__))
    return results


def verify_restore(
    file, *, scratch_url=None, decrypt_cmd=None, on_restored=None, work_dir=None
):
    """Restore a backup into a scratch database and compare it with the counts
    and hashes stored for that dump (never with the live database).

    scratch_url defaults to THROUGHLINE_SCRATCH_DATABASE_URL; without one a
    temporary database tl_verify_<random> is created and dropped afterwards.
    decrypt_cmd ({in} {out}) defaults to THROUGHLINE_BACKUP_DECRYPT_CMD.
    on_restored(url) runs after the restore, before the comparison.
    work_dir holds the decrypted dump and any restore script (default the
    system temporary directory, often a small tmpfs: pass a directory on disk
    for a large database).
    """
    file = Path(file)
    started = _now()
    clock = time.monotonic()
    decrypt_cmd = decrypt_cmd or os.getenv("THROUGHLINE_BACKUP_DECRYPT_CMD") or None
    scratch_url = scratch_url or os.getenv("THROUGHLINE_SCRATCH_DATABASE_URL") or None
    detail = {}
    file_sha = None
    size = None
    temp_dir = None
    created = None
    result = dict(
        ok=False,
        counts_unavailable=True,
        tables=[],
        hashes=[],
        probes=[],
        duration_s=None,
    )
    try:
        if not file.is_file():
            raise OpsError("backup file not found")
        file_sha = sha256_file(file)
        size = file.stat().st_size
        with open(file, "rb") as handle:
            encrypted = handle.read(len(DUMP_MAGIC)) != DUMP_MAGIC
        detail["encrypted"] = encrypted
        plain = file
        if encrypted:
            if not decrypt_cmd:
                raise OpsError(
                    "the file is not a plain dump; set THROUGHLINE_BACKUP_DECRYPT_CMD"
                )
            temp_dir = tempfile.mkdtemp(prefix="tl-verify-", dir=work_dir)
            plain = _private_file(temp_dir, "restore.dump")
            run_command(
                command_args(decrypt_cmd, **{"in": file, "out": plain}),
                step="decrypt",
            )
            with open(plain, "rb") as handle:
                if handle.read(len(DUMP_MAGIC)) != DUMP_MAGIC:
                    raise OpsError("decrypt did not produce a PostgreSQL dump")
        plain_sha = sha256_file(plain)
        stored = _stored_values(file_sha, plain_sha)
        if scratch_url:
            target = scratch_url
            _refuse_live(target)
        else:
            settings = conninfo_to_dict(database_url())
            created = "tl_verify_" + secrets.token_hex(8)
            with direct_connection(database_url(), autocommit=True) as admin:
                admin.execute(
                    sql.SQL("CREATE DATABASE {}").format(sql.Identifier(created))
                )
            settings["dbname"] = created
            target = make_conninfo(**settings)
            _refuse_live(target)
        with direct_connection(target, row_factory=dict_row) as conn:
            if conn.execute(
                "SELECT to_regnamespace('tl') IS NOT NULL AS used"
            ).fetchone()["used"]:
                raise OpsError("the restore target is not empty; refused")
        restore_dsn, restore_env = _libpq(target)
        restore_env = {
            **restore_env,
            "PGOPTIONS": (
                os.environ.get("PGOPTIONS", "")
                + " -c throughline.bulk_restore=on"
                # Serial index builds: parallel ones need dynamic shared memory,
                # which a container's default 64 MB /dev/shm cannot hold for
                # large (vector) indexes.
                + " -c max_parallel_maintenance_workers=0"
            ).strip(),
        }
        try:
            run_command(
                [
                    _pg_program("pg_restore"),
                    "--dbname",
                    restore_dsn,
                    "--no-owner",
                    "--exit-on-error",
                    str(plain),
                ],
                step="pg_restore",
                env=restore_env,
            )
        except OpsError as exc:
            # Client tools newer than the scratch server: retry through a SQL
            # script without the preamble that server refuses (cli.py).
            from .cli import refused_transaction_timeout, restore_through_script

            if not refused_transaction_timeout(getattr(exc, "stderr", None)):
                raise
            temp_dir = temp_dir or tempfile.mkdtemp(prefix="tl-verify-", dir=work_dir)
            restore_through_script(
                plain,
                restore_dsn,
                lambda args: run_command(
                    args, step=Path(args[0]).stem, env=restore_env
                ),
                _private_file(temp_dir, "restore.sql"),
                _private_file(temp_dir, "restore.filtered.sql"),
            )
        # harden_reader alters cluster-wide role settings: serialized with
        # migrations and other drills on the same cluster (db.cluster_lock).
        with (
            cluster_lock(target),
            direct_connection(target, row_factory=dict_row) as conn,
        ):
            if conn.execute(
                "SELECT to_regprocedure('tl.harden_reader()') IS NOT NULL AS ok"
            ).fetchone()["ok"]:
                conn.execute("SELECT tl.harden_reader()")
            conn.commit()
        if on_restored is not None:
            on_restored(target)
        with direct_connection(target, row_factory=dict_row) as conn:
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            counts, hashes = snapshot_values(conn)
            conn.rollback()
            result["probes"] = probes(conn)
        # A probe that fails on the restored copy is run once more on the
        # live database: failing there too, it is a defect of the live
        # database (a slow catalogue example), not of the backup. It is
        # reported (live_fails) but does not fail the drill.
        failed = [p["name"] for p in result["probes"] if not p["ok"]]
        if failed:
            try:
                with direct_connection(database_url(), row_factory=dict_row) as live:
                    live_failed = {
                        p["name"] for p in probes(live, set(failed)) if not p["ok"]
                    }
            except psycopg.Error:
                live_failed = set()
            for p in result["probes"]:
                if not p["ok"] and p["name"] in live_failed:
                    p["live_fails"] = True
        probes_ok = all(p["ok"] or p.get("live_fails") for p in result["probes"])
        if stored is None or "counts" not in (stored["detail"] or {}):
            result["counts_unavailable"] = True
            result["tables"] = [
                dict(name=name, expected=None, restored=n, equal=None)
                for name, n in sorted(counts.items())
            ]
            result["hashes"] = [dict(name=name, equal=None) for name in sorted(hashes)]
            compared_ok = True
        else:
            result["counts_unavailable"] = False
            detail["dump_id"] = stored["id"]
            expected = stored["detail"]["counts"]
            expected_hashes = stored["detail"].get("hashes") or {}
            result["tables"] = [
                dict(
                    name=name,
                    expected=expected.get(name),
                    restored=counts.get(name),
                    equal=expected.get(name) == counts.get(name),
                )
                for name in sorted(set(expected) | set(counts))
            ]
            result["hashes"] = [
                dict(name=name, equal=expected_hashes.get(name) == hashes.get(name))
                for name in sorted(set(expected_hashes) | set(hashes))
            ]
            compared_ok = all(t["equal"] for t in result["tables"]) and all(
                h["equal"] for h in result["hashes"]
            )
        result["ok"] = bool(compared_ok and probes_ok)
    except OpsError as exc:
        result["error"] = str(exc)
    except psycopg.Error as exc:
        result["error"] = redact(f"{type(exc).__name__}: {exc}")
    finally:
        if temp_dir:
            for path in Path(temp_dir).iterdir():
                _remove(path)
            try:
                os.rmdir(temp_dir)
            except OSError:
                pass
        if created:
            try:
                with direct_connection(database_url(), autocommit=True) as admin:
                    admin.execute(
                        sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                            sql.Identifier(created)
                        )
                    )
            except psycopg.Error:
                result.setdefault("cleanup_error", "scratch database not dropped")
        result["duration_s"] = round(time.monotonic() - clock, 3)
    detail.update(
        counts_unavailable=result["counts_unavailable"],
        tables_unequal=[t["name"] for t in result["tables"] if t["equal"] is False],
        hashes_unequal=[h["name"] for h in result["hashes"] if h["equal"] is False],
        probes_failed=[
            p["name"]
            for p in result["probes"]
            if not p["ok"] and not p.get("live_fails")
        ],
        probes_failed_live_too=[
            p["name"] for p in result["probes"] if p.get("live_fails")
        ],
        probes=len(result["probes"]),
        duration_s=result["duration_s"],
    )
    if "error" in result:
        detail["error"] = result["error"]
    result["run_id"] = _record(
        "verify",
        "ok" if result["ok"] else "failed",
        started_at=started,
        file_sha256=file_sha,
        size=size,
        detail=detail,
    )
    return result


# --- staleness and listing -------------------------------------------------------


def check(stale_hours=None):
    """ok when the last successful dump and the last successful verify are
    both younger than stale_hours (THROUGHLINE_BACKUP_STALE_HOURS, 36)."""
    if stale_hours is None:
        stale_hours = _env_float("THROUGHLINE_BACKUP_STALE_HOURS", DEFAULT_STALE_HOURS)
    with connect() as conn:
        # A bundle (throughline.backups) counts only when every part made it.
        row = conn.execute(
            """SELECT max(finished_at) FILTER (WHERE kind='dump' AND status='ok' AND coalesce((detail->'bundle'->>'ok')::boolean,true)) AS last_dump_at,
                      max(finished_at) FILTER (WHERE kind='verify' AND status='ok') AS last_verified_at,
                      extract(epoch FROM now()-max(finished_at) FILTER (WHERE kind='dump' AND status='ok' AND coalesce((detail->'bundle'->>'ok')::boolean,true)))/3600.0 AS dump_h,
                      extract(epoch FROM now()-max(finished_at) FILTER (WHERE kind='verify' AND status='ok'))/3600.0 AS verify_h
               FROM tl.backup_runs"""
        ).fetchone()
    dump_h = None if row["dump_h"] is None else round(float(row["dump_h"]), 3)
    verify_h = None if row["verify_h"] is None else round(float(row["verify_h"]), 3)
    reasons = []
    if dump_h is None:
        reasons.append("no_dump")
    elif dump_h > stale_hours:
        reasons.append("dump_stale")
    if verify_h is None:
        reasons.append("no_verify")
    elif verify_h > stale_hours:
        reasons.append("verify_stale")
    return dict(
        ok=not reasons,
        reasons=reasons,
        stale_hours=float(stale_hours),
        last_dump_at=row["last_dump_at"],
        dump_age_hours=dump_h,
        last_verified_at=row["last_verified_at"],
        verify_age_hours=verify_h,
    )


def runs(limit=20):
    with connect() as conn:
        return conn.execute(
            "SELECT id,kind,status,started_at,finished_at,file_sha256,bytes,detail FROM tl.backup_runs ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
