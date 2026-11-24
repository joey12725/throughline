"""The agent surface (INT-1, INT-7, INT-8, INT-10..12, QRY-16).

Routes, views, functions and record keys are enumerated from the running
application and the database, never from a fixed list, so later packages extend
these checks by registering things. Features of sibling packages that are still
being built are optional-feature: a check skips naming the missing feature.
"""

import json
import re
import socket
from pathlib import Path

import httpx
import pytest
from conftest import ingest, message
from fastapi.routing import APIRoute
from typer.testing import CliRunner

# Imported at collection time, while the name 'conftest' still resolves to
# tests/conftest.py (a lazy import inside a test would get a plugin's).
from test_v11_recipes import build_corpus

from throughline import budget, passages, providers, recipes
from throughline.api import create_app
from throughline.db import connect
from throughline.query import FUNCTIONS
from throughline.retrieval.types import RECORD_KEYS
from throughline.semantic_ops import discover

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "throughline" / "SKILL.md"
# Names that sibling packages register; until their code lands a
# registry mismatch limited to these is optional-feature, not a failure.
WAVE4_SQL = {"coverage_for", "news", "pending", "consumer_horizon"}
WAVE4_SEMANTIC = {"librarian"}
# Retrieval internals: building blocks of recall legs and operators. They are
# reachable through SQL for power users, but never presented as tools.
INTERNAL = (
    "scope_filter",
    "search_records",
    "vector_records",
    "list_records",
    "entity_search",
    "time_overlap_many",
    "near_many",
    "follow_many",
    "rerank",
    "fusion",
    "rrf",
    "planner",
    "legs",
)
EXECUTION_KEYS = {
    "status",
    "completeness",
    "truncation",
    "stages",
    "timings_ms",
    "incomplete",
    "budget",
    "warnings",
    "trace_id",
}


def ok(response):
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


def ref(record):
    return {"record_id": record["record_id"], "version": record["version"]}


def flattened(routes):
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from flattened(inner.routes)
        else:
            yield route


def registry():
    with connect() as c:
        return {r["name"]: r for r in c.execute("SELECT * FROM tl.api_functions")}


def api_functions():
    with connect() as c:
        return {
            r["proname"]
            for r in c.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api'"
            )
        }


def corpus(client):
    build_corpus(client)


# --- INT-8 -------------------------------------------------------------------


def test_int_8_registry_equals_allowlist_and_semantic_registry(client):
    rows = registry()
    sql_rows = {n for n, r in rows.items() if r["kind"] == "sql"}
    semantic_rows = {n for n, r in rows.items() if r["kind"] == "semantic"}
    ops = set(discover())
    gaps = {
        "registry rows not allow-listed": sql_rows - FUNCTIONS,
        "allow-listed names without a registry row": FUNCTIONS - sql_rows,
        "semantic rows without an operator": semantic_rows - ops,
        "operators without a semantic row": ops - semantic_rows,
    }
    unexpected = {
        k: v - WAVE4_SQL - WAVE4_SEMANTIC
        for k, v in gaps.items()
        if v - WAVE4_SQL - WAVE4_SEMANTIC
    }
    assert not unexpected, unexpected
    # Every sql row is a real api function, every semantic one is not.
    assert sql_rows <= api_functions()
    assert not semantic_rows & api_functions()
    assert not semantic_rows & FUNCTIONS
    # Examples execute (0 rows is fine: they reference synthetic ids). A
    # placeholder handle in an example stands for one of the caller's sets.
    corpus(client)
    handle = ok(
        client.post("/v1/recall", json={"query": "orchard", "k": 3, "save": True})
    )["result_set"]
    failures = {}
    for name, row in sorted(rows.items()):
        assert re.search(rf"\bapi\.{name}\b", row["example"]), name
        if name in WAVE4_SEMANTIC and name not in ops:
            continue
        example = re.sub(r"rs_0{32}", handle, row["example"])
        r = client.post("/v1/sql", json={"statement": example})
        if r.status_code != 200:
            failures[name] = (r.status_code, r.text[:200])
    assert not failures, failures
    pending = {k: sorted(v) for k, v in gaps.items() if v}
    if pending:
        pytest.skip(f"optional-feature: registry and code not yet equal for {pending}")


def test_int_8_parameters_guarantees_cost_documented(client):
    rows = registry()
    ops = discover()
    for name, row in rows.items():
        assert row["signature"].startswith(name + "("), name
        assert ") RETURNS " in row["signature"], name
        assert len(row["description"]) >= 20, name
        assert row["exactness"] in ("exact", "approximate", "mixed"), name
        assert row["cost_class"] in ("free", "embedding", "model"), name
        assert 0 <= row["level"] <= 3, name
        assert row["access"].strip(), name
        assert row["example"].lstrip().upper().startswith("SELECT"), name
        if row["ranked_limit_arg"] is not None:
            # A ranked function documents where its limit is and its default.
            assert row["default_limit"] is not None, name
            assert row["exactness"] != "exact" or row["kind"] == "sql", name
        if row["kind"] == "semantic" and name in ops:
            op = ops[name]
            for param in op.params:
                assert param in row["signature"], (name, param)
            # The catalogue may state a higher cost class than the operator's
            # own default (recall embeds when a vector leg is configured) and
            # 'mixed' when one mode is approximate; it never understates either.
            order = ("free", "embedding", "model")
            assert order.index(row["cost_class"]) >= order.index(op.cost_class), name
            allowed = ("exact", "mixed") if op.exact else ("approximate", "mixed")
            assert row["exactness"] in allowed, name
    # The catalogue over HTTP is the same registry.
    catalog = ok(client.get("/v1/catalog"))
    assert sorted(f["name"] for f in catalog["functions"]) == sorted(rows)
    one = ok(client.get("/v1/describe", params={"subject": "function:recall"}))
    assert one["kind"] == "semantic" and "query" in one["signature"]


def test_int_8_describe_subjects_access_operators_egress_recipes(client):
    index = ok(client.get("/v1/describe"))
    subjects = {s["subject"] for s in index["subjects"]}
    assert {"access", "operators", "egress", "recipes", "surface", "levels"} <= subjects
    # Every dynamic subject function answers; every section is readable.
    with connect() as c:
        dynamic = [
            r["proname"][len("describe_") :]
            for r in c.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='api' AND p.pronargs=0 AND p.proname LIKE 'describe\\_%'"
            )
        ]
        sections = [
            r["subject"] for r in c.execute("SELECT subject FROM tl.catalog_sections")
        ]
    for subject in dynamic + sections:
        assert subject in subjects or subject.startswith("recipe:"), subject
        value = ok(client.get("/v1/describe", params={"subject": subject}))
        assert value is not None, subject
    access = ok(client.get("/v1/describe", params={"subject": "access"}))
    assert {"rule", "unrestricted", "policy_version"} <= set(access)
    operators = ok(client.get("/v1/describe", params={"subject": "operators"}))
    assert isinstance(operators, list)
    egress = ok(client.get("/v1/describe", params={"subject": "egress"}))
    assert {"providers", "excerpts_leave_host", "rule"} <= set(egress)
    described = ok(client.get("/v1/describe", params={"subject": "recipes"}))
    assert {r["name"] for r in described["recipes"]} == {
        n for n, r in recipes.load_files().items() if r["status"] == "tested"
    }
    assert ok(client.get("/v1/describe", params={"subject": "no_such_subject"})) is None
    if "coverage" not in dynamic:
        pytest.skip("optional-feature: api.describe_coverage() (source coverage) missing")
    coverage = ok(client.get("/v1/describe", params={"subject": "coverage"}))
    assert coverage is not None


# --- INT-7 -------------------------------------------------------------------


def test_int_7_skill_limits_and_no_internal_technique_as_tool():
    text = SKILL.read_text(encoding="utf-8")
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert match, "front matter"
    front = dict(
        line.split(": ", 1) for line in match.group(1).splitlines() if ": " in line
    )
    assert front["name"] == "throughline"
    assert len(front["description"]) < 240
    # The skill carries research and answering guidance; still well bounded.
    assert budget.estimate_tokens(text) < 3200, budget.estimate_tokens(text)
    lowered = text.lower()
    # Host-neutral: no agent host or vendor is named.
    for host in ("claude", "hermes", "grokbot", "codex", "openai", "anthropic", "mcp"):
        assert not re.search(rf"\b{host}\b", lowered), host
    # The ordering and the concepts an agent needs.
    body = lowered[match.end() :]
    order = [
        body.index(term)
        for term in (
            "post /v1/find",
            "api.context(",
            "api.person_brief",
            "exact sql",
            "get /v1/describe",
        )
    ]
    assert order == sorted(order)
    for term in (
        "/v1/sql",
        "completeness",
        "coverage",
        "a gap is not absence",
        "evidence_class",
        "primary",
        "inference",
        "draw the conclusions",
    ):
        assert term in lowered, term
    # Five worked queries, each a tested recipe.
    tested = {n for n, r in recipes.load_files().items() if r["status"] == "tested"}
    named = set(re.findall(r"`([a-z_]+)`", text)) & tested
    assert len(named) >= 5, named
    # Internal techniques are not listed as tools.
    for internal in INTERNAL:
        assert internal not in lowered, internal


def test_int_7_surface_section_is_catalogue_sql_context_optional_librarian(client):
    surface = ok(client.get("/v1/describe", params={"subject": "surface"}))
    steps = [s["step"] for s in surface["order"]]
    assert steps == ["catalogue", "sql", "context", "recall", "librarian"]
    optional = {s["step"] for s in surface["order"] if s.get("optional")}
    assert optional == {"librarian"}
    assert "gap in coverage is not absence" in surface["execution"]
    assert "evidence_class" in surface["evidence"]
    assert "agent draws the conclusion" in surface["conclusions"]
    for step in surface["order"]:
        assert step["call"] and step["why"], step


# --- INT-10 ------------------------------------------------------------------


def test_int_10_same_envelope_and_record_keys_across_recall_expand_match_sets_plans_packets_librarian_over_http_and_cli(
    client, monkeypatch
):
    corpus(client)
    recall = ok(
        client.post("/v1/recall", json={"query": "orchard", "k": 5, "save": True})
    )
    handle = recall["result_set"]
    assert handle
    responses = {
        "recall": recall,
        "expand": ok(
            client.post(
                "/v1/expand",
                json={"input": handle, "mode": "context", "before": 1, "after": 1},
            )
        ),
        "match": ok(
            client.post(
                "/v1/match",
                json={"input": handle, "condition": "orchard", "max_rows": 10},
            )
        ),
        "set": ok(client.get(f"/v1/sets/{handle}")),
    }
    plan = ok(
        client.post(
            "/v1/plans",
            json={
                "plan": {
                    "nodes": [
                        {
                            "id": "a",
                            "op": "recall",
                            "args": {"query": "orchard", "k": 3},
                        }
                    ]
                }
            },
        )
    )
    responses["plan"] = ok(client.get(f"/v1/sets/{plan['handles']['a']}"))
    assert EXECUTION_KEYS <= set(plan["execution"])
    for name, body in responses.items():
        assert {"records", "execution"} <= set(body), name
        assert EXECUTION_KEYS <= set(body["execution"]), name
        assert body["records"], name
        for record in body["records"]:
            assert set(RECORD_KEYS) <= set(record), (
                name,
                set(RECORD_KEYS) - set(record),
            )
            assert record["evidence_class"] in (
                "primary",
                "normalized",
                "derived",
                "inference",
            )
    # Packets and the librarian return the packet envelope, same record keys.
    packet = ok(
        client.post(
            "/v1/packets",
            json={
                "handle": handle,
                "notes": [
                    {
                        "generated_text": "A note",
                        "supporting_refs": [ref(recall["records"][0])],
                    }
                ],
            },
        )
    )
    packets = {"packet": packet}
    librarian_routes = {
        r.path for r in flattened(client.app.routes) if isinstance(r, APIRoute)
    }
    if "/v1/librarian" in librarian_routes:
        packets["librarian"] = ok(
            client.post("/v1/librarian", json={"goal": "orchard"})
        )
    keys = None
    for name, body in packets.items():
        assert {
            "records",
            "execution_status",
            "coverage",
            "truncation_and_omissions",
        } <= set(body), name
        for record in body["records"]:
            assert "evidence_class" in record and "record_id" in record, name
            keys = keys or set(record)
            assert set(record) == keys, name
    # The CLI prints the same JSON as HTTP (it calls the same routes).
    import throughline.cli as cli

    def request(method, path, data=None, params=None):
        response = client.request(method, "/v1/" + path, json=data, params=params)
        assert response.status_code < 400, response.text
        return response.json()

    monkeypatch.setattr(cli, "request", request)
    runner = CliRunner()
    shown = runner.invoke(cli.app, ["lib", "set", "show", handle, "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["records"] == responses["set"]["records"]
    recalled = runner.invoke(
        cli.app, ["lib", "recall", "orchard", "--k", "5", "--json"]
    )
    assert recalled.exit_code == 0, recalled.output
    cli_records = json.loads(recalled.output)["records"]
    assert [r["record_id"] for r in cli_records] == [
        r["record_id"] for r in recall["records"]
    ]
    assert all(set(RECORD_KEYS) <= set(r) for r in cli_records)


# --- INT-11 ------------------------------------------------------------------


def test_int_11_no_push_no_outbound_calls_with_defaults(client, monkeypatch):
    for name in list(__import__("os").environ):
        if re.fullmatch(
            r"THROUGHLINE_(EMBEDDING|RERANK|SEMANTIC|CONTROLLER|ASSISTANT|MODEL)_\w+", name
        ):
            monkeypatch.delenv(name, raising=False)
    # Defaults: nothing leaves the host.
    for row in providers.describe():
        assert row["local"] and not row["excerpts_leave_host"], row
    attempts = []

    def refuse_transport(self, request):
        attempts.append(str(request.url))
        raise httpx.ConnectError("outbound call in a test")

    real_connect = socket.socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(str(address))
            raise OSError("outbound connection in a test")
        return real_connect(self, address)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse_transport)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    corpus(client)
    recall = ok(
        client.post("/v1/recall", json={"query": "orchard", "k": 5, "save": True})
    )
    handle = recall["result_set"]
    ok(
        client.post(
            "/v1/match", json={"input": handle, "condition": "orchard", "max_rows": 10}
        )
    )
    ok(client.post("/v1/expand", json={"input": handle, "mode": "thread"}))
    ok(
        client.post(
            "/v1/sql",
            json={
                "statement": "SELECT * FROM api.recall(query => 'orchard', k => 3) r"
            },
        )
    )
    ok(client.post("/v1/packets", json={"handle": handle}))
    if any(r.path == "/v1/librarian" for r in flattened(client.app.routes)):
        ok(client.post("/v1/librarian", json={"goal": "orchard"}))
    assert attempts == []
    # Nothing is pushed: plain request/response routes only, none that
    # subscribes, streams or calls back.
    app = create_app()
    for route in flattened(app.routes):
        if not isinstance(route, APIRoute):
            continue
        if route.path == "/v1/sql/stream":
            # Pull-based HTTP streaming response for a single POST SELECT query,
            # not server-push, webhooks, or subscriptions.
            continue
        assert not re.search(
            r"push|subscribe|stream|webhook|callback|notify", route.path
        ), route.path
    assert not [r for r in flattened(app.routes) if "WebSocket" in type(r).__name__]


# --- INT-12 ------------------------------------------------------------------


def test_int_12_generated_text_flagged_everywhere(client):
    corpus(client)
    ok(
        client.post(
            "/v1/channels",
            json=dict(
                name="synthetic.visits", plugin="synthetic", record_kind="derived"
            ),
        )
    )
    r = ingest(
        client,
        [
            message(
                5,
                channel="synthetic.visits",
                external_id="visit-1",
                thread={"external_id": "visits"},
                content="Derived synthetic visit",
                fields={},
                author=None,
            )
        ],
    )
    assert r.status_code == 200 and not r.json()["rejected"], r.text
    with connect("app") as c:
        passages.build(c, 100)

    def rows(statement):
        return ok(client.post("/v1/sql", json={"statement": statement}))["rows"]

    pages = rows(
        "SELECT generated, unsupported FROM api.records WHERE channel = 'synthetic.page'"
    )
    assert pages and all(p["generated"] for p in pages)
    passage_rows = rows("SELECT generated FROM api.passage_records")
    assert passage_rows and all(p["generated"] for p in passage_rows)
    derived = rows(
        "SELECT generated, unsupported FROM api.records WHERE channel = 'synthetic.visits'"
    )
    assert derived == [{"generated": True, "unsupported": True}]
    primary = rows("SELECT generated FROM api.records WHERE channel = 'synthetic.chat'")
    assert primary and not any(p["generated"] for p in primary)
    # Over HTTP the evidence class says the same.
    recall = ok(
        client.post(
            "/v1/recall",
            json={
                "query": "orchard revision",
                "k": 30,
                "save": True,
                "scope": {"kinds": ["life_event", "passage", "entity", "derived"]},
            },
        )
    )
    for record in recall["records"]:
        if record["generated"]:
            assert record["evidence_class"] != "primary", record["record_id"]
    # Packet notes are generated text with verified references. A note may
    # cite a versioned record (by its version) or a versionless one (an entity).
    versioned = next(r for r in recall["records"] if r["version"] is not None)
    entity_recall = ok(
        client.post(
            "/v1/recall",
            json={
                "query": "Synthetic place",
                "k": 10,
                "save": True,
                "scope": {"kinds": ["life_event", "entity"]},
            },
        )
    )
    versionless = next(r for r in entity_recall["records"] if r["version"] is None)
    combined = ok(
        client.post(
            "/v1/sets/transform",
            json={
                "op": "union",
                "handles": [recall["result_set"], entity_recall["result_set"]],
            },
        )
    )["handle"]
    packet = ok(
        client.post(
            "/v1/packets",
            json={
                "handle": combined,
                "notes": [
                    {
                        "generated_text": "Orientation note",
                        "supporting_refs": [ref(versioned)],
                    },
                    {
                        "generated_text": "Note on a versionless record",
                        "supporting_refs": [ref(versionless)],
                    },
                    {
                        "generated_text": "A versioned record cited without version",
                        "supporting_refs": [
                            {"record_id": versioned["record_id"], "version": None}
                        ],
                    },
                ],
            },
        )
    )
    notes = packet["orientation"]
    assert len(notes) == 2, packet["truncation_and_omissions"]
    assert [
        o["index"]
        for o in packet["truncation_and_omissions"]
        if o.get("kind") == "note"
    ] == [2]
    assert all(n["generated"] is True for n in notes), notes


# --- QRY-16 ------------------------------------------------------------------


def test_qry_16_levels_list_existing_functions(client):
    levels = ok(client.get("/v1/describe", params={"subject": "levels"}))
    assert [entry["level"] for entry in levels["levels"]] == [0, 1, 2, 3]
    rows = registry()
    existing = api_functions() | set(discover())
    by_level = {}
    for name, row in rows.items():
        by_level.setdefault(row["level"], set()).add(name)
        assert name in existing or name in WAVE4_SEMANTIC, name
    for level in (0, 1, 2):
        assert by_level.get(level), level
    if not by_level.get(3):
        pytest.skip(
            "optional-feature: no level-3 function (api.librarian, librarian)"
        )
    assert by_level[3] <= set(discover()) | WAVE4_SEMANTIC


# --- INT-1 -------------------------------------------------------------------


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


def test_int_1_help_budget_with_all_lib_commands_and_every_route_reachable(
    client, monkeypatch
):
    import throughline.cli as cli
    from throughline import cli_commands

    loaded = cli_commands.load(cli.lib)
    assert "recipes" in loaded and set(cli_commands.names()) <= set(loaded) | {
        "__init__"
    }
    text = measured_help()
    assert len(text) / 4 < 1500, len(text) / 4
    # Library commands live under `lib` only; none reaches the measured help.
    assert "recipe" not in text
    runner = CliRunner()
    lib = runner.invoke(cli.app, ["lib", "--help"], color=False)
    assert lib.exit_code == 0 and "recipe" in lib.output
    recipe_help = runner.invoke(cli.app, ["lib", "recipe", "--help"], color=False)
    for command in ("list", "show", "run"):
        assert command in recipe_help.output, command
    # Every route is reachable through `throughline api METHOD PATH`.
    seen, server_errors = {}, set()

    # A server error still proves the route is reachable; it is reported, not
    # hidden (the request bodies here are deliberately empty).
    from fastapi.testclient import TestClient

    lenient = TestClient(client.app, raise_server_exceptions=False)
    lenient.headers.update(client.headers)

    def request(method, path, data=None, params=None):
        response = lenient.request(method, "/v1/" + path, json=data, params=params)
        seen[(method, path)] = response
        return {}

    monkeypatch.setattr(cli, "request", request)
    routes = [r for r in flattened(client.app.routes) if isinstance(r, APIRoute)]
    assert len(routes) >= 60
    for route in routes:
        if not route.path.startswith("/v1/") or route.path == "/v1/openapi.json":
            continue
        path = re.sub(r"\{[^}]+\}", "0", route.path)[len("/v1/") :]
        for method in sorted(route.methods):
            args = ["api", method, path]
            if method in ("POST", "PUT", "PATCH"):
                args += ["--body", "{}"]
            result = runner.invoke(cli.app, args)
            assert result.exit_code == 0, (method, path, result.output)
            response = seen[(method, path)]
            body = response.json() if response.status_code < 500 else None
            detail = body.get("detail") if isinstance(body, dict) else None
            unrouted = response.status_code in (404, 405) and detail in (
                "Not Found",
                "Method Not Allowed",
            )
            assert not unrouted, (method, route.path)
            if response.status_code >= 500:
                server_errors.add((method, route.path))
    # An empty body is a client error (4xx), never a server error.
    assert not server_errors, server_errors


# --- docs index -------------------------------------------------------------


def v11_ids():
    ranges = (
        ("MSG", 7, 10),
        ("BRN", 5, 17),
        ("QRY", 13, 46),
        ("CST", 4, 10),
        ("INT", 7, 12),
        ("ACL", 8, 10),
        ("ARC", 6, 6),
        ("BEN", 9, 21),
    )
    return [f"{p}-{n}" for p, a, b in ranges for n in range(a, b + 1)]


def test_docs_index_maps_every_id_to_existing_tests_or_deferred():
    index = ROOT / "docs" / "reference" / "index.md"
    if not index.exists():
        index = ROOT / "docs" / "v11" / "index.md"
    text = index.read_text(encoding="utf-8")
    ids = v11_ids()
    assert len(ids) == 81
    rows = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and re.fullmatch(r"[A-Z]{3}-\d+", cells[0]):
            rows[cells[0]] = cells[1]
    assert sorted(rows, key=ids.index) == ids, set(ids) ^ set(rows)
    defined = set()
    for path in (
        list((ROOT / "tests").glob("test_*.py"))
        + list((ROOT / "plugins").glob("*/tests/test_*.py"))
        + list((ROOT / "sdk" / "tests").glob("test_*.py"))
    ):
        defined |= set(
            re.findall(
                r"^\s*(?:async )?def (test_\w+)",
                path.read_text(encoding="utf-8", errors="ignore"),
                re.M,
            )
        )
    for rid, cell in rows.items():
        assert re.search(r"`test_\w+`", cell) or cell == "deferred", rid
    # Every named test exists, in the ID rows, LIB sections, behaviour priorities and
    # scenarios alike; a name marked planned belongs to a sibling package
    # that has not landed it yet.
    for name, planned in re.findall(r"`(test_\w+)`( \*\(planned)?", text):
        assert planned or name in defined, name
    for priority in range(1, 12):
        assert re.search(rf"^\| P{priority} ", text, re.M), priority
