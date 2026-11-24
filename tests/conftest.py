import os
import uuid

# Typer/Rich forces ANSI styling under GITHUB_ACTIONS=true even when CliRunner
# passes color=False, which inflates help-output token budgets; disable ANSI in
# tests before typer/rich are imported.
os.environ["TERM"] = "dumb"

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from throughline.api import create_app
from throughline.db import connect, create_token, migrate


# Tables whose rows are part of the schema (written by migrations), not test
# state. Every other table in schema tl is emptied before each test, so a new
# table never leaks rows between tests, whether or not it has a foreign key.
KEEP_TABLES = (
    "schema_migrations",
    "api_functions",
    "catalog_sections",
    "record_sources",
    "gates",
    "purge_hooks",
    "policy_state",
    "recipes",
    "seed_hooks",
)


def truncate_tl(conn, keep=()):
    """Empty every tl table except KEEP_TABLES and `keep`; reset the policy
    version. Returns the names that were emptied."""
    existing = [
        r["tablename"]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='tl' ORDER BY tablename"
        )
    ]
    kept = set(KEEP_TABLES) | set(keep)
    names = [name for name in existing if name not in kept]
    if names:
        conn.execute(
            sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(",").join(sql.Identifier("tl", name) for name in names)
            )
        )
    if "policy_state" in existing:
        conn.execute("UPDATE tl.policy_state SET version=1")
    # Registries seeded by migrations (link types, synonyms, ...) come back.
    if conn.execute(
        "SELECT to_regprocedure('tl.reseed()') IS NOT NULL AS ok"
    ).fetchone()["ok"]:
        conn.execute("SELECT tl.reseed()")
    return names


@pytest.fixture(scope="session", autouse=True)
def _setup_isolation(tmp_path_factory):
    """Onboarding writes settings and secrets under a setup directory and
    runs a plugin scheduler: in tests both stay inside a temporary directory
    and the scheduler stays off, never touching ~/.throughline."""
    base = tmp_path_factory.mktemp("setup")
    saved = {k: os.environ.get(k) for k in ("THROUGHLINE_SETUP_DIR", "THROUGHLINE_SETUP_DATA", "THROUGHLINE_SETUP_SCHEDULER")}
    os.environ["THROUGHLINE_SETUP_DIR"] = str(base / "config")
    os.environ["THROUGHLINE_SETUP_DATA"] = str(base / "data")
    os.environ["THROUGHLINE_SETUP_SCHEDULER"] = "0"
    yield base
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="session")
def database():
    url = os.getenv(
        "THROUGHLINE_TEST_DATABASE_URL",
        "postgresql://postgres@127.0.0.1:54331/postgres",
    )
    settings = conninfo_to_dict(url)
    if settings.get("host") != "127.0.0.1" or settings.get("dbname") != "postgres":
        raise RuntimeError("tests require the local administrative postgres database")
    name = "tl_test_" + uuid.uuid4().hex
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    settings["dbname"] = name
    test_url = make_conninfo(**settings)
    old = os.environ.get("THROUGHLINE_DATABASE_URL")
    os.environ["THROUGHLINE_DATABASE_URL"] = test_url
    try:
        migrate()
        yield test_url
    finally:
        if old is None:
            os.environ.pop("THROUGHLINE_DATABASE_URL", None)
        else:
            os.environ["THROUGHLINE_DATABASE_URL"] = old
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture()
def client(database):
    with connect() as c:
        truncate_tl(c)
    token = create_token("synthetic owner")["token"]
    with TestClient(create_app()) as client:
        client.headers["Authorization"] = "Bearer " + token
        yield client


@pytest.fixture()
def channel(client):
    r = client.post(
        "/v1/channels",
        json=dict(
            name="synthetic.chat",
            plugin="synthetic",
            fields=[dict(name="rating", type="integer", description="Synthetic score")],
        ),
    )
    assert r.status_code == 200, r.text
    return "synthetic.chat"


def message(i=0, **kwargs):
    from datetime import datetime, timedelta, timezone

    result = dict(
        channel="synthetic.chat",
        external_id=f"fake-{i}",
        thread={"external_id": "fake-thread"},
        happened_at=(
            datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i)
        ).isoformat(),
        content=f"Synthetic orchard message {i}",
        author=dict(system="fake", kind="account", value="fake-account"),
        fields={"rating": i},
        payload={"synthetic": i},
    )
    result.update(kwargs)
    return result


def ingest(client, messages, **kwargs):
    return client.post(
        "/v1/ingest/messages",
        json=dict(plugin="synthetic", messages=messages, **kwargs),
    )
