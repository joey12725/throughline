"""Ops guarantees (ONB-2, ARC-5, PLG-7; throughline.ops and
`throughline lib backup`): snapshot-consistent dumps with counts and hashes,
encryption and off-site copy through external commands, the restore drill
compared against the stored snapshot values, retention and the staleness
alarm. Offline: encryption and off-site are stub scripts written to tmp.
Synthetic data only."""

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from conftest import ingest, message
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from typer.testing import CliRunner

from throughline import cli, ops
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

FAIL = """
import sys
sys.stderr.write(
    "cannot encrypt: password=hunter2-synthetic url=postgresql://synth:pw-synthetic@h/db "
    + "Q" * 48 + " " + "x" * 2000
)
sys.exit(3)
"""

SLOW = """
import time
time.sleep(30)
"""

COPY = """
import shutil, sys
shutil.copy(sys.argv[1], sys.argv[2])
"""


@pytest.fixture()
def pg(monkeypatch):
    try:
        program = cli.pg_program("pg_dump")
        cli.pg_program("pg_restore")
    except Exception:
        pytest.skip("pg_dump/pg_restore not found; set THROUGHLINE_PG_BIN")
    monkeypatch.setenv("THROUGHLINE_PG_BIN", str(Path(program).parent))
    for name in (
        "THROUGHLINE_BACKUP_ENCRYPT_CMD",
        "THROUGHLINE_BACKUP_DECRYPT_CMD",
        "THROUGHLINE_BACKUP_OFFSITE_CMD",
        "THROUGHLINE_BACKUP_KEEP",
        "THROUGHLINE_BACKUP_STALE_HOURS",
        "THROUGHLINE_SCRATCH_DATABASE_URL",
        "THROUGHLINE_BACKUP_DIR",
        "THROUGHLINE_BACKUP_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def stubs(tmp_path):
    folder = tmp_path / "stubs"
    folder.mkdir()
    out = {}
    for name, source in (
        ("crypt", CRYPT),
        ("fail", FAIL),
        ("slow", SLOW),
        ("copy", COPY),
    ):
        path = folder / f"{name}.py"
        path.write_text(source, encoding="utf-8")
        out[name] = path
    py = sys.executable
    remote = tmp_path / "offsite"
    remote.mkdir()
    return dict(
        encrypt=f'"{py}" "{out["crypt"]}" enc {{in}} {{out}}',
        decrypt=f'"{py}" "{out["crypt"]}" dec {{in}} {{out}}',
        fail=f'"{py}" "{out["fail"]}" {{in}} {{out}}',
        slow=f'"{py}" "{out["slow"]}" {{in}} {{out}}',
        offsite=f'"{py}" "{out["copy"]}" {{file}} "{remote}"',
        remote=remote,
    )


def owner(statement, params=()):
    with connect() as c:
        return c.execute(statement, params).fetchall()


def files(directory):
    return sorted(p.name for p in Path(directory).iterdir())


def corpus(client, n=4):
    rows = [message(i) for i in range(n)]
    assert ingest(client, rows).status_code == 200
    changed = message(0, content="Synthetic orchard message 0, corrected")
    assert ingest(client, [changed]).status_code == 200
    r = client.post(
        "/v1/links",
        json={
            "type": "about",
            "from": {"channel": "synthetic.chat", "external_id": "fake-1"},
            "to": {"channel": "synthetic.chat", "external_id": "fake-2"},
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/v1/identity/people",
        json={"handle": "synthetic-person", "display_name": "Synthetic Person"},
    )
    assert r.status_code == 200, r.text


# --- backup ------------------------------------------------------------------------


def test_p10_backup_encrypts_and_removes_plaintext_even_on_failure(
    client, channel, pg, stubs, tmp_path
):
    corpus(client)
    out = tmp_path / "backups"
    result = ops.backup(out, encrypt_cmd=stubs["encrypt"])
    assert result["ok"] and result["encrypted"]
    assert files(out) == [Path(result["file"]).name]
    assert result["file"].endswith(".dump.enc")
    head = Path(result["file"]).read_bytes()[:6]
    assert head == b"TLENC1" and not head.startswith(b"PGDMP")
    # Encryption fails: the plaintext is removed anyway and the run is failed.
    bad = tmp_path / "bad"
    with pytest.raises(ops.OpsError) as exc:
        ops.backup(bad, encrypt_cmd=stubs["fail"])
    assert "encrypt failed" in str(exc.value)
    assert files(bad) == []
    failed = owner("SELECT * FROM tl.backup_runs WHERE status='failed'")
    assert [r["kind"] for r in failed] == ["dump"]
    assert "encrypt failed" in failed[0]["detail"]["error"]
    # The CLI reports the failure with a non-zero exit code.
    r = CliRunner().invoke(
        cli.app,
        ["lib", "backup", "run", "--dir", str(bad)],
        env={"THROUGHLINE_BACKUP_ENCRYPT_CMD": stubs["fail"]},
    )
    assert r.exit_code == 1 and files(bad) == []


def test_plaintext_dump_refused_without_encrypt_command(
    client, channel, pg, stubs, tmp_path, monkeypatch
):
    """With no encrypt command nothing is dumped unless plaintext is asked for."""
    corpus(client)
    monkeypatch.delenv("THROUGHLINE_BACKUP_ENCRYPT_CMD", raising=False)
    out = tmp_path / "backups"
    with pytest.raises(ops.OpsError) as exc:
        ops.backup(out)
    assert "plaintext dump is refused" in str(exc.value)
    assert not out.exists() or files(out) == []
    failed = owner("SELECT * FROM tl.backup_runs WHERE status='failed'")
    assert [(r["kind"], r["detail"]["error"]) for r in failed] == [
        ("dump", "plaintext_refused")
    ]
    r = CliRunner().invoke(cli.app, ["lib", "backup", "run", "--dir", str(out)])
    assert r.exit_code == 1 and "plaintext dump is refused" in r.output
    assert not out.exists() or files(out) == []
    r = CliRunner().invoke(
        cli.app, ["lib", "backup", "run", "--dir", str(out), "--allow-plaintext"]
    )
    assert r.exit_code == 0, r.output
    assert len(files(out)) == 1 and files(out)[0].endswith(".dump")


def test_p10_offsite_refuses_plaintext(client, channel, pg, stubs, tmp_path):
    corpus(client)
    out = tmp_path / "backups"
    result = ops.backup(out, offsite_cmd=stubs["offsite"], allow_plaintext=True)
    assert not result["ok"]
    assert result["offsite"]["status"] == "failed"
    assert result["offsite"]["error"] == "plaintext_refused"
    assert files(stubs["remote"]) == []
    offsite = owner("SELECT * FROM tl.backup_runs WHERE kind='offsite'")
    assert [r["status"] for r in offsite] == ["failed"]
    # The CLI exits non-zero and never copies plaintext by default.
    r = CliRunner().invoke(
        cli.app,
        ["lib", "backup", "run", "--dir", str(out), "--allow-plaintext"],
        env={"THROUGHLINE_BACKUP_OFFSITE_CMD": stubs["offsite"]},
    )
    assert r.exit_code == 1, r.output
    assert files(stubs["remote"]) == []
    # Only an explicit flag sends a plaintext dump off-site.
    r = CliRunner().invoke(
        cli.app,
        ["lib", "backup", "run", "--dir", str(out), "--allow-plaintext-offsite"],
        env={"THROUGHLINE_BACKUP_OFFSITE_CMD": stubs["offsite"]},
    )
    assert r.exit_code == 0, r.output
    assert len(files(stubs["remote"])) == 1


def test_p10_offsite_receives_encrypted_file_and_hash_recorded(
    client, channel, pg, stubs, tmp_path
):
    corpus(client)
    result = ops.backup(
        tmp_path / "backups",
        encrypt_cmd=stubs["encrypt"],
        offsite_cmd=stubs["offsite"],
    )
    assert result["ok"] and result["offsite"]["status"] == "ok"
    [copied] = files(stubs["remote"])
    assert copied.endswith(".dump.enc")
    remote = stubs["remote"] / copied
    assert ops.sha256_file(remote) == result["sha256"]
    assert remote.read_bytes()[:5] != b"PGDMP"
    rows = {
        r["kind"]: r
        for r in owner("SELECT * FROM tl.backup_runs WHERE status='ok' ORDER BY id")
    }
    assert set(rows) == {"dump", "offsite"}
    assert (
        rows["dump"]["file_sha256"]
        == rows["offsite"]["file_sha256"]
        == result["sha256"]
    )
    assert rows["offsite"]["bytes"] == remote.stat().st_size
    assert rows["offsite"]["detail"]["dump_id"] == rows["dump"]["id"]
    assert rows["dump"]["detail"]["counts"]["messages"] == 5
    assert set(ops.HASHES) <= set(rows["dump"]["detail"]["hashes"])


def test_p10_counts_and_hashes_taken_in_dump_snapshot(
    client, channel, pg, stubs, tmp_path
):
    corpus(client)
    seen = []

    def concurrent(snapshot):
        # Another connection commits a message after the snapshot export,
        # before pg_dump runs and before the counts are taken.
        seen.append(snapshot)
        assert ingest(client, [message(99)]).status_code == 200

    result = ops.backup(
        tmp_path / "backups", on_snapshot=concurrent, allow_plaintext=True
    )
    assert seen and result["snapshot"] == seen[0]
    live = owner("SELECT count(*) AS n FROM tl.messages")[0]["n"]
    assert live == 6 and result["counts"]["messages"] == 5
    restored = []

    def look(url):
        with psycopg.connect(url) as c:
            restored.extend(
                r[0] for r in c.execute("SELECT external_id FROM tl.messages")
            )

    verified = ops.verify_restore(result["file"], on_restored=look)
    assert verified["ok"], verified
    assert restored and "fake-99" not in restored
    messages = next(t for t in verified["tables"] if t["name"] == "messages")
    assert messages == dict(name="messages", expected=5, restored=5, equal=True)


# --- restore drill ---------------------------------------------------------------


def test_p10_verify_restore_counts_and_hashes_equal_on_fixture_corpus(
    client, channel, pg, stubs, tmp_path
):
    corpus(client)
    result = ops.backup(tmp_path / "backups", encrypt_cmd=stubs["encrypt"])
    # The cluster is shared with other test sessions, which may run their own
    # drills at the same time: the scratch database checked is the one this
    # drill restored into (named through on_restored), not every tl_verify_*.
    scratch = "SELECT datname FROM pg_database WHERE datname LIKE %s"
    pattern = ("tl\\_verify\\_%",)
    before = {r["datname"] for r in owner(scratch, pattern)}
    restored_into = []
    verified = ops.verify_restore(
        result["file"],
        decrypt_cmd=stubs["decrypt"],
        on_restored=lambda url: restored_into.append(conninfo_to_dict(url)["dbname"]),
    )
    assert verified["ok"], verified
    assert verified["counts_unavailable"] is False
    assert verified["tables"] and all(t["equal"] for t in verified["tables"])
    names = {t["name"] for t in verified["tables"]}
    assert {"messages", "raw_payloads", "links", "people", "backup_runs"} <= names
    assert {h["name"] for h in verified["hashes"]} >= set(ops.HASHES)
    assert all(h["equal"] for h in verified["hashes"])
    assert verified["probes"] and all(p["ok"] for p in verified["probes"])
    assert {"catalog()", "search", "describe"} <= {
        p["name"] for p in verified["probes"]
    }
    assert verified["duration_s"] >= 0
    run = owner("SELECT * FROM tl.backup_runs WHERE kind='verify'")
    assert [r["status"] for r in run] == ["ok"]
    assert run[0]["detail"]["dump_id"] == result["dump_id"]
    # The scratch database was created by this drill and dropped afterwards.
    assert len(restored_into) == 1 and restored_into[0].startswith("tl_verify_")
    assert restored_into[0] not in before
    assert restored_into[0] not in {r["datname"] for r in owner(scratch, pattern)}
    # The CLI drill exits 0.
    r = CliRunner().invoke(
        cli.app,
        ["lib", "backup", "verify-restore", result["file"], "--json"],
        env={"THROUGHLINE_BACKUP_DECRYPT_CMD": stubs["decrypt"]},
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["ok"] is True


def test_p10_verify_restore_ignores_live_changes_after_dump(
    client, channel, pg, stubs, tmp_path
):
    corpus(client)
    result = ops.backup(tmp_path / "backups", allow_plaintext=True)
    assert ingest(client, [message(i) for i in range(10, 20)]).status_code == 200
    with connect() as c:
        c.execute("UPDATE tl.people SET display_name='Synthetic renamed'")
    verified = ops.verify_restore(result["file"])
    assert verified["ok"], verified
    messages = next(t for t in verified["tables"] if t["name"] == "messages")
    assert messages["expected"] == messages["restored"] == 5


def test_verify_restore_hardens_under_the_cluster_lock(
    client, channel, pg, tmp_path, monkeypatch
):
    # tl.harden_reader() alters cluster-wide role settings, so a drill that
    # runs it beside a migration of another database of the same cluster
    # would fail with "tuple concurrently updated": it holds the same
    # cluster lock as migrations while it hardens the scratch database.
    corpus(client)
    result = ops.backup(tmp_path / "backups", allow_plaintext=True)
    maintenance = make_conninfo(
        **(conninfo_to_dict(database_url()) | {"dbname": "postgres"})
    )
    seen = []
    real = ops.cluster_lock

    @contextmanager
    def recording(url=None):
        with real(url):
            with psycopg.connect(maintenance, autocommit=True) as other:
                free = other.execute(
                    "SELECT pg_try_advisory_xact_lock(71824003)"
                ).fetchone()[0]
            seen.append((conninfo_to_dict(url)["dbname"], free))
            yield

    monkeypatch.setattr(ops, "cluster_lock", recording)
    verified = ops.verify_restore(result["file"])
    assert verified["ok"], verified
    assert len(seen) == 1 and seen[0][0].startswith("tl_verify_")
    assert seen[0][1] is False  # held by the drill, not free


def test_p10_verify_restore_detects_tampered_dump(client, channel, pg, stubs, tmp_path):
    corpus(client)
    result = ops.backup(tmp_path / "backups", allow_plaintext=True)

    def tamper(url):
        with psycopg.connect(url) as c:
            c.execute("SET session_replication_role=replica")
            c.execute(
                "UPDATE tl.messages SET content='Synthetic tampered' WHERE external_id='fake-1'"
            )

    verified = ops.verify_restore(result["file"], on_restored=tamper)
    assert not verified["ok"]
    hashes = {h["name"]: h["equal"] for h in verified["hashes"]}
    assert hashes["messages"] is False and hashes["links"] is True
    assert all(t["equal"] for t in verified["tables"])
    run = owner("SELECT * FROM tl.backup_runs WHERE kind='verify'")[0]
    assert run["status"] == "failed" and run["detail"]["hashes_unequal"] == ["messages"]


def test_p10_verify_restore_foreign_dump_reports_counts_unavailable(
    client, channel, pg, tmp_path
):
    corpus(client)
    path = tmp_path / "foreign.dump"
    r = CliRunner().invoke(cli.app, ["dump", str(path)])
    assert r.exit_code == 0, r.output
    verified = ops.verify_restore(path)
    assert verified["ok"], verified
    assert verified["counts_unavailable"] is True
    assert all(t["expected"] is None for t in verified["tables"])
    assert verified["probes"] and all(p["ok"] for p in verified["probes"])


def test_p10_verify_restore_refuses_live_database_target(client, channel, pg, tmp_path):
    corpus(client)
    result = ops.backup(tmp_path / "backups", allow_plaintext=True)
    live = database_url()
    settings = conninfo_to_dict(live)
    alias = make_conninfo(**(settings | {"host": "localhost"}))
    before = owner("SELECT count(*) AS n FROM tl.messages")[0]["n"]
    for target in (live, alias):
        verified = ops.verify_restore(result["file"], scratch_url=target)
        assert not verified["ok"] and "live database" in verified["error"]
    r = CliRunner().invoke(
        cli.app,
        ["lib", "backup", "verify-restore", result["file"]],
        env={"THROUGHLINE_SCRATCH_DATABASE_URL": live},
    )
    assert r.exit_code == 1
    assert owner("SELECT count(*) AS n FROM tl.messages")[0]["n"] == before


# --- alarm, status, retention, commands -----------------------------------------


def test_p10_check_exit_code_stale_and_fresh(client, pg):
    runner = CliRunner()
    r = runner.invoke(cli.app, ["lib", "backup", "check", "--json"])
    assert r.exit_code == 2
    assert set(json.loads(r.output)["reasons"]) == {"no_dump", "no_verify"}
    with connect() as c:
        c.execute(
            "INSERT INTO tl.backup_runs(kind,started_at,finished_at,status) VALUES ('dump',now(),now(),'ok')"
        )
    r = runner.invoke(cli.app, ["lib", "backup", "check", "--json"])
    assert r.exit_code == 2 and json.loads(r.output)["reasons"] == ["no_verify"]
    with connect() as c:
        c.execute(
            "INSERT INTO tl.backup_runs(kind,started_at,finished_at,status) VALUES ('verify',now(),now(),'ok'),('verify',now(),now(),'failed')"
        )
    assert runner.invoke(cli.app, ["lib", "backup", "check"]).exit_code == 0
    assert ops.check()["ok"] is True
    with connect() as c:
        c.execute(
            "UPDATE tl.backup_runs SET finished_at=now()-interval '40 hours' WHERE kind='verify'"
        )
    r = runner.invoke(cli.app, ["lib", "backup", "check", "--json"])
    assert r.exit_code == 2 and json.loads(r.output)["reasons"] == ["verify_stale"]
    assert (
        runner.invoke(
            cli.app, ["lib", "backup", "check", "--stale-hours", "48"]
        ).exit_code
        == 0
    )
    r = runner.invoke(
        cli.app,
        ["lib", "backup", "check"],
        env={"THROUGHLINE_BACKUP_STALE_HOURS": "41"},
    )
    assert r.exit_code == 0
    listed = runner.invoke(cli.app, ["lib", "backup", "list", "--json"])
    assert listed.exit_code == 0 and len(json.loads(listed.output)) == 3


def test_backup_runs_hold_no_content(client, channel, pg, stubs, tmp_path):
    marker = "zebraquartz-synthetic-marker"
    rows = [
        message(i, content=f"Synthetic {marker} {i}", payload={"m": marker + str(i)})
        for i in range(3)
    ]
    assert ingest(client, rows).status_code == 200
    result = ops.backup(
        tmp_path / "backups",
        encrypt_cmd=stubs["encrypt"],
        offsite_cmd=stubs["offsite"],
    )
    assert ops.verify_restore(result["file"], decrypt_cmd=stubs["decrypt"])["ok"]
    with pytest.raises(ops.OpsError):
        ops.backup(tmp_path / "bad", encrypt_cmd=stubs["fail"])
    stored = owner("SELECT * FROM tl.backup_runs")
    assert {r["kind"] for r in stored} == {"dump", "offsite", "verify"}
    text = json.dumps([r["detail"] for r in stored], default=str)
    assert marker not in text and "Synthetic" not in text
    assert "hunter2" not in text and "pw-synthetic" not in text
    listed = CliRunner().invoke(cli.app, ["lib", "backup", "list", "--json"])
    assert listed.exit_code == 0 and marker not in listed.output


def test_status_staleness_reflects_backup_runs(client, channel, pg, tmp_path):
    corpus(client)
    before = client.get("/v1/status").json()["staleness"]["backup"]
    assert before["stale"] is True and before["last_dump_at"] is None
    result = ops.backup(tmp_path / "backups", allow_plaintext=True)
    assert ops.verify_restore(result["file"])["ok"]
    after = client.get("/v1/status").json()["staleness"]["backup"]
    assert after["stale"] is False
    assert after["last_dump_at"] and after["last_verified_at"]
    assert after["age_hours"] < 1
    assert ops.check()["ok"] is True


def test_retention_keeps_n(client, channel, pg, tmp_path, monkeypatch):
    corpus(client, 3)
    out = tmp_path / "backups"
    out.mkdir()
    (out / "unrelated.txt").write_text("synthetic", encoding="utf-8")
    made = [ops.backup(out, keep=2, allow_plaintext=True)["file"] for _ in range(3)]
    kept = [n for n in files(out) if n.startswith("throughline-")]
    assert kept == sorted(Path(f).name for f in made[1:])
    assert "unrelated.txt" in files(out)
    monkeypatch.setenv("THROUGHLINE_BACKUP_KEEP", "1")
    last = ops.backup(out, allow_plaintext=True)
    assert [n for n in files(out) if n.startswith("throughline-")] == [
        Path(last["file"]).name
    ]
    assert len(last["removed"]) == 2


def test_commands_run_without_shell_and_redact_stderr(
    client, channel, pg, stubs, tmp_path, monkeypatch
):
    # Template values are filled inside arguments, never re-split or shelled.
    assert ops.command_args("prog {in} ; rm -rf x", **{"in": "a b; c"}) == [
        "prog",
        "a b; c",
        ";",
        "rm",
        "-rf",
        "x",
    ]
    corpus(client, 3)
    calls = []
    real = subprocess.run

    def spy(args, **kwargs):
        calls.append((args, kwargs))
        return real(args, **kwargs)

    monkeypatch.setattr(ops.subprocess, "run", spy)
    result = ops.backup(
        tmp_path / "backups",
        encrypt_cmd=stubs["encrypt"],
        offsite_cmd=stubs["offsite"],
    )
    ops.verify_restore(result["file"], decrypt_cmd=stubs["decrypt"])
    # pg_dump, encrypt, offsite, decrypt, pg_restore; when the client tools
    # are newer than the server (pg_restore 17+ against PostgreSQL 16) the
    # refused direct restore is followed by pg_restore to a script and psql.
    names = [Path(args[0]).stem for args, _ in calls]
    if names.count("pg_restore") == 2:
        assert names[4:] == ["pg_restore", "pg_restore", "psql"], names
        assert "--file" in calls[5][0] and "ON_ERROR_STOP=1" in calls[6][0]
    else:
        assert len(calls) == 5, names
    for args, kwargs in calls:
        assert isinstance(args, list) and kwargs.get("shell") is False
        assert kwargs.get("timeout")
    # A failing command: exit code checked, stderr truncated and redacted.
    with pytest.raises(ops.OpsError) as exc:
        ops.backup(tmp_path / "bad", encrypt_cmd=stubs["fail"])
    message_text = str(exc.value)
    assert "exit code 3" in message_text
    for secret in ("hunter2-synthetic", "pw-synthetic", "Q" * 48, "x" * 500):
        assert secret not in message_text
    assert len(message_text) < 600
    failed = owner("SELECT detail FROM tl.backup_runs WHERE status='failed'")
    assert failed and "hunter2" not in json.dumps(failed)

    # A hanging command is stopped by the timeout and leaves no plaintext.
    def short(args, **kwargs):
        if any("slow.py" in str(a) for a in args):
            kwargs["timeout"] = 1
        return real(args, **kwargs)

    monkeypatch.setattr(ops.subprocess, "run", short)
    with pytest.raises(ops.OpsError) as exc:
        ops.backup(tmp_path / "slow", encrypt_cmd=stubs["slow"])
    assert "timed out" in str(exc.value)
    assert files(tmp_path / "slow") == []


# --- checker additions -------------------------------------------------------


def test_pg_programs_get_the_password_through_the_environment():
    dsn, env = ops._libpq("postgresql://synth:pw-synthetic@127.0.0.1:5999/synthdb")
    assert "pw-synthetic" not in dsn
    assert conninfo_to_dict(dsn) == dict(
        user="synth", host="127.0.0.1", port="5999", dbname="synthdb"
    )
    assert env["PGPASSWORD"] == "pw-synthetic"
    dsn, env = ops._libpq("host=127.0.0.1 dbname=synthdb")
    assert conninfo_to_dict(dsn)["dbname"] == "synthdb"


def test_hashes_do_not_depend_on_session_settings(
    client, channel, pg, tmp_path, monkeypatch
):
    corpus(client)
    with connect() as c:
        c.execute("UPDATE tl.links SET valid_from='2021-03-04 05:06:07.123+00'")
    result = ops.backup(tmp_path / "backups", allow_plaintext=True)
    # The drill's sessions render times in another time zone.
    monkeypatch.setenv("PGTZ", "<+14>-14")  # POSIX form: no tz files
    verified = ops.verify_restore(result["file"])
    assert verified["ok"], verified
    assert all(h["equal"] for h in verified["hashes"])


def test_verify_restore_into_scratch_url_refuses_non_empty_target(
    client, channel, pg, stubs, tmp_path, monkeypatch
):
    corpus(client)
    result = ops.backup(tmp_path / "backups", encrypt_cmd=stubs["encrypt"])
    made = []
    real = ops.tempfile.mkdtemp

    def mkdtemp(**kwargs):
        made.append(real(**kwargs))
        return made[-1]

    monkeypatch.setattr(ops.tempfile, "mkdtemp", mkdtemp)
    name = "tl_scratch_" + ops.secrets.token_hex(4)
    with psycopg.connect(database_url(), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        scratch = make_conninfo(**(conninfo_to_dict(database_url()) | {"dbname": name}))
        verified = ops.verify_restore(
            result["file"], scratch_url=scratch, decrypt_cmd=stubs["decrypt"]
        )
        assert verified["ok"], verified
        assert verified["counts_unavailable"] is False
        # The decrypted plaintext lived in a private temp dir, now removed.
        assert made and not Path(made[0]).exists()
        # The scratch database now holds a tl schema: a second drill refuses it.
        again = ops.verify_restore(
            result["file"], scratch_url=scratch, decrypt_cmd=stubs["decrypt"]
        )
        assert not again["ok"] and "not empty" in again["error"]
        # A scratch URL is never dropped by the drill.
        assert owner("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
    finally:
        with psycopg.connect(database_url(), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_probes_run_read_only(client, channel, pg, tmp_path):
    corpus(client, 3)
    result = ops.backup(tmp_path / "backups", allow_plaintext=True)

    def writing_example(url):
        with psycopg.connect(url) as c:
            c.execute("SET session_replication_role=replica")
            c.execute(
                "UPDATE tl.api_functions SET example='CREATE TABLE tl.probe_wrote(x int)' WHERE name='describe'"
            )

    verified = ops.verify_restore(result["file"], on_restored=writing_example)
    assert not verified["ok"]
    probe = next(p for p in verified["probes"] if p["name"] == "describe")
    assert probe == dict(name="describe", ok=False, error="ReadOnlySqlTransaction")
    assert all(p["ok"] for p in verified["probes"] if p["name"] != "describe")


def test_check_reports_dump_stale(client, pg):
    with connect() as c:
        c.execute(
            "INSERT INTO tl.backup_runs(kind,started_at,finished_at,status) VALUES "
            "('dump',now()-interval '50 hours',now()-interval '50 hours','ok'),"
            "('dump',now(),now(),'failed'),('verify',now(),now(),'ok')"
        )
    result = ops.check()
    assert result["ok"] is False and result["reasons"] == ["dump_stale"]
    assert result["dump_age_hours"] > 36
    r = CliRunner().invoke(cli.app, ["lib", "backup", "check"])
    assert r.exit_code == 2
