"""Whole-install backup bundles (throughline.backups): settings file and its
validation, the schedule, tar excludes, a full bundle with an extra database,
roles and files, the bundle verify, local off-site copy with pruning, the
scheduler tick and the HTTP settings routes. Encryption is the stub from
test_v11_ops (no gpg needed). Synthetic data only."""

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from conftest import ingest, message
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from throughline import backups, cli, ops
from throughline.db import connect, database_url

CRYPT = """
import sys
mode, source, target = sys.argv[1], sys.argv[2], sys.argv[3]
data = open(source, "rb").read()
if mode == "enc":
    out = b"TLENC1" + bytes(b ^ 0x5A for b in data)
else:
    assert data[:6] == b"TLENC1"
    out = bytes(b ^ 0x5A for b in data[6:])
open(target, "wb").write(out)
"""


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("THROUGHLINE_BACKUP_CONFIG", str(tmp_path / "backup.json"))
    for name in (
        "THROUGHLINE_BACKUP_DIR",
        "THROUGHLINE_BACKUP_KEEP",
        "THROUGHLINE_BACKUP_ENCRYPT_CMD",
        "THROUGHLINE_BACKUP_DECRYPT_CMD",
        "THROUGHLINE_BACKUP_OFFSITE_CMD",
        "THROUGHLINE_BACKUP_STALE_HOURS",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "backup.json"


# --- settings ------------------------------------------------------------------------


def test_settings_defaults_save_merge_and_validation(config, tmp_path):
    settings = backups.load()
    # By default a bundle holds the Throughline database only.
    assert settings["include"]["databases"] == []
    assert settings["include"]["roles"] is False and settings["include"]["paths"] == []
    assert "tl_test_*" in settings["include"]["exclude_databases"]
    saved = backups.save({"schedule": {"every_hours": 6}, "destination": {"dir": "/x"}})
    assert saved["schedule"] == {"every_hours": 6, "at": "03:30"}
    assert json.loads(config.read_text()) == {
        "destination": {"dir": "/x"},
        "schedule": {"every_hours": 6},
    }
    assert config.stat().st_mode & 0o777 == 0o600
    for bad in (
        {"schedule": {"at": "25:00"}},
        {"schedule": {"every_hours": 0}},
        {"offsite": {"kind": "rsync"}},
        {"offsite": {"kind": "ftp"}},
        {"destination": {"keep": 0}},
        {"nonsense": 1},
        {"include": {"containers": [{"container": "a;rm -rf"}]}},
        {"enabled": "yes"},
    ):
        with pytest.raises(ValueError):
            backups.save(bad)
    # A refused change leaves the file as it was.
    assert backups.load()["schedule"]["every_hours"] == 6
    backups.save({"encryption": {"passphrase_file": str(tmp_path / "key")}})
    public = backups.public(backups.load())
    assert public["encryption"]["passphrase_present"] is False
    assert public["config_file"] == str(config)


def test_env_variables_seed_defaults(config, monkeypatch, tmp_path):
    monkeypatch.setenv("THROUGHLINE_BACKUP_DIR", str(tmp_path / "b"))
    monkeypatch.setenv("THROUGHLINE_BACKUP_ENCRYPT_CMD", "enc {in} {out}")
    monkeypatch.setenv("THROUGHLINE_BACKUP_DECRYPT_CMD", "dec {in} {out}")
    s = backups.load()
    assert s["destination"]["dir"] == str(tmp_path / "b")
    assert s["encryption"]["kind"] == "command"


def test_set_value_parses_json_or_string():
    changes = {}
    backups.set_value(changes, "schedule.every_hours", "12")
    backups.set_value(changes, "offsite.target", "host:/path")
    backups.set_value(changes, "include.paths", '["/a","/b"]')
    assert changes == {
        "schedule": {"every_hours": 12},
        "offsite": {"target": "host:/path"},
        "include": {"paths": ["/a", "/b"]},
    }


def test_schedule_slots():
    s = backups.validate(backups._merge(backups.DEFAULTS, {}))
    s["schedule"] = {"every_hours": 6, "at": "03:30"}
    tz = datetime.now().astimezone().tzinfo
    now = datetime(2026, 1, 10, 2, 0, tzinfo=tz)
    assert backups.last_slot(s, now) == datetime(2026, 1, 9, 21, 30, tzinfo=tz)
    assert backups.next_slot(s, now) == datetime(2026, 1, 10, 3, 30, tzinfo=tz)
    now = datetime(2026, 1, 10, 16, 0, tzinfo=tz)
    assert backups.last_slot(s, now) == datetime(2026, 1, 10, 15, 30, tzinfo=tz)
    s["schedule"] = {"every_hours": 24, "at": "03:30"}
    assert backups.stale_limits(s) == (36.0, 252.0)
    s["verify"]["every_hours"] = 0
    assert backups.stale_limits(s)[1] is None


def test_tar_excludes(tmp_path):
    root = tmp_path / "home"
    for rel in (
        "keep/state/a.json",
        "keep/pg-linux/base/1",
        "keep/pgkeep/x",
        "keep/tool/.venv/lib/x.py",
        "keep/tool/src/__pycache__/m.pyc",
        "keep/tool/src/m.py",
        "keep/backups/old",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
    s = backups._merge(backups.DEFAULTS, {})
    s["include"]["paths"] = [str(root / "keep"), str(root / "missing")]
    s["include"]["exclude_paths"] = [
        str(root / "keep/pg-linux"),
        ".venv",
        "__pycache__",
    ]
    archive = tmp_path / "f.tar.zst"
    args, roots = backups._tar_args(s, archive, root / "keep/backups")
    assert roots == [str((root / "keep").resolve())]
    backups._tar(args)
    import subprocess

    names = subprocess.run(
        ["tar", "--list", "--zstd", "--file", str(archive)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()
    joined = "\n".join(names)
    assert "keep/state/a.json" in joined and "keep/tool/src/m.py" in joined
    assert "keep/pgkeep/x" in joined  # a prefix is not a component
    assert "pg-linux" not in joined
    assert ".venv" not in joined and "__pycache__" not in joined
    assert "keep/backups" not in joined


# --- a whole bundle ------------------------------------------------------------------


@pytest.fixture()
def pg(monkeypatch):
    try:
        program = cli.pg_program("pg_dump")
        cli.pg_program("pg_restore")
        cli.pg_program("pg_dumpall")
    except Exception:
        pytest.skip("pg_dump/pg_restore/pg_dumpall not found; set THROUGHLINE_PG_BIN")
    monkeypatch.setenv("THROUGHLINE_PG_BIN", str(Path(program).parent))
    monkeypatch.delenv("THROUGHLINE_SCRATCH_DATABASE_URL", raising=False)


@pytest.fixture()
def extra_db(database):
    """A small second database in the same cluster, standing in for gbrain."""
    settings = conninfo_to_dict(database_url())
    name = "tl_bkextra_" + uuid.uuid4().hex[:12]
    admin = make_conninfo(**{**settings, "dbname": "postgres"})
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(**{**settings, "dbname": name})
    with psycopg.connect(url) as c:
        c.execute("CREATE TABLE pages(slug text PRIMARY KEY, body text)")
        c.execute("INSERT INTO pages VALUES ('synthetic','synthetic page')")
    yield name
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


def _bundle_settings(tmp_path, extra_db):
    stub = tmp_path / "crypt.py"
    stub.write_text(CRYPT)
    files = tmp_path / "state"
    (files / "cursors").mkdir(parents=True)
    (files / "cursors" / "synthetic.json").write_text('{"cursor": 1}')
    py = sys.executable
    return dict(
        destination=dict(dir=str(tmp_path / "out"), keep=2),
        offsite=dict(kind="rsync", target=str(tmp_path / "remote"), keep=1),
        encryption=dict(
            kind="command",
            encrypt_cmd=[py, str(stub), "enc", "{in}", "{out}"],
            decrypt_cmd=[py, str(stub), "dec", "{in}", "{out}"],
        ),
        include=dict(
            databases=[extra_db],
            roles=True,
            paths=[str(files)],
            exclude_paths=[],
        ),
    )


def test_bundle_covers_every_part_verifies_and_prunes(
    client, channel, pg, config, tmp_path, extra_db
):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    backups.save(_bundle_settings(tmp_path, extra_db))
    settings = backups.load()

    first = backups.run(settings)
    assert first["ok"], (first["errors"], first["offsite"])
    kinds = {p["kind"]: p for p in first["parts"]}
    assert set(kinds) == {"throughline", "globals", "database", "files"}
    assert kinds["database"]["name"] == extra_db
    bundle = Path(first["dir"])
    assert backups.BUNDLE_RE.match(bundle.name)
    # Nothing plaintext stays behind; every part is the stub's ciphertext.
    for path in bundle.iterdir():
        if path.name != "MANIFEST.json":
            assert path.read_bytes()[:6] == b"TLENC1", path.name
    manifest = json.loads((bundle / "MANIFEST.json").read_text())
    assert manifest["ok"] and manifest["name"] == bundle.name
    assert first["offsite"]["status"] == "ok"
    assert (tmp_path / "remote" / bundle.name / "MANIFEST.json").is_file()

    # The dump row carries the bundle summary; status counts it.
    with connect() as conn:
        row = conn.execute(
            "SELECT detail->'bundle' AS b FROM tl.backup_runs WHERE kind='dump' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["b"]["ok"] is True and len(row["b"]["parts"]) == 4
    with connect() as conn:
        st = backups.status(conn, settings)
    assert st["last_ok_at"] is not None and "no_backup" not in st["reasons"]
    assert "no_verify" in st["reasons"]

    result = backups.verify(bundle, settings)
    assert result["ok"], result
    assert result["drill"]["ok"] is True
    assert {p["name"] for p in result["parts"]} >= {extra_db, "globals", "files"}

    # The files part restores the tree.
    plain = tmp_path / "files.tar.zst"
    ops.run_command(
        ops.command_args(
            settings["encryption"]["decrypt_cmd"],
            **{"in": bundle / "files.tar.zst.gpg", "out": plain},
        ),
        step="decrypt",
    )
    import subprocess

    listing = subprocess.run(
        ["tar", "--list", "--zstd", "--file", str(plain)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "cursors/synthetic.json" in listing

    # Retention: local keeps 2, off-site keeps 1.
    second = backups.run(settings)
    third = backups.run(settings)
    assert second["ok"] and third["ok"]
    assert [p.name for p in backups.bundles(tmp_path / "out")] == [
        Path(second["dir"]).name,
        Path(third["dir"]).name,
    ]
    assert [p.name for p in backups.bundles(tmp_path / "remote")] == [
        Path(third["dir"]).name
    ]


def test_a_failed_part_marks_the_bundle_incomplete(
    client, channel, pg, config, tmp_path, extra_db
):
    s = _bundle_settings(tmp_path, extra_db)
    s["include"]["containers"] = [{"container": "tl-no-such-container"}]
    s["include"]["docker"] = {"command": "false"}
    s["offsite"] = {"kind": "none"}
    backups.save(s)
    settings = backups.load()
    manifest = backups.run(settings)
    assert manifest["ok"] is False
    assert manifest["errors"][0]["part"] == "container:tl-no-such-container"
    # Other parts still made it.
    assert {p["kind"] for p in manifest["parts"]} == {
        "throughline",
        "globals",
        "database",
        "files",
    }
    with connect() as conn:
        st = backups.status(conn, settings)
    assert st["last_ok_at"] is None
    assert "last_run_failed" in st["reasons"]
    assert ops.check()["reasons"][0] == "no_dump"


def test_tick_runs_when_due_then_waits(client, channel, pg, config, tmp_path, extra_db):
    s = _bundle_settings(tmp_path, extra_db)
    s["verify"] = {"every_hours": 0}
    backups.save(s)
    settings = backups.load()
    out = backups.tick(settings=settings)
    assert out["due"] == "scheduled" and out["backup"]["ok"] is True
    assert out["verify"] is None
    again = backups.tick(settings=settings)
    assert again["due"] == "not_due" and again["backup"] is None
    # The next slot is due again.
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    with connect() as conn:
        assert backups.due(settings, conn, later)["backup"] is True
    backups.save({"enabled": False})
    with connect() as conn:
        assert backups.due(backups.load(), conn, later)["reason"] == "disabled"


def test_missing_passphrase_refuses_before_dumping(client, config, tmp_path, pg):
    backups.save(
        dict(
            destination=dict(dir=str(tmp_path / "out")),
            encryption=dict(passphrase_file=str(tmp_path / "nokey")),
        )
    )
    with pytest.raises(ops.OpsError, match="passphrase"):
        backups.run()
    assert [p.name for p in (tmp_path / "out").iterdir()] == [".backup.lock"]
    created = backups.init_key()
    assert created["created"] and (tmp_path / "nokey").stat().st_mode & 0o777 == 0o600
    assert backups.init_key()["created"] is False


# --- HTTP ----------------------------------------------------------------------------


def test_http_settings_and_status(client, config, tmp_path):
    r = client.get("/v1/backup/settings")
    assert r.status_code == 200, r.text
    assert r.json()["schedule"]["every_hours"] == 24
    r = client.put(
        "/v1/backup/settings",
        json={
            "schedule": {"every_hours": 12, "at": "02:15"},
            "destination": {"dir": str(tmp_path / "dest")},
            "offsite": {"kind": "rsync", "target": "backup-host:/srv/tl"},
            "encryption": {"passphrase_present": True},
            "config_file": "/ignored",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schedule"] == {"every_hours": 12, "at": "02:15"}
    assert body["offsite"]["target"] == "backup-host:/srv/tl"
    assert body["config_file"] == str(config)
    assert (
        client.put("/v1/backup/settings", json={"schedule": {"at": "nope"}}).status_code
        == 400
    )
    r = client.get("/v1/backup/status")
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and "no_backup" in r.json()["reasons"]
    assert r.json()["destination"] == str(tmp_path / "dest")


def test_partial_restore_merges_config_lenses_pages_and_keeps_onboarding_open(
    client, channel, pg, config, tmp_path, monkeypatch
):
    from throughline.onboarding import state as onboarding_state, store

    # Build a synthetic backup bundle containing a DB dump + .throughline files.
    source_home = tmp_path / "src_home" / ".throughline"
    (source_home / "config").mkdir(parents=True)
    (source_home / "lenses").mkdir(parents=True)
    (source_home / "config" / "settings.json").write_text(
        json.dumps({"env": {"THROUGHLINE_TEST_RESTORED": "from_backup", "THROUGHLINE_TEST_KEEP": "backup_val"}, "plugins": {"wave": {"WAVE_URL": "https://wave.example"}}}),
        encoding="utf-8",
    )
    (source_home / "config" / "secrets.json").write_text(
        json.dumps({"env": {"THROUGHLINE_TEST_SECRET": "sec_from_backup"}, "plugins": {"wave": {"WAVE_KEY": "k1"}}}),
        encoding="utf-8",
    )
    (source_home / "lenses" / "custom_notes.toml").write_text('name = "custom_notes"\n', encoding="utf-8")
    (source_home / "pages.toml").write_text('[[sinks]]\nkind = "markdown"\npath = "/tmp/p/{slug}.md"\n', encoding="utf-8")

    crypt = tmp_path / "crypt.py"
    crypt.write_text(CRYPT)
    dest = tmp_path / "bundles"
    backups.save(
        dict(
            destination=dict(dir=str(dest), keep=3),
            encryption=dict(
                kind="command",
                encrypt_cmd=f"{sys.executable} {crypt} enc {{in}} {{out}}",
                decrypt_cmd=f"{sys.executable} {crypt} dec {{in}} {{out}}",
            ),
            include=dict(paths=[str(source_home)], exclude_paths=[]),
        )
    )
    manifest = backups.run()
    assert manifest["ok"] is True
    bundle_dir = Path(manifest["dir"])
    info = backups.inspect_bundle(bundle_dir)
    assert set(info["available_scopes"]) == {"database", "config", "lenses", "pages"}

    # Target install already has a setting and setup marked "done".
    target_home = tmp_path / "dst_home" / ".throughline"
    target_setup = target_home / "config"
    monkeypatch.setenv("THROUGHLINE_SETUP_DIR", str(target_setup))
    store.set_env({"THROUGHLINE_TEST_KEEP": "existing_val"})
    store.set_plugin_values("granola", {"GRANOLA_DIR": "/tmp"})
    onboarding_state.complete()
    assert onboarding_state.get()["status"] == "done"

    # Partial restore (config + lenses + pages only) merges without overwriting existing keys
    # and re-opens onboarding in restore mode.
    res = backups.restore_bundle(
        bundle_dir,
        only=["config", "lenses", "pages"],
        home_dir=target_home,
        setup_dir=target_setup,
        continue_onboarding=True,
    )
    assert res["ok"] is True
    assert res["scopes"] == ["config", "lenses", "pages"]
    assert res["onboarding_open"] is True
    assert store.get_env("THROUGHLINE_TEST_RESTORED") == "from_backup"
    assert store.get_env("THROUGHLINE_TEST_KEEP") == "existing_val"  # preserved!
    assert store.get_env("THROUGHLINE_TEST_SECRET") == "sec_from_backup"
    assert store.plugin_values("wave")["WAVE_KEY"] == "k1"
    assert store.plugin_values("granola")["GRANOLA_DIR"] == "/tmp"  # preserved!
    assert (target_home / "lenses" / "custom_notes.toml").is_file()
    assert (target_home / "pages.toml").is_file()
    assert (target_setup / "restored").is_file()
    st = onboarding_state.get()
    assert st["mode"] == "restore" and st["status"] == "in_progress"

    # Database restore refuses a non-empty database unless clean=True.
    with pytest.raises(ops.OpsError, match="already contains Throughline data"):
        backups.restore_bundle(bundle_dir, only=["database"], clean=False)

    # Cleanup test env vars.
    store.set_env({"THROUGHLINE_TEST_RESTORED": None, "THROUGHLINE_TEST_KEEP": None, "THROUGHLINE_TEST_SECRET": None})
