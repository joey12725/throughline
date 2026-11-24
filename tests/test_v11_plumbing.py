import ast
import json
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from conftest import KEEP_TABLES, ingest, message, truncate_tl
from fastapi.testclient import TestClient
from psycopg import sql
from typer.testing import CliRunner

import throughline
from throughline import allowlist, budget, operators, tokens
from throughline.api import create_app
from throughline.db import connect, create_token
from throughline.execution import Execution, Trace, scrub
from throughline.query import query, query_ex, reader_transaction, validate_sql
from throughline.scope import Scope

SRC = Path(throughline.__file__).parent


def bearer(token, asker=None):
    headers = {"Authorization": "Bearer " + token}
    if asker:
        headers["X-Throughline-Asker"] = asker
    return headers


def audit_rows():
    with connect() as c:
        return c.execute("SELECT * FROM tl.audit_log ORDER BY id").fetchall()


# --- reader path -----------------------------------------------------------


def test_query_legacy_return_shape_unchanged(client):
    assert query("SELECT i FROM generate_series(1,5) AS s(i) ORDER BY i", 3) == {
        "rows": [{"i": 1}, {"i": 2}, {"i": 3}],
        "truncated": True,
        "row_count": 3,
    }
    result = query_ex("SELECT 1 AS one", scope=Scope.local())
    assert set(result) == {"rows", "truncated", "row_count", "execution"}
    assert set(result["execution"]) >= {
        "status",
        "truncation",
        "completeness",
        "stages",
        "timings_ms",
        "incomplete",
        "budget",
        "warnings",
        "trace_id",
    }
    assert result["execution"]["status"] == "complete"
    assert result["execution"]["truncation"] is None


def test_acl_8_scope_reaches_reader_transaction(client):
    scope = Scope(
        token_id=7,
        token_kind="agent",
        asker="fake'asker; --",
        mode="asker",
        investigation_id="inv-1",
        unknown_asker="deny",
    )
    with reader_transaction(scope) as c:
        row = c.execute(
            "SELECT current_user AS role,current_setting('throughline.scope_mode') AS mode,current_setting('throughline.asker') AS asker,current_setting('throughline.token_id') AS token_id,current_setting('throughline.unknown_asker') AS unknown,current_setting('throughline.investigation') AS investigation,current_setting('transaction_read_only') AS read_only"
        ).fetchone()
    assert row == {
        "role": "throughline_reader",
        "mode": "asker",
        "asker": "fake'asker; --",
        "token_id": "7",
        "unknown": "deny",
        "investigation": "inv-1",
        "read_only": "on",
    }
    # Settings are transaction-local: the pooled connection does not keep them.
    with reader_transaction(Scope.local()) as c:
        row = c.execute(
            "SELECT current_setting('throughline.scope_mode') AS mode,current_setting('throughline.asker') AS asker"
        ).fetchone()
    assert row == {"mode": "owner", "asker": ""}
    # There is no unscoped reader transaction.
    for missing in (None, "owner", {"mode": "owner"}):
        with pytest.raises(TypeError):
            with reader_transaction(missing):
                pass
    with pytest.raises(TypeError):
        query_ex("SELECT 1")
    with pytest.raises(ValueError):
        Scope(token_id=1, token_kind="agent", asker=None, mode="everything")


def test_acl_8_validated_sql_cannot_read_or_set_scope(client):
    for statement in (
        "SELECT current_setting('throughline.scope_mode')",
        "SELECT pg_catalog.current_setting('throughline.asker', true)",
        "SELECT set_config('throughline.scope_mode','owner',true)",
        "SELECT pg_catalog.set_config('throughline.asker','x',false)",
        "SET LOCAL throughline.scope_mode='owner'",
        "SET throughline.asker='x'",
        "RESET ALL",
        "SELECT 1; SET LOCAL throughline.scope_mode='owner'",
        "WITH x AS (SELECT set_config('throughline.scope_mode','owner',true)) SELECT * FROM x",
        "SELECT * FROM pg_settings",
        "SHOW throughline.scope_mode",
    ):
        with pytest.raises(ValueError):
            validate_sql(statement)
        assert (
            client.post("/v1/sql", json={"statement": statement}).status_code == 400
        ), statement
    # The reader role itself cannot call set_config either.
    with reader_transaction(Scope.local()) as c:
        with pytest.raises(psycopg.Error):
            c.execute("SELECT set_config('throughline.scope_mode','deny',true)")


def test_acl_5_token_with_asker_list_must_name_one(client, channel):
    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    single = create_token("fake-single", "agent", askers=["fake-solo"])["token"]
    several = create_token("fake-several", "agent", askers=["fake-a", "fake-b"])[
        "token"
    ]
    legacy = create_token("fake-legacy", "agent")["token"]
    statement = {"statement": "SELECT count(*) AS n FROM api.messages"}
    before = len(audit_rows())

    # One entry: the token is bound to it without a header.
    r = client.post("/v1/sql", json=statement, headers=bearer(single))
    assert r.status_code == 200, r.text
    assert audit_rows()[before]["asker"] == "fake-solo"
    # Bound to a listed asker, which is not registered: never unrestricted.
    assert client.get("/v1/channels", headers=bearer(single)).status_code == 403

    # Several entries: the caller must say which.
    r = client.post("/v1/sql", json=statement, headers=bearer(several))
    assert r.status_code == 403, r.text
    assert client.get("/v1/channels", headers=bearer(several)).status_code == 403
    r = client.post("/v1/sql", json=statement, headers=bearer(several, "fake-b"))
    assert r.status_code == 200, r.text
    assert audit_rows()[-1]["asker"] == "fake-b"
    r = client.post("/v1/sql", json=statement, headers=bearer(several, "fake-c"))
    assert r.status_code == 403

    # Empty list: the legacy full-read agent token.
    r = client.post("/v1/sql", json=statement, headers=bearer(legacy))
    assert r.status_code == 200 and r.json()["rows"] == [{"n": 3}], r.text
    assert client.get("/v1/channels", headers=bearer(legacy)).status_code == 200
    assert (
        client.post(
            "/v1/sql", json=statement, headers=bearer(legacy, "fake-solo")
        ).status_code
        == 403
    )


def test_acl_8_scope_modes_from_token(monkeypatch):
    monkeypatch.delenv("THROUGHLINE_UNKNOWN_ASKER", raising=False)
    owner = dict(id=1, kind="owner", askers=[], asker=None)
    scope = Scope.from_token(owner)
    assert (scope.mode, scope.asker, scope.token_id) == ("owner", None, 1)
    scope = Scope.from_token(dict(owner, asker="fake-friend"))
    assert (scope.mode, scope.asker, scope.unknown_asker) == (
        "asker",
        "fake-friend",
        "unrestricted",
    )
    monkeypatch.setenv("THROUGHLINE_UNKNOWN_ASKER", "deny")
    assert Scope.from_token(dict(owner, asker="fake-friend")).unknown_asker == "deny"
    monkeypatch.setenv("THROUGHLINE_UNKNOWN_ASKER", "nonsense")
    assert Scope.from_token(dict(owner, asker="fake-friend")).unknown_asker == "deny"
    monkeypatch.delenv("THROUGHLINE_UNKNOWN_ASKER")

    plugin = dict(id=2, kind="plugin", askers=[], asker=None)
    assert Scope.from_token(plugin).mode == "owner"
    legacy = dict(id=3, kind="agent", askers=[], asker=None)
    assert Scope.from_token(legacy).mode == "owner"
    listed = dict(id=4, kind="agent", askers=["fake-a", "fake-b"], asker="fake-a")
    scope = Scope.from_token(listed)
    assert (scope.mode, scope.asker, scope.unknown_asker) == ("asker", "fake-a", "deny")
    # Fail closed: a listed token without a bound or allowed asker, or an
    # unknown token kind, sees nothing.
    assert Scope.from_token(dict(listed, asker=None)).mode == "deny"
    assert Scope.from_token(dict(listed, asker="fake-z")).mode == "deny"
    assert Scope.from_token(dict(id=5, kind="other", askers=[])).mode == "deny"

    local = Scope.local()
    assert (local.mode, local.token_id, local.asker) == ("owner", None, None)
    assert Scope.from_token(listed, investigation_id="inv-9").gucs() == {
        "throughline.scope_mode": "asker",
        "throughline.asker": "fake-a",
        "throughline.token_id": "4",
        "throughline.unknown_asker": "deny",
        "throughline.investigation": "inv-9",
    }
    assert local.gucs()["throughline.token_id"] == ""
    with pytest.raises(Exception):
        local.mode = "deny"


def test_acl_8_no_reader_connection_or_query_ex_without_scope():
    def called(node):
        function = node.func
        if isinstance(function, ast.Name):
            return function.id
        if isinstance(function, ast.Attribute):
            return function.attr
        return None

    def literals(node):
        values = list(node.args) + [k.value for k in node.keywords]
        return [v.value for v in values if isinstance(v, ast.Constant)]

    reader_connections, unscoped, query_ex_calls = [], [], 0
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), str(path))
        owners = {}
        for function in ast.walk(tree):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(function):
                    owners.setdefault(id(node), function.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            where = f"{path.relative_to(SRC)}:{node.lineno}"
            if called(node) == "connect" and "reader" in literals(node):
                reader_connections.append((where, owners.get(id(node))))
            if called(node) == "query_ex":
                query_ex_calls += 1
                if "scope" not in {k.arg for k in node.keywords}:
                    unscoped.append(where)
    # ast.walk yields outer functions first, so a call is owned by the
    # outermost function containing it.
    assert [owner for _, owner in reader_connections] == ["reader_transaction"], (
        reader_connections
    )
    assert reader_connections[0][0].startswith("query.py:")
    assert unscoped == []
    assert query_ex_calls >= 2


# --- app-role routes -------------------------------------------------------

APP_ROLE_ROUTES = [
    ("GET", "/v1/channels", None),
    ("GET", "/v1/status", None),
    ("POST", "/v1/identity/people", {"handle": "fake-person"}),
    ("GET", "/v1/identity/people", None),
    ("GET", "/v1/identity/people/999999", None),
    ("POST", "/v1/identity/merge", {"source": 999998, "target": 999999}),
    ("POST", "/v1/identity/attach", {"identifier_id": 999999, "person_id": 999999}),
    ("POST", "/v1/identity/no-such-operation", {}),
    ("GET", "/v1/identity/unclaimed", None),
    ("POST", "/v1/registry/fact-keys", {"key": "fake-key"}),
    ("GET", "/v1/registry/fact-keys", None),
    ("GET", "/v1/registry/no-such-resource", None),
    (
        "POST",
        "/v1/links",
        {
            "type": "fake",
            "from": {"kind": "message", "id": 999999},
            "to": {"kind": "entity", "id": 999999},
        },
    ),
    ("PATCH", "/v1/links/999999", {"confidence": 0.5}),
    ("POST", "/v1/entities", {"kind": "work", "name": "fake"}),
    ("POST", "/v1/entities/merge", {"source": 999998, "target": 999999}),
    ("POST", "/v1/embed", {"text": "fake"}),
    ("POST", "/v1/reparse", {"channel": "no-such-channel"}),
]


def test_acl_8_app_role_routes_refuse_restricted(client, channel):
    function = "api.asker_unrestricted()"
    with connect() as c:
        original = c.execute(
            "SELECT pg_get_functiondef(to_regprocedure(%s)) AS source", (function,)
        ).fetchone()["source"]
        people = c.execute("SELECT count(*) n FROM tl.people").fetchone()["n"]
    try:
        with connect() as c:
            c.execute(
                "CREATE OR REPLACE FUNCTION api.asker_unrestricted() RETURNS boolean LANGUAGE sql STABLE AS 'SELECT false'"
            )
            c.execute(
                "GRANT EXECUTE ON FUNCTION api.asker_unrestricted() TO throughline_reader,throughline_app"
            )
        for method, path, body in APP_ROLE_ROUTES:
            r = client.request(method, path, json=body)
            # 403 whether or not the thing named exists: no 404, no 400.
            assert r.status_code == 403, (method, path, r.status_code, r.text)
        # Nothing was written or looked up on the way.
        with connect() as c:
            assert c.execute("SELECT count(*) n FROM tl.people").fetchone()["n"] == (
                people
            )
            assert c.execute("SELECT count(*) n FROM tl.entities").fetchone()["n"] == 0
            assert c.execute("SELECT count(*) n FROM tl.fact_keys").fetchone()["n"] == 0
        # The reader path stays open; the database gate decides what it shows.
        r = client.post("/v1/sql", json={"statement": "SELECT 1 AS one"})
        assert r.status_code == 200 and r.json()["rows"] == [{"one": 1}]
    finally:
        with connect() as c:
            if original:
                c.execute(original)
            else:
                c.execute("DROP FUNCTION api.asker_unrestricted()")
    # Unrestricted again: the same routes answer on their merits.
    assert client.get("/v1/channels").status_code == 200
    assert client.get("/v1/identity/people/999999").status_code == 404
    assert client.patch("/v1/links/999999", json={"confidence": 0.5}).status_code == 404


# --- budgets ---------------------------------------------------------------

SLOW = "SELECT count(*) AS n FROM generate_series(1,300000000) AS s(i)"


def test_cst_4_deadline_sets_statement_timeout(client):
    def timeout(deadline):
        with reader_transaction(Scope.local(), deadline) as c:
            return c.execute("SHOW statement_timeout").fetchone()["statement_timeout"]

    assert timeout(None) == "30s"
    assert timeout(1234) == "1234ms"
    assert timeout(10**9) == "30s"
    assert timeout(0) == "1ms"

    with pytest.raises(psycopg.Error) as cancelled:
        query_ex(SLOW, scope=Scope.local(), deadline_ms=150)
    assert cancelled.value.sqlstate == "57014"
    execution = Execution()
    with pytest.raises(psycopg.Error):
        query_ex(SLOW, scope=Scope.local(), deadline_ms=150, execution=execution)
    described = execution.to_dict()
    assert described["status"] == "degraded"
    assert described["stages"][0]["status"] == "timeout"
    assert described["timings_ms"]["database"] < 5000

    r = client.post("/v1/sql", json={"statement": SLOW, "deadline_ms": 150})
    assert r.status_code == 400 and r.json()["code"] == "57014", r.text
    # The connection is reusable afterwards.
    assert client.post("/v1/sql", json={"statement": "SELECT 1"}).status_code == 200

    # A request cannot raise the token's limit, and the limit applies unasked.
    with connect() as c:
        token_id = c.execute("SELECT id FROM tl.tokens").fetchone()["id"]
    tokens.set_limits(token_id, max_deadline_ms=150)
    for body in ({"statement": SLOW}, {"statement": SLOW, "deadline_ms": 20000}):
        r = client.post("/v1/sql", json=body)
        assert r.status_code == 400 and r.json()["code"] == "57014", r.text


def test_cst_4_token_budget_truncates_with_reason(client):
    statement = "SELECT i, to_char(i,'0000000000') AS padded FROM generate_series(1,500) AS s(i) ORDER BY i"
    full = query_ex(statement, scope=Scope.local())
    assert full["row_count"] == 500 and not full["truncated"]
    result = query_ex(statement, scope=Scope.local(), token_budget=200)
    assert result["truncated"] and 0 < result["row_count"] < 500
    assert result["execution"]["truncation"] == {"reason": "token_budget", "limit": 200}
    spent = sum(
        budget.estimate_tokens(json.dumps(row, ensure_ascii=False))
        for row in result["rows"]
    )
    assert spent <= 200
    # Rows are a prefix of the full result, in order.
    assert result["rows"] == full["rows"][: result["row_count"]]

    r = client.post("/v1/sql", json={"statement": statement, "token_budget": 200})
    assert r.status_code == 200, r.text
    assert r.json()["execution"]["truncation"]["reason"] == "token_budget"
    assert r.json()["row_count"] == result["row_count"]
    # Without a requested budget nothing but the row cap bounds the result.
    assert client.post("/v1/sql", json={"statement": statement}).json()[
        "row_count"
    ] == (500)
    # The token's limit caps what a request may ask for.
    with connect() as c:
        token_id = c.execute("SELECT id FROM tl.tokens").fetchone()["id"]
    tokens.set_limits(token_id, max_return_tokens=100)
    r = client.post("/v1/sql", json={"statement": statement, "token_budget": 100000})
    assert r.json()["execution"]["truncation"] == {
        "reason": "token_budget",
        "limit": 100,
    }


def test_qry_36_truncation_reasons(client):
    scope = Scope.local()
    result = query_ex("SELECT i FROM generate_series(1,5) AS s(i)", 3, scope=scope)
    assert result["truncated"] and result["row_count"] == 3
    assert result["execution"]["truncation"] == {"reason": "row_cap", "limit": 3}
    assert result["execution"]["stages"][-1]["truncated"] == "row_cap"

    result = query_ex("SELECT i FROM generate_series(1,3) AS s(i)", 3, scope=scope)
    assert not result["truncated"] and result["execution"]["truncation"] is None

    # About 110 kB per row: the 4 MB response budget ends it before the cap.
    result = query_ex(
        "SELECT i, (SELECT array_agg(g) FROM generate_series(1,20000) AS t(g)) AS big FROM generate_series(1,200) AS s(i) ORDER BY i",
        scope=scope,
    )
    assert result["truncated"] and 0 < result["row_count"] < 200
    assert result["execution"]["truncation"]["reason"] == "response_bytes"

    # One row above the row-text limit is never transferred.
    result = query_ex(
        "SELECT i, CASE WHEN i=3 THEN (SELECT array_agg(g) FROM generate_series(1,1500000) AS t(g)) END AS big FROM generate_series(1,5) AS s(i) ORDER BY i",
        scope=scope,
    )
    assert result["truncated"] and [r["i"] for r in result["rows"]] == [1, 2]
    assert result["execution"]["truncation"]["reason"] == "row_too_large"

    result = query_ex(
        "SELECT i FROM generate_series(1,50) AS s(i)", scope=scope, token_budget=20
    )
    assert result["execution"]["truncation"]["reason"] == "token_budget"


def test_cst_9_timings_sum_not_above_total(client, channel):
    assert ingest(client, [message(i) for i in range(20)]).status_code == 200
    result = query_ex("SELECT * FROM api.messages ORDER BY id", scope=Scope.local())
    timings = result["execution"]["timings_ms"]
    assert set(timings) == {"materialize", "database", "total"}
    assert all(value >= 0 for value in timings.values())
    assert timings["database"] > 0
    assert timings["materialize"] + timings["database"] <= timings["total"] + 0.01
    # A shared Execution accumulates the parts of several statements.
    execution = Execution()
    for _ in range(3):
        query_ex("SELECT 1", scope=Scope.local(), execution=execution)
    described = execution.to_dict()
    assert len(described["stages"]) == 3
    timings = described["timings_ms"]
    assert timings["materialize"] + timings["database"] <= timings["total"] + 0.01
    r = client.post("/v1/sql", json={"statement": "SELECT 1"})
    assert set(r.json()["execution"]["timings_ms"]) == {
        "materialize",
        "database",
        "total",
    }


def test_materialize_hoist_flag(client, monkeypatch):
    import throughline.derived as derived

    calls = []

    def counted(timeout=None):
        calls.append(timeout)
        return True

    monkeypatch.setattr(derived, "materialize_for_read", counted)
    scope = Scope.local()
    query_ex("SELECT 1", scope=scope)
    assert calls == [None]
    result = query_ex("SELECT 1", scope=scope, materialize=False)
    assert calls == [None]
    assert result["execution"]["timings_ms"]["materialize"] == 0
    # The maintenance wait is bounded by the deadline.
    query_ex("SELECT 1", scope=scope, deadline_ms=500)
    assert calls[-1] == 0.5
    query("SELECT 1")
    assert len(calls) == 3

    monkeypatch.setattr(derived, "materialize_for_read", lambda timeout=None: False)
    result = query_ex("SELECT 1", scope=scope)
    assert result["rows"] == [{"?column?": 1}]
    assert result["execution"]["warnings"]


def test_cst_4_budget_clamp_and_tracker():
    defaults = budget.clamp(None, {})
    assert defaults == budget.Budget(30000, 50000, 0.10, 32, 3)
    row = dict(
        max_deadline_ms=5000,
        max_return_tokens=1000,
        max_model_cost=0.5,
        max_controller_turns=2,
    )
    # A request only lowers the token's limits.
    asked = budget.Budget(
        deadline_ms=60000, tokens=10, model_cost=9.0, model_calls=4, controller_turns=9
    )
    assert budget.clamp(asked, row) == budget.Budget(5000, 10, 0.5, 4, 2)
    assert budget.clamp(budget.Budget(deadline_ms=100), row).deadline_ms == 100
    assert budget.clamp(budget.Budget(tokens=-5), row).tokens == 0
    # Columns that do not exist yet, or are NULL, fall back to the defaults.
    assert budget.clamp(None, {"max_model_cost": None}).model_cost == 0.10

    assert budget.estimate_tokens("") == 0
    assert budget.estimate_tokens("abcd") == 1
    assert budget.estimate_tokens("abcde") == 2
    assert budget.estimate_tokens({"a": 1}) == budget.estimate_tokens('{"a": 1}')

    now = [100.0]
    tracker = budget.BudgetTracker(
        budget.Budget(
            deadline_ms=1000,
            tokens=10,
            model_cost=0.02,
            model_calls=3,
            controller_turns=1,
        ),
        clock=lambda: now[0],
    )
    assert tracker.remaining_ms() == 1000 and not tracker.expired()
    now[0] += 0.4
    assert round(tracker.remaining_ms()) == 600
    assert tracker.reserve(0.01, 1) and tracker.reserve(0.01, 1)
    assert not tracker.reserve(0.01, 1)  # cost exhausted, nothing reserved
    assert tracker.reserve(0, 1)
    assert not tracker.reserve(0, 1)  # calls exhausted
    assert tracker.spend_tokens(6) and not tracker.spend_tokens(5)
    assert tracker.spend_tokens(4) and tracker.remaining_tokens() == 0
    assert tracker.take_turn() and not tracker.take_turn()
    now[0] += 0.7
    assert tracker.remaining_ms() == 0 and tracker.expired()
    snapshot = tracker.snapshot()
    assert snapshot["spent"] == dict(
        ms=1100.0, tokens=10, model_cost=0.02, model_calls=3, controller_turns=1
    )
    assert snapshot["expired"] and snapshot["limits"]["deadline_ms"] == 1000
    assert Execution(tracker).to_dict()["budget"] == snapshot

    from concurrent.futures import ThreadPoolExecutor

    shared = budget.BudgetTracker(budget.Budget(model_cost=1.0, model_calls=50))
    with ThreadPoolExecutor(max_workers=8) as pool:
        granted = list(pool.map(lambda _: shared.reserve(0.01, 1), range(400)))
    assert sum(granted) == 50 and shared.snapshot()["spent"]["model_calls"] == 50


# --- execution metadata and traces -----------------------------------------


def test_execution_status_and_fields():
    execution = Execution()
    assert execution.to_dict()["status"] == "complete"
    execution.stage("lexical", rows=3)
    execution.stage("vector", "skipped", reason="embeddings unavailable")
    execution.warn("fake warning")
    execution.warn("fake warning")
    execution.set_truncation("row_cap", 10)
    execution.completeness("partial")
    execution.add_timing("database", 1.5)
    execution.add_timing("database", 2.0)
    execution.set("as_of_honoured", ["lexical"])
    described = execution.to_dict()
    assert described["status"] == "degraded"
    assert described["incomplete"] == [
        {"stage": "vector", "reason": "embeddings unavailable"}
    ]
    assert described["warnings"] == ["fake warning"]
    assert described["truncation"] == {"reason": "row_cap", "limit": 10}
    assert described["completeness"] == "partial"
    assert described["timings_ms"] == {"database": 3.5}
    assert described["as_of_honoured"] == ["lexical"]
    execution.require_approval({"approval_id": "fake"})
    assert execution.to_dict()["status"] == "approval_required"
    execution.fail("fake failure")
    assert execution.to_dict()["status"] == "failed"
    json.dumps(execution.to_dict())


def test_qry_36_trace_only_on_request_and_detail_has_no_text(client, channel):
    secret = "syntheticsecretexcerpt " * 20
    assert ingest(client, [message(0, content=secret)]).status_code == 200
    statement = "SELECT content FROM api.messages"

    def traces():
        with connect() as c:
            return c.execute("SELECT * FROM tl.traces ORDER BY created_at").fetchall()

    before = len(audit_rows())
    r = client.post("/v1/sql", json={"statement": statement})
    assert r.status_code == 200 and r.json()["execution"]["trace_id"] is None
    assert (
        client.get("/v1/search", params={"q": "syntheticsecretexcerpt"}).json()[
            "execution"
        ]["trace_id"]
        is None
    )
    assert traces() == []
    assert audit_rows()[before]["detail"] == {"route": "POST /v1/sql"}

    r = client.post("/v1/sql", json={"statement": statement, "trace": True})
    assert r.status_code == 200, r.text
    trace_id = r.json()["execution"]["trace_id"]
    stored = traces()
    assert [str(t["id"]) for t in stored] == [trace_id]
    assert stored[0]["kind"] == "sql" and stored[0]["asker"] is None
    assert stored[0]["execution"]["status"] == "complete"
    assert audit_rows()[-1]["detail"] == {"route": "POST /v1/sql", "trace_id": trace_id}
    with connect() as c:
        events = c.execute(
            "SELECT * FROM tl.trace_events WHERE trace_id=%s ORDER BY seq", (trace_id,)
        ).fetchall()
        whole = c.execute(
            "SELECT (SELECT string_agg(t::text,' ') FROM tl.traces t)||' '||(SELECT string_agg(e::text,' ') FROM tl.trace_events e) AS text"
        ).fetchone()["text"]
    assert [
        (e["seq"], e["op"], e["statement"], e["status"], e["rows"]) for e in events
    ] == [(1, "sql", statement, "ok", 1)]
    # Statement, ids and counts; never the rows that were returned.
    assert "syntheticsecretexcerpt" not in whole

    # A failed statement asked with a trace is traced as failed.
    r = client.post(
        "/v1/sql",
        json={
            "statement": "SELECT 1/(i-1) FROM generate_series(1,1) AS s(i)",
            "trace": True,
        },
    )
    assert r.status_code == 400
    with connect() as c:
        failed = c.execute(
            "SELECT e.status,e.detail FROM tl.trace_events e JOIN tl.traces t ON t.id=e.trace_id WHERE t.id<>%s",
            (trace_id,),
        ).fetchall()
    assert failed == [{"status": "failed", "detail": {"code": "22012"}}]

    # Anything long in a detail could be an excerpt or a prompt: only its hash
    # is kept, at any depth, in memory and in the store.
    assert scrub({"n": 3, "ids": [1, 2], "ok": True, "short": "m:12"}) == {
        "n": 3,
        "ids": [1, 2],
        "ok": True,
        "short": "m:12",
    }
    trace = Trace(Scope.local(), "fake", {"query": secret, "max_rows": 5})
    event = trace.event(
        "model",
        status="ok",
        model="fake-model",
        tokens=12,
        cost=0.001,
        detail={"prompt": secret, "nested": {"items": [secret, "m:1"]}, "count": 2},
    )
    assert event["detail"]["prompt"].startswith("sha256:")
    assert event["detail"]["nested"]["items"][1] == "m:1"
    assert event["detail"]["count"] == 2
    assert trace.request["query"].startswith("sha256:")
    assert trace.flush(Execution()) is True
    with connect() as c:
        whole = c.execute(
            "SELECT (SELECT string_agg(t::text,' ') FROM tl.traces t)||' '||(SELECT string_agg(e::text,' ') FROM tl.trace_events e) AS text"
        ).fetchone()["text"]
    assert "syntheticsecretexcerpt" not in whole and "sha256:" in whole
    # Later events are appended once; flush never raises.
    trace.event("sql", "SELECT 1", rows=1)
    assert trace.flush() and trace.flush()
    with connect() as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM tl.trace_events WHERE trace_id=%s", (trace.id,)
            ).fetchone()["n"]
            == 2
        )
    broken = Trace(
        Scope(token_id=987654321, token_kind="agent", asker=None, mode="owner"), "fake"
    )
    assert broken.flush() is False


def test_qry_44_trace_access(client):
    first = create_token("fake-agent-one", "agent")["token"]
    second = create_token("fake-agent-two", "agent")["token"]
    listed = create_token("fake-agent-listed", "agent", askers=["fake-a", "fake-b"])[
        "token"
    ]
    body = {"statement": "SELECT 1 AS one", "trace": True}

    def traced(headers):
        r = client.post("/v1/sql", json=body, headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["execution"]["trace_id"]

    def get(trace_id, headers=None):
        return client.get(f"/v1/traces/{trace_id}", headers=headers)

    mine = traced(bearer(first))
    r = get(mine, bearer(first))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == mine and r.json()["kind"] == "sql"
    assert [e["statement"] for e in r.json()["events"]] == ["SELECT 1 AS one"]
    # Another token, an unknown id and a malformed id are indistinguishable.
    assert get(mine, bearer(second)).status_code == 404
    assert get(uuid.uuid4(), bearer(first)).status_code == 404
    assert get("not-a-trace", bearer(first)).status_code == 404
    # The owner reads any trace.
    assert get(mine).status_code == 200

    # Same token, another asker.
    as_a = traced(bearer(listed, "fake-a"))
    assert get(as_a, bearer(listed, "fake-a")).status_code == 200
    assert get(as_a, bearer(listed, "fake-b")).status_code == 404
    assert get(mine, bearer(listed, "fake-a")).status_code == 404

    # A trace written under another access policy version is not replayed.
    with connect() as c:
        c.execute("UPDATE tl.traces SET policy_version=policy_version+1")
    assert get(mine, bearer(first)).status_code == 409
    assert get(as_a, bearer(listed, "fake-a")).status_code == 409
    assert get(mine, bearer(second)).status_code == 404
    assert get(mine).status_code == 200

    # A plugin token has no traces, and revoking a token removes its traces
    # only when the token row itself is deleted (ON DELETE CASCADE).
    plugin = create_token("fake-plugin", "plugin", "synthetic")["token"]
    assert get(mine, bearer(plugin)).status_code == 403
    runner = CliRunner()
    import throughline.cli as cli

    def through_test_client(method, path, data=None, params=None):
        response = client.request(method, "/v1/" + path, json=data, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(cli, "request", through_test_client)
        shown = runner.invoke(cli.app, ["lib", "trace", mine, "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["id"] == mine


# --- discovery -------------------------------------------------------------


def test_allowlist_loader(tmp_path):
    assert allowlist.load(tmp_path) == (set(), set())
    (tmp_path / "0001_first.json").write_text(
        '{"functions": ["fake_rows"], "scalar": ["fake_value"]}', encoding="utf-8"
    )
    (tmp_path / "0002_second.json").write_text(
        '{"functions": ["more_rows", "fake_rows"]}', encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    assert allowlist.load(tmp_path) == ({"fake_rows", "more_rows"}, {"fake_value"})

    for content in (
        "{not json",
        "[]",
        '{"functions": "fake_rows"}',
        '{"functions": ["Fake"]}',
        '{"functions": ["fake;drop"]}',
        '{"functions": ["1fake"]}',
        '{"scalar": [7]}',
        '{"functions": ["fake"], "other": []}',
        '{"functions": ["fake_value"]}',
        '{"functions": ["both"], "scalar": ["both"]}',
    ):
        bad = tmp_path / "0003_bad.json"
        bad.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            allowlist.load(tmp_path)
        bad.unlink()

    # The shipped files are folded into the query allow-list; scalar names are
    # in both sets, exactly like the built-in catalog function.
    from throughline import query as module

    functions, scalar = allowlist.load()
    assert functions | scalar <= module.FUNCTIONS
    assert scalar <= module.SCALAR_FUNCTIONS
    assert not functions & module.SCALAR_FUNCTIONS
    assert {"search", "context", "catalog"} <= module.FUNCTIONS
    assert "catalog" in module.SCALAR_FUNCTIONS


def spec(name, implements, **kwargs):
    return operators.OperatorSpec(
        name=name,
        implements=implements,
        implementation=name.rsplit(".", 1)[-1],
        kind=kwargs.pop("kind", "leg"),
        **kwargs,
    )


def test_cst_8_operator_equivalents_and_choose(client):
    logical = "fake_lookup"
    reference = spec("fake.reference", logical, inputs=["query"], outputs=["records"])
    faster = spec("fake.faster", logical)
    worse = spec("fake.worse", logical)
    unmeasured = spec("fake.unmeasured", logical)
    approximate = spec("fake.approximate", logical, exact=False, cost_class="embedding")
    other = spec("fake.other", "fake_other_operation", kind="stage")
    names = [s.name for s in (reference, faster, worse, unmeasured, approximate, other)]
    try:
        for item in (reference, faster, worse, unmeasured, approximate, other):
            operators.register(item)
        with pytest.raises(ValueError):
            operators.register(spec("fake.faster", logical))
        with pytest.raises(ValueError):
            spec("fake.bad", logical, kind="wizard")
        with pytest.raises(ValueError):
            spec("fake.bad", logical, cost_class="expensive")

        assert operators.equivalents(logical) == [reference, faster, worse, unmeasured]
        assert operators.equivalents(logical, exact_only=False)[-1] == approximate
        assert operators.equivalents("fake_nothing") == []
        assert operators.choose("fake_nothing", {}) is None

        quality = {"recall": 0.9, "precision": 0.8}
        profiles = {
            "fake.reference": {"p95_ms": 100, "quality": quality},
            "fake.faster": {
                "p95_ms": 40,
                "quality": {"recall": 0.9, "precision": 0.85},
            },
            "fake.worse": {"p95_ms": 5, "quality": {"recall": 0.7, "precision": 0.9}},
            "fake.unmeasured": {"p95_ms": 1},
            "fake.approximate": {
                "p95_ms": 2,
                "quality": {"recall": 0.95, "precision": 0.9},
            },
        }
        # Cheaper with equal or better quality wins; worse or unrecorded
        # quality never does, however fast.
        assert operators.choose(logical, profiles) is faster
        assert (
            operators.choose(logical, profiles, allow_approximate=True) is approximate
        )
        # Missing one of the reference's metrics counts as not recorded.
        partial = dict(
            profiles, **{"fake.faster": {"p95_ms": 40, "quality": {"recall": 1}}}
        )
        assert operators.choose(logical, partial) is reference
        # Slower equal-quality implementations do not replace the reference.
        slow = dict(profiles, **{"fake.faster": {"p95_ms": 400, "quality": quality}})
        assert operators.choose(logical, slow) is reference
        # No measurement of the reference: nothing can be shown equivalent.
        assert operators.choose(logical, {}) is reference
        assert operators.choose(logical, None) is reference
        assert (
            operators.choose(logical, {"fake.faster": profiles["fake.faster"]})
            is reference
        )

        described = {d["name"]: d for d in operators.describe()}
        assert described["fake.reference"]["inputs"] == ["query"]
        assert described["fake.approximate"]["exact"] is False

        # The registry and measured profiles are visible in the database.
        with connect("app") as c:
            assert operators.sync(c) >= 6
            assert operators.sync(c) >= 6
            operators.record_profile(c, reference, 50, 100, quality)
            operators.record_profile(c, faster, 20, 400, quality)
            # The latest measurement counts, also within one transaction.
            operators.record_profile(c, faster, 20, 40, quality)
        with connect("app") as c:
            rows = operators.load_profiles(c)
        assert {r["operator"]: float(r["p95_ms"]) for r in rows} == {
            "fake.reference": 100.0,
            "fake.faster": 40.0,
        }
        assert operators.choose(logical, rows) is faster
        with reader_transaction(Scope.local()) as c:
            listed = c.execute("SELECT api.describe_operators() AS d").fetchone()["d"]
        listed = {d["name"]: d for d in listed}
        assert listed["fake.faster"]["profile"]["p95_ms"] == 40
        assert listed["fake.faster"]["implements"] == logical
        assert listed["fake.unmeasured"]["profile"] is None
        assert listed["fake.approximate"]["exact"] is False
        with connect("app") as c:
            with pytest.raises(psycopg.Error):
                c.execute("DELETE FROM tl.operator_profiles")
    finally:
        for name in names:
            operators.unregister(name)
    # create_app publishes the registry when the application starts.
    operators.register(reference)
    try:
        with connect() as c:
            c.execute("TRUNCATE tl.operators")
        with TestClient(create_app()):
            pass
        with connect() as c:
            assert c.execute(
                "SELECT implements FROM tl.operators WHERE name='fake.reference'"
            ).fetchone() == {"implements": logical}
    finally:
        operators.unregister(reference.name)


FAKE_ROUTE = """
from fastapi import APIRouter

router = APIRouter()


@router.get("/v1/zz-fake-plumbing")
def fake():
    return {"ok": True}
"""


def test_route_discovery_and_openapi_security(client, tmp_path, monkeypatch):
    from throughline.api import routes

    assert "traces" in [name for name, _ in routes.routers()]
    (tmp_path / "zz_fake_plumbing.py").write_text(FAKE_ROUTE, encoding="utf-8")
    (tmp_path / "zz_not_a_route.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(routes, "__path__", list(routes.__path__) + [str(tmp_path)])
    try:
        names = [name for name, _ in routes.routers()]
        assert names == sorted(names)
        assert "zz_fake_plumbing" in names and "zz_not_a_route" not in names
        app = create_app()
        schema = app.openapi()
        assert "/v1/traces/{trace_id}" in schema["paths"]
        assert "/v1/zz-fake-plumbing" in schema["paths"]
        for path, methods in schema["paths"].items():
            for method, operation in methods.items():
                assert operation.get("security"), (path, method)
        # A discovered route is authenticated and audited even though its
        # handler never mentions a token.
        token = client.headers["Authorization"]
        before = len(audit_rows())
        with TestClient(app) as fresh:
            assert fresh.get("/v1/zz-fake-plumbing").status_code == 401
            assert (
                fresh.get(
                    "/v1/zz-fake-plumbing", headers={"Authorization": "Bearer nope"}
                ).status_code
                == 401
            )
            r = fresh.get("/v1/zz-fake-plumbing", headers={"Authorization": token})
            assert r.status_code == 200 and r.json() == {"ok": True}
        rows = audit_rows()[before:]
        assert [r["statement"] for r in rows] == ["GET /v1/zz-fake-plumbing"]
    finally:
        for name in ("zz_fake_plumbing", "zz_not_a_route"):
            sys.modules.pop("throughline.api.routes." + name, None)
    # The request fields the SQL route gained are part of the contract.
    properties = create_app().openapi()["components"]["schemas"]
    assert {"deadline_ms", "token_budget", "trace"} <= set(
        properties["SQLRequest"]["properties"]
    )
    assert "execution" in properties["SQLResponse"]["properties"]


def measured_help():
    import throughline.cli as cli

    runner = CliRunner()
    text = ""
    for name in ("", "token", "identity", "track"):
        result = runner.invoke(
            cli.app, ([name] if name else []) + ["--help"], color=False
        )
        assert result.exit_code == 0, result.output
        text += result.output
    return text


def test_int_1_help_budget_with_ten_fake_lib_commands():
    import throughline.cli as cli
    from throughline import cli_commands

    assert "admin" in cli_commands.names()
    assert cli_commands.load(cli.lib) == cli_commands.load(cli.lib)
    before = measured_help()
    assert "lib" in before and "Library operations." in before
    registered = list(cli.lib.registered_commands)
    try:
        for i in range(10):

            def fake(value: str, json: bool = False):
                """A synthetic library command with a long explanatory help text."""

            cli.lib.command(f"fake-command-{i}")(fake)
        runner = CliRunner()
        listed = runner.invoke(cli.app, ["lib", "--help"], color=False)
        assert listed.exit_code == 0, listed.output
        assert "fake-command-9" in listed.output
        assert "trace" in listed.output and "token-limits" in listed.output
        after = measured_help()
        # Library commands cost nothing in the measured help.
        assert after == before
        assert "fake-command" not in after
        assert len(after) / 4 < 1500, len(after) / 4
    finally:
        cli.lib.registered_commands[:] = registered
    # The groups the budget is measured over are still importable by name.
    assert cli.tokens.registered_commands and cli.identities.registered_commands
    assert cli.track.registered_commands
    # New options are reachable and cost nothing in the command lists.
    runner = CliRunner()
    sql_help = runner.invoke(cli.app, ["sql", "--help"], color=False).output
    assert "--deadline-ms" in sql_help and "--token-budget" in sql_help
    search_help = runner.invoke(cli.app, ["search", "--help"], color=False).output
    for option in (
        "--person",
        "--channel",
        "--after",
        "--before",
        "--place",
        "--max-results",
    ):
        assert option in search_help, option


def test_cli_search_and_sql_options_reach_the_api(client, channel, monkeypatch):
    import throughline.cli as cli

    assert ingest(client, [message(i) for i in range(6)]).status_code == 200
    seen = []

    def through_test_client(method, path, data=None, params=None):
        seen.append((path, data, params))
        response = client.request(method, "/v1/" + path, json=data, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(cli, "request", through_test_client)
    runner = CliRunner()
    r = runner.invoke(
        cli.app,
        [
            "search",
            "orchard",
            "--channel",
            "synthetic.chat",
            "--after",
            "2020-01-01T00:00:02+00:00",
            "--before",
            "2020-01-01T00:00:05+00:00",
            "--max-results",
            "2",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    assert seen[-1][2] == {
        "q": "orchard",
        "mode": "keyword",
        "channel": "synthetic.chat",
        "after_at": "2020-01-01T00:00:02+00:00",
        "before_at": "2020-01-01T00:00:05+00:00",
        "max_results": 2,
    }
    assert json.loads(r.output)["row_count"] == 2
    r = runner.invoke(cli.app, ["search", "orchard", "--json"])
    assert seen[-1][2] == {"q": "orchard", "mode": "keyword"}
    assert json.loads(r.output)["row_count"] == 6

    r = runner.invoke(
        cli.app,
        [
            "sql",
            "SELECT i FROM generate_series(1,400) AS s(i)",
            "--token-budget",
            "50",
            "--deadline-ms",
            "5000",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    assert seen[-1][1]["token_budget"] == 50 and seen[-1][1]["deadline_ms"] == 5000
    assert json.loads(r.output)["execution"]["truncation"]["reason"] == "token_budget"
    runner.invoke(cli.app, ["sql", "SELECT 1"])
    assert seen[-1][1] == {"statement": "SELECT 1"}


def test_token_limits_sets_every_limit_column(client):
    token_id = create_token("fake-limited", "agent")["id"]
    with connect() as c:
        columns = tokens.limit_columns(c)
    assert {
        "max_deadline_ms",
        "max_return_tokens",
        "max_controller_turns",
        "allow_escalation",
    } <= set(columns)
    assert all(
        name.startswith("max_") or name in ("approve_limit", "allow_escalation")
        for name in columns
    )
    defaults = tokens.get_limits(token_id)
    assert defaults["max_deadline_ms"] == 30000
    assert defaults["max_return_tokens"] == 50000
    assert defaults["max_controller_turns"] == 3
    assert defaults["allow_escalation"] is False

    values = {
        name: "true" if kind == "boolean" else "0.25" if kind == "numeric" else "7"
        for name, kind in columns.items()
    }
    row = tokens.set_limits(token_id, **values)
    assert row["id"] == token_id
    for name, kind in columns.items():
        expected = True if kind == "boolean" else 0.25 if kind == "numeric" else 7
        assert float(row[name]) == float(expected), name
    # Other tokens are untouched.
    with connect() as c:
        other = c.execute(
            "SELECT max_deadline_ms FROM tl.tokens WHERE id<>%s", (token_id,)
        ).fetchall()
    assert other and all(r["max_deadline_ms"] == 30000 for r in other)

    for bad in (
        {"askers": "{x}"},
        {"kind": "owner"},
        {"token_hash": "x"},
        {"max_nothing": "1"},
        {"max_deadline_ms": "-1"},
        {"max_deadline_ms": "soon"},
        {"max_deadline_ms": None},
        # Values the column type cannot hold are refused the same way.
        {"allow_escalation": "maybe"},
        {"max_deadline_ms": "1.5"},
        {"max_deadline_ms": "inf"},
        {"max_deadline_ms": "nan"},
        {"max_deadline_ms": "99999999999"},
    ):
        with pytest.raises(ValueError):
            tokens.set_limits(token_id, **bad)
    with pytest.raises(ValueError):
        tokens.set_limits(999999, max_deadline_ms=1)
    with pytest.raises(ValueError):
        tokens.get_limits(999999)
    runner = CliRunner()
    import throughline.cli as cli

    r = runner.invoke(
        cli.app,
        [
            "lib",
            "token-limits",
            str(token_id),
            "--set",
            "max_deadline_ms=5000",
            "--set",
            "max-return-tokens=1234",
            "--set",
            "allow_escalation=false",
            "--json",
        ],
    )
    assert r.exit_code == 0, r.output
    shown = json.loads(r.output)
    assert shown["max_deadline_ms"] == 5000 and shown["max_return_tokens"] == 1234
    assert shown["allow_escalation"] is False
    r = runner.invoke(cli.app, ["lib", "token-limits", str(token_id), "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["max_deadline_ms"] == 5000
    assert (
        runner.invoke(
            cli.app, ["lib", "token-limits", str(token_id), "--set", "kind=owner"]
        ).exit_code
        != 0
    )
    assert (
        runner.invoke(
            cli.app, ["lib", "token-limits", str(token_id), "--set", "novalue"]
        ).exit_code
        != 0
    )
    # The limits reach the request budget.
    limits = budget.clamp(None, tokens.get_limits(token_id))
    assert (limits.deadline_ms, limits.tokens) == (5000, 1234)


def test_purge_hooks_run_in_order(client, channel):
    import throughline.cli as cli

    assert ingest(client, [message(i) for i in range(3)]).status_code == 200
    hooks = ("fake_plumbing_a", "fake_plumbing_b", "fake_plumbing_c", "fake_plumbing_d")
    log = "INSERT INTO tl.fake_purge_log(name,channel_id,messages) SELECT %s,$1,count(*) FROM tl.messages WHERE channel_id=$1"
    try:
        with connect() as c:
            c.execute(
                "CREATE TABLE tl.fake_purge_log(n int GENERATED ALWAYS AS IDENTITY,name text,channel_id bigint,messages int)"
            )
            c.execute(
                "INSERT INTO tl.purge_hooks(name,ord,statement) VALUES (%s,50,%s),(%s,20,%s),(%s,10,%s),(%s,30,%s)",
                (
                    hooks[0],
                    log % "'a'",
                    hooks[1],
                    log % "'b'",
                    hooks[2],
                    log % "'c'",
                    # A hook need not use the channel parameter.
                    hooks[3],
                    "INSERT INTO tl.fake_purge_log(name) VALUES ('d')",
                ),
            )
            channel_id = c.execute(
                "SELECT id FROM tl.channels WHERE name=%s", (channel,)
            ).fetchone()["id"]
        # Hooks are arbitrary SQL: runtime roles can neither add nor run them.
        for role in ("app", "reader"):
            for statement in (
                "SELECT tl.run_purge_hooks(1)",
                "INSERT INTO tl.purge_hooks(name,ord,statement) VALUES ('x',1,'SELECT 1')",
            ):
                with pytest.raises(psycopg.Error):
                    if role == "reader":
                        with reader_transaction(Scope.local()) as c:
                            c.execute(statement)
                    else:
                        with connect(role) as c:
                            c.execute(statement)
        # An unknown channel runs nothing.
        assert (
            CliRunner().invoke(cli.app, ["purge", "no.such", "--confirm"]).exit_code
            == 0
        )
        with connect() as c:
            assert (
                c.execute("SELECT count(*) n FROM tl.fake_purge_log").fetchone()["n"]
                == 0
            )

        result = CliRunner().invoke(cli.app, ["purge", channel, "--confirm"])
        assert result.exit_code == 0, result.output
        with connect() as c:
            rows = c.execute(
                "SELECT name,channel_id,messages FROM tl.fake_purge_log ORDER BY n"
            ).fetchall()
            assert c.execute("SELECT count(*) n FROM tl.messages").fetchone()["n"] == 0
        # In ord order, with the channel id, before the messages are deleted.
        assert rows == [
            {"name": "c", "channel_id": channel_id, "messages": 3},
            {"name": "b", "channel_id": channel_id, "messages": 3},
            {"name": "d", "channel_id": None, "messages": None},
            {"name": "a", "channel_id": channel_id, "messages": 3},
        ]
        # A failing hook stops the purge: nothing is half deleted.
        assert ingest(client, [message(9)]).status_code == 200
        with connect() as c:
            c.execute(
                "UPDATE tl.purge_hooks SET statement='SELECT 1/0' WHERE name=%s",
                (hooks[1],),
            )
        result = CliRunner().invoke(cli.app, ["purge", channel, "--confirm"])
        assert result.exit_code != 0
        with connect() as c:
            assert c.execute("SELECT count(*) n FROM tl.messages").fetchone()["n"] == 1
    finally:
        with connect() as c:
            c.execute("DELETE FROM tl.purge_hooks WHERE name=ANY(%s)", (list(hooks),))
            c.execute("DROP TABLE IF EXISTS tl.fake_purge_log")


def test_conftest_truncates_new_tables(client):
    assert "purge_hooks" in KEEP_TABLES and "schema_migrations" in KEEP_TABLES
    try:
        with connect() as c:
            # No foreign key to anything the old fixed list truncated.
            c.execute(
                "CREATE TABLE tl.fake_plumbing_state(id bigint GENERATED ALWAYS AS IDENTITY,value text)"
            )
            c.execute(
                "CREATE TABLE tl.fake_plumbing_kept(id bigint GENERATED ALWAYS AS IDENTITY,value text)"
            )
            c.execute(
                "INSERT INTO tl.fake_plumbing_state(value) VALUES ('a'),('b');INSERT INTO tl.fake_plumbing_kept(value) VALUES ('a')"
            )
            migrations = c.execute(
                "SELECT count(*) n FROM tl.schema_migrations"
            ).fetchone()["n"]
            seeded = {
                name: c.execute(
                    sql.SQL("SELECT count(*) n FROM tl.{}").format(sql.Identifier(name))
                ).fetchone()["n"]
                for name in KEEP_TABLES
                if c.execute(
                    "SELECT to_regclass(%s) IS NOT NULL ok", ("tl." + name,)
                ).fetchone()["ok"]
            }
            emptied = truncate_tl(c, keep=("fake_plumbing_kept",))
            assert "fake_plumbing_state" in emptied and "tokens" in emptied
            assert not set(emptied) & (set(KEEP_TABLES) | {"fake_plumbing_kept"})
            assert (
                c.execute("SELECT count(*) n FROM tl.fake_plumbing_state").fetchone()[
                    "n"
                ]
                == 0
            )
            # Identities restart, so ids do not depend on test order.
            assert (
                c.execute(
                    "INSERT INTO tl.fake_plumbing_state(value) VALUES ('c') RETURNING id"
                ).fetchone()["id"]
                == 1
            )
            assert (
                c.execute("SELECT count(*) n FROM tl.fake_plumbing_kept").fetchone()[
                    "n"
                ]
                == 1
            )
            assert c.execute("SELECT count(*) n FROM tl.tokens").fetchone()["n"] == 0
            assert c.execute("SELECT count(*) n FROM tl.audit_log").fetchone()["n"] == 0
            assert (
                migrations
                == c.execute("SELECT count(*) n FROM tl.schema_migrations").fetchone()[
                    "n"
                ]
            )
            for name, count in seeded.items():
                assert (
                    count
                    == c.execute(
                        sql.SQL("SELECT count(*) n FROM tl.{}").format(
                            sql.Identifier(name)
                        )
                    ).fetchone()["n"]
                ), name
            if "policy_state" in seeded:
                c.execute("UPDATE tl.policy_state SET version=41")
                truncate_tl(c)
                assert (
                    c.execute("SELECT version FROM tl.policy_state").fetchone()[
                        "version"
                    ]
                    == 1
                )
    finally:
        with connect() as c:
            c.execute(
                "DROP TABLE IF EXISTS tl.fake_plumbing_state,tl.fake_plumbing_kept"
            )


def test_acl_8_query_ex_without_scope_does_no_work(client, monkeypatch):
    import throughline.derived as derived

    calls = []
    monkeypatch.setattr(
        derived, "materialize_for_read", lambda timeout=None: calls.append(1) or True
    )
    for missing in (None, "owner", {"mode": "owner"}):
        with pytest.raises(TypeError):
            query_ex("SELECT 1", scope=missing)
    # Refused before validation and before the maintenance wait.
    with pytest.raises(TypeError):
        query_ex("not even SQL", scope=None)
    assert calls == []


def test_qry_36_multi_statement_trace_is_shared_and_flushed_by_owner(client):
    from throughline.api.deps import audited

    with connect() as c:
        row = c.execute("SELECT * FROM tl.tokens ORDER BY id LIMIT 1").fetchone()
    token = dict(row, asker=None)
    trace = Trace(Scope.from_token(token), "fake_operation", {"legs": 2})
    execution = Execution()
    for statement in ("SELECT 1 AS one", "SELECT 2 AS two"):
        result = audited(
            statement, token, execution=execution, materialize=False, trace=trace
        )
        assert result["execution"]["trace_id"] is None
    assert [e["statement"] for e in trace.events] == [
        "SELECT 1 AS one",
        "SELECT 2 AS two",
    ]
    assert len(execution.to_dict()["stages"]) == 2
    # Nothing is written until the operation flushes its own trace.
    with connect() as c:
        assert c.execute("SELECT count(*) n FROM tl.traces").fetchone()["n"] == 0
    assert "trace_id" not in (token["audit"].get("detail") or {})
    assert trace.flush(execution)
    with connect() as c:
        stored = c.execute(
            "SELECT t.kind,t.token_id,count(e.seq) n FROM tl.traces t JOIN tl.trace_events e ON e.trace_id=t.id GROUP BY t.id"
        ).fetchall()
    assert stored == [{"kind": "fake_operation", "token_id": row["id"], "n": 2}]


def test_cli_trace_id_cannot_name_another_path(monkeypatch):
    import throughline.cli as cli

    seen = []
    monkeypatch.setattr(
        cli, "request", lambda method, path, *a, **k: seen.append(path) or {}
    )
    result = CliRunner().invoke(cli.app, ["lib", "trace", "../channels?x=1"])
    assert result.exit_code == 0, result.output
    assert seen == ["traces/..%2Fchannels%3Fx%3D1"]


# --- checker additions -----------------------------------------------------

# Routes that serve only the caller's own working state but are not marked
# with deps.own_working_state. Anything else must be marked or refuse.
UNMARKED_OWN_STATE: set[tuple[str, str]] = set()


def _route_calls(function, seen=None):
    """Names called by a route handler and by the same-module helpers it uses."""
    import inspect
    import textwrap

    seen = set() if seen is None else seen
    if id(function) in seen:
        return set()
    seen.add(id(function))
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError, SyntaxError):
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    try:
        variables = inspect.getclosurevars(function)
        referenced = {**variables.globals, **variables.nonlocals}.values()
    except (ValueError, TypeError):
        referenced = ()
    for value in referenced:
        if (
            inspect.isfunction(value)
            and value.__module__ == function.__module__
            and value is not function
        ):
            names |= _route_calls(value, seen)
    return names


def test_acl_8_every_app_role_route_refuses_restricted_or_is_own_state():
    from fastapi.routing import APIRoute

    app_role, refusing, offenders = [], [], []

    def flattened(routes):
        # Newer FastAPI keeps an included router as one wrapper entry.
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from flattened(inner.routes)
            else:
                yield route

    for route in flattened(create_app().routes):
        if not isinstance(route, APIRoute):
            continue
        calls = _route_calls(route.endpoint)
        if "require_unrestricted" in calls:
            refusing += [(method, route.path) for method in route.methods]
        if not calls & {"authenticated_connection", "connect"}:
            continue  # reader path (audited / query_ex) or no database at all
        for method in sorted(route.methods):
            app_role.append((method, route.path))
            if "require_unrestricted" in calls:
                continue
            if getattr(route.endpoint, "own_working_state", False):
                continue
            if (method, route.path) in UNMARKED_OWN_STATE:
                continue
            offenders.append((method, route.path))
    assert offenders == [], offenders
    # The scan sees what the fixed list above names, and the trace route.
    listed = {(m, p) for m, p, _ in APP_ROLE_ROUTES if "no-such" not in p}
    found = set(app_role)
    for method, path in listed:
        # Listed routes either use the app role or refuse anyway (/v1/embed
        # reaches the database through the embeddings module).
        assert any(
            m == method and (p == path or ("{" in p and p.split("{")[0] in path))
            for m, p in found | set(refusing)
        ), (method, path)
    assert ("GET", "/v1/traces/{trace_id}") in found
    assert len(found) >= 20


def test_qry_36_flush_trace_names_the_trace_in_the_audit_row(client):
    from throughline.api.deps import flush_trace

    with connect() as c:
        row = c.execute("SELECT * FROM tl.tokens ORDER BY id LIMIT 1").fetchone()
    token = dict(row, asker=None)
    execution = Execution()
    trace = Trace(Scope.from_token(token), "fake_operation")
    trace.event("sql", "SELECT 1", rows=1)
    assert flush_trace(token, trace, execution) == trace.id
    assert token["audit"]["detail"] == {"trace_id": trace.id}
    assert execution.to_dict()["trace_id"] == trace.id
    # A trace that cannot be written never fails the operation.
    broken = Trace(
        Scope(token_id=987654321, token_kind="agent", asker=None, mode="owner"), "x"
    )
    # A fresh request: the first flush already put an audit entry on token.
    other = {key: value for key, value in token.items() if key != "audit"}
    execution = Execution()
    assert flush_trace(other, broken, execution) is None
    assert "audit" not in other
    assert execution.to_dict()["warnings"] == ["trace was not written"]


def test_allowlist_names_cannot_carry_a_trailing_newline(tmp_path):
    (tmp_path / "0001_bad.json").write_text(
        '{"functions": ["fake_rows\n"]}', encoding="utf-8"
    )
    with pytest.raises(ValueError):
        allowlist.load(tmp_path)


def test_acl_8_no_connection_with_a_computed_role():
    # connect(role) with a variable would open a reader connection the scan
    # above cannot see. Only db.py builds connections from a role variable.
    computed = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "db.py" and path.parent == SRC:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (
                target.id
                if isinstance(target, ast.Name)
                else target.attr
                if isinstance(target, ast.Attribute)
                else None
            )
            if name != "connect":
                continue
            roles = list(node.args[:1]) + [
                k.value for k in node.keywords if k.arg == "role"
            ]
            if any(not isinstance(r, ast.Constant) for r in roles):
                computed.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert computed == [], computed
