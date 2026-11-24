"""Integration: lineage, passages, retrieval and result sets fit together.

Synthetic data only. Each package has its own tests; these check the seams
between them: migrations, registries, discovery, and one page whose sources
gate its passages, their lineage and a result set holding them.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import ingest, message
from psycopg import sql
from typer.testing import CliRunner

from throughline import access, passages, resultsets
from throughline.api import create_app
from throughline.db import connect
from throughline.query import FUNCTIONS, SCALAR_FUNCTIONS
from throughline.scope import Scope

SRC = Path(__file__).resolve().parents[1] / "src" / "throughline"
WAVE2 = ("0036_lineage", "0037_passages", "0038_retrieval", "0039_result_sets")
PAGE = "fake.page"
AT = datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_askers(database):
    with connect() as c:
        c.execute("DELETE FROM tl.askers")
    yield
    with connect() as c:
        c.execute("DELETE FROM tl.askers")


def reader(statement, params=None, asker=None):
    with connect("reader") as c:
        if asker is not None:
            for name, value in (
                ("scope_mode", "asker"),
                ("asker", asker),
                ("unknown_asker", "deny"),
            ):
                c.execute(
                    sql.SQL("SET LOCAL throughline.{}={}").format(
                        sql.SQL(name), sql.Literal(value)
                    )
                )
        return c.execute(statement, params).fetchall()


def register(client, name, **kwargs):
    r = client.post("/v1/channels", json=dict(name=name, plugin="synthetic", **kwargs))
    assert r.status_code == 200, r.text


def put(client, items):
    r = ingest(client, items)
    assert r.status_code == 200 and not r.json()["rejected"], r.text


def message_id(channel, external_id):
    with connect() as c:
        return c.execute(
            "SELECT m.id FROM tl.messages m JOIN tl.channels c ON c.id=m.channel_id WHERE c.name=%s AND m.external_id=%s AND m.superseded_by IS NULL",
            (channel, external_id),
        ).fetchone()["id"]


def scope_of(asker):
    with connect() as c:
        token = c.execute(
            "SELECT * FROM tl.tokens WHERE kind='owner' AND revoked_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
    return Scope.from_token(dict(token, asker=asker))


# --- schema and registries ----------------------------------------------------------


def test_wave2_migrations_applied_on_a_fresh_database(client):
    files = sorted(p.name for p in (SRC / "migrations").glob("*.sql"))
    with connect() as c:
        applied = [
            r["version"]
            for r in c.execute(
                "SELECT version FROM tl.schema_migrations ORDER BY version"
            )
        ]
    assert applied == files
    assert [f"{name}.sql" for name in WAVE2] == [
        f for f in files if f[:4] in {"0036", "0037", "0038", "0039"}
    ]


def test_wave2_allowlist_names_are_registered_row_functions(client):
    with connect() as c:
        registry = {
            r["name"]: r for r in c.execute("SELECT * FROM tl.api_functions").fetchall()
        }
        composite = {
            r["proname"]
            for r in c.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_type t ON t.oid=p.prorettype WHERE n.nspname='api' AND p.proretset AND (t.typtype='c' OR p.prorettype='record'::regtype)"
            )
        }
    for name in WAVE2:
        data = json.loads((SRC / "allowlist" / f"{name}.json").read_text("utf-8"))
        for function in data["functions"]:
            assert function in FUNCTIONS and function not in SCALAR_FUNCTIONS
            assert function in composite, function
            assert registry[function]["kind"] == "sql", function
    catalog = reader("SELECT api.catalog() c")[0]["c"]
    subjects = json.dumps(catalog)
    for section in ("lineage", "passages", "recall", "result_sets"):
        assert f'"{section}"' in subjects, section


def test_every_record_source_has_the_record_contract_columns(client):
    def columns(relation):
        schema, name = relation.split(".")
        with connect() as c:
            return [
                (r["column_name"], r["data_type"])
                for r in c.execute(
                    "SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                    (schema, name),
                )
            ]

    with connect() as c:
        sources = {
            r["prefix"]: r["relation"]
            for r in c.execute("SELECT * FROM tl.record_sources")
        }
    assert {"m", "p", "l", "e"} <= set(sources)
    contract = columns("api.record_versions")
    for prefix, relation in sources.items():
        assert columns(relation) == contract, prefix


def test_wave2_routes_and_cli_commands_are_discovered(client):
    paths = {
        (method.upper(), path)
        for path, operations in create_app().openapi()["paths"].items()
        for method in operations
    }
    for expected in (
        ("POST", "/v1/provenance"),
        ("GET", "/v1/sources"),
        ("GET", "/v1/links/{link_id}/history"),
        ("POST", "/v1/links/{link_id}/supersede"),
        ("POST", "/v1/entities/split"),
        ("POST", "/v1/recall"),
        ("POST", "/v1/sessions"),
        ("POST", "/v1/sets"),
        ("POST", "/v1/sets/transform"),
        ("GET", "/v1/sets/{handle}"),
        ("POST", "/v1/sets/{handle}/inspect"),
        ("DELETE", "/v1/sets/{handle}"),
    ):
        assert expected in paths, expected
    from throughline.cli import app

    result = CliRunner().invoke(app, ["lib", "--help"])
    assert result.exit_code == 0, result.output
    for command in ("passages", "recall", "set", "session"):
        assert command in result.output, command


def test_describe_access_names_the_source_rule(client):
    rule = reader("SELECT api.describe_access() d")[0]["d"]["rule"]
    assert "every source it cites is visible" in rule
    assert "passage" in rule


# --- one sourced page across the packages ---------------------------------------------


def test_page_sources_gate_passages_lineage_and_result_sets(client):
    register(client, PAGE, is_reference=True, record_kind="page_revision")
    register(client, "fake.public")
    register(client, "fake.secret")
    put(
        client,
        [
            message(1, channel="fake.public", external_id="pub", fields={}),
            message(2, channel="fake.secret", external_id="sec", fields={}),
            message(
                3,
                channel=PAGE,
                external_id="wiki",
                thread={"external_id": "wiki"},
                content="# Orchard\n\nSynthetic orchard page.",
                happened_at=AT.isoformat(),
                fields={},
            ),
        ],
    )
    r = client.post(
        "/v1/provenance",
        json={
            "derived": {"channel": PAGE, "external_id": "wiki"},
            "sources": [
                {"channel": "fake.public", "external_id": "pub"},
                {"channel": "fake.secret", "external_id": "sec"},
                {"channel": "fake.public", "external_id": "missing"},
            ],
        },
    )
    assert r.status_code == 200, r.text
    # Writes now carry a receipt (write contract); compare the counts only.
    assert {k: v for k, v in r.json().items() if k != "receipt"} == {
        "written": 2,
        "unresolved": 1,
    }
    with connect("app") as c:
        assert passages.build(c, 100) == 1
    page, public, secret = (
        message_id(PAGE, "wiki"),
        message_id("fake.public", "pub"),
        message_id("fake.secret", "sec"),
    )
    (passage,) = reader("SELECT * FROM api.passage_records")
    assert passage["generated"] and not passage["unsupported"]
    # Without sources of its own, a passage is supported by its revision's.
    assert sorted(s["message_id"] for s in passage["support"]) == sorted(
        [public, secret]
    )
    pid = passage["ref_id"]
    # A passage-level source of its own.
    with connect() as c:
        c.execute(
            "INSERT INTO tl.provenance(derived_kind,derived_id,source_message_id,source_version,origin) SELECT 'passage',%s,m.id,m.version,'source' FROM tl.messages m WHERE m.id=%s",
            (pid, public),
        )
    assert [
        r["id"] for r in reader("SELECT * FROM api.sources('passage',%s)", (pid,))
    ] == [public]
    assert [
        r["id"] for r in reader("SELECT * FROM api.sources('message',%s)", (page,))
    ] == sorted([public, secret])

    # Granted the source channel only: the passage is hidden, so is its lineage.
    access.register_asker("guest")
    access.grant("guest", channel="fake.public")
    assert reader("SELECT * FROM api.passages", asker="guest") == []
    assert reader("SELECT * FROM api.sources('passage',%s)", (pid,), "guest") == []
    assert (
        reader(
            "SELECT * FROM api.provenance WHERE derived_kind='passage'", asker="guest"
        )
        == []
    )
    # Granted the page too: the revision still cites a hidden source.
    access.grant("guest", channel=PAGE)
    assert reader("SELECT * FROM api.passages", asker="guest") == []
    assert reader("SELECT * FROM api.sources('passage',%s)", (pid,), "guest") == []
    guest = scope_of("guest")
    handle = resultsets.create(
        guest, [{"record_id": f"p:{pid}"}], completeness="exhaustive"
    )
    assert resultsets.read(guest, handle)["records"] == []
    assert resultsets.info(guest, handle)["counts"]["returned"] == 0
    # Every source visible: the passage, its lineage and the set member appear;
    # the earlier set is stale under the new policy version.
    access.grant("guest", channel="fake.secret")
    with pytest.raises(resultsets.ResultSetStale):
        resultsets.read(guest, handle)
    assert [
        r["passage_id"] for r in reader("SELECT * FROM api.passages", asker="guest")
    ] == [pid]
    assert [
        r["id"]
        for r in reader("SELECT * FROM api.sources('passage',%s)", (pid,), "guest")
    ] == [public]
    handle = resultsets.create(
        guest, [{"record_id": f"p:{pid}"}], completeness="exhaustive"
    )
    (record,) = resultsets.read(guest, handle)["records"]
    assert record["record_id"] == f"p:{pid}" and record["kind"] == "passage"
    # Another asker cannot read guest's set.
    with pytest.raises(resultsets.ResultSetNotFound):
        resultsets.read(scope_of("rival"), handle)
