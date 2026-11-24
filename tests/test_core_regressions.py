"""Core regressions using only disposable databases and invented identities."""

from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import traceback
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from throughline.db import connect, create_token, migrate
from throughline_sdk._server import _safe_run, _safe_output, _safe_popen


def test_channel_reserved_columns_match_view(client):
    with connect() as conn:
        columns = [
            r["column_name"]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema='api' AND table_name='messages'"
            )
        ]
    for name in columns:
        response = client.post(
            "/v1/channels",
            json=dict(
                name="fake.reserved",
                plugin="fake",
                fields=[dict(name=name, type="text")],
            ),
        )
        assert response.status_code == 422, name
        assert name in response.text and "reserved names" in response.text


def test_migrations_serialize_cluster_roles(database):
    settings = conninfo_to_dict(database)
    settings["dbname"] = "postgres"
    admin = make_conninfo(**settings)
    names = ["tl_test_" + uuid.uuid4().hex for _ in range(4)]
    with psycopg.connect(admin, autocommit=True) as conn:
        for name in names:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(migrate, [make_conninfo(admin, dbname=name) for name in names])
            )
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            for name in names:
                conn.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )


def test_migration_uses_shared_database_lock(monkeypatch):
    from throughline import db
    from contextlib import contextmanager

    calls = []

    class Connection:
        def execute(self, statement):
            calls.append(statement)

    @contextmanager
    def connection(**kwargs):
        assert conninfo_to_dict(kwargs["url"])["dbname"] == "postgres"
        yield Connection()

    monkeypatch.setattr(db, "connect", connection)
    monkeypatch.setattr(db, "_migrate", lambda url: calls.append("migrate"))
    migrate("postgresql://postgres@127.0.0.1/tl_test_fake")
    assert calls == ["SELECT pg_advisory_xact_lock(71824003)", "migrate"]


def test_contract_subprocess_error_redacts_environment(monkeypatch):
    secret = "-".join(("invented", "secret", "canary"))

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(
            1,
            ["fake", secret],
            output=secret,
            stderr=str(dict(os.environ, FAKE_SECRET=secret)),
        )

    monkeypatch.setattr(subprocess, "run", fail)
    monkeypatch.setattr(subprocess, "Popen", fail)
    for runner in (_safe_run, _safe_output, _safe_popen):
        with pytest.raises(RuntimeError) as error:
            runner(["fake"], env={"FAKE_SECRET": secret})
        assert secret not in "".join(traceback.format_exception(error.value))
        assert secret not in str(error.getrepr())


def test_identity_metadata_history_resolution_and_not_same(client):
    def post(path, data):
        response = client.post("/v1/" + path, json=data)
        assert response.status_code == 200, response.text
        return response.json()

    a = post(
        "identity/people",
        dict(
            handle="fake-a",
            kind="human",
            source_system="fake",
            source_id="1",
            created_at="2020-01-01T00:00:00Z",
        ),
    )["id"]
    b = post("identity/people", dict(handle="fake-b"))["id"]
    name = post(
        "registry/names",
        dict(
            person_id=a,
            name="Fake retired alias",
            kind="alias",
            is_primary=False,
            note="invented",
            source="fake",
            created_at="2020-01-01T00:00:00Z",
            retired_at="2021-01-01T00:00:00Z",
        ),
    )
    assert name["note"] == "invented"
    assert (
        client.get("/v1/person", params={"q": "Fake retired alias"}).json()["rows"]
        == []
    )
    post("registry/fact-keys", dict(key="fake-city"))
    fact = post(
        "registry/facts",
        dict(
            person_id=a,
            key="fake-city",
            value="Invented",
            note="invented",
            source="fake",
            as_of="2019-01-01",
            asserted_by="fake-source",
            created_at="2020-01-01T00:00:00Z",
            retired_at="2021-01-01T00:00:00Z",
        ),
    )
    assert (
        fact["original_asserted_by"] == "fake-source"
        and fact["asserted_by"] == "owner"
        and fact["as_of"] == "2019-01-01"
    )
    post("registry/relation-kinds", dict(kind="fake-peer", inverse="fake-peer"))
    post(
        "registry/relations",
        dict(
            from_id=a,
            to_id=b,
            kind="fake-peer",
            note="invented",
            asserted_by="fake-source",
            created_at="2020-01-01T00:00:00Z",
            ended_at="2021-01-01T00:00:00Z",
        ),
    )
    with connect() as conn:
        assert conn.execute("SELECT count(*) n FROM api.facts").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT count(*) n FROM api.facts_history").fetchone()["n"]
            == 1
        )
        assert conn.execute("SELECT count(*) n FROM api.relations").fetchone()["n"] == 0
        assert (
            conn.execute("SELECT count(*) n FROM api.relations_history").fetchone()["n"]
            == 1
        )
    post(
        "registry/not-same", dict(person_a=b, person_b=a, reason="Invented distinction")
    )
    assert (
        client.post("/v1/identity/merge", json=dict(source=a, target=b)).status_code
        == 400
    )
    agent = create_token("fake agent", kind="agent")["token"]
    response = client.post(
        "/v1/identity/merge",
        json=dict(source=a, target=b, override_not_same=True),
        headers={"Authorization": "Bearer " + agent},
    )
    assert response.status_code == 400
    c = post("identity/people", dict(handle="fake-c"))["id"]
    post("identity/merge", dict(source=a, target=c))
    assert (
        client.post("/v1/identity/merge", json=dict(source=c, target=b)).status_code
        == 400
    )
    post("identity/merge", dict(source=c, target=b, override_not_same=True))
