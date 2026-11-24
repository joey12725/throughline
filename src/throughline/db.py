"""Connection roles and forward-only, transactional migrations."""

import hashlib
import os
import secrets
import select
import time
from contextlib import ExitStack, contextmanager
from queue import Empty, Full, LifoQueue
from threading import Lock
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row


def database_url():
    from . import secrets

    return secrets.require("THROUGHLINE_DATABASE_URL")


def role_password(role):
    from . import secrets

    password = secrets.getenv(f"THROUGHLINE_{role.upper()}_PASSWORD")
    if not password and os.getenv("THROUGHLINE_DEV") != "1":
        raise ValueError(
            f"THROUGHLINE_{role.upper()}_PASSWORD is required (or explicitly set THROUGHLINE_DEV=1)"
        )
    return password or ""


_pools = {}
_pool_lock = Lock()
# A pooled connection idle for longer than this is pinged before reuse.
_IDLE_CHECK_S = 30.0


def _socket_dead(conn):
    # An idle connection has nothing to read. Readable means the server closed
    # it (restart, idle timeout, terminated backend) or sent a fatal notice.
    try:
        return bool(select.select([conn.pgconn.socket], [], [], 0)[0])
    except (OSError, ValueError, psycopg.Error):
        return True


def _usable(conn, idle_s):
    if conn.closed or conn.broken or _socket_dead(conn):
        return False
    if idle_s < _IDLE_CHECK_S:
        return True
    try:
        conn.execute("SELECT 1")
        conn.rollback()
        return True
    except psycopg.Error:
        return False


def _discard(conn):
    try:
        conn.close()
    except Exception:
        pass


def _checkout(pool, conninfo):
    # A pool can hold connections that died while idle (for example across a
    # PostgreSQL restart). Never hand one out: drop every dead one found.
    while True:
        try:
            conn, returned = pool.get_nowait()
        except Empty:
            return psycopg.connect(conninfo, row_factory=dict_row)
        if _usable(conn, time.monotonic() - returned):
            return conn
        _discard(conn)


@contextmanager
def _runtime_connection(conninfo):
    # Runtime connections are expensive on Windows. Reuse a small bounded pool;
    # each checkout still owns an independent transaction, including rollback on
    # dry runs/errors. Each pool is keyed by the complete connection string, including the database
    # and principal; administrative and runtime connections are never mixed.
    with _pool_lock:
        pool = _pools.setdefault(conninfo, LifoQueue(maxsize=8))
    conn = _checkout(pool, conninfo)
    try:
        yield conn
        conn.commit()
    except BaseException:
        # A failed rollback (connection lost mid-request) must not replace the
        # original error; the connection is discarded below instead.
        try:
            conn.rollback()
        except psycopg.Error:
            _discard(conn)
        raise
    finally:
        if conn.closed or conn.broken:
            _discard(conn)
        else:
            try:
                pool.put_nowait((conn, time.monotonic()))
            except Full:
                conn.close()


def _apply_pgpassword(settings):
    if "password" not in settings:
        pgpass = os.getenv("PGPASSWORD")
        if pgpass:
            from . import secrets

            if secrets.is_ref(pgpass):
                settings["password"] = secrets.resolve(pgpass)


def connect(role=None, url=None):
    settings = conninfo_to_dict(url or database_url())
    if role:
        settings.update(
            user=f"throughline_{role}",
            password=role_password(role),
        )
    else:
        _apply_pgpassword(settings)
    conninfo = make_conninfo(**settings)
    return _runtime_connection(conninfo)


def ping():
    """True when the database answers the app role (health checks)."""
    try:
        with connect("app") as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


def direct_connection(url=None, **options):
    """A plain, unpooled connection as the URL's own principal (the owner
    URL by default), for administrative work such as backups and restore
    drills. It never takes a role: role connections go through connect(role),
    so this module stays the only place that builds connections."""
    settings = conninfo_to_dict(url or database_url())
    _apply_pgpassword(settings)
    return psycopg.connect(make_conninfo(**settings), **options)


@contextmanager
def cluster_lock(url=None):
    """Hold the cluster-wide lock for work that changes shared catalog rows.

    Migrations and tl.harden_reader() alter cluster-wide role settings (and
    database grants); two such runs against different databases of one
    cluster fail with "tuple concurrently updated". Advisory locks are per
    database, so the lock is taken in the maintenance database of the
    cluster `url` names (the owner URL by default) when it is reachable. It
    is an optimisation for shared clusters, not a requirement: without a
    connectable "postgres" database the work runs under its own locks."""
    settings = conninfo_to_dict(url or database_url())
    settings["dbname"] = "postgres"
    with ExitStack() as stack:
        try:
            guard = stack.enter_context(connect(url=make_conninfo(**settings)))
        except psycopg.OperationalError:
            guard = None
        if guard is not None:
            guard.execute("SELECT pg_advisory_xact_lock(71824003)")
        yield


def migrate(url=None):
    for role in ("app", "reader"):
        role_password(role)
    # Runs against different databases of one cluster are serialized.
    with cluster_lock(url):
        _migrate(url)


def _migrate(url=None):
    with connect(url=url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(71824001)")
        conn.execute("CREATE SCHEMA IF NOT EXISTS tl")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tl.schema_migrations(version text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        # Renumbered when the web-ui and onboarding branches merged (their
        # numbers were taken): a database that ran the old names keeps them.
        for old, new in (
            ("0063_accounts.sql", "0075_accounts.sql"),
            ("0072_accounts.sql", "0075_accounts.sql"),
            ("0070_setup.sql", "0076_setup.sql"),
            ("0073_setup.sql", "0076_setup.sql"),
        ):
            conn.execute(
                "UPDATE tl.schema_migrations SET version=%s WHERE version=%s"
                " AND NOT EXISTS (SELECT 1 FROM tl.schema_migrations WHERE version=%s)",
                (new, old, new),
            )
        conn.commit()
        for path in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(71824001)")
                if conn.execute(
                    "SELECT 1 FROM tl.schema_migrations WHERE version=%s", (path.name,)
                ).fetchone():
                    continue
                source = path.read_text(encoding="utf-8-sig").replace(
                    "CREATE SCHEMA tl;", "CREATE SCHEMA IF NOT EXISTS tl;"
                )
                conn.execute(source)
                conn.execute(
                    "INSERT INTO tl.schema_migrations(version) VALUES (%s)",
                    (path.name,),
                )
        # Recipes (INT-9) are files, stored once per version after the schema
        # is complete; a recipe changed without a version bump stops here.
        from . import recipes

        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(71824001)")
            recipes.sync(conn)
        conn.execute("SELECT tl.harden_reader()")


def create_token(name, kind="owner", plugin=None, askers=None, location_source=False):
    token = secrets.token_urlsafe(32)
    with connect() as conn:
        row = conn.execute(
            "INSERT INTO tl.tokens(name,token_hash,kind,plugin,askers,location_source) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (
                name,
                hashlib.sha256(token.encode()).hexdigest(),
                kind,
                plugin,
                askers or [],
                location_source,
            ),
        ).fetchone()
    return dict(id=row["id"], token=token)


def set_location_source(token_id, enabled=True):
    """Grant or withdraw the track-writing capability of a live plugin token.

    Uses the owner database connection, like token creation: no HTTP token can
    change its own or another token's capabilities.
    """
    with connect() as conn:
        row = conn.execute(
            "UPDATE tl.tokens SET location_source=%s WHERE id=%s AND kind='plugin' AND revoked_at IS NULL RETURNING id,name,plugin,location_source",
            (enabled, token_id),
        ).fetchone()
    if not row:
        raise ValueError("no active plugin token with that id")
    return row


def configure_role_passwords():
    from . import secrets

    passwords = {
        role: secrets.require(f"THROUGHLINE_{role.upper()}_PASSWORD")
        for role in ("app", "reader")
    }
    if not all(passwords.values()):
        raise ValueError("explicit nonempty runtime passwords required")
    with cluster_lock(), connect() as conn:
        for role, password in passwords.items():
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier("throughline_" + role), sql.Literal(password)
                )
            )


def close_runtime_connections():
    with _pool_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        while not pool.empty():
            pool.get_nowait()[0].close()
