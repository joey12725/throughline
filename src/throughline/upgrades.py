"""Safe upgrades: a database snapshot before any pending migration runs.

Migrations are forward-only. Before `throughline migrate` applies one to a
database that already holds data, it writes a pg_dump (custom format) of that
database to the upgrade snapshot folder, so a failed or unwanted update can be
undone with one pg_restore (docs/updating.md). A fresh database (no applied
migrations) needs no snapshot.

    THROUGHLINE_UPGRADE_SNAPSHOT        on (default) | off
    THROUGHLINE_UPGRADE_SNAPSHOT_DIR    default ~/.throughline/upgrade-snapshots
    THROUGHLINE_UPGRADE_SNAPSHOT_KEEP   default 2 (newest kept)

When the snapshot cannot be written (no pg_dump, too little disk), the
migration does not run: the update stops with the reason, and nothing has
changed. `throughline migrate --no-snapshot` skips it deliberately.
"""

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row

from .db import cluster_lock, database_url, direct_connection
from .ops import OpsError, _libpq, _pg_program, _private_file, run_command

PREFIX = "pre-upgrade-"
SUFFIX = ".dump"
MIGRATIONS = Path(__file__).parent / "migrations"


def version():
    from importlib.metadata import PackageNotFoundError, version as _version

    try:
        return _version("throughline")
    except PackageNotFoundError:
        return "unknown"


def snapshot_dir():
    return Path(
        os.path.expanduser(
            os.getenv("THROUGHLINE_UPGRADE_SNAPSHOT_DIR") or "~/.throughline/upgrade-snapshots"
        )
    )


def _keep():
    try:
        return max(1, int(os.getenv("THROUGHLINE_UPGRADE_SNAPSHOT_KEEP") or 2))
    except ValueError:
        return 2


def enabled():
    return (os.getenv("THROUGHLINE_UPGRADE_SNAPSHOT") or "on").strip().lower() not in (
        "off", "0", "false", "no",
    )


def schema_state(url=None):
    """Applied, pending and unknown (applied but not shipped) migrations."""
    shipped = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    with direct_connection(url or database_url(), row_factory=dict_row) as conn:
        exists = conn.execute(
            "SELECT to_regclass('tl.schema_migrations') IS NOT NULL AS e"
        ).fetchone()["e"]
        applied = (
            {r["version"] for r in conn.execute("SELECT version FROM tl.schema_migrations")}
            if exists
            else set()
        )
        size = conn.execute(
            "SELECT pg_database_size(current_database()) AS s"
        ).fetchone()["s"]
    return dict(
        applied=sorted(applied),
        pending=[name for name in shipped if name not in applied],
        unknown=sorted(applied - set(shipped)),
        database_bytes=size,
    )


def snapshots():
    directory = snapshot_dir()
    if not directory.is_dir():
        return []
    return sorted(
        (p for p in directory.glob(PREFIX + "*" + SUFFIX) if p.is_file()),
        key=lambda p: p.name,
    )


def take_snapshot(url=None, *, label=None):
    """pg_dump the database to the snapshot folder; prune to the newest KEEP."""
    url = url or database_url()
    directory = snapshot_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    state = schema_state(url)
    free = shutil.disk_usage(directory).free
    # A custom-format dump is compressed; half the live size is a generous floor.
    if free < state["database_bytes"] // 2:
        raise OpsError(
            f"not enough disk for the pre-upgrade snapshot in {directory} "
            f"({free // 2**20} MB free, database {state['database_bytes'] // 2**20} MB). "
            "Free some space, point THROUGHLINE_UPGRADE_SNAPSHOT_DIR elsewhere, "
            "or run `throughline migrate --no-snapshot` if you have a recent backup."
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tail = state["applied"][-1].removesuffix(".sql") if state["applied"] else "empty"
    name = f"{PREFIX}{stamp}-{label or tail}{SUFFIX}"
    path = _private_file(directory, name + ".partial")
    dsn, env = _libpq(url)
    try:
        run_command(
            [_pg_program("pg_dump"), "--dbname", dsn, "--format=custom", "--no-owner",
             "--file", str(path)],
            step="pre-upgrade pg_dump",
            env=env,
        )
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    final = directory / name
    os.replace(path, final)
    for old in snapshots()[: -_keep()]:
        old.unlink(missing_ok=True)
    return final


def before_migrate(url=None, *, snapshot=True, echo=print):
    """Report what an update will do and snapshot first when it changes an
    existing database. Returns the snapshot path, or None."""
    state = schema_state(url)
    if state["unknown"]:
        echo(
            "warning: this database has migrations this version does not ship "
            f"({', '.join(state['unknown'][-3:])}): it was used by a newer "
            f"Throughline than {version()}. To roll back, restore the "
            "pre-upgrade snapshot (docs/updating.md)."
        )
    if not state["pending"]:
        return None
    if not state["applied"]:
        echo(f"new database: applying {len(state['pending'])} migrations")
        return None
    echo(f"{len(state['pending'])} migration(s) to apply: {', '.join(state['pending'])}")
    if not snapshot or not enabled():
        echo("pre-upgrade snapshot skipped")
        return None
    path = take_snapshot(url)
    echo(f"pre-upgrade snapshot: {path} ({path.stat().st_size // 2**20} MB)")
    return path


def restore_snapshot(path, url=None):
    """Replace the database with a pre-upgrade snapshot (a rollback).

    The database is dropped and recreated from the dump, so anything written
    after the snapshot is gone. Runtime roles are cluster-wide and survive;
    the caller runs the (older) `throughline migrate` afterwards, which finds
    nothing pending and re-hardens the reader role."""
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    path = Path(path).expanduser()
    if not path.is_file() or path.stat().st_size == 0:
        raise OpsError(f"no snapshot at {path}")
    with open(path, "rb") as handle:
        if handle.read(5) != b"PGDMP":
            raise OpsError(f"{path} is not a pg_dump custom-format file")
    url = url or database_url()
    settings = conninfo_to_dict(url)
    name = settings.get("dbname") or "throughline"
    admin = make_conninfo(**{**settings, "dbname": "postgres"})
    with direct_connection(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn, env = _libpq(url)
    # As in the restore drill (ops.py): bulk mode, serial index builds (a
    # container's 64 MB /dev/shm), and a retry through SQL for client tools
    # newer than the server.
    env["PGOPTIONS"] = (
        env.get("PGOPTIONS", "")
        + " -c throughline.bulk_restore=on -c max_parallel_maintenance_workers=0"
    ).strip()
    try:
        run_command(
            [_pg_program("pg_restore"), "--dbname", dsn, "--no-owner", "--exit-on-error",
             str(path)],
            step="pg_restore",
            env=env,
        )
    except OpsError as exc:
        from .cli import refused_transaction_timeout, restore_through_script

        if not refused_transaction_timeout(getattr(exc, "stderr", None)):
            raise
        work = Path(tempfile.mkdtemp(prefix=".restore-", dir=path.parent))
        try:
            restore_through_script(
                path,
                dsn,
                lambda args: run_command(args, step=Path(args[0]).stem, env=env),
                _private_file(work, "restore.sql"),
                _private_file(work, "restore.filtered.sql"),
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)
    with cluster_lock(url), direct_connection(url) as conn:
        if conn.execute(
            "SELECT to_regprocedure('tl.harden_reader()') IS NOT NULL"
        ).fetchone()[0]:
            conn.execute("SELECT tl.harden_reader()")
        conn.commit()
    return path
