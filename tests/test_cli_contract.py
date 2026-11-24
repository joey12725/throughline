from pathlib import Path

import pytest
from conftest import ingest, message
from openapi_spec_validator import validate
from typer.testing import CliRunner

from throughline.api import create_app
from throughline.cli import app, pg_program
from throughline.db import connect


def test_int_3_openapi_validated(client):
    schema = create_app().openapi()
    validate(schema)
    item = schema["components"]["schemas"]["Ingest"]["properties"]["messages"]["items"]
    assert {"channel", "external_id", "happened_at", "content", "payload"} <= set(
        item["required"]
    )
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            assert operation.get("security"), (path, method)
    assert client.get("/v1/openapi.json").status_code == 200


def test_int_1_whole_cli_help_budget():
    runner = CliRunner()
    text = ""
    groups = {
        "": list(app.registered_commands),
        "token": list(
            __import__(
                "throughline.cli", fromlist=["tokens"]
            ).tokens.registered_commands
        ),
        "identity": list(
            __import__(
                "throughline.cli", fromlist=["identities"]
            ).identities.registered_commands
        ),
        "track": list(
            __import__("throughline.cli", fromlist=["track"]).track.registered_commands
        ),
    }
    # Whole CLI command inventory plus each group, not repeated option scaffolding.
    for name in groups:
        result = runner.invoke(app, ([name] if name else []) + ["--help"], color=False)
        assert result.exit_code == 0, result.output
        text += result.output
    assert len(text) / 4 < 1500, len(text) / 4


def test_int_1_token_cli_and_confirmation(client):
    runner = CliRunner()
    r = runner.invoke(
        app, ["token", "create", "synthetic-cli", "--kind", "agent", "--json"]
    )
    assert r.exit_code == 0, r.output
    import json

    token_id = json.loads(r.output)["id"]
    assert runner.invoke(app, ["token", "list", "--json"]).exit_code == 0
    assert runner.invoke(app, ["token", "revoke", str(token_id)]).exit_code == 0
    assert runner.invoke(app, ["purge", "synthetic.chat"]).exit_code != 0


def test_onb_2_pg_bin_and_backup_roundtrip(client, channel, tmp_path, monkeypatch):
    # CI can supply PATH or THROUGHLINE_PG_BIN; no embedded machine paths.
    try:
        program = pg_program("pg_dump")
    except Exception:
        pytest.skip("pg_dump not found; set THROUGHLINE_PG_BIN")
    monkeypatch.setenv("THROUGHLINE_PG_BIN", str(Path(program).parent))
    assert Path(pg_program("pg_dump")).parent == Path(program).parent
    ingest(client, [message(i) for i in range(3)])
    with connect() as c:
        before = c.execute("SELECT * FROM api.messages ORDER BY id").fetchall()
    runner = CliRunner()
    path = tmp_path / "synthetic.dump"
    r = runner.invoke(app, ["dump", str(path)])
    assert r.exit_code == 0, r.output
    with connect() as c:
        c.execute("DROP SCHEMA api CASCADE; DROP SCHEMA tl CASCADE")
    r = runner.invoke(app, ["restore", str(path)])
    assert r.exit_code == 0, r.output
    with connect() as c:
        assert c.execute("SELECT * FROM api.messages ORDER BY id").fetchall() == before


def test_arc_5_owner_purge_cli(client, channel):
    ingest(client, [message()])
    m = message()
    m["content"] = "Changed synthetic message"
    ingest(client, [m])
    result = CliRunner().invoke(app, ["purge", channel, "--confirm"])
    assert result.exit_code == 0, result.output
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.messages").fetchone()["n"] == 0
        assert c.execute("SELECT count(*) n FROM tl.raw_payloads").fetchone()["n"] == 0


def test_int_1_owner_maintenance_commands_are_listed():
    # Commands an owner needs are discoverable from help, not only from docs.
    runner = CliRunner()
    track = runner.invoke(app, ["track", "--help"], color=False).output
    assert "detect-visits" in track and "retire" in track
    assert "location-source" in runner.invoke(app, ["token", "--help"]).output
    root = runner.invoke(app, ["--help"], color=False).output
    for described in ("token administration", "Identity", "Location"):
        assert described in root
