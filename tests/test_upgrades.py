"""Updates keep data: pre-upgrade snapshots, rollback restore, health check."""

import shutil
import uuid
from collections import namedtuple

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from throughline import db, upgrades
from throughline.api import create_app
from throughline.db import migrate
from throughline.ops import OpsError

pytestmark = pytest.mark.skipif(not shutil.which("pg_dump"), reason="needs pg_dump")


@pytest.fixture()
def scratch(database, tmp_path, monkeypatch):
    """A migrated database of its own and a private snapshot folder."""
    admin = make_conninfo(database, dbname="postgres")
    name = "tl_test_" + uuid.uuid4().hex
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(database, dbname=name)
    monkeypatch.setenv("THROUGHLINE_UPGRADE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    try:
        yield url
    finally:
        db.close_runtime_connections()
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _ship_extra(tmp_path, monkeypatch, drop=None):
    """Pretend this version ships one more migration (or one fewer)."""
    shipped = tmp_path / "migrations"
    shipped.mkdir()
    for path in sorted(upgrades.MIGRATIONS.glob("*.sql")):
        if path.name != drop:
            (shipped / path.name).symlink_to(path)
    (shipped / "9999_next.sql").write_text("SELECT 1;")
    monkeypatch.setattr(upgrades, "MIGRATIONS", shipped)


def test_fresh_and_current_databases_need_no_snapshot(scratch):
    lines = []
    assert upgrades.before_migrate(scratch, echo=lines.append) is None
    assert lines and lines[0].startswith("new database")
    migrate(scratch)
    assert upgrades.before_migrate(scratch, echo=lines.append) is None
    assert upgrades.snapshots() == []


def test_pending_migration_snapshots_and_restore_rolls_back(scratch, tmp_path, monkeypatch):
    migrate(scratch)
    with psycopg.connect(scratch, autocommit=True) as c:
        c.execute("CREATE TABLE tl.kept_probe(v text); INSERT INTO tl.kept_probe VALUES ('before')")
    _ship_extra(tmp_path, monkeypatch)
    lines = []
    path = upgrades.before_migrate(scratch, echo=lines.append)
    assert path and path.read_bytes()[:5] == b"PGDMP"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert any("9999_next.sql" in line for line in lines)

    # The "update" changes data; the rollback brings back the snapshot.
    with psycopg.connect(scratch, autocommit=True) as c:
        c.execute("UPDATE tl.kept_probe SET v='after'; CREATE TABLE tl.new_probe()")
    db.close_runtime_connections()
    upgrades.restore_snapshot(path, scratch)
    with psycopg.connect(scratch) as c:
        assert c.execute("SELECT v FROM tl.kept_probe").fetchone()[0] == "before"
        assert c.execute("SELECT to_regclass('tl.new_probe')").fetchone()[0] is None
        assert c.execute("SELECT count(*) FROM tl.schema_migrations").fetchone()[0] > 50
    migrate(scratch)  # the older version's migrate after a rollback finds nothing to do


def test_snapshots_are_pruned_to_keep(scratch, tmp_path, monkeypatch):
    migrate(scratch)
    monkeypatch.setenv("THROUGHLINE_UPGRADE_SNAPSHOT_KEEP", "2")
    made = [upgrades.take_snapshot(scratch, label=f"n{i}") for i in range(3)]
    kept = upgrades.snapshots()
    assert len(kept) == 2 and made[0] not in kept


def test_snapshot_off_and_low_disk(scratch, tmp_path, monkeypatch):
    migrate(scratch)
    _ship_extra(tmp_path, monkeypatch)
    monkeypatch.setenv("THROUGHLINE_UPGRADE_SNAPSHOT", "off")
    assert upgrades.before_migrate(scratch, echo=lambda _: None) is None
    monkeypatch.delenv("THROUGHLINE_UPGRADE_SNAPSHOT")
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(upgrades.shutil, "disk_usage", lambda _: usage(1, 1, 0))
    with pytest.raises(OpsError, match="not enough disk"):
        upgrades.before_migrate(scratch, echo=lambda _: None)
    assert upgrades.snapshots() == []


def test_newer_database_is_reported(scratch, tmp_path, monkeypatch):
    migrate(scratch)
    last = sorted(p.name for p in upgrades.MIGRATIONS.glob("*.sql"))[-1]
    _ship_extra(tmp_path, monkeypatch, drop=last)
    lines = []
    upgrades.before_migrate(scratch, snapshot=False, echo=lines.append)
    assert any(line.startswith("warning:") and last in line for line in lines)


def test_restore_refuses_a_file_that_is_not_a_dump(scratch, tmp_path):
    bogus = tmp_path / "x.dump"
    bogus.write_text("nope")
    with pytest.raises(OpsError, match="not a pg_dump"):
        upgrades.restore_snapshot(bogus, scratch)


def test_healthz_needs_no_token_and_reveals_nothing(database):
    with TestClient(create_app()) as client:
        r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
